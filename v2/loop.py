#!/usr/bin/env python3
"""v2 pipeline loop (Phase 5) — scrape, dedup, gate, persist.

The v1 RAPH loop with its one weak organ replaced. v1 scored jobs with an
unmeasured cv_matcher/overall_score; this loop scores them with the measured
Qwen gate (the design notes) and writes the verdict onto the row.

What it reuses from the working v1 system, unchanged:
  - shared.scraper.job_scraper.run_scrape   (the scraper)
  - shared.database.models.Job              (one store, one source of truth)

What it does NOT do: send anything. The gate is below its 85% agreement bar
(§9.1), so every surfaced job stays for human review. Auto-send is earned
later, by a gate that clears the bar — not by this loop.

Two independent dedup guarantees keep the M4 cool (the whole point of
gating only NEW work):
  1. scrape dedup  — a job whose id is already a row is not re-stored.
  2. gate dedup    — only rows with gate_verdict IS NULL are sent to Qwen.
     A row stored but left ungated (a mid-run tunnel drop) is retried next
     cycle; a fully-gated row never hits the M4 again.

Usage:
  .venv/bin/python v2/loop.py --once --offline   # one cycle, no network gate
  .venv/bin/python v2/loop.py --once             # one cycle, live 14b gate
  .venv/bin/python v2/loop.py                     # hourly scheduler (live)
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.database.models import Job, get_session, migrate_gate_columns
from v2 import dedup
from v2.gate_stage import GATE_MODEL, gate_one, offline_transport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("v2_loop")

SCRAPE_FIELDS = ("id", "title", "company", "location", "is_remote",
                 "description", "apply_link", "job_url", "date_posted", "source")

# --- Thermal pacing (M4 courtesy) --------------------------------------------
# The gate host is an M4; a back-to-back 14B batch drives its GPU to
# ~100% duty cycle and ~100°C sustained (measured 2026-07-11: qwen2.5:14b loads
# 48/48 layers on the Metal GPU). The fix is NOT to offload layers to CPU (that
# relocates heat to the shared die and slows every job); it is to PACE — the
# workload is a latency-insensitive overnight drain, so we sleep between jobs
# and let the heatsink shed heat in the gaps. Full GPU offload is retained; the
# GPU does each job at full speed, just not continuously. Pacing lives here in
# the caller because Ollama has no thermal/rate-limit knob of its own (verified
# against ollama source, 2026-07-11) — it MUST be client-side.
#
# The cooldown is a FIXED interval, deliberately not adaptive. An earlier arm
# treated per-job latency as thermal telemetry ("Metal throttling shows up as
# latency creep") and lengthened the cooldown on creep past 1.5x a running-minimum
# baseline. Direct measurement (macmon on the Mac, 2026-07-15 — see v2/THERMAL.md)
# retired that premise: the M4 holds ~8s/job flat at 100C, so latency does not
# track heat here. The baseline was also a running MINIMUM, which only ratchets
# down — the trip threshold drifted below the median job and fired on ordinary
# content variance (description length, reasoning verbosity), so the backoff
# doubled faster than it halved and pinned at the cap on a GPU sitting at 77C.
# Spacing is the real lever; pick it by measurement, not by a proxy.
GATE_COOLDOWN_S = 60.0      # sleep after each LIVE gate call (0 disables pacing)
# Keep the cooldown strictly BELOW keep_alive ("5m"=300s in qwen_gate.py): if the
# inter-job gap reached the unload timer, each lost race against expiry adds a
# ~5.3s cold reload. 240s leaves margin. NOTE this bound holds for the sleep only
# — a job that burns its full retry budget (3 x 120s timeout + 2 x 3s backoff in
# eval_gate._gate_with_retry) already exceeds keep_alive and will reload.
COOLDOWN_MAX_S = 240.0


def _within_window(last_seen, now) -> bool:
    """True if last_seen is within DUP_WINDOW_DAYS of now (so a match is a
    re-post, not a re-opening). A missing timestamp is treated as in-window so a
    row with no scraped_at still dedups rather than duplicating unboundedly."""
    if last_seen is None:
        return True
    return (now - last_seen).days < dedup.DUP_WINDOW_DAYS


def _merge_into(existing: Job, incoming: dict) -> None:
    """Fold a duplicate incoming posting into the row we keep.

    Keeps the existing id (labels/verdicts/decisions attach by id) and NEVER
    touches status or any gate_* column (the gate-status invariant the rescue
    view depends on). Refreshes only volatile fields: fill a blank description
    from the incoming side; refresh apply_link/job_url when the merge was made
    with both descriptions present (high confidence) or the existing row was
    blank; take the freshest non-junk date_posted; and refresh scraped_at to
    NOW so the recency window (dedup.DUP_WINDOW_DAYS) measures the gap since the
    role was LAST seen, not first seen.
    """
    inc_desc = incoming.get("description") or ""
    existing_blank = dedup.is_blank(existing.description)
    if existing_blank and not dedup.is_blank(inc_desc):
        existing.description = inc_desc
    both_present = not existing_blank and not dedup.is_blank(inc_desc)
    if existing_blank or both_present:
        if incoming.get("apply_link"):
            existing.apply_link = incoming["apply_link"]
        if incoming.get("job_url"):
            existing.job_url = incoming["job_url"]
    inc_date = dedup.clean_date(incoming.get("date_posted"))
    if inc_date:
        existing.date_posted = inc_date
    existing.scraped_at = datetime.utcnow()


def _store_new_jobs(session, raw_jobs: list[dict]) -> int:
    """Insert genuinely-new jobs; merge duplicates into the row we already keep.

    Dedup is two-layer: exact id (a re-seen posting) and content
    (v2/dedup.content_key + description guard — the same role re-posted under a
    fresh id, or arriving from another source). A merged duplicate is folded into
    the existing row instead of adding a second one the gate would re-score.
    Returns the count of genuinely-new rows stored. New rows have
    gate_verdict = NULL (the 'not yet gated' sentinel)."""
    rows = session.query(Job).all()
    by_id = {r.id: r for r in rows}
    by_key: dict[tuple[str, str], list[Job]] = {}
    for r in rows:
        by_key.setdefault(dedup.content_key(
            {"title": r.title, "company": r.company}), []).append(r)

    stored = 0
    for j in raw_jobs:
        jid = j.get("id")
        if not jid:
            continue
        if jid in by_id:                       # exact re-post of a known id
            _merge_into(by_id[jid], j)
            continue
        key = dedup.content_key(j)
        # A content match is a duplicate only if it was last seen within the
        # recency window; a matching row scraped longer ago is a genuine
        # re-opening and must fall through to be stored as a new row.
        now = datetime.utcnow()
        match = next(
            (r for r in by_key.get(key, [])
             if dedup.is_duplicate(r.description, j.get("description") or "")
             and _within_window(r.scraped_at, now)),
            None)
        if match is not None:                  # same role, seen recently
            _merge_into(match, j)
            continue
        row = Job(**{k: j.get(k) for k in SCRAPE_FIELDS if k in j}, status="new")
        session.add(row)
        by_id[jid] = row
        by_key.setdefault(key, []).append(row)
        stored += 1
    session.commit()
    return stored


def _apply_verdict(job: Job, rec: dict) -> None:
    job.gate_verdict = rec["gate_verdict"]
    job.gate_reason = rec["gate_reason"]
    job.gate_model = rec["gate_model"]
    job.gate_tags = json.dumps(rec["gate_tags"])
    job.gate_confidence = rec["gate_confidence"]
    job.gate_source = rec["gate_source"]
    job.gate_run_at = rec["gate_run_at"]


def gate_pending(session, *, offline: bool, model: str = GATE_MODEL,
                 system_prompt=None, limit: int | None = None,
                 cooldown_s: float = GATE_COOLDOWN_S) -> dict:
    """Gate every row with gate_verdict IS NULL. Returns a summary.

    `system_prompt` is built once (not per job) and passed through, so the
    same validated prompt scores the whole batch. offline=True uses the
    honest surface-everything transport (no network).

    Live batches PACE the M4: after each call that actually hit the model,
    sleep `cooldown_s` so the GPU sheds heat (see module header). Offline runs
    and hard-gate skips never touch the M4, so they are never paced."""
    transport = offline_transport if offline else None
    if system_prompt is None and not offline:
        from v2.gate_prompt import build_system_prompt
        system_prompt = build_system_prompt()

    # A blank description gives Qwen nothing to judge — the prompt and rubric
    # assume real body text, and hard_gates.py keys its regexes off it too, so
    # an empty one silently disables the salary/sponsorship/location checks.
    # Excluding beats gating-on-empty: the verdict would be indistinguishable
    # in the DB from a real one while resting on nothing, and a wrong verdict
    # buries a good job (§6a). These rows stay NULL until
    # v2/repair_descriptions.py fills the column, which needs no M4 time.
    pending = session.query(Job).filter(
        Job.gate_verdict.is_(None),
        Job.description.isnot(None), Job.description != "")
    if limit:
        pending = pending.limit(limit)
    pending = pending.all()

    # Pace only when we are actually driving the M4 (live + a real cooldown).
    pacing = not offline and cooldown_s > 0

    summary = {"gated": 0, "surfaced": 0, "skipped": 0, "by_source": {},
               "m4_down": False}
    for job in pending:
        t0 = time.monotonic()
        try:
            rec = gate_one({"id": job.id, "title": job.title,
                            "company": job.company, "location": job.location,
                            "description": job.description},
                           system_prompt=system_prompt, model=model,
                           transport=transport)
        except OSError as exc:
            # Transport failure = the M4 is down (URLError/timeout, both OSError).
            # It is down for *every* job, so stop the cycle rather than hammer a
            # dead host N times. This job stays gate_verdict IS NULL, so the next
            # cycle retries it automatically — NULL is the retry queue, no per-job
            # state needed. A malformed *model* response is NOT an OSError; it's
            # caught inside gate_one and surfaced, so it never reaches here.
            logger.warning(f"[GATE] M4 unreachable, stopping cycle: {exc}")
            summary["m4_down"] = True
            break
        elapsed = time.monotonic() - t0
        _apply_verdict(job, rec)
        summary["gated"] += 1
        summary["surfaced" if rec["surfaced"] else "skipped"] += 1
        src = rec["gate_source"]
        summary["by_source"][src] = summary["by_source"].get(src, 0) + 1
        session.commit()  # persist per job: a crash never loses gated work

        # Pace AFTER a call that reached the M4. A hard-gate skip returns
        # source='hard_gate' without any network call, so it needs no cooldown.
        # NOTE 'error' IS paced: a GateError means the M4 ran the FULL inference
        # and only the output parse failed — thermally identical to 'qwen'. A
        # schema-drift streak overnight must not run back-to-back unpaced.
        if pacing and src != "hard_gate":
            logger.info(f"[PACE] job latency {elapsed:.1f}s — sleeping "
                        f"{cooldown_s:.0f}s")
            time.sleep(cooldown_s)
    return summary


def cycle(*, offline: bool, scrape: bool = True,
          model: str = GATE_MODEL, limit: int | None = None,
          cooldown_s: float = GATE_COOLDOWN_S) -> dict:
    """One pipeline cycle: (scrape ->) store new -> gate pending."""
    logger.info("=" * 60)
    logger.info(f"V2 CYCLE START — {datetime.utcnow().isoformat()} "
                f"(offline={offline}, model={model})")
    session = get_session()
    try:
        # Backlog-first: clear the NULL debt BEFORE scraping more. Across repeated
        # short M4 outages this keeps the ungated pile from growing unboundedly —
        # we pay down what we owe before adding new jobs to gate.
        logger.info("[GATE] clearing NULL backlog before scraping...")
        summary = gate_pending(session, offline=offline, model=model, limit=limit,
                               cooldown_s=cooldown_s)

        stored = 0
        if scrape:
            # Scrape even when the gate is dead. The LinkedIn search is filtered
            # to a narrow recency window (LINKEDIN_FILTERS f_TPR), so an hour not
            # scraped is an hour of postings lost for good — whereas a job stored
            # ungated costs nothing but a row and is picked up by the next cycle's
            # backlog pass. Losing postings to a sleeping laptop is the expensive
            # error; a longer NULL queue is the cheap one.
            from shared.scraper.job_scraper import run_scrape
            logger.info("[READ] scraping...")
            raw = run_scrape()
            logger.info(f"[READ] {len(raw)} raw jobs")
            stored = _store_new_jobs(session, raw)
            logger.info(f"[READ] {stored} new jobs stored (rest already seen)")

            if summary["m4_down"]:
                logger.warning(f"[GATE] M4 down — {stored} new jobs stored UNGATED; "
                               f"they queue for the next cycle.")
            else:
                # Gate the freshly-stored jobs in the same cycle (folding the
                # second pass's counts into the summary).
                logger.info("[GATE] scoring newly-stored rows...")
                second = gate_pending(session, offline=offline, model=model, limit=limit,
                                      cooldown_s=cooldown_s)
                for k in ("gated", "surfaced", "skipped"):
                    summary[k] += second[k]
                for src, n in second["by_source"].items():
                    summary["by_source"][src] = summary["by_source"].get(src, 0) + n
                summary["m4_down"] = summary["m4_down"] or second["m4_down"]

        summary["stored"] = stored
        logger.info(f"[GATE] {summary['gated']} gated — "
                    f"{summary['surfaced']} surfaced, {summary['skipped']} skipped "
                    f"(by source: {summary['by_source']})  m4_down={summary['m4_down']}")
        if summary["m4_down"]:
            logger.warning("[DONE] cycle ended early — M4 unreachable; NULL jobs "
                           "retried next cycle.")
        else:
            logger.info("[DONE] surfaced jobs await human review (no auto-send).")
        return summary
    finally:
        session.close()
        logger.info("=" * 60)


def main(argv=None):
    ap = argparse.ArgumentParser(description="v2 scrape+gate pipeline loop")
    ap.add_argument("--once", action="store_true",
                    help="run one cycle and exit (default without scheduler)")
    ap.add_argument("--offline", action="store_true",
                    help="honest offline gate (surface-all, no network)")
    ap.add_argument("--no-scrape", action="store_true",
                    help="skip scraping; only gate already-stored NULL rows")
    ap.add_argument("--limit", type=int, default=None,
                    help="gate at most N pending rows (smoke testing)")
    ap.add_argument("--model", default=GATE_MODEL,
                    help=f"Ollama model tag (default {GATE_MODEL})")
    ap.add_argument("--cooldown", type=float, default=GATE_COOLDOWN_S,
                    help=f"seconds to sleep after each live gate call, pacing "
                         f"the M4 (default {GATE_COOLDOWN_S:g}; 0 disables). "
                         f"Adaptive: backs off on latency creep. Ignored offline.")
    args = ap.parse_args(argv)

    # A cooldown at/above the 5m keep_alive lets the model unload between jobs,
    # so every job pays a cold reload (thrash, and counterproductive heat). Clamp
    # the floor under the adaptive cap, which is itself kept under keep_alive.
    if args.cooldown >= COOLDOWN_MAX_S:
        logger.warning(f"[PACE] --cooldown {args.cooldown:g}s >= {COOLDOWN_MAX_S:g}s "
                       f"(near keep_alive) would thrash reloads; clamping to "
                       f"{COOLDOWN_MAX_S:g}s.")
        args.cooldown = COOLDOWN_MAX_S

    added = migrate_gate_columns()
    if added:
        logger.info(f"[migrate] added gate columns: {added}")

    run = lambda: cycle(offline=args.offline, scrape=not args.no_scrape,
                        model=args.model, limit=args.limit,
                        cooldown_s=args.cooldown)

    if args.once or args.no_scrape:
        summary = run()
        # Exit code carries the outcome so a scheduled run can be trusted: 0 =
        # the cycle gated what it set out to, 1 = the M4 was unreachable and the
        # NULL rows are still pending. Without this every run looks identical to
        # cron: exit 0 while nothing happened.
        return 1 if summary.get("m4_down") else 0

    # hourly scheduler, mirroring v1's loop (apscheduler, single instance).
    from apscheduler.schedulers.blocking import BlockingScheduler
    from shared.config import SCRAPE_INTERVAL_MINUTES
    logger.info(f"v2 loop starting — every {SCRAPE_INTERVAL_MINUTES} min")
    run()
    sched = BlockingScheduler()
    sched.add_job(run, "interval", minutes=SCRAPE_INTERVAL_MINUTES,
                  id="v2_loop", max_instances=1)
    try:
        sched.start()
    except KeyboardInterrupt:
        logger.info("v2 loop stopped by user.")
        sched.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
