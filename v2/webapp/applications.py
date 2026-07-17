"""Application-lifecycle logic for the web UI's Apply / Hold / Dismiss actions.

The web UI is otherwise read-only; this is the ONE write seam. It layers an
append-only `Application` log over the immutable gate verdict — a job's current
UI state is the LATEST application row for it (by id), never a mutation of the
Job row's gate_* columns.

The one asymmetry (reviewed): APPLY dual-writes. It appends an `applied`
row AND calls the canonical `tracking.mark_applied`, because the CLI's
double-apply guard and stats read Job.applied / Job.status — skipping that would
fork "did I apply?" between the CLI and the web UI. The two writes share ONE
session and ONE commit (see apply_to_job) so they are atomic: a failure in
either rolls back both, and the log can never claim 'applied' while Job.applied
stays False. Hold / Dismiss / Restore / status-change / note only ever touch
this table; they never mutate Job.status.

INTENDED cross-tool behaviour (declared, not a bug): Dismiss and Hold do NOT set
Job.status, so a web-dismissed/held job STILL appears in the *CLI* queue/rescue
views (which filter Job.status=='new'). That is deliberate — dismiss/hold are a
reversible web-view preference, and touching Job.status would silently drain the
CLI rescue queue whose bias defence depends on status=='new'.

Writers return a plain dict (not a live ORM row): the commit expires ORM
attributes, so returning the object would DetachedInstanceError once the session
closes. A web seam serialises dicts anyway.
"""
from typing import Dict, List, Optional

from shared.database.models import Application, Job, get_session
from shared.database import tracking

# The post-apply lifecycle the Applied table cycles through (UI clickable cell).
STATUSES = ("applied", "interview", "offer", "rejected", "ghosted")
# The actions the UI can take on a job. 'status_changed' is distinct from
# 'applied' so a lifecycle edit is never miscounted as a fresh application.
EVENTS = ("applied", "status_changed", "dismissed", "held", "restored")
# An action that removes a job from its gate bucket (the worklist shrinks). A
# 'restored' event returns it, so it is deliberately NOT in this set.
_ACTIVE_EVENTS = {"applied", "status_changed", "dismissed", "held"}
# Events that place a job in the Applied tab (it has been applied to).
_APPLIED_EVENTS = {"applied", "status_changed"}


def _as_dict(row: Application) -> dict:
    return {"id": row.id, "job_id": row.job_id, "event": row.event,
            "status": row.status, "note": row.note}


def latest_states(session=None) -> Dict[str, Application]:
    """Map job_id -> its most recent Application row (highest id wins).

    The append-only table means the latest row per job is its live state; a
    'restored' row supersedes an earlier 'dismissed'/'held' and un-hides the job.
    Read-only (no commit), so the returned rows stay attribute-loaded.
    """
    own = session is None
    session = session or get_session()
    try:
        latest: Dict[str, Application] = {}
        # ascending id: later rows overwrite earlier, so the last write per job
        # left in the dict is the highest-id (most recent) row.
        for a in session.query(Application).order_by(Application.id.asc()).all():
            latest[a.job_id] = a
        return latest
    finally:
        if own:
            session.close()


def actioned_job_ids(session=None) -> set:
    """Job ids whose latest action removes them from the browse buckets."""
    return {jid for jid, a in latest_states(session).items()
            if a.event in _ACTIVE_EVENTS}


def _latest_event(session, job_id: str) -> Optional[str]:
    row = (session.query(Application)
           .filter(Application.job_id == job_id)
           .order_by(Application.id.desc())
           .first())
    return row.event if row else None


def _append(session, job_id: str, event: str, status: Optional[str],
            note: Optional[str]) -> Application:
    """Stage one append-only row. FLUSHes (assigns the id) but does NOT commit —
    the caller owns the single commit so multi-write actions stay atomic."""
    row = Application(job_id=job_id, event=event, status=status, note=note)
    session.add(row)
    session.flush()
    return row


def apply_to_job(job_id: str, note: Optional[str] = None, session=None):
    """Record an application: append an `applied` row AND mark_applied, atomically.

    Both writes share this session and ONE commit; on any failure both roll back,
    so the log never claims 'applied' while Job.applied stays False.
    Returns (dict|None, error).
    """
    own = session is None
    session = session or get_session()
    try:
        # Double-apply guard. The CLI has its own pre-check (cli.py), but the web
        # path never runs it, so without this a second Apply (double-click, stale
        # tab) would append a duplicate 'applied' row AND — via mark_applied —
        # re-zero applied_at / followup_count, silently restarting the follow-up
        # schedule for a job applied days ago. Refuse instead.
        if session.query(Job.applied).filter(Job.id == job_id).scalar():
            return None, f"{job_id} is already applied"
        row = _append(session, job_id, "applied", "applied", note)
        ok, err = tracking.mark_applied(job_id, via="website", session=session)
        if not ok:
            session.rollback()
            return None, err
        session.commit()
        return _as_dict(row), None
    finally:
        if own:
            session.close()


def dismiss_job(job_id: str, note: Optional[str] = None, session=None) -> dict:
    """Park a job in the Dismissed tab (reversible). Applications-table only."""
    return _single_write(job_id, "dismissed", None, note, session)


def hold_job(job_id: str, note: Optional[str] = None, session=None) -> dict:
    """Move a job to the manual Hold bucket. Applications-table only."""
    return _single_write(job_id, "held", None, note, session)


def restore_job(job_id: str, session=None) -> dict:
    """Un-dismiss / un-hold: append a 'restored' row so the job returns to its
    gate bucket. Legal ONLY when the job is currently dismissed/held — restoring
    an applied job is incoherent (raises)."""
    own = session is None
    session = session or get_session()
    try:
        latest = _latest_event(session, job_id)
        if latest not in ("dismissed", "held"):
            raise ValueError(
                f"cannot restore {job_id!r}: latest event is {latest!r}, "
                "restore is only valid for a dismissed or held job")
        row = _append(session, job_id, "restored", None, None)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()


def set_status(job_id: str, status: str, session=None) -> dict:
    """Advance the post-apply lifecycle (Applied->Interview->Offer/Rejected/...).

    Appends a `status_changed` row carrying the new status. Legal ONLY once the
    job has been applied to — setting a status on a never-applied job would
    conjure a phantom Applied-tab entry (raises).
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    own = session is None
    session = session or get_session()
    try:
        if _latest_event(session, job_id) not in _APPLIED_EVENTS:
            raise ValueError(
                f"cannot set status on {job_id!r}: it has not been applied to")
        row = _append(session, job_id, "status_changed", status, None)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()


def _single_write(job_id: str, event: str, status: Optional[str],
                  note: Optional[str], session) -> dict:
    """Shared body for the single-row writers (dismiss/hold): append + commit."""
    own = session is None
    session = session or get_session()
    try:
        row = _append(session, job_id, event, status, note)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()
