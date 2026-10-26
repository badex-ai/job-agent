"""The recency filter's data contract.

The filter keys on scraped_at (populated on every row) rather than date_posted
(absent on ~3/4 of the live DB), so these tests pin that both dates reach the
page and that an absent date arrives as null — never as the string "None",
which is what the old scraper wrote and what made a dateless job look dated.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.database.models import Base, Job, Application, migrate_applications
from v2.webapp.queries import get_classified_jobs, get_applications


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", echo=False)
    Base.metadata.create_all(engine)
    migrate_applications(engine)
    return sessionmaker(bind=engine)


def _job(session, **kw):
    base = dict(id="j1", title="Platform Engineer", company="Acme", location="Remote",
                description="d", apply_link="u", status="new", gate_verdict="surface",
                gate_reason="fit", gate_tags="[]", gate_model="qwen2.5:14b",
                gate_run_at=datetime.utcnow())
    base.update(kw)
    session.add(Job(**base))
    session.commit()


def test_scraped_at_is_exposed_iso(Session):
    s = Session()
    when = datetime(2026, 4, 18, 14, 14, 50)
    _job(s, scraped_at=when)

    job = get_classified_jobs(session=s)["CONSIDER"][0]

    assert job["scraped_at"] == when.isoformat()


def test_scraped_at_is_always_populated(Session):
    """The column carries default=datetime.utcnow, so a row inserted without an
    explicit scrape time still gets one. This is WHY the filter keys on
    scraped_at rather than date_posted: it cannot be missing."""
    s = Session()
    _job(s, scraped_at=None)

    job = get_classified_jobs(session=s)["CONSIDER"][0]

    assert job["scraped_at"] is not None
    datetime.fromisoformat(job["scraped_at"])  # parseable ISO, not "None"


def test_real_date_posted_is_exposed(Session):
    s = Session()
    _job(s, date_posted="2026-04-17")

    job = get_classified_jobs(session=s)["CONSIDER"][0]

    assert job["date_posted"] == "2026-04-17"


@pytest.mark.parametrize("absent", ["", None])
def test_absent_date_posted_is_null(Session, absent):
    """Absent must arrive falsy so the page can say "no posting date" honestly."""
    s = Session()
    _job(s, date_posted=absent)

    job = get_classified_jobs(session=s)["CONSIDER"][0]

    assert job["date_posted"] is None


def test_the_two_dates_are_independent(Session):
    """A job with no LinkedIn date still carries a scrape date to filter on —
    the exact case covering 3/4 of the live DB."""
    s = Session()
    _job(s, scraped_at=datetime(2026, 4, 18), date_posted="")

    job = get_classified_jobs(session=s)["CONSIDER"][0]

    assert job["date_posted"] is None
    assert job["scraped_at"] is not None, "filterable even with no posting date"


def test_applications_expose_acted_at(Session):
    s = Session()
    _job(s, id="j1")
    when = datetime(2026, 5, 1, 9, 0, 0)
    s.add(Application(job_id="j1", event="applied", status="applied",
                      cv="cv_devops", created_at=when))
    s.commit()

    row = get_applications(session=s)["applied"][0]

    assert row["acted_at"] == when.isoformat()


def test_acted_at_present_even_when_job_applied_at_is_not(Session):
    """acted_at comes from the log, so it survives when the Job-row dual-write
    never set applied_at — which is why the filter keys on it."""
    s = Session()
    _job(s, id="j1", applied_at=None)
    s.add(Application(job_id="j1", event="applied", status="applied",
                      created_at=datetime(2026, 5, 1)))
    s.commit()

    row = get_applications(session=s)["applied"][0]

    assert row["applied_at"] is None
    assert row["acted_at"] is not None


def test_dismissed_rows_also_carry_acted_at(Session):
    s = Session()
    _job(s, id="j1")
    s.add(Application(job_id="j1", event="dismissed",
                      created_at=datetime(2026, 5, 2)))
    s.commit()

    row = get_applications(session=s)["dismissed"][0]

    assert row["acted_at"] == datetime(2026, 5, 2).isoformat()
