"""The one new read: gated jobs, grouped into the browse buckets.

The gate (Qwen, run in the CLI/loop) writes an immutable three-valued verdict on
each Job row — `surface_priority` / `surface` / `skip`. THAT is the browse
bucket source, not the Decision table: Decision.human_label is the gate's
training signal (populated only when the *CLI* triages a job), so keying the web
buckets off it showed almost nothing. We read Job.gate_verdict directly:

    surface_priority -> APPLY      (lead with these)
    surface          -> CONSIDER
    skip             -> SKIP       (kept as an audit view: eyeball what Qwen
                                    buried, per the manual-apply workflow where
                                    a buried good job is the expensive error)

HOLD is NOT a gate verdict — it is a manual park action recorded in the
Application table (event='held'). A held job is pulled from its gate bucket and
shown under HOLD until restored.

Acted-on jobs (applied / dismissed / held) are FILTERED OUT of the three gate
buckets — the worklist shrinks as you work it — while their gate_verdict on the
Job row stays untouched (immutable ground truth). Applied and Dismissed render
from the Application table in their own tabs (server layer), not here.

Read-only: this module opens a session, queries, and returns plain dicts. It
never writes. The dicts are JSON-serialisable so the server can inject them
straight into the page.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

from shared.database.models import Job, get_session
from v2.webapp import applications

# The CVs the picker offers, read from disk rather than hardcoded, so the web
# code names no specific CV (calibration seam, plan §6). data/ sits at the repo
# root: queries.py is v2/webapp/queries.py, so three parents up is the root.
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# The browse buckets, in render order (Apply-first). HOLD comes from the
# Application table, not a gate verdict; SKIP is the audit view.
BUCKETS = ("APPLY", "CONSIDER", "HOLD", "SKIP")

# Gate verdict -> browse bucket. A verdict outside this map (or NULL, i.e. a job
# the gate hasn't scored yet) does not appear in any bucket.
_VERDICT_BUCKET = {
    "surface_priority": "APPLY",
    "surface": "CONSIDER",
    "skip": "SKIP",
}


def _tags(raw: Optional[str]) -> List[str]:
    """Parse the gate_tags JSON snapshot into a list; tolerate null/garbage."""
    try:
        tags = json.loads(raw or "[]")
        return tags if isinstance(tags, list) else []
    except (ValueError, TypeError):
        return []


def _job_view(job: Job, note: Optional[str] = None) -> dict:
    """A flat, JSON-serialisable view of a Job for the page. Read-only projection.

    Missing gate_reason degrades to None here; the front-end shows tags + a
    "(no reason)" fallback rather than crashing (plan §3 edge cases). `note`
    carries the latest Application note (e.g. the reason a job was held).
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
        "note": note,
        # priority flag drives the surface_priority highlight (plan §3).
        "priority": job.gate_verdict == "surface_priority",
    }


def get_classified_jobs(session=None) -> Dict[str, List[dict]]:
    """Return {bucket: [job_view, ...]} for the browse buckets.

    APPLY / CONSIDER / SKIP are read from Job.gate_verdict. Jobs the user has
    already applied to or dismissed are filtered out; a held job is pulled from
    its gate bucket and listed under HOLD instead. Jobs the gate hasn't scored
    (gate_verdict NULL) do not appear. All four bucket keys are always present;
    an empty bucket is [], not an error. Newest-scored first within a bucket.
    """
    own_session = session is None
    session = session or get_session()
    try:
        states = applications.latest_states(session)
        # held jobs -> HOLD bucket (with their note); applied/dismissed -> gone.
        held: Dict[str, str] = {
            jid: st.note for jid, st in states.items() if st.event == "held"
        }
        hidden = {
            jid for jid, st in states.items()
            if st.event in ("applied", "status_changed", "dismissed")
        }

        buckets: Dict[str, List[dict]] = {b: [] for b in BUCKETS}

        # Newest-scored first; gate_run_at NULL sorts last. gate_verdict NULL
        # (ungated) is filtered by the bucket lookup below.
        jobs = (
            session.query(Job)
            .filter(Job.gate_verdict.isnot(None))
            .order_by(Job.gate_run_at.desc().nullslast(), Job.id.desc())
            .all()
        )

        for job in jobs:
            if job.id in hidden:
                continue
            if job.id in held:
                buckets["HOLD"].append(_job_view(job, note=held[job.id]))
                continue
            bucket = _VERDICT_BUCKET.get(job.gate_verdict)
            if bucket is not None:
                buckets[bucket].append(_job_view(job))

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
