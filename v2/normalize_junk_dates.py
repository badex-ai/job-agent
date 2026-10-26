#!/usr/bin/env python3
"""One-off repair: turn the literal text "None"/"nan"/"NaT" in Job.date_posted
into "" so an absent date reads as absent.

Why this exists: the scraper used to stringify pandas nulls (str(NaT) == "NaT"),
so 754 of 986 rows stored a truthy 4-character string where a date belonged.
Every "if job.date_posted:" check downstream treated those rows as dated. The
scraper boundary (shared/scraper/job_scraper._clean_date) now blocks new junk;
this cleans what the old code already wrote.

This does NOT recover the real dates — it only stops the lie. Re-fetching the
true posting dates from LinkedIn is a separate job.

  python v2/normalize_junk_dates.py --dry-run   # report, change nothing
  python v2/normalize_junk_dates.py             # apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.database.models import Job, get_session

JUNK = {"None", "nan", "NaT", "none", "NaN", "null", "NaTType"}


def normalize(session=None, *, dry_run: bool = False) -> dict:
    """Blank every junk date_posted. Returns a per-value count of what changed."""
    own = session is None
    session = session or get_session()
    try:
        rows = session.query(Job).filter(Job.date_posted.in_(sorted(JUNK))).all()
        counts: dict = {}
        for job in rows:
            counts[job.date_posted] = counts.get(job.date_posted, 0) + 1
            if not dry_run:
                job.date_posted = ""
        if not dry_run:
            session.commit()
        return counts
    finally:
        if own:
            session.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    session = get_session()
    try:
        total = session.query(Job).count()
        real_before = (session.query(Job)
                       .filter(Job.date_posted.isnot(None))
                       .filter(~Job.date_posted.in_(sorted(JUNK)))
                       .filter(Job.date_posted != "").count())
        counts = normalize(session, dry_run=args.dry_run)
        changed = sum(counts.values())

        verb = "would blank" if args.dry_run else "blanked"
        print(f"jobs in DB          : {total}")
        print(f"real dates (kept)   : {real_before}")
        print(f"{verb:<20}: {changed}")
        for value, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {value!r:<10} {n}")
        if args.dry_run:
            print("\ndry run — nothing written")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
