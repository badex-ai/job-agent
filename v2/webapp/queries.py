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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import func

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
        # The event-meaning sets are imported from applications (single source of
        # truth); a job is in exactly one of held / hidden / (still in a bucket).
        held: Dict[str, str] = {
            jid: st.note for jid, st in states.items()
            if st.event in applications.HELD_EVENTS
        }
        hidden = {
            jid for jid, st in states.items()
            if st.event in applications.HIDDEN_FROM_BUCKETS
            and st.event not in applications.HELD_EVENTS
        }

        buckets: Dict[str, List[dict]] = {b: [] for b in BUCKETS}

        # Newest-scored first; gate_run_at NULL sorts last. gate_verdict NULL
        # (ungated) is filtered by the bucket lookup below.
        #
        # Job.applied is excluded at the SQL level: a job applied to via the CLI
        # (`a` in the queue) sets Job.applied but writes NO Application row, so
        # the Application-log filter above cannot see it. Without this it would
        # linger as a zombie card whose Apply button 409s forever ("already
        # applied"). Applied == out of the worklist, full stop — this also keeps
        # an applied job out of every bucket including HOLD.
        jobs = (
            session.query(Job)
            .filter(Job.gate_verdict.isnot(None))
            .filter((Job.applied.is_(None)) | (Job.applied.is_(False)))
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


# A gate run older than this (or none at all) marks the pipeline stale. The
# loop is scheduled hourly (shared.config.SCRAPE_INTERVAL_MINUTES), so 24h means
# ~24 cycles have failed to gate anything — a genuine stall, not one slow batch.
STALE_AFTER = timedelta(hours=24)


def gate_health(session=None, *, now: Optional[datetime] = None,
                stale_after: timedelta = STALE_AFTER) -> dict:
    """Pipeline liveness for the header banner: how many jobs await the gate,
    and when it last ran. Read-only, one small aggregate query.

    The webapp filters gate_verdict-NULL (ungated) rows out of every bucket, so
    a backlog of ungated jobs is otherwise INVISIBLE — the page looks quiet when
    the gate is dead (e.g. M4 unreachable, 956/986 rows NULL). This surfaces it.

    `pending` counts jobs the gate still owes a verdict, EXCLUDING ones already
    applied to (a CLI-applied job sets Job.applied but never gets gated; it is
    out of the worklist, not "waiting"). `last_gate_at` is the freshest
    gate_run_at across all rows (None if nothing has ever been gated). `stale`
    is True when nothing has been gated, or the last run is older than
    `stale_after`. `now`/`stale_after` are injectable for deterministic tests.
    """
    own_session = session is None
    session = session or get_session()
    try:
        now = now or datetime.utcnow()
        pending = (
            session.query(func.count(Job.id))
            .filter(Job.gate_verdict.is_(None))
            .filter((Job.applied.is_(None)) | (Job.applied.is_(False)))
            .scalar()
        ) or 0
        total = session.query(func.count(Job.id)).scalar() or 0
        last = session.query(func.max(Job.gate_run_at)).scalar()
        stale = last is None or (now - last) > stale_after
        return {
            "pending": pending,
            "total": total,
            "last_gate_at": last.isoformat() if last else None,
            "stale": stale,
        }
    finally:
        if own_session:
            session.close()


def _application_row(job: Optional[Job], state) -> dict:
    """A flat row for the Applied/Dismissed tables. `job` may be None if the
    Application log references a job no longer in the jobs table (defensive: a
    scrape re-run could drop a row) — the table still shows the id + note rather
    than crashing or hiding a tracked action."""
    return {
        "id": state.job_id,
        "title": job.title if job else state.job_id,
        "company": job.company if job else None,
        "apply_link": (job.apply_link or job.job_url) if job else None,
        "status": state.status,   # current lifecycle status (Applied table)
        "note": state.note,
        "cv": state.cv,           # which CV was sent (editable in the Applied table)
        # applied_at lives on the Job row (mark_applied set it in the dual-write),
        # not on the Application log; expose it as an ISO date for the Date column.
        "applied_at": (job.applied_at.date().isoformat()
                       if job and job.applied_at else None),
    }


def get_applications(session=None) -> Dict[str, List[dict]]:
    """Return {"applied": [...], "dismissed": [...]} for the tracking tables.

    Sourced from the Application log (latest row per job), NOT the gate verdict:
      applied   = latest event in APPLIED_EVENTS (applied / status_changed),
                  carrying the current lifecycle status + applied date.
      dismissed = latest event is 'dismissed' (reversible via Restore).
    Held jobs are NOT here — they render in the HOLD browse bucket. Read-only;
    returns plain JSON-serialisable dicts. Newest action first (highest id).
    """
    own_session = session is None
    session = session or get_session()
    try:
        states = applications.latest_states(session)
        if not states:
            return {"applied": [], "dismissed": []}

        # One batched Job lookup for every tracked job, rather than per-row.
        jobs = {
            j.id: j for j in
            session.query(Job).filter(Job.id.in_(states.keys())).all()
        }

        applied, dismissed = [], []
        for jid, st in states.items():
            row = _application_row(jobs.get(jid), st)
            if st.event in applications.APPLIED_EVENTS:
                applied.append((st.id, row))
            elif st.event in applications.DISMISSED_EVENTS:
                dismissed.append((st.id, row))

        # Newest action first: sort by the Application row id (append order).
        applied.sort(key=lambda p: p[0], reverse=True)
        dismissed.sort(key=lambda p: p[0], reverse=True)
        return {
            "applied": [r for _, r in applied],
            "dismissed": [r for _, r in dismissed],
        }
    finally:
        if own_session:
            session.close()


def list_cvs(data_dir: Path = _DATA_DIR) -> List[str]:
    """The CV names offered by the picker: `data/cv_*.txt` stems, sorted.

    Returns e.g. ["cv_devops", "cv_frontend", "cv_fullstack"]. A missing data
    dir yields [] rather than raising, so the page degrades to an empty picker
    instead of 500-ing. `data_dir` is injectable for tests.
    """
    if not data_dir.is_dir():
        return []
    return sorted(p.stem for p in data_dir.glob("cv_*.txt"))


def cv_text(name: str, data_dir: Path = _DATA_DIR) -> Optional[str]:
    """The raw text of the CV file `<name>.txt`, or None if it isn't a known CV.

    `name` is validated against list_cvs() — an exact-stem membership check — so
    a caller-supplied string can never escape the data dir (no '..', no absolute
    path, no arbitrary file): only a real cv_*.txt stem resolves. Returns the
    file's decoded text; None when the name is unknown or the file is unreadable.
    """
    if name not in list_cvs(data_dir):
        return None
    try:
        return (data_dir / f"{name}.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # OSError: gone/unreadable since the list_cvs() check (TOCTOU).
        # UnicodeDecodeError: a non-UTF-8 CV file — a 404 is truer than letting
        # it escape as an unhandled 500, and matches the "unreadable -> None"
        # contract above.
        return None
