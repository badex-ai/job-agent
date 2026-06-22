"""Tests for the append-only `decisions` table (SPEC §4a / §12 step 1).

The contract these pin: a Decision row is an immutable snapshot of the gate's
verdict at decision time paired with the human's four-way label. Deciding on the
same job twice must INSERT a second row, never overwrite the first — the training
export reads this table as the source of truth, so a lost label is a lost signal.

All tests run against an isolated temp SQLite engine; the real data/jobs.db is
never touched. migrate_decisions(engine=...) mirrors migrate_gate_columns's
explicit-engine seam so this isolation is possible.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Decision, migrate_decisions


@pytest.fixture
def session(tmp_path):
    """A session bound to a throwaway file-backed SQLite DB (not data/jobs.db)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    migrate_decisions(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _decision(**overrides):
    base = dict(
        job_id="job-123",
        decided_at=datetime(2026, 6, 22, 10, 0, 0),
        gate_verdict="skip",
        gate_reason="S2: primary stack is Java",
        gate_tags='["S2"]',
        gate_model="qwen2.5:14b",
        human_label="APPLY",
        human_rule="S2",
        note="Java is co-listed with Node, gate over-fired",
        source_view="rescue",
    )
    base.update(overrides)
    return Decision(**base)


def test_migrate_decisions_creates_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", echo=False)
    assert not inspect(engine).has_table("decisions")
    created = migrate_decisions(engine)
    assert created is True
    assert inspect(engine).has_table("decisions")


def test_migrate_decisions_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", echo=False)
    assert migrate_decisions(engine) is True   # first call creates
    assert migrate_decisions(engine) is False  # second call is a no-op


def test_insert_and_read_back(session):
    session.add(_decision())
    session.commit()

    row = session.query(Decision).one()
    assert row.job_id == "job-123"
    assert row.gate_verdict == "skip"
    assert row.gate_tags == '["S2"]'
    assert row.human_label == "APPLY"
    assert row.source_view == "rescue"
    assert row.id is not None  # autoincrement PK assigned


def test_redecide_inserts_second_row_never_overwrites(session):
    """The core append-only contract: a second decision on the same job_id is a
    NEW row; the first is preserved verbatim. Taste changing or the posting
    changing must not destroy the earlier label."""
    first = _decision(human_label="SKIP", note="agreed with gate", source_view="queue")
    session.add(first)
    session.commit()
    first_id = first.id

    # later, the user changes their mind on the SAME job
    second = _decision(human_label="APPLY", note="rescued on second look",
                       source_view="rescue")
    session.add(second)
    session.commit()

    rows = session.query(Decision).filter(Decision.job_id == "job-123") \
        .order_by(Decision.id.asc()).all()
    assert len(rows) == 2, "re-deciding must append, not overwrite"
    assert rows[0].id == first_id
    assert rows[0].human_label == "SKIP"   # original untouched
    assert rows[1].human_label == "APPLY"  # the correction
    assert rows[0].id != rows[1].id


def test_gate_snapshot_is_frozen_not_a_live_join(session):
    """The gate fields on a Decision are a SNAPSHOT. Two decisions on the same
    job recorded under different gate verdicts must each keep their own — there
    is no foreign-key join that could later rewrite history when the Job re-gates."""
    session.add(_decision(gate_verdict="skip", gate_model="qwen2.5:14b"))
    session.add(_decision(gate_verdict="surface", gate_model="qwen2.5:32b"))
    session.commit()

    verdicts = {r.gate_verdict for r in session.query(Decision).all()}
    models = {r.gate_model for r in session.query(Decision).all()}
    assert verdicts == {"skip", "surface"}
    assert models == {"qwen2.5:14b", "qwen2.5:32b"}
