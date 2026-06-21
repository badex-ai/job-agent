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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.database.models import Job, get_session, migrate_gate_columns
from v2.gate_stage import GATE_MODEL, gate_one, offline_transport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("v2_loop")

SCRAPE_FIELDS = ("id", "title", "company", "location", "is_remote",
                 "description", "apply_link", "job_url", "date_posted", "source")


def _store_new_jobs(session, raw_jobs: list[dict]) -> int:
    """Insert jobs whose id is not already a row. Returns count stored.
    New rows have gate_verdict = NULL (the 'not yet gated' sentinel)."""
    existing = {jid for (jid,) in session.query(Job.id).all()}
    stored = 0
    for j in raw_jobs:
        jid = j.get("id")
        if not jid or jid in existing:
            continue
        session.add(Job(**{k: j.get(k) for k in SCRAPE_FIELDS if k in j},
                         status="new"))
        existing.add(jid)
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
                 system_prompt=None, limit: int | None = None) -> dict:
    """Gate every row with gate_verdict IS NULL. Returns a summary.

    `system_prompt` is built once (not per job) and passed through, so the
    same validated prompt scores the whole batch. offline=True uses the
    honest surface-everything transport (no network)."""
    transport = offline_transport if offline else None
    if system_prompt is None and not offline:
        from v2.gate_prompt import build_system_prompt
        system_prompt = build_system_prompt()

    pending = session.query(Job).filter(Job.gate_verdict.is_(None))
    if limit:
        pending = pending.limit(limit)
    pending = pending.all()

    summary = {"gated": 0, "surfaced": 0, "skipped": 0, "by_source": {}}
    for job in pending:
        rec = gate_one({"id": job.id, "title": job.title,
                        "company": job.company, "location": job.location,
                        "description": job.description},
                       system_prompt=system_prompt, model=model,
                       transport=transport)
        _apply_verdict(job, rec)
        summary["gated"] += 1
        summary["surfaced" if rec["surfaced"] else "skipped"] += 1
        src = rec["gate_source"]
        summary["by_source"][src] = summary["by_source"].get(src, 0) + 1
        session.commit()  # persist per job: a crash never loses gated work
    return summary


def cycle(*, offline: bool, scrape: bool = True,
          model: str = GATE_MODEL, limit: int | None = None) -> dict:
    """One pipeline cycle: (scrape ->) store new -> gate pending."""
    logger.info("=" * 60)
    logger.info(f"V2 CYCLE START — {datetime.utcnow().isoformat()} "
                f"(offline={offline}, model={model})")
    session = get_session()
    try:
        stored = 0
        if scrape:
            from shared.scraper.job_scraper import run_scrape
            logger.info("[READ] scraping...")
            raw = run_scrape()
            logger.info(f"[READ] {len(raw)} raw jobs")
            stored = _store_new_jobs(session, raw)
            logger.info(f"[READ] {stored} new jobs stored (rest already seen)")

        logger.info("[GATE] scoring rows with gate_verdict IS NULL...")
        summary = gate_pending(session, offline=offline, model=model,
                               limit=limit)
        summary["stored"] = stored
        logger.info(f"[GATE] {summary['gated']} gated — "
                    f"{summary['surfaced']} surfaced, {summary['skipped']} skipped "
                    f"(by source: {summary['by_source']})")
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
    args = ap.parse_args(argv)

    added = migrate_gate_columns()
    if added:
        logger.info(f"[migrate] added gate columns: {added}")

    run = lambda: cycle(offline=args.offline, scrape=not args.no_scrape,
                        model=args.model, limit=args.limit)

    if args.once or args.no_scrape:
        run()
        return 0

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
