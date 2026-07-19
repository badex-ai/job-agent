"""Tests for the web-UI application-lifecycle logic (v2/webapp/applications.py)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database import models
from shared.database.models import Base, Job, Application
from v2.webapp import applications as app
from v2.webapp.queries import get_classified_jobs


def _in_a_bucket(session, job_id):
    """True if the job currently appears in one of the browse buckets. Replaces
    the old actioned_job_ids() helper (deleted): the behaviour under test is
    "does acting on a job remove it from the worklist", so assert it through the
    real consumer, get_classified_jobs."""
    buckets = get_classified_jobs(session=session)
    return any(j["id"] == job_id for v in buckets.values() for j in v)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'a.db'}", echo=False)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Job(id="j1", title="DevOps Engineer", company="Acme",
              gate_verdict="surface_priority"))
    s.commit()
    return s


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the module's OWN get_session() at a temp DB, so the sessionless
    (default) code path — the one the HTTP handler uses — is actually exercised."""
    engine = create_engine(f"sqlite:///{tmp_path / 'own.db'}", echo=False)
    Base.metadata.create_all(engine)
    Maker = sessionmaker(bind=engine)
    monkeypatch.setattr(models, "get_session", Maker)
    monkeypatch.setattr(app, "get_session", Maker)
    monkeypatch.setattr("shared.database.tracking.get_session", Maker)
    s = Maker()
    s.add(Job(id="j1", title="DevOps Engineer", company="Acme",
              gate_verdict="surface_priority"))
    s.commit()
    s.close()
    return Maker


def test_dismiss_hides_job_from_buckets(session):
    app.dismiss_job("j1", session=session)
    assert not _in_a_bucket(session, "j1")


def test_hold_hides_job_from_gate_bucket(session):
    """Hold pulls a job from its gate bucket into HOLD (still "in a bucket", but
    the HOLD one, not its verdict bucket)."""
    app.hold_job("j1", session=session)
    buckets = get_classified_jobs(session=session)
    assert [j["id"] for j in buckets["HOLD"]] == ["j1"]
    assert buckets["APPLY"] == []                    # left its verdict bucket


def test_restore_unhides_a_dismissed_job(session):
    app.dismiss_job("j1", session=session)
    app.restore_job("j1", session=session)          # latest event = restored
    assert _in_a_bucket(session, "j1")              # back in its bucket


def test_latest_row_wins(session):
    app.hold_job("j1", session=session)
    app.dismiss_job("j1", session=session)          # later row supersedes
    latest = app.latest_states(session)["j1"]
    assert latest.event == "dismissed"


def test_apply_returns_dict_row(session):
    row, err = app.apply_to_job("j1", session=session)
    assert err is None
    assert row["event"] == "applied"
    assert row["status"] == "applied"


def test_apply_dual_writes_job_applied_flag(session):
    # The Review-flagged invariant: Apply must mark_applied so the CLI's
    # double-apply guard (Job.applied) stays in sync with the web UI.
    app.apply_to_job("j1", session=session)
    job = session.query(Job).filter(Job.id == "j1").one()
    assert job.applied is True
    assert job.status == "applied"


def test_apply_is_atomic_no_orphan_log_on_failure(session):
    # Torn-write fix: applying a NON-existent job must leave NO 'applied' log row
    # (mark_applied fails -> the whole transaction rolls back).
    row, err = app.apply_to_job("ghost", session=session)
    assert row is None and err is not None
    assert session.query(Application).filter(Application.job_id == "ghost").count() == 0
    assert not _in_a_bucket(session, "ghost")


def test_dismiss_never_mutates_job_status(session):
    # Dismiss is a reversible view preference — it must NOT touch Job.status
    # (that would silently drain the CLI rescue queue).
    before = session.query(Job).filter(Job.id == "j1").one().status
    app.dismiss_job("j1", session=session)
    after = session.query(Job).filter(Job.id == "j1").one().status
    assert after == before  # untouched


def test_set_status_advances_lifecycle(session):
    app.apply_to_job("j1", session=session)
    app.set_status("j1", "interview", session=session)
    assert app.latest_states(session)["j1"].status == "interview"


def test_set_status_rejects_unknown_value(session):
    with pytest.raises(ValueError):
        app.set_status("j1", "hired_immediately", session=session)


def test_set_status_on_unapplied_job_raises(session):
    # Guard: no status change on a job that was never applied to (phantom entry).
    with pytest.raises(ValueError):
        app.set_status("j1", "interview", session=session)


def test_restore_on_applied_job_raises(session):
    # Guard: restore is only for dismissed/held jobs; restoring an applied job
    # is incoherent (it would drop out of the Applied tab while Job.applied=True).
    app.apply_to_job("j1", session=session)
    with pytest.raises(ValueError):
        app.restore_job("j1", session=session)


def test_dismiss_on_applied_job_raises(session):
    # A2: dismissing an applied job would strand the application (leaves the
    # Applied tab while Job.applied stays True, unreachable via Restore).
    app.apply_to_job("j1", session=session)
    with pytest.raises(ValueError):
        app.dismiss_job("j1", session=session)


def test_hold_on_applied_job_raises(session):
    app.apply_to_job("j1", session=session)
    with pytest.raises(ValueError):
        app.hold_job("j1", session=session)


