"""Application-lifecycle logic for the web UI's Apply / Hold / Dismiss actions.

The web UI is otherwise read-only; this is the ONE write seam. It layers an
append-only `Application` log over the immutable gate verdict — a job's current
UI state is the LATEST application row for it (by id), never a mutation of the
Job row's gate_* columns.

The one asymmetry: APPLY dual-writes. It appends an `applied`
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

# --- event-meaning sets: the single source of truth for "what does a job's
# latest event mean". queries.py and the /api/applications read import these
# rather than re-listing the literals, so the taxonomy lives in exactly one
# place (a stray literal in two modules is how the buckets and the tables drift
# apart). Every non-'restored' event puts a job somewhere; 'restored' returns it
# to its gate bucket, so it is deliberately in none of these sets.
APPLIED_EVENTS = {"applied", "status_changed"}   # -> Applied tab
DISMISSED_EVENTS = {"dismissed"}                 # -> Dismissed tab
HELD_EVENTS = {"held"}                           # -> HOLD browse bucket
# A job whose latest event is any of these is pulled out of the three gate
# browse buckets (the worklist shrinks as it is worked).
HIDDEN_FROM_BUCKETS = APPLIED_EVENTS | DISMISSED_EVENTS | HELD_EVENTS


def _as_dict(row: Application) -> dict:
    return {"id": row.id, "job_id": row.job_id, "event": row.event,
            "status": row.status, "note": row.note, "cv": row.cv}


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


def _latest_row(session, job_id: str) -> Optional[Application]:
    return (session.query(Application)
            .filter(Application.job_id == job_id)
            .order_by(Application.id.desc())
            .first())


def _latest_event(session, job_id: str) -> Optional[str]:
    row = _latest_row(session, job_id)
    return row.event if row else None


def _append(session, job_id: str, event: str, status: Optional[str],
            note: Optional[str], cv: Optional[str] = None) -> Application:
    """Stage one append-only row. FLUSHes (assigns the id) but does NOT commit —
    the caller owns the single commit so multi-write actions stay atomic.

    `cv` records which CV was sent (only meaningful on the applied lifecycle);
    re-append sites carry the latest row's cv forward so an edit never drops it.
    """
    row = Application(job_id=job_id, event=event, status=status, note=note,
                      cv=cv)
    session.add(row)
    session.flush()
    return row


def apply_to_job(job_id: str, note: Optional[str] = None,
                 cv: Optional[str] = None, session=None):
    """Record an application: append an `applied` row AND mark_applied, atomically.

    Both writes share this session and ONE commit; on any failure both roll back,
    so the log never claims 'applied' while Job.applied stays False. `cv` is the
    CV that was sent (e.g. 'cv_devops'), captured here because the (job, cv,
    outcome) triple is unrecoverable after the fact; None when no CV was picked.
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
        row = _append(session, job_id, "applied", "applied", note, cv)
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
        if latest not in DISMISSED_EVENTS | HELD_EVENTS:
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
        latest = _latest_row(session, job_id)
        if not latest or latest.event not in APPLIED_EVENTS:
            raise ValueError(
                f"cannot set status on {job_id!r}: it has not been applied to")
        # Carry the current note AND cv forward. Appending with note=None here
        # would silently wipe the displayed note (set_note re-reads the latest
        # row), and dropping cv would destroy the training datum on the first
        # status change — so both ride along with the new status.
        row = _append(session, job_id, "status_changed", status,
                      latest.note, latest.cv)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()


def set_note(job_id: str, note: Optional[str], session=None) -> dict:
    """Edit the note on an applied/dismissed/held job — without moving it.

    A note is not a placement change, but the tab a job sits in is decided by its
    LATEST event, so we cannot append a bare 'note' event (it would orphan the
    row out of applied/dismissed). Instead we RE-APPEND the job's current
    placement event with the new note, carrying its current status forward. The
    latest-event invariant is preserved and the note rides along. Empty/blank
    text stores NULL (clears the note). Legal only on a job that is currently
    placed (applied/status_changed/dismissed/held); a 'restored' or never-touched
    job has no note to edit (raises).
    """
    clean = note.strip() if isinstance(note, str) and note.strip() else None
    own = session is None
    session = session or get_session()
    try:
        latest = _latest_row(session, job_id)
        event = latest.event if latest else None
        if event not in HIDDEN_FROM_BUCKETS:
            raise ValueError(
                f"cannot set a note on {job_id!r}: its latest event is "
                f"{event!r}, which is not a job you can annotate")
        # carry the current status AND cv forward (both only meaningful for
        # applied rows) so editing the note never drops which CV was sent.
        row = _append(session, job_id, event, latest.status, clean, latest.cv)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()


def set_cv(job_id: str, cv: Optional[str], session=None) -> dict:
    """Correct which CV an applied job records — without moving it.

    Same re-append mechanism as set_note: the tab a job sits in is decided by its
    latest event, so we re-append the current placement event with the new cv,
    carrying its status and note forward. Legal only on an APPLIED job (cv is
    meaningless on a dismissed/held row). Empty/blank cv stores NULL. This is the
    click-to-edit fix for a wrong header CV recorded on the Applied table.
    """
    clean = cv.strip() if isinstance(cv, str) and cv.strip() else None
    own = session is None
    session = session or get_session()
    try:
        latest = _latest_row(session, job_id)
        event = latest.event if latest else None
        if event not in APPLIED_EVENTS:
            raise ValueError(
                f"cannot set a cv on {job_id!r}: its latest event is "
                f"{event!r}, which is not an applied job")
        row = _append(session, job_id, event, latest.status, latest.note, clean)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()


def _single_write(job_id: str, event: str, status: Optional[str],
                  note: Optional[str], session) -> dict:
    """Shared body for the single-row writers (dismiss/hold): append + commit.

    Refuses to act on an APPLIED job. Dismiss/hold have no state guard by
    default, unlike restore/set_status/set_note; without this, dismissing an
    applied job (e.g. from a stale tab still showing its card) would make
    'dismissed' the latest event and strand the application — it leaves the
    Applied table while Job.applied stays True, and Restore only sends it back to
    a gate bucket where Apply 409s. An applied lifecycle ends with a set_status
    (rejected/ghosted), never a dismiss. Both the Application log AND Job.applied
    are checked, since a CLI-applied job has the flag but no log row.
    """
    own = session is None
    session = session or get_session()
    try:
        if _latest_event(session, job_id) in APPLIED_EVENTS or \
                session.query(Job.applied).filter(Job.id == job_id).scalar():
            raise ValueError(
                f"cannot {event} {job_id!r}: it has already been applied to; "
                "change its status instead")
        row = _append(session, job_id, event, status, note)
        session.commit()
        return _as_dict(row)
    finally:
        if own:
            session.close()
