"""The one new read: classified jobs, grouped into the four buckets.

Classification lives in the CLI, which on each triage action appends an immutable
Decision row (human_label = APPLY/CONSIDER/HOLD/SKIP) and advances a coarse
Job.status. Decision.human_label is the true four-way bucket — CONSIDER and HOLD
both collapse to status='reviewed', so status alone cannot separate them
(SPEC §6). So we group by the LATEST Decision.human_label per job.

Read-only: this module opens a session, queries, and returns plain dicts. It never
writes. The dicts are JSON-serialisable so the server can inject them straight into
the page.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

from shared.database.models import Job, Decision, get_session

# The CVs the picker offers, read from disk rather than hardcoded, so the web
# code names no specific CV (calibration seam, plan §6). data/ sits at the repo
# root: queries.py is v2/webapp/queries.py, so three parents up is the root.
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# The rubric's four-way label space (matches decisions.HUMAN_LABELS). Kept in this
# order so buckets render Apply-first.
BUCKETS = ("APPLY", "CONSIDER", "HOLD", "SKIP")


def _tags(raw: Optional[str]) -> List[str]:
    """Parse the gate_tags JSON snapshot into a list; tolerate null/garbage."""
    try:
        tags = json.loads(raw or "[]")
        return tags if isinstance(tags, list) else []
    except (ValueError, TypeError):
        return []


def _job_view(job: Job) -> dict:
    """A flat, JSON-serialisable view of a Job for the page. Read-only projection.

    Missing gate_reason degrades to None here; the front-end shows tags + a
    "(no reason)" fallback rather than crashing (plan §3 edge cases).
    """
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "description": job.description,
        "apply_link": job.apply_link or job.job_url,
        "gate_reason": job.gate_reason,
        "gate_verdict": job.gate_verdict,
        "tags": _tags(job.gate_tags),
        # priority flag drives the surface_priority highlight (plan §3).
        "priority": job.gate_verdict == "surface_priority",
    }


def get_classified_jobs(session=None) -> Dict[str, List[dict]]:
    """Return {bucket: [job_view, ...]} for the four buckets, newest decision first.

    For each job that has >=1 Decision, its bucket is the human_label of its MOST
    RECENT decision (append-only table; re-deciding inserts a second row and the
    latest wins — plan §3). Jobs with no decision do not appear. All four bucket
    keys are always present; an untouched bucket is an empty list, not an error.
    """
    own_session = session is None
    session = session or get_session()
    try:
        # Newest decision first, with id as a deterministic tiebreak: decided_at
        # defaults to datetime.utcnow() and two decisions can share a second, so
        # decided_at alone is not a total order. Highest id = most recent insert.
        decisions = (
            session.query(Decision)
            .order_by(Decision.decided_at.desc(), Decision.id.desc())
            .all()
        )

        # First decision seen per job_id is its latest (query is newest-first).
        latest_label: Dict[str, str] = {}
        for d in decisions:
            if d.job_id not in latest_label:
                latest_label[d.job_id] = d.human_label

        buckets: Dict[str, List[dict]] = {b: [] for b in BUCKETS}
        job_ids = list(latest_label.keys())
        if not job_ids:
            return buckets

        jobs = {
            j.id: j
            for j in session.query(Job).filter(Job.id.in_(job_ids)).all()
        }

        # Preserve the newest-first order from `decisions` when filling buckets.
        for job_id in latest_label:  # dict preserves insertion (newest-first) order
            label = latest_label[job_id]
            job = jobs.get(job_id)
            if job is None or label not in buckets:
                # A decided job whose Job row was deleted, or a label outside the
                # four-way space, is skipped rather than crashing the whole view.
                continue
            buckets[label].append(_job_view(job))

        return buckets
    finally:
        if own_session:
            session.close()


def bucket_counts(classified: Dict[str, List[dict]]) -> Dict[str, int]:
    """Live per-bucket counts for the tab labels."""
    return {b: len(classified.get(b, [])) for b in BUCKETS}


def list_cvs(data_dir: Path = _DATA_DIR) -> List[str]:
    """The CV names offered by the picker: `data/cv_*.txt` stems, sorted.

    Returns e.g. ["cv_devops", "cv_frontend", "cv_fullstack"]. A missing data
    dir yields [] rather than raising, so the page degrades to an empty picker
    instead of 500-ing. `data_dir` is injectable for tests.
    """
    if not data_dir.is_dir():
        return []
    return sorted(p.stem for p in data_dir.glob("cv_*.txt"))