def test_dismiss_on_cli_applied_job_raises(session):
    # A2, CLI seam: a job applied to via the CLI has Job.applied=True but NO
    # Application row. Dismiss must still refuse it (the _single_write guard
    # checks Job.applied, not just the log).
    session.query(Job).filter(Job.id == "j1").update({"applied": True})
    session.commit()
    with pytest.raises(ValueError):
        app.dismiss_job("j1", session=session)


def test_dismiss_on_fresh_job_still_works(session):
    # The guard must not over-fire: a never-touched job dismisses normally.
    row = app.dismiss_job("j1", session=session)
    assert row["event"] == "dismissed"


def test_status_change_not_counted_as_new_application(session):
    # 'status_changed' is a distinct event so lifecycle edits never inflate the
    # count of real applications.
    app.apply_to_job("j1", session=session)
    app.set_status("j1", "interview", session=session)
    app.set_status("j1", "offer", session=session)
    applied_rows = (session.query(Application)
                    .filter(Application.job_id == "j1",
                            Application.event == "applied").count())
    assert applied_rows == 1  # exactly one real application, not three


def test_dismiss_does_not_touch_gate_verdict(session):
    # The gate verdict is immutable ground truth; an action never rewrites it.
    app.dismiss_job("j1", session=session)
    job = session.query(Job).filter(Job.id == "j1").one()
    assert job.gate_verdict == "surface_priority"


# --- the sessionless (default) code path the HTTP handler will use ------------

def test_dismiss_sessionless_returns_serialisable_dict(db):
    # Regression for the DetachedInstanceError: the default path must return a
    # plain dict usable AFTER its own session closed.
    row = app.dismiss_job("j1")
    assert row["job_id"] == "j1" and row["event"] == "dismissed"


def test_apply_sessionless_dual_writes(db):
    row, err = app.apply_to_job("j1")
    assert err is None and row["event"] == "applied"
    s = db()
    assert s.query(Job).filter(Job.id == "j1").one().applied is True
    s.close()


# --- B2: record which CV was sent ---------------------------------------------

def test_apply_records_the_cv(session):
    # The core B2 datum: the CV sent on apply lands on the append-only row.
    row, err = app.apply_to_job("j1", cv="cv_devops", session=session)
    assert err is None
    assert row["cv"] == "cv_devops"
    assert app.latest_states(session)["j1"].cv == "cv_devops"


def test_apply_with_no_cv_stores_null(session):
    # Apply must proceed with cv=NULL when no CV was picked (list_cvs()==[]).
    row, err = app.apply_to_job("j1", session=session)
    assert err is None
    assert row["cv"] is None


def test_status_change_carries_cv_forward(session):
    # The training datum must survive a status change — set_status re-appends
    # with the latest row's cv, not None.
    app.apply_to_job("j1", cv="cv_devops", session=session)
    app.set_status("j1", "interview", session=session)
    assert app.latest_states(session)["j1"].cv == "cv_devops"


def test_status_change_no_longer_wipes_the_note(session):
    # Review-flagged bug fixed: set_status used to re-append with note=None,
    # silently erasing the displayed note on the first lifecycle change.
    app.apply_to_job("j1", note="sent Tuesday", cv="cv_devops", session=session)
    app.set_status("j1", "interview", session=session)
    latest = app.latest_states(session)["j1"]
    assert latest.note == "sent Tuesday"   # carried, not wiped
    assert latest.cv == "cv_devops"


def test_set_note_carries_cv_forward(session):
    app.apply_to_job("j1", cv="cv_frontend", session=session)
    app.set_note("j1", "recruiter emailed", session=session)
    assert app.latest_states(session)["j1"].cv == "cv_frontend"


def test_set_cv_corrects_the_recorded_cv(session):
    app.apply_to_job("j1", cv="cv_devops", session=session)
    row = app.set_cv("j1", "cv_fullstack", session=session)
    assert row["cv"] == "cv_fullstack"
    latest = app.latest_states(session)["j1"]
    assert latest.cv == "cv_fullstack"
    assert latest.event == "applied"       # placement unchanged (not a move)


def test_set_cv_carries_status_and_note_forward(session):
    app.apply_to_job("j1", note="note A", cv="cv_devops", session=session)
    app.set_status("j1", "interview", session=session)
    app.set_cv("j1", "cv_frontend", session=session)
    latest = app.latest_states(session)["j1"]
    assert latest.status == "interview"    # status carried forward
    assert latest.note == "note A"         # note carried forward
    assert latest.cv == "cv_frontend"


def test_set_cv_empty_clears_it(session):
    app.apply_to_job("j1", cv="cv_devops", session=session)
    app.set_cv("j1", "   ", session=session)   # whitespace -> NULL
    assert app.latest_states(session)["j1"].cv is None


def test_set_cv_on_unapplied_job_raises(session):
    # cv is meaningless on a job that was never applied to.
    with pytest.raises(ValueError):
        app.set_cv("j1", "cv_devops", session=session)


def test_set_cv_on_dismissed_job_raises(session):
    app.dismiss_job("j1", session=session)
    with pytest.raises(ValueError):
        app.set_cv("j1", "cv_devops", session=session)
