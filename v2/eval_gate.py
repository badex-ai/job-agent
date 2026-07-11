#!/usr/bin/env python3
"""Eval harness for the Qwen gate (v2 Phase 4, T3).

Runs the gate over data/labels.json (READ-ONLY) and scores its verdicts
against the user's hand labels. Reports every metric with its n.

Verdict -> label alignment (grounded in rubric.md sec 0):
  the labels collapse to two action-classes —
    SKIP                         = "don't surface"  (gate verdict: skip)
    APPLY / CONSIDER / HOLD      = the act-set, "surface" (rubric sec 0:
                                   CONSIDER and HOLD must stay visible)
  the gate's surface / surface_priority both mean "surface".

Anchor metric: SKIP recall + precision (does the gate catch the jobs the
user would skip, without over-skipping the act-set?).

False negative (the quiet, dangerous error, per the design notes):
  a job the user labeled SKIP that the gate let through (surface*). These
  waste an application slot. The full list is emitted for inspection.

Usage:
  .venv/bin/python v2/eval_gate.py --stub-model --limit 10   # no network
  .venv/bin/python v2/eval_gate.py --limit 100               # live Qwen
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from v2.gate_prompt import build_system_prompt
from v2.model_profiles import profile_for, resolve_model
from v2.qwen_gate import (DEFAULT_MODEL, MAX_DESCRIPTION_CHARS, GateError,
                          gate_job, ollama_url)

REPO_ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = REPO_ROOT / "data" / "labels.json"
DEFAULT_OUT = REPO_ROOT / "data" / "eval_results.json"

VALID_LABELS = {"APPLY", "CONSIDER", "HOLD", "SKIP"}

# Thermal pacing for a live held-out run (mirrors v2/loop.py): keep the M4's
# GPU below saturation by sleeping between calls. 240s caps the adaptive backoff
# strictly under the 5m keep_alive so the model never unloads mid-batch.
EVAL_THROTTLE_RATIO = 1.5
EVAL_COOLDOWN_MAX_S = 240.0


def load_labeled_jobs(path: Path) -> list[dict]:
    """Read a labeled job file and return the list of jobs.

    Accepts both file shapes the project uses:
      - data/labels.json    : a bare JSON list of job dicts
      - heldout_*.json       : {"jobs": [...]} (the sealed held-out wrapper)

    Every returned job MUST carry a valid `label` — scoring a null/unlabeled
    job is meaningless and would silently corrupt FN/FP counts, so this refuses
    rather than guesses. The held-out file ships with `label: null` until it is
    hand-labeled; pointing the eval at it before labeling fails loudly here."""
    raw = json.loads(path.read_text())
    jobs = raw["jobs"] if isinstance(raw, dict) else raw
    unlabeled = [j for j in jobs if j.get("label") not in VALID_LABELS]
    if unlabeled:
        ids = ", ".join(str(j.get("id")) for j in unlabeled[:5])
        more = "" if len(unlabeled) <= 5 else f" (+{len(unlabeled) - 5} more)"
        raise SystemExit(
            f"{len(unlabeled)} of {len(jobs)} jobs in {path} have no valid "
            f"label (e.g. {ids}{more}). Label them first "
            f"(v2/labeling/label_heldout.py) — scoring unlabeled jobs is "
            f"meaningless.")
    return jobs

SURFACE_LABELS = {"APPLY", "CONSIDER", "HOLD"}  # the act-set
SURFACE_VERDICTS = {"surface", "surface_priority"}


def stub_transport(url: str, payload: dict, timeout: float) -> dict:
    """Deterministic fake: skip iff the job text trips an obvious hard marker,
    else surface. No network. Lets us verify the harness math offline."""
    blob = (payload.get("prompt", "") + payload.get("system", "")).lower()
    if any(m in blob for m in ("unpaid", "no stipend", "lpa", "₹", "india")):
        verdict, conf = "skip", 90
    else:
        verdict, conf = "surface", 60
    return {"response": json.dumps(
        {"reasoning": "stub", "verdict": verdict, "confidence": conf,
         "tags": ["stub"]})}


_EVIDENCE_RE = re.compile(r"evidence:\s*['\"]?(.+?)['\"]?\s*$",
                          re.IGNORECASE | re.MULTILINE | re.DOTALL)


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def extract_evidence(reasoning: str) -> str | None:
    """Pull the quoted text after 'Evidence:' from the model's reasoning."""
    m = _EVIDENCE_RE.search(reasoning or "")
    if not m:
        return None
    quote = m.group(1).strip().strip("'\"").strip()
    return quote or None


