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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from v2.gate_prompt import build_system_prompt
from v2.qwen_gate import MAX_DESCRIPTION_CHARS, GateError, gate_job

REPO_ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = REPO_ROOT / "data" / "labels.json"
DEFAULT_OUT = REPO_ROOT / "data" / "eval_results.json"

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


def verify_evidence(verdict: str, reasoning: str, description: str) -> tuple[str, str | None]:
    """Guard against fabricated quotes. If the model skipped but its Evidence
    quote is NOT an actual substring of the description, the skip is invalid
    -> downgrade to surface. Returns (effective_verdict, reason_if_downgraded).

    A short quote (<8 chars) is too weak to verify reliably, so we don't
    downgrade on it; we only reject quotes that are substantive AND absent."""
    if verdict not in ("skip",):
        return verdict, None
    quote = extract_evidence(reasoning)
    if quote is None:
        return "surface", "skip with no Evidence quote"
    if len(quote) < 8:
        return verdict, None  # too short to verify; let it stand
    # truncate to the same window the model actually saw
    desc = description[:MAX_DESCRIPTION_CHARS]
    if _norm(quote) in _norm(desc):
        return verdict, None
    # try a partial: first 40 normalized chars of the quote (model may pad)
    head = _norm(quote)[:40]
    if head and head in _norm(desc):
        return verdict, None
    return "surface", f"fabricated/unverifiable evidence: {quote[:80]!r}"


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


def evaluate(jobs: list[dict], system_prompt: str, transport=None) -> dict:
    """Run the gate over jobs; return a metrics dict (every count has its n).

    Per-job failures (malformed output OR transport errors that survive
    retries) are recorded and skipped — one network blip never discards the
    jobs already completed."""
    kw = {"transport": transport} if transport is not None else {}
    results = []
    errors = []
    latencies = []
    for j in jobs:
        t0 = time.time()
        try:
            v = _gate_with_retry(j, system_prompt, kw)
        except GateError as e:
            errors.append({"id": j.get("id"), "title": j.get("title"),
                           "error": f"malformed: {e}"})
            continue
        except Exception as e:
            errors.append({"id": j.get("id"), "title": j.get("title"),
                           "error": f"transport: {type(e).__name__}: {e}"})
            continue
        latencies.append(time.time() - t0)
        raw_verdict = v["verdict"]
        eff_verdict, downgrade = verify_evidence(
            raw_verdict, v.get("reasoning", ""), j.get("description", ""))
        results.append({
            "id": j.get("id"), "title": j.get("title"),
            "label": j["label"], "verdict": eff_verdict,
            "raw_verdict": raw_verdict,
            "downgraded": downgrade,
            "confidence": v.get("confidence"), "tags": v.get("tags", []),
            "reasoning": v.get("reasoning", ""),
            "notes": (j.get("notes") or "")[:120],
        })

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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Eval the Qwen gate vs hand labels")
    ap.add_argument("--limit", type=int, default=None,
                    help="only N labeled jobs (after --offset)")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the first N jobs (for held-out eval, e.g. 100)")
    ap.add_argument("--stub-model", action="store_true",
                    help="deterministic fake transport, no network")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    jobs = json.loads(LABELS_PATH.read_text())  # read-only
    jobs = jobs[args.offset:]
    if args.limit is not None:
        jobs = jobs[:args.limit]

    system_prompt = build_system_prompt()
    transport = stub_transport if args.stub_model else None

    mode = "STUB" if args.stub_model else "LIVE Qwen"
    print(f"[{mode}] evaluating {len(jobs)} jobs ...", file=sys.stderr)
    t0 = time.time()
    m = evaluate(jobs, system_prompt, transport=transport)
    m["wall_clock_s"] = round(time.time() - t0, 1)
    m["mode"] = mode

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
