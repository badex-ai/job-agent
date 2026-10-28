"""Tests for the loop's gate queue (v2/loop.py gate_pending).

A row whose description never made it into the DB used to reach the model and
crash the batch on `description[:MAX_DESCRIPTION_CHARS]` — 23 such rows sat in
the backlog, each one killing a run at an unpredictable point. The queue now
excludes them, and that exclusion is what these tests pin.

The assertion that matters is that a blank row is never SENT, not merely that
the run survives: a guard further down would also stop the crash, but it would
store a verdict resting on no text, indistinguishable in the DB from one the
model actually reasoned about. So the blank rows must come back untouched
(gate_verdict IS NULL), which only holds if the query skipped them.

Everything runs offline against a temp DB — no network, no M4.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job
from v2 import loop


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _job(session, jid, *, description):
    session.add(Job(
        id=jid, title=f"Role {jid}", company="Acme", location="Remote",
        description=description,
        job_url=f"https://linkedin.com/jobs/view/{jid}",
        scraped_at=datetime.utcnow(),
    ))
    session.commit()


def test_blank_descriptions_never_reach_the_gate(Session):
    session = Session()
    _job(session, "real", description="Senior Python engineer, remote, visa ok.")
    _job(session, "null", description=None)
    _job(session, "empty", description="")

    summary = loop.gate_pending(session, offline=True, cooldown_s=0,
                                 system_prompt="offline test prompt")

    assert summary["gated"] == 1
    by_id = {j.id: j for j in session.query(Job).all()}
    assert by_id["real"].gate_verdict is not None
    assert by_id["null"].gate_verdict is None
    assert by_id["empty"].gate_verdict is None


def test_blank_rows_stay_out_of_the_queue_on_every_pass(Session):
    """NULL is the retry queue, so a blank row must not be picked up later."""
    session = Session()
    _job(session, "null", description=None)
    _job(session, "empty", description="")

    first = loop.gate_pending(session, offline=True, cooldown_s=0,
                              system_prompt="offline test prompt")
    second = loop.gate_pending(session, offline=True, cooldown_s=0,
                               system_prompt="offline test prompt")

    assert first["gated"] == 0
    assert second["gated"] == 0
    assert all(j.gate_verdict is None for j in session.query(Job).all())


def test_a_repaired_description_becomes_gate_eligible(Session):
    """The exclusion keys off the description column, so repair_descriptions
    filling it is the whole path back — no separate un-exclude step."""
    session = Session()
    _job(session, "blank", description=None)

    summary = loop.gate_pending(session, offline=True, cooldown_s=0,
                                 system_prompt="offline test prompt")
    assert summary["gated"] == 0

    session.query(Job).filter(Job.id == "blank").one().description = (
        "Backend role, Django, remote within EU."
    )
    session.commit()

    summary = loop.gate_pending(session, offline=True, cooldown_s=0,
                                 system_prompt="offline test prompt")
    assert summary["gated"] == 1
    assert session.query(Job).filter(Job.id == "blank").one().gate_verdict is not None