# S1 (geographic eligibility) is the only rule whose evidence must EXCLUDE,
# not merely MENTION, a place. The live n=100 run (2026-06-15) showed the 7B
# firing S1 on a bare 'Evidence: Remote' or 'Evidence: United States' — a real
# substring, so the substring check passed, but a location mention is surface
# per rubric S1's boundary, not a skip. An exclusionary S1 quote must carry one
# of these markers; without one, the quote does not prove restriction.
S1_EXCLUSION_RE = re.compile(
    r"\bonly\b|\bmust be\b|authoriz|\beligib|citizens?\b|\brequired to (be|reside|live)\b|"
    r"\bbased in\b|\blocated in\b|\bno sponsor|work authoriz|right to work\b",
    re.IGNORECASE)


def _fragments(quote: str) -> list[str]:
    """A model Evidence value is sometimes several quoted snippets joined by
    commas (e.g. "'SOAR Engineer', 'SOAR technologies'"). Split on the snippet
    separators and strip each fragment's quote chars so any one real fragment
    can verify the skip — the model citing the title plus a body phrase must
    not read as fabrication just because the joined string isn't a substring."""
    parts = re.split(r"',\s*'|\",\s*\"|',\s*\"|\",\s*'", quote)
    return [p.strip().strip("'\"").strip() for p in parts if p.strip()]


def verify_evidence(
    verdict: str, reasoning: str, description: str, tags: list[str] | None = None,
    title: str = "",
) -> tuple[str, str | None]:
    """Guard against fabricated AND non-exclusionary quotes. If the model
    skipped but its Evidence quote is NOT an actual substring of the
    description (or the title), the skip is invalid -> downgrade to surface.
    Additionally, a skip tagged S1 (geographic eligibility) must quote text
    that actually EXCLUDES — a bare place name or 'Remote' is a mention, not a
    restriction, and is downgraded. Returns (effective_verdict, reason).

    A short quote (<8 chars) is too weak to verify reliably, so we don't
    downgrade on it; we only reject quotes that are substantive AND absent."""
    if verdict not in ("skip",):
        return verdict, None
    quote = extract_evidence(reasoning)
    if quote is None:
        return "surface", "skip with no Evidence quote"
    # The S1-exclusion veto applies ONLY when S1 is the SOLE basis for the skip.
    # Live 0-99 (2026-06-15) showed the 7B defaulting to an S1 tag for wrong-
    # field/wrong-stack skips it actually judged on other grounds — vetoing
    # those on the geographic check destroyed correct skips. If the model also
    # cited a non-geographic rule (S2/S3/S4/S5/S6), the skip stands on that rule
    # and the S1 quote-quality check must not override it.
    other_rules = {"S2", "S3", "S4", "S5", "S6"}
    s1_is_sole_basis = bool(tags and "S1" in tags
                            and not (set(tags) & other_rules))
    # When S1 is the sole basis, it MUST quote exclusionary language regardless
    # of length — a bare 'Remote' (6 chars) or 'US' is exactly the mention-not-
    # restriction case this check exists to catch, and it must run BEFORE the
    # short-quote leniency below.
    if s1_is_sole_basis and not S1_EXCLUSION_RE.search(quote):
        return "surface", f"S1 quote mentions a place but states no exclusion: {quote[:80]!r}"
    if len(quote) < 8:
        return verdict, None  # too short to verify substring; let it stand
    # Verify against the same window the model saw, PLUS the title — the model
    # often quotes the job title (a real field it was shown) to prove a wrong-
    # field/wrong-stack skip, and the title is not part of the description body.
    haystack = _norm(title + "\n" + description[:MAX_DESCRIPTION_CHARS])

    def _verifies(frag: str) -> bool:
        if len(frag) < 8:
            return False  # too short to verify a multi-part fragment reliably
        nf = _norm(frag)
        # full fragment, or its first 40 chars (model may pad past the quote)
        return nf in haystack or (nf[:40] in haystack)

    # Accept if the whole quote verifies OR any substantive fragment of a
    # multi-snippet quote does. A model citing title + body must not be called
    # fabrication because the comma-joined string isn't a literal substring.
    substr_ok = _verifies(quote) or any(_verifies(f) for f in _fragments(quote))
    if not substr_ok:
        return "surface", f"fabricated/unverifiable evidence: {quote[:80]!r}"
    return verdict, None


