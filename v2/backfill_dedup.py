"""One-time backfill: collapse existing duplicate rows in jobs.db.

The store-time dedup (v2/dedup + _store_new_jobs) only helps FUTURE scrapes; the
~900 duplicate rows already in the DB would still each cost the gate (M4) a call.
This script collapses existing content-key dup groups into one canonical row and
deletes the redundant rows — recovering ~15-30 M4-hours of avoided re-scoring.

SAFETY (dry-run by default; pass --apply to write):
- A row is deletable ONLY if it is simultaneously: ungated (gate_verdict IS NULL),
  unlabeled (id not in data/labels.json — 148 labeled ids sit in dup groups), and
  unreferenced by the decisions table. Any row failing a guard is kept.
- Within a group, the canonical row (the one kept) is chosen by:
  labeled > gated > longest description. Before deleting the rest, the freshest
  apply_link / job_url / date_posted and any non-blank description are merged into
  the canonical (same merge policy as the live store path).
- status and gate_* columns are never mutated (the gate-status invariant).
- Deletion is safe against re-scrape resurrection because the store-time content
  dedup now catches a returning id.

Usage (from repo root, venv):
    .venv/bin/python -m v2.backfill_dedup            # dry run, prints plan
    .venv/bin/python -m v2.backfill_dedup --apply    # writes (back up jobs.db first)
"""
import argparse
import json
import sys
from pathlib import Path

from shared.database.models import Job, Decision, get_session
from v2 import dedup

LABELS_PATH = Path("data/labels.json")


def _labeled_ids() -> set[str]:
    # Refuse rather than run with an empty guard: labels.json is the ONLY thing
    # protecting ungated labeled rows from the deletable predicate. A missing
    # file must abort, not silently permit deleting ground-truth rows.
    if not LABELS_PATH.exists():
        raise SystemExit(f"refusing to run: {LABELS_PATH} not found — the "
                         "labeled-row guard would be empty and could delete "
                         "ground-truth rows.")
    return {str(r["id"]) for r in json.loads(LABELS_PATH.read_text()) if r.get("id")}


def _decision_job_ids(session) -> set[str]:
    return {jid for (jid,) in session.query(Decision.job_id).distinct().all()}


def _canonical(group: list[Job], labeled: set[str]) -> Job:
    """Pick the row to KEEP: labeled > gated > longest description."""
    def rank(r: Job):
        return (r.id in labeled, r.gate_verdict is not None,
                len(r.description or ""))
    return max(group, key=rank)


def _fold(canonical: Job, others: list[Job]) -> None:
    """Merge volatile fields from the duplicates into the canonical row."""
    for o in others:
        if dedup.is_blank(canonical.description) and not dedup.is_blank(o.description):
            canonical.description = o.description
        if o.apply_link:
            canonical.apply_link = o.apply_link
        if o.job_url:
            canonical.job_url = o.job_url
        o_date = dedup.clean_date(o.date_posted)
        if o_date:
            canonical.date_posted = o_date


def plan(session):
    """Compute (deletable, blocked) per dup group without writing."""
    labeled = _labeled_ids()
    decided = _decision_job_ids(session)
    rows = session.query(Job).all()

    groups: dict[tuple[str, str], list[Job]] = {}
    for r in rows:
        groups.setdefault(dedup.content_key(
            {"title": r.title, "company": r.company}), []).append(r)

    to_delete, blocked, folds = [], [], []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        # Only treat members as dups of the canonical if the description guard
        # agrees (protects distinct roles sharing a title+company).
        canon = _canonical(group, labeled)
        dups = [r for r in group
                if r.id != canon.id
                and dedup.is_duplicate(canon.description, r.description or "")]
        if not dups:
            continue
        deletable = [r for r in dups
                     if r.gate_verdict is None
                     and r.id not in labeled
                     and r.id not in decided]
        kept_back = [r for r in dups if r not in deletable]
        if deletable:
            folds.append((canon, deletable))
            to_delete.extend(deletable)
        if kept_back:
            blocked.extend(kept_back)
    return to_delete, blocked, folds


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry run)")
    args = ap.parse_args(argv)

    session = get_session()
    try:
        to_delete, blocked, folds = plan(session)
        total = session.query(Job).count()
        print(f"DB rows:                 {total}")
        print(f"redundant rows to remove:{len(to_delete)}")
        print(f"dup rows KEPT (labeled/gated/decided guard): {len(blocked)}")
        print(f"canonical rows to refresh:{len(folds)}")
        print(f"projected rows after:    {total - len(to_delete)}")

        if not args.apply:
            print("\n[DRY RUN] nothing written. Re-run with --apply to execute.")
            for canon, dups in folds[:10]:
                print(f"  keep {canon.id!r} ({canon.title!r} @ {canon.company!r}) "
                      f"<- drop {len(dups)}")
            return 0

        for canon, dups in folds:
            _fold(canon, dups)
        for r in to_delete:
            session.delete(r)
        session.commit()
        print(f"\n[APPLIED] deleted {len(to_delete)} rows, "
              f"refreshed {len(folds)} canonicals.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
