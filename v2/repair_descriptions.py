"""Re-fetch descriptions for jobs whose text was lost to LinkedIn's rate limiter.

A large scrape (1183 jobs in one pass, 2026-05-07) trips LinkedIn's throttle
partway through; the jobspy path then stores the job with an empty description
rather than failing it. Those rows are real postings — Athennian, Arbrite,
Netrolynx AI — carrying a valid job_url, so the text is recoverable without a
re-scrape and without touching the M4: fetch the stored URL, fill the column.

Deleting them would discard good jobs over a network hiccup, which is why this
repairs rather than prunes. A posting that has since closed stays empty and is
simply skipped again next run — no row is ever removed and no description is
ever fabricated.

Rate limiting is the whole reason the data is missing, so this is deliberately
slow: SLEEP_S between fetches, and a 429 backs off hard rather than burning the
remaining rows. Interrupt-safe — each repair commits on the spot, so a killed
run keeps its progress and the next invocation resumes where it stopped.

    python -m v2.repair_descriptions --limit 50        # try 50
    python -m v2.repair_descriptions --dry-run         # count only
"""

import argparse
import sys
import time

from shared.database.models import Job, get_session
from shared.scraper.job_scraper import (
    DescriptionFetchError,
    fetch_job_description,
)

SLEEP_S = 2.5          # between fetches; the throttle is what caused this mess
BACKOFF_S = 120.0      # on a suspected 429
MIN_DESC_CHARS = 100   # below this the gate cannot judge the job anyway


def blank_jobs(session, limit=None):
    """Rows with no usable description but a URL to recover it from."""
    q = (session.query(Job)
         .filter(Job.job_url.isnot(None), Job.job_url != "")
         .filter((Job.description.is_(None)) | (Job.description == ""))
         .order_by(Job.scraped_at.desc()))  # newest first: likeliest still open
    return q.limit(limit).all() if limit else q.all()


def repair(limit=None, dry_run=False, sleep_s=SLEEP_S):
    session = get_session()
    try:
        jobs = blank_jobs(session, limit)
        total = len(jobs)
        print(f"{total} job(s) with a recoverable URL and no description")
        if dry_run or not total:
            return 0

        fixed = gone = failed = 0
        for i, job in enumerate(jobs, 1):
            try:
                desc, apply_link = fetch_job_description(job.job_url)
            except DescriptionFetchError as e:          # throttle/network — back off
                failed += 1
                print(f"  [{i}/{total}] {(job.title or '')[:38]:<38} ERROR {e}")
                time.sleep(BACKOFF_S)                    # the whole reason this is slow
                continue

            if len(desc.strip()) >= MIN_DESC_CHARS:
                job.description = desc
                if apply_link and not job.apply_link:
                    job.apply_link = apply_link
                session.commit()                        # per row: a kill keeps progress
                fixed += 1
                status = f"OK {len(desc)} chars"
            else:
                # Fetched fine, but the posting has no usable body — closed, or
                # a stub too thin for the gate to judge. Leave the row untouched:
                # an empty description is honest; a placeholder would be read by
                # the gate as if it were the job's text. (A throttle no longer
                # lands here — it raises DescriptionFetchError and backs off.)
                gone += 1
                status = "still empty (closed or too thin)"

            print(f"  [{i}/{total}] {(job.title or '')[:38]:<38} {status}")
            time.sleep(sleep_s)

        print(f"\nrepaired {fixed} | still empty {gone} | errors {failed}")
        return 0
    finally:
        session.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=None,
                    help="repair at most N jobs (newest first)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report how many are repairable, change nothing")
    ap.add_argument("--sleep", type=float, default=SLEEP_S,
                    help=f"seconds between fetches (default {SLEEP_S})")
    args = ap.parse_args(argv)
    return repair(limit=args.limit, dry_run=args.dry_run, sleep_s=args.sleep)


if __name__ == "__main__":
    sys.exit(main())