def is_surface(verdict: str) -> bool:
    return verdict in SURFACE_VERDICTS


def label_is_surface(label: str) -> bool:
    return label in SURFACE_LABELS


def _gate_with_retry(job, system_prompt, kw, retries=2, backoff=3.0):
    """Call the gate, retrying transient transport errors (timeouts, dropped
    relay) before giving up. GateError (bad model output) is NOT retried."""
    last = None
    for attempt in range(retries + 1):
        try:
            return gate_job(
                system_prompt=system_prompt,
                title=job.get("title", ""),
                company=job.get("company", ""),
                location=job.get("location", ""),
                description=job.get("description", ""),
                **kw,
            )
        except GateError:
            raise  # malformed output is deterministic; retry won't help
        except Exception as e:  # transport: URLError/TimeoutError/etc.
            last = e
            if attempt < retries:
                time.sleep(backoff)
    raise last


class TunnelDown(Exception):
    """Raised when consecutive transport errors indicate the Ollama tunnel
    died mid-run — abort fast instead of grinding timeouts on every remaining
    job. Carries the partial results gathered so far for inspection."""

    def __init__(self, msg, partial):
        super().__init__(msg)
        self.partial = partial


def evaluate(jobs: list[dict], system_prompt: str, transport=None,
             on_progress=None, abort_after_consecutive_errors=None,
             model=None, options=None, cooldown_s=0.0) -> dict:
    """Run the gate over jobs; return a metrics dict (every count has its n).

    Per-job failures (malformed output OR transport errors that survive
    retries) are recorded and skipped — one network blip never discards the
    jobs already completed.

    `on_progress(done, total, info)` is called after each job (default None =
    silent, so the function stays pure for tests). `info` is a dict with the
    job title, the effective verdict (or an 'error' key), and the job's
    latency in seconds — enough to render a live progress line.

    `abort_after_consecutive_errors` (default None = off): if this many
    transport errors occur back-to-back, raise TunnelDown — the tunnel has
    dropped and the rest of the batch would just be doomed timeouts. A
    successful job resets the streak; malformed output (GateError) is the
    model's fault, not the tunnel's, so it does NOT count toward the breaker.

    `cooldown_s` (default 0.0 = off): sleep this long AFTER each job that hit
    the model, to keep the M4 cool on a full 150-job held-out run (the biggest
    sustained load on that machine). This is a THERMAL knob only — it changes
    when calls happen, never the verdict — so a paced run and an unpaced run
    score identically. Mirrors v2/loop.py's pacing; kept scoring-neutral by
    sleeping at the end of the loop body, outside verdict computation. An
    adaptive arm lengthens the cooldown when latency creeps past 1.5x a running
    minimum (a throttling proxy), capped under the 5m keep_alive."""
    kw = {"transport": transport} if transport is not None else {}
    if model is not None:
        kw["model"] = model
    if options:
        kw["options"] = options
    results = []
    errors = []
    latencies = []
    total = len(jobs)
    consecutive_errors = 0
    pacing = cooldown_s > 0 and transport is None  # only when driving the live M4
    baseline_latency = None
    cur_cooldown = cooldown_s

    def _tick(done, title, info):
        if on_progress is not None:
            on_progress(done, total, {"title": title, **info})

    def _pace(dt, *, hit_model):
        # Sleep after a call that reached the M4, to shed GPU heat. A GateError
        # (malformed output) still ran the full inference, so it paces too; a
        # pathological latency never sets the baseline. Baseline is a running
        # minimum, so a cold-load-inflated first job self-corrects downward.
        nonlocal baseline_latency, cur_cooldown
        if not (pacing and hit_model):
            return
        if dt is not None and (baseline_latency is None or dt < baseline_latency):
            baseline_latency = dt
        if baseline_latency is not None and dt is not None \
                and dt > EVAL_THROTTLE_RATIO * baseline_latency:
            cur_cooldown = min(cur_cooldown * 2, EVAL_COOLDOWN_MAX_S)
        else:
            cur_cooldown = max(cur_cooldown / 2, cooldown_s)
        time.sleep(cur_cooldown)

    for i, j in enumerate(jobs, 1):
        t0 = time.time()
        try:
            v = _gate_with_retry(j, system_prompt, kw)
        except GateError as e:
            dt = time.time() - t0
            errors.append({"id": j.get("id"), "title": j.get("title"),
                           "error": f"malformed: {e}"})
            _tick(i, j.get("title"), {"error": "malformed", "dt": dt})
            _pace(None, hit_model=True)  # inference ran; pace but don't anchor on it
            continue
        except Exception as e:
            errors.append({"id": j.get("id"), "title": j.get("title"),
                           "error": f"transport: {type(e).__name__}: {e}"})
            _tick(i, j.get("title"), {"error": type(e).__name__,
                                      "dt": time.time() - t0})
            consecutive_errors += 1
            if (abort_after_consecutive_errors is not None
                    and consecutive_errors >= abort_after_consecutive_errors):
                raise TunnelDown(
                    f"{consecutive_errors} consecutive transport errors at "
                    f"job {i}/{total} — tunnel down, aborting",
                    partial=results)
            # A transport error means the call likely did NOT complete on the
            # GPU (timeout/refused), so it produced little heat — don't pace it.
            continue
        consecutive_errors = 0
        dt = time.time() - t0
        latencies.append(dt)
        raw_verdict = v["verdict"]
        eff_verdict, downgrade = verify_evidence(
            raw_verdict, v.get("reasoning", ""), j.get("description", ""),
            tags=v.get("tags", []), title=j.get("title", ""))
        results.append({
            "id": j.get("id"), "title": j.get("title"),
            "label": j["label"], "verdict": eff_verdict,
            "raw_verdict": raw_verdict,
            "downgraded": downgrade,
            "confidence": v.get("confidence"), "tags": v.get("tags", []),
            "reasoning": v.get("reasoning", ""),
            "notes": (j.get("notes") or "")[:120],
        })
        _tick(i, j.get("title"), {"verdict": eff_verdict,
                                  "label": j.get("label"), "dt": dt})
        _pace(dt, hit_model=True)

    n = len(results)
    # confusion on the binary skip-vs-surface axis
    skip_labels = [r for r in results if r["label"] == "SKIP"]
    surf_labels = [r for r in results if label_is_surface(r["label"])]
    gate_skips = [r for r in results if r["verdict"] == "skip"]

    tp = sum(1 for r in skip_labels if r["verdict"] == "skip")   # caught skips
    fn = [r for r in skip_labels if is_surface(r["verdict"])]     # leaked skips
    fp = [r for r in surf_labels if r["verdict"] == "skip"]       # over-skipped

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    # per-rubric-tier slice (tier = the rule the gate cited in tags)
    tier_slice = {}
    for r in gate_skips:
        for tag in r["tags"]:
            tier_slice.setdefault(tag, {"n": 0, "correct": 0})
            tier_slice[tag]["n"] += 1
            if r["label"] == "SKIP":
                tier_slice[tag]["correct"] += 1

    downgraded = [r for r in results if r.get("downgraded")]
    return {
        "n": n,
        # full per-job records (incl. the model's reasoning) so a run is
        # self-diagnosing — no live re-run needed to inspect why a job went
        # the way it did (closes the T3(f) harness gap).
        "results": results,
        "errors": errors,
        "n_errors": len(errors),
        "n_downgraded_bad_evidence": len(downgraded),
        "downgraded": [{"title": r["title"], "label": r["label"],
                        "why": r["downgraded"]} for r in downgraded],
        "latency_avg_s": round(sum(latencies) / len(latencies), 2)
        if latencies else None,
        "label_distribution": {
            lbl: sum(1 for r in results if r["label"] == lbl)
            for lbl in ("APPLY", "CONSIDER", "HOLD", "SKIP")
        },
        "skip_recall": pct(tp, len(skip_labels)),       # of your SKIPs, % caught
        "skip_recall_n": len(skip_labels),
        "skip_precision": pct(tp, len(gate_skips)),     # of gate skips, % right
        "skip_precision_n": len(gate_skips),
        "surface_recall": pct(len(surf_labels) - len(fp), len(surf_labels)),
        "surface_recall_n": len(surf_labels),
        "false_negatives": fn,        # labeled SKIP, gate surfaced (leaked)
        "n_false_negatives": len(fn),
        "false_positives": fp,        # labeled act-set, gate skipped (buried)
        "n_false_positives": len(fp),
        "tier_slice": tier_slice,
    }


