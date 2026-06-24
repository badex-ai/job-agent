"""Retry semantics for gate_pending (the M4-down fix).

The invariant: NULL gate_verdict IS the retry queue. A transport failure means
the M4 is down (down for every job, not one), so the cycle stops fast and leaves
the remaining jobs NULL — the next cycle re-queries them automatically. No
per-job attempt counter: the only non-NULL-leaving failure is a host outage, and
that is surfaced once, at the host level, via summary['m4_down'] (-> the buzz).

A *malformed model response* is a different animal: gate_one catches it and
surfaces the job (source='error'), so it never raises OSError here and never
trips m4_down. These tests pin both paths apart.
"""
import urllib.error

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job
import v2.loop as loop


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(session, *ids):
    for i in ids:
        session.add(Job(id=i, title="Platform Engineer", company="Acme",
                        location="Remote", description="Kubernetes, Terraform.",
                        gate_verdict=None))
    session.commit()


# --- a live verdict: a job with no verdict gets one, NULL drains ------------

def test_pending_gates_null_jobs(session, monkeypatch):
    """The happy path: NULL jobs receive a verdict and leave the retry queue."""
    monkeypatch.setattr(loop, "gate_one", lambda *a, **k: {
        "gate_verdict": "surface", "gate_reason": "ok", "gate_model": "m",
        "gate_tags": "[]", "gate_confidence": 50, "gate_source": "qwen",
        "gate_run_at": None, "surfaced": True})
    _seed(session, "j1", "j2")
    summary = loop.gate_pending(session, offline=False, system_prompt="x")
    assert summary["gated"] == 2 and summary["m4_down"] is False
    assert session.query(Job).filter(Job.gate_verdict.is_(None)).count() == 0


# --- M4 down: cycle stops, jobs stay NULL (retried next time) ---------------

def test_transport_error_stops_cycle_and_flags_m4_down(session, monkeypatch):
    """A transport OSError = M4 down. The cycle breaks immediately, flags
    m4_down, and the job that hit it is LEFT NULL so the next cycle retries it.
    No per-job state, no permanent drop."""
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(loop, "gate_one", boom)
    _seed(session, "j1", "j2", "j3")

    summary = loop.gate_pending(session, offline=False, system_prompt="x")

    assert summary["m4_down"] is True
    assert summary["gated"] == 0
    # every job is still NULL -> all three are retried next cycle
    assert session.query(Job).filter(Job.gate_verdict.is_(None)).count() == 3


def test_m4_down_after_partial_progress_keeps_gated_work(session, monkeypatch):
    """If the M4 dies mid-batch, jobs gated BEFORE the failure are committed and
    keep their verdict; only the rest stay NULL for retry. Per-job commit (the
    existing line) plus fail-fast gives crash-safe partial progress."""
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"gate_verdict": "surface", "gate_reason": "ok",
                    "gate_model": "m", "gate_tags": "[]", "gate_confidence": 50,
                    "gate_source": "qwen", "gate_run_at": None, "surfaced": True}
        raise TimeoutError("read timed out")        # TimeoutError IS an OSError
    monkeypatch.setattr(loop, "gate_one", flaky)
    _seed(session, "j1", "j2", "j3")

    summary = loop.gate_pending(session, offline=False, system_prompt="x")

    assert summary["m4_down"] is True and summary["gated"] == 1
    gated = session.query(Job).filter(Job.gate_verdict.isnot(None)).count()
    nulls = session.query(Job).filter(Job.gate_verdict.is_(None)).count()
    assert gated == 1 and nulls == 2          # 1 kept, 2 left for retry


# --- a malformed model response must NOT look like an outage ----------------

def test_model_error_does_not_trip_m4_down(session, monkeypatch):
    """gate_one surfaces a malformed model response (source='error') and returns
    normally — it never raises OSError, so it must NOT set m4_down and must NOT
    stop the cycle. (This is what separates 'one bad job' from 'host down'.)"""
    monkeypatch.setattr(loop, "gate_one", lambda *a, **k: {
        "gate_verdict": "surface", "gate_reason": "gate error, surfaced",
        "gate_model": "m", "gate_tags": '["error"]', "gate_confidence": 0,
        "gate_source": "error", "gate_run_at": None, "surfaced": True})
    _seed(session, "j1", "j2")

    summary = loop.gate_pending(session, offline=False, system_prompt="x")

    assert summary["m4_down"] is False        # not an outage
    assert summary["gated"] == 2              # cycle ran to completion
    assert session.query(Job).filter(Job.gate_verdict.is_(None)).count() == 0
