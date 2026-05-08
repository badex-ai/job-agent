"""
Batch Processor — Handle 10+ jobs efficiently.
Features:
- Parallel evaluation (3-5 concurrent API calls)
- Resumable state (crash-tolerant)
- Progress tracking
- Checkpoint recovery
"""
import sys
import os
# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import logging
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import sessionmaker
from shared.database.models import Job, get_engine
from v1.analyzer.cv_matcher import analyse_job
from shared.scraper.contact_extractor import extract_contacts
from shared.config import MIN_MATCH_SCORE

logger = logging.getLogger(__name__)
engine = get_engine()
Session = sessionmaker(bind=engine)

CHECKPOINT_DIR = Path("data/checkpoints")
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


class BatchProcessor:
    """Process jobs in batches with parallelization and checkpointing."""
    
    def __init__(self, batch_id: str = None, max_workers: int = 4):
        self.batch_id = batch_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.max_workers = max_workers
        self.checkpoint_file = CHECKPOINT_DIR / f"batch_{self.batch_id}.json"
        self.processed = set()
        self.results = {}
        self._lock = threading.Lock()  # Protect concurrent access to results/processed
        self.load_checkpoint()
    
    def load_checkpoint(self):
        """Recover from previous run if exists."""
        if self.checkpoint_file.exists():
            try:
                data = json.load(open(self.checkpoint_file))
                self.processed = set(data.get("processed", []))
                self.results = data.get("results", {})
                logger.info(f"Resumed batch {self.batch_id}: {len(self.processed)} already processed")
            except Exception as e:
                logger.warning(f"Could not load checkpoint: {e}")
    
    def save_checkpoint(self):
        """Save progress to checkpoint."""
        # Snapshot the dict/set under lock to avoid race condition
        with self._lock:
            processed_snapshot = list(self.processed)
            results_snapshot = dict(self.results)

        data = {
            "batch_id": self.batch_id,
            "processed": processed_snapshot,
            "results": results_snapshot,
            "timestamp": datetime.now().isoformat(),
        }
        with open(self.checkpoint_file, "w") as f:
            json.dump(data, f, indent=2)
    
    def process_job_wrapper(self, job: Job) -> tuple:
        """Wrapper to handle individual job processing."""
        try:
            with self._lock:
                if job.id in self.processed:
                    logger.debug(f"Skipping already processed: {job.id}")
                    return job.id, True, "skipped"
            
            # Convert SQLAlchemy object to dict (guard against None values)
            job_dict = {
                "id": job.id,
                "title": job.title or "",
                "company": job.company or "",
                "description": job.description or "",
                "location": job.location or "",
                "apply_link": job.apply_link or "",
                "job_url": job.job_url or "",
                "date_posted": job.date_posted,
                "source": job.source or "",
            }

            logger.info(f"Processing: {job_dict['title']} @ {job_dict['company']}")
            result = analyse_job(job_dict)

            # Extract contact info (0 tokens, regex only)
            contacts = extract_contacts(job_dict)
            result.update(contacts)

            # Store result (protected by lock for thread safety)
            with self._lock:
                self.results[job.id] = result
                self.processed.add(job.id)
            
            score = result.get("overall_score", 0)
            action = result.get("recommended_action", "SKIP")
            
            return job.id, True, f"{action} (score: {score:.0f})"
        
        except Exception as e:
            logger.error(f"Error processing job {job.id}: {e}")
            with self._lock:
                self.processed.add(job.id)
            return job.id, False, str(e)
    
    def process_batch(self, jobs: list[Job], save_interval: int = 5):
        """Process batch with parallelization and checkpointing."""
        total = len(jobs)
        completed = 0
        failed = 0
        
        logger.info(f"Starting batch {self.batch_id}: {total} jobs (max {self.max_workers} parallel)")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all jobs
            futures = {
                executor.submit(self.process_job_wrapper, job): job.id
                for job in jobs
            }
            
            # Process as they complete
            for future in as_completed(futures):
                job_id, success, message = future.result()
                completed += 1
                
                if not success:
                    failed += 1
                
                # Progress update
                pct = (completed / total) * 100
                logger.info(f"[{completed}/{total} {pct:.0f}%] {job_id}: {message}")
                
                # Checkpoint every N jobs
                if completed % save_interval == 0:
                    self.save_checkpoint()
                    logger.info(f"Checkpoint saved ({completed}/{total})")
        
        # Final save
        self.save_checkpoint()
        
        logger.info(f"Batch complete: {completed} processed, {failed} failed")
        return {
            "batch_id": self.batch_id,
            "total": total,
            "completed": completed,
            "failed": failed,
            "results": self.results,
        }
    
    def save_to_database(self):
        """Save evaluated results to database."""
        session = Session()
        saved = 0
        
        for job_id, result in self.results.items():
            job = session.query(Job).filter(Job.id == job_id).first()
            if not job:
                logger.warning(f"Job {job_id} not found in database")
                continue
            
            # Update job fields
            job.overall_score = result.get("overall_score", 0)
            job.cv_match = result.get("cv_match", 0)
            job.level_fit = result.get("level_fit", 0)
            job.culture_fit = result.get("culture_fit", 0)
            job.red_flags = json.dumps(result.get("red_flags", []))
            job.top_strengths = json.dumps(result.get("top_strengths", []))
            job.gaps = json.dumps(result.get("gaps", []))
            job.why_good_fit = result.get("why_good_fit", "")
            job.why_concerns = result.get("why_concerns", "")
            job.recommended_action = result.get("recommended_action", "SKIP")
            job.report_markdown = result.get("report_markdown", "")
            job.job_type = result.get("job_type", "fullstack")

            # Contact info (extracted via regex, 0 tokens)
            job.company_email = result.get("company_email")
            job.recruiter_email = result.get("recruiter_email")
            job.recruiter_linkedin = result.get("recruiter_linkedin")
            job.recruiter_name = result.get("recruiter_name")

            saved += 1
        
        session.commit()
        session.close()
        
        logger.info(f"Saved {saved} evaluations to database")
        return saved