_VERDICT_MARK = {"skip": "SKIP", "surface": "surf", "surface_priority": "SURF+"}


def _make_progress_printer(stream=sys.stderr, heartbeat_every=5):
    """Return an on_progress callback that renders a live line.

    On a TTY it rewrites one line in place (bar + percent + ETA + last
    verdict) — the animated bar. When stderr is NOT a TTY (piped to a file,
    or captured by a wrapping tool like Claude Code), an in-place bar can't
    render, so it prints a throttled heartbeat instead: one line every
    `heartbeat_every` jobs (plus always the first and the last), carrying
    percent + count + ETA + last verdict. That keeps progress legible in a
    captured terminal without flooding the log with one line per job.
    Carries running latency so a stall is visible the moment a job runs slow."""
    start = time.time()
    is_tty = hasattr(stream, "isatty") and stream.isatty()

    def on_progress(done, total, info):
        elapsed = time.time() - start
        rate = elapsed / done if done else 0
        eta = rate * (total - done)
        pct_done = 100 * done / total if total else 100
        title = (info.get("title") or "")[:32]
        if "error" in info:
            mark = f"ERR:{info['error']}"
        else:
            mark = _VERDICT_MARK.get(info.get("verdict"), info.get("verdict", "?"))
        dt = info.get("dt", 0)
        if is_tty:
            width = 24
            filled = int(width * done / total) if total else width
            bar = "█" * filled + "·" * (width - filled)
            line = (f"\r[{bar}] {pct_done:5.1f}%  {done}/{total}  "
                    f"{dt:4.1f}s  ETA {eta:4.0f}s  {mark:<5} {title}")
            stream.write(line.ljust(96))
            if done == total:
                stream.write("\n")
        else:
            # Non-TTY (captured/piped): throttle to a heartbeat so the log
            # stays readable. Always emit the first and last job so the run
            # has a visible start and a clean finish line.
            if done == 1 or done == total or done % heartbeat_every == 0:
                stream.write(f"  {done:>3}/{total} ({pct_done:5.1f}%)  "
                             f"ETA {eta:4.0f}s  {dt:4.1f}s/job  "
                             f"{mark:<5} {title}\n")
                stream.flush()
                return
        stream.flush()

    return on_progress


