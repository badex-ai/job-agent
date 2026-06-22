"""Tests for record_decision() — the single headless seam (SPEC §12 step 2).

These pin the contract the whole CLI depends on: every triage action snapshots
the gate verdict onto an immutable Decision row AND advances Job.status per §6,
in one transaction. Run against an isolated temp DB; data/jobs.db is untouched.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job, Decision, migrate_decisions
from shared.database.decisions import record_decision, _LABEL_TO_STATUS


@pytest.fixture
def Session(tmp_path):
    """A sessionmaker bound to a throwaway DB with both tables created."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)          # creates jobs + decisions
    migrate_decisions(engine)
    return sessionmaker(bind=engine)


def _seed_job(session, **overrides):
    base = dict(
        id="job-1",
        title="Senior Platform Engineer",
        company="Acme",
        status="new",
        gate_verdict="skip",
        gate_reason="S2: primary stack is Java",
        gate_tags='["S2"]',
        gate_model="qwen2.5:14b",
    )
    base.update(overrides)
    job = Job(**base)
    session.add(job)
    session.commit()
    return job


# --- the four-way -> status map (SPEC §6) ----------------------------------

@pytest.mark.parametrize("label,expected_status", [
    ("APPLY", "applied"),
    ("CONSIDER", "reviewed"),
    ("HOLD", "reviewed"),
    ("SKIP", "ignored"),
])
def test_status_map(Session, label, expected_status):
    s = Session()
    job = _seed_job(s)
    ok, err = record_decision(job, label, session=s)
    assert ok and err is None
    refreshed = s.query(Job).filter(Job.id == "job-1").one()
    assert refreshed.status == expected_status
    # the Decision keeps the precise label even when status collapses it:
    d = s.query(Decision).one()
    assert d.human_label == label
    s.close()


def test_consider_and_hold_share_status_but_differ_in_decision(Session):
    """The §6 subtlety: CONSIDER and HOLD both map to 'reviewed' for the queue,
    but must stay DISTINCT in the training signal."""
    assert _LABEL_TO_STATUS["CONSIDER"] == _LABEL_TO_STATUS["HOLD"] == "reviewed"
    s = Session()
    _seed_job(s, id="j-consider")
    _seed_job(s, id="j-hold")
    record_decision(s.get(Job, "j-consider"), "CONSIDER", session=s)
    record_decision(s.get(Job, "j-hold"), "HOLD", session=s)
    labels = {d.job_id: d.human_label for d in s.query(Decision).all()}
    assert labels == {"j-consider": "CONSIDER", "j-hold": "HOLD"}
    s.close()


# --- the gate snapshot is copied off the Job (SPEC §4a) --------------------

def test_snapshots_gate_verdict_off_the_job(Session):
    s = Session()
    job = _seed_job(s, gate_verdict="surface_priority", gate_reason="strong infra fit",
                    gate_tags='["S6"]', gate_model="qwen2.5:14b")
    record_decision(job, "APPLY", rule=None, note="good one", source_view="queue", session=s)
    d = s.query(Decision).one()
    assert d.gate_verdict == "surface_priority"
    assert d.gate_reason == "strong infra fit"
    assert d.gate_tags == '["S6"]'
    assert d.gate_model == "qwen2.5:14b"
    assert d.note == "good one"
    assert d.source_view == "queue"
    s.close()


def test_snapshot_is_frozen_against_later_regate(Session):
    """After a decision is recorded, re-gating the Job to a different verdict must
    NOT change the historical Decision row — the snapshot is frozen (SPEC §4a)."""
    s = Session()
    job = _seed_job(s, gate_verdict="skip")
    record_decision(job, "APPLY", source_view="rescue", session=s)

    # later the gate re-scores the same job differently
    db_job = s.get(Job, "job-1")
    db_job.gate_verdict = "surface"
    s.commit()

    d = s.query(Decision).one()
    assert d.gate_verdict == "skip", "the historical label's snapshot must not move"
    s.close()


# --- append-only through the seam ------------------------------------------

def test_redecide_appends_through_seam(Session):
    s = Session()
    job = _seed_job(s)
    record_decision(job, "SKIP", source_view="queue", session=s)
    record_decision(s.get(Job, "job-1"), "APPLY", source_view="rescue", session=s)
    rows = s.query(Decision).order_by(Decision.id.asc()).all()
    assert [r.human_label for r in rows] == ["SKIP", "APPLY"]
    # final status reflects the latest decision
    assert s.get(Job, "job-1").status == "applied"
    s.close()


# --- validation -------------------------------------------------------------

def test_rejects_bad_label(Session):
    s = Session()
    job = _seed_job(s)
    ok, err = record_decision(job, "MAYBE", session=s)
    assert not ok and "Invalid human_label" in err
    assert s.query(Decision).count() == 0      # nothing written on reject
    s.close()


def test_rejects_bad_source_view(Session):
    s = Session()
    job = _seed_job(s)
    ok, err = record_decision(job, "APPLY", source_view="archive", session=s)
    assert not ok and "Invalid source_view" in err
    assert s.query(Decision).count() == 0
    s.close()


def test_missing_job_row_is_reported(Session):
    s = Session()
    # a Job instance whose row was never persisted
    ghost = Job(id="ghost", status="new")
    ok, err = record_decision(ghost, "SKIP", session=s)
    assert not ok and "not found" in err
    s.close()


# --- the own-session path (no session injected) ----------------------------

def test_opens_its_own_session_when_none_given(tmp_path, monkeypatch):
    """When called without a session, record_decision opens/commits/closes its
    own — the path the headless CLI will actually hit when it doesn't manage a tx."""
    engine = create_engine(f"sqlite:///{tmp_path / 'own.db'}", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    # point the module's get_session at this throwaway engine
    monkeypatch.setattr("shared.database.decisions.get_session", SessionLocal)

    s = SessionLocal()
    s.add(Job(id="job-own", status="new", gate_verdict="surface", gate_tags="[]"))
    s.commit()
    s.close()

    ok, err = record_decision(Job(id="job-own"), "CONSIDER")  # no session arg
    assert ok and err is None

    verify = SessionLocal()
    assert verify.query(Decision).count() == 1
    assert verify.get(Job, "job-own").status == "reviewed"
    verify.close()
