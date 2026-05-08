"""
RAPH Loop — the autonomous agent core.
READ → ANALYSE → PLAN → HANDLE

Runs every SCRAPE_INTERVAL_MINUTES (default 60 min).
This is the top-level orchestration layer.
"""
import logging
import json
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from shared.config import SCRAPE_INTERVAL_MINUTES, MIN_MATCH_SCORE
from shared.scraper.job_scraper import run_scrape
from v1.analyzer.cv_matcher import analyse_batch
from v1.agents.batch_processor import BatchProcessor
from v1.outreach.email_sender import send_job_application
from shared.database.models import get_session, Job, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("raph_loop")


def raph_cycle():
    """One full RAPH cycle."""
    logger.info("=" * 60)
    logger.info(f"RAPH CYCLE START — {datetime.utcnow().isoformat()}")
    logger.info("=" * 60)

    session = get_session()

    # ── READ ──────────────────────────────────────────────────────
    logger.info("[READ] Scraping job boards...")
    raw_jobs = run_scrape()
    logger.info(f"[READ] Found {len(raw_jobs)} raw jobs")

    # Filter out jobs already in DB
    existing_ids = {j.id for j in session.query(Job.id).all()}
    new_jobs = [j for j in raw_jobs if j.get("id") not in existing_ids]
    logger.info(f"[READ] {len(new_jobs)} new jobs (not seen before)")

    if not new_jobs:
        logger.info("[READ] No new jobs this cycle — done early")
        session.close()
        return

    # ── ANALYSE ───────────────────────────────────────────────────
    logger.info(f"[ANALYSE] Running batch evaluation on {len(new_jobs)} jobs (4 parallel workers)...")
    
    # Use batch processor for parallel evaluation
    processor = BatchProcessor(max_workers=4)
    
    # Convert job dicts to Job objects for batch processor
    session_tmp = get_session()
    for job_data in new_jobs:
        # Save job to DB first
        job = Job(
            id=job_data["id"],
            title=job_data.get("title", ""),
            company=job_data.get("company", ""),
            location=job_data.get("location", ""),
            is_remote=job_data.get("is_remote", True),
            description=job_data.get("description", ""),
            apply_link=job_data.get("apply_link", ""),
            job_url=job_data.get("job_url", ""),
            date_posted=job_data.get("date_posted", ""),
            source=job_data.get("source", ""),
            status="new",
        )
        session_tmp.merge(job)
    session_tmp.commit()
    
    # Get all new jobs from DB
    jobs_to_process = session_tmp.query(Job).filter(Job.overall_score == 0).all()
    session_tmp.close()
    
    # Process batch
    batch_summary = processor.process_batch(jobs_to_process, save_interval=5)
    processor.save_to_database()
    logger.info(f"[ANALYSE] Completed {batch_summary['completed']}/{batch_summary['total']} evaluations")

    # ── PLAN ──────────────────────────────────────────────────────
    session = get_session()
    
    qualified = session.query(Job).filter(
        Job.overall_score >= MIN_MATCH_SCORE,
        Job.status == "new"
    ).all()
    logger.info(f"[PLAN] {len(qualified)} jobs meet minimum score ({MIN_MATCH_SCORE}+)")
    logger.info(f"[PLAN] Use 'python -m dashboard.cli' to review and apply to jobs")

    # ── HANDLE ────────────────────────────────────────────────────
    # Manual control: User reviews jobs in CLI dashboard and marks for application
    # (No auto-send to preserve user control)
    
    logger.info(f"[HANDLE] All jobs ready for manual review in CLI dashboard")
    logger.info(f"[HANDLE] Run: python -m dashboard.cli")
    session.close()

    logger.info(f"RAPH CYCLE COMPLETE — {datetime.utcnow().isoformat()}")
    logger.info("=" * 60)


def start_agent():
    """Start the RAPH loop scheduler."""
    init_db()
    logger.info(f"Job Agent starting — running every {SCRAPE_INTERVAL_MINUTES} minutes")
    logger.info("Dashboard: run `python -m dashboard.cli` in a separate terminal")

    # Run once immediately on startup
    raph_cycle()

    # Then schedule
    scheduler = BlockingScheduler()
    scheduler.add_job(
        raph_cycle,
        "interval",
        minutes=SCRAPE_INTERVAL_MINUTES,
        id="raph_loop",
        max_instances=1,  # never run two cycles simultaneously
    )
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
        scheduler.shutdown()
