"""The junk-date repair must blank the lies and touch nothing else."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.database.models import Base, Job
from v2.normalize_junk_dates import normalize


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(s, jid, date):
    s.add(Job(id=jid, title="t", company="c", date_posted=date, status="new"))


def test_junk_is_blanked_and_real_dates_survive(session):
    _add(session, "1", "None")
    _add(session, "2", "nan")
    _add(session, "3", "NaT")
    _add(session, "4", "2026-04-17")
    _add(session, "5", "")
    session.commit()

    counts = normalize(session)

    assert sum(counts.values()) == 3
    got = {j.id: j.date_posted for j in session.query(Job).all()}
    assert got["1"] == got["2"] == got["3"] == ""
    assert got["4"] == "2026-04-17", "a real date must never be destroyed"
    assert got["5"] == ""


def test_dry_run_writes_nothing(session):
    _add(session, "1", "None")
    session.commit()

    counts = normalize(session, dry_run=True)

    assert counts == {"None": 1}
    assert session.query(Job).get("1").date_posted == "None", "dry run mutated the DB"


def test_idempotent(session):
    _add(session, "1", "None")
    _add(session, "2", "2026-04-17")
    session.commit()

    normalize(session)
    second = normalize(session)

    assert second == {}, "a second run should find nothing left to change"
    assert session.query(Job).get("2").date_posted == "2026-04-17"


def test_absent_becomes_falsy(session):
    """The whole point: after repair, a dateless job is falsy."""
    _add(session, "1", "None")
    session.commit()
    normalize(session)
    assert not session.query(Job).get("1").date_posted