def preflight(probes=3, timeout=4.0) -> tuple[bool, str]:
    """Probe Ollama before a run so a dead tunnel fails in seconds, not after
    a multi-minute hang. Returns (ok, message). One success is enough — we are
    checking reachability, not stability."""
    url = f"{ollama_url()}/api/tags"
    last = ""
    for attempt in range(1, probes + 1):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True, f"Ollama reachable at {ollama_url()}"
                last = f"HTTP {resp.status}"
        except Exception as e:  # URLError/timeout/etc.
            last = f"{type(e).__name__}: {e}"
    return False, f"Ollama unreachable at {ollama_url()} after {probes} probes ({last})"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Eval the Qwen gate vs hand labels")
    ap.add_argument("--limit", type=int, default=None,
                    help="only N labeled jobs (after --offset)")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the first N jobs (for held-out eval, e.g. 100)")
    ap.add_argument("--stub-model", action="store_true",
                    help="deterministic fake transport, no network")
    ap.add_argument("--no-progress", action="store_true",
                    help="suppress the per-job progress line")
    ap.add_argument("--abort-after", type=int, default=5,
                    help="abort if this many transport errors occur in a row "
                         "(tunnel died mid-run); 0 disables the breaker")
    ap.add_argument("--model", default=None,
                    help="Ollama model tag or profile alias (e.g. qwen2.5:14b, "
                         "qwen3:14b, qwen3:14b:think). Loads that model's "
                         "documented-best sampling profile automatically.")
    ap.add_argument("--no-think", dest="no_think", action="store_true",
                    default=None,
                    help="append /no_think to the system prompt (force "
                         "Qwen3 out of thinking mode); overrides the profile")
    ap.add_argument("--labels", type=Path, default=LABELS_PATH,
                    help="labeled job file to score against (default: the 200 "
                         "tuning labels). Point at data/heldout_unlabeled.json "
                         "for the sealed held-out run. Accepts a bare list or a "
                         "{'jobs': [...]} wrapper; all jobs must be labeled.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--cooldown", type=float, default=0.0,
                    help="seconds to sleep after each live gate call, to keep "
                         "the M4 cool on a full held-out run (default 0 = off). "
                         "Adaptive: backs off on latency creep, capped at "
                         f"{EVAL_COOLDOWN_MAX_S:g}s (< keep_alive). Ignored for "
                         "--stub-model. Thermal-only: does not change verdicts.")
    args = ap.parse_args(argv)
    if args.cooldown >= EVAL_COOLDOWN_MAX_S:
        print(f"[PACE] --cooldown {args.cooldown:g}s >= {EVAL_COOLDOWN_MAX_S:g}s "
              f"(near keep_alive) would thrash reloads; clamping.")
        args.cooldown = EVAL_COOLDOWN_MAX_S

    jobs = load_labeled_jobs(args.labels)  # read-only
    jobs = jobs[args.offset:]
    if args.limit is not None:
        jobs = jobs[:args.limit]

    # Per-model profile: the model name selects its documented-best sampling
    # settings (qwen2.5 -> certified greedy defaults; qwen3 -> temp 0.7 etc).
    # An alias like qwen3:14b:think resolves to the real tag for the API call.
    profile = profile_for(args.model) if args.model else {"options": {},
                                                          "no_think": False}
    real_model = resolve_model(args.model) if args.model else None
    no_think = args.no_think if args.no_think is not None else profile["no_think"]

    system_prompt = build_system_prompt()
    if no_think:
        system_prompt = system_prompt + "\n\n/no_think"
    transport = stub_transport if args.stub_model else None

    mode = "STUB" if args.stub_model else "LIVE Qwen"

    # preflight: don't launch a live batch into a dead tunnel (skip for stub).
    if not args.stub_model:
        ok, msg = preflight()
        print(f"[preflight] {msg}", file=sys.stderr)
        if not ok:
            print("[preflight] aborting — start ollama / bring up the tunnel "
                  "and retry.", file=sys.stderr)
            return 2

    model_label = args.model or "qwen2.5:7b (default)"
    opts_label = profile["options"] or "gate defaults"
    print(f"[{mode}] evaluating {len(jobs)} jobs "
          f"(offset {args.offset}, model {model_label}, "
          f"no_think={no_think}, opts={opts_label}) ...", file=sys.stderr)
    progress = None if args.no_progress else _make_progress_printer()
    breaker = args.abort_after if args.abort_after > 0 else None
    t0 = time.time()
    try:
        m = evaluate(jobs, system_prompt, transport=transport,
                     on_progress=progress, abort_after_consecutive_errors=breaker,
                     model=real_model, options=profile["options"] or None,
                     cooldown_s=args.cooldown)
    except TunnelDown as e:
        print(f"\n[abort] {e}", file=sys.stderr)
        print(f"[abort] {len(e.partial)} jobs completed before the drop were "
              f"NOT written (re-run when the tunnel is stable).", file=sys.stderr)
        return 3
    m["wall_clock_s"] = round(time.time() - t0, 1)
    m["mode"] = mode
    m["model"] = args.model or DEFAULT_MODEL      # the alias asked for (provenance)
    m["model_tag"] = real_model or DEFAULT_MODEL  # the real Ollama tag sent
    m["sampling"] = profile["options"] or "gate defaults"
    m["no_think"] = no_think

    args.out.write_text(json.dumps(m, indent=2))

    print(f"\n=== Qwen gate vs your labels  (n={m['n']}, {mode}) ===")
    print(f"wall clock: {m['wall_clock_s']}s   avg/job: {m['latency_avg_s']}s"
          f"   errors: {m['n_errors']}")
    print(f"label mix: {m['label_distribution']}")
    print(f"\nSKIP recall   : {m['skip_recall']}%  (caught {m['skip_recall_n']} of your SKIPs)")
    print(f"SKIP precision: {m['skip_precision']}%  (of {m['skip_precision_n']} gate skips)")
    print(f"surface recall: {m['surface_recall']}%  (of {m['surface_recall_n']} act-set jobs)")
    print(f"\nFALSE NEGATIVES (you=SKIP, gate surfaced) — the leak: {m['n_false_negatives']}")
    for r in m["false_negatives"]:
        print(f"  [{r['verdict']}] {r['title']}  conf={r['confidence']}")
        if r["notes"]:
            print(f"        your note: {r['notes']}")
    print(f"\nFALSE POSITIVES (you=act-set, gate skipped) — buried good jobs: {m['n_false_positives']}")
    for r in m["false_positives"]:
        print(f"  [{r['label']}->skip] {r['title']}  tags={r['tags']}")
        if r["notes"]:
            print(f"        your note: {r['notes']}")
    print(f"\nper-tier (rule the gate cited): {json.dumps(m['tier_slice'])}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