def process_new_jobs(max_parallel: int = 4) -> dict:
    """Process all new (unevaluated) jobs."""
    session = Session()
    new_jobs = session.query(Job).filter(Job.overall_score == 0).all()
    session.close()
    
    if not new_jobs:
        logger.info("No new jobs to process")
        return {"batch_id": None, "total": 0, "completed": 0}
    
    processor = BatchProcessor(max_workers=max_parallel)
    summary = processor.process_batch(new_jobs)
    processor.save_to_database()
    
    return summary


def process_jobs_above_score(min_score: int = 0, max_parallel: int = 4) -> dict:
    """Re-evaluate jobs above a certain score."""
    session = Session()
    jobs = session.query(Job).filter(Job.overall_score >= min_score).all()
    session.close()
    
    logger.info(f"Re-evaluating {len(jobs)} jobs with score >= {min_score}")
    
    processor = BatchProcessor(max_workers=max_parallel)
    summary = processor.process_batch(jobs)
    processor.save_to_database()
    
    return summary


def get_batch_status(batch_id: str) -> dict:
    """Get status of a batch run."""
    checkpoint_file = CHECKPOINT_DIR / f"batch_{batch_id}.json"
    if not checkpoint_file.exists():
        return {"status": "not_found"}
    
    try:
        data = json.load(open(checkpoint_file))
        return {
            "status": "completed",
            "batch_id": batch_id,
            "processed": len(data.get("processed", [])),
            "timestamp": data.get("timestamp"),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def cleanup_old_checkpoints(days: int = 7):
    """Remove checkpoints older than N days."""
    import time
    cutoff = time.time() - (days * 86400)
    
    for checkpoint in CHECKPOINT_DIR.glob("batch_*.json"):
        if os.path.getmtime(checkpoint) < cutoff:
            os.remove(checkpoint)
            logger.info(f"Deleted old checkpoint: {checkpoint}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Example: process all new jobs with 4 parallel workers
    summary = process_new_jobs(max_parallel=4)
    print(json.dumps(summary, indent=2))
