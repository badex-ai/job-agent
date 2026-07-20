"""Tests for the blank-description repair script (v2/repair_descriptions.py).

Two things worth pinning on a script that writes straight to the jobs DB:

1. `blank_jobs()` selects exactly the recoverable rows — a URL to fetch from and
   no usable description — and nothing else. A query that quietly widened would
   re-fetch already-good rows against LinkedIn; one that narrowed would abandon
   real blanks.
2. `repair()` treats a fetch FAILURE (DescriptionFetchError) differently from an
   empty-but-successful fetch: the failure hits the hard BACKOFF_S sleep and is
   counted as an error, the empty one is left untouched and counted as "gone".
   That split was dead before the guard fix (a throttle used to return "" and
   fall into the "gone" branch, burning the batch at the flat interval); these
   tests are the regression fence around it.

Everything runs against an isolated temp DB with `fetch_job_description` and both
sleeps mocked, so no network is touched and no real time passes.
"""
from datetime import datetime, timedelta
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job
from shared.scraper.job_scraper import DescriptionFetchError
from v2 import repair_descriptions


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _job(session, jid, *, url="https://linkedin.com/jobs/view/x-at-y-1",
         description="", scraped_at=None):
    session.add(Job(
        id=jid, title=f"Role {jid}", company="Acme", location="Remote",
        description=description, job_url=url,
        scraped_at=scraped_at or datetime.utcnow(),
    ))
    session.commit()


# --- blank_jobs: selects exactly the recoverable rows --------------------------

def test_blank_jobs_selects_only_urld_blanks(Session):
    s = Session()
    _job(s, "blank-null", description=None)
    _job(s, "blank-empty", description="")
    _job(s, "has-desc", description="A real description, long enough to keep.")
    _job(s, "blank-no-url", description="", url=None)
    _job(s, "blank-empty-url", description="", url="")

    got = {j.id for j in repair_descriptions.blank_jobs(s)}
    assert got == {"blank-null", "blank-empty"}
    s.close()


def test_blank_jobs_orders_newest_first_and_honours_limit(Session):
    s = Session()
    old = datetime.utcnow() - timedelta(days=10)
    mid = datetime.utcnow() - timedelta(days=5)
    new = datetime.utcnow()
    _job(s, "old", scraped_at=old)
    _job(s, "mid", scraped_at=mid)
    _job(s, "new", scraped_at=new)

    ordered = [j.id for j in repair_descriptions.blank_jobs(s)]
    assert ordered == ["new", "mid", "old"]          # newest first: likeliest open
    assert [j.id for j in repair_descriptions.blank_jobs(s, limit=2)] == ["new", "mid"]
    s.close()


# --- repair: failure backs off, empty is left, good is written -----------------

@pytest.fixture
def _mock_time(monkeypatch):
    slept = []
    monkeypatch.setattr(repair_descriptions.time, "sleep", lambda s: slept.append(s))
    return slept


def _run_repair(Session, monkeypatch, fetch):
    """Point repair() at the temp DB and a stubbed fetch, and run it."""
    session = Session()
    monkeypatch.setattr(repair_descriptions, "get_session", lambda: session)
    monkeypatch.setattr(repair_descriptions, "fetch_job_description", fetch)
    return session


def test_repair_writes_recovered_description(Session, monkeypatch, _mock_time):
    s = _run_repair(Session, monkeypatch,
                    fetch=lambda url: ("x" * 200, "https://apply/1"))
    _job(s, "blank", description="")

    repair_descriptions.repair()

    row = s.query(Job).filter_by(id="blank").one()
    assert len(row.description) == 200
    assert row.apply_link == "https://apply/1"
    s.close()


def test_repair_leaves_thin_posting_untouched(Session, monkeypatch, _mock_time):
    # Fetched fine, but under MIN_DESC_CHARS -> "gone", row not written, flat sleep.
    s = _run_repair(Session, monkeypatch, fetch=lambda url: ("too short", ""))
    _job(s, "thin", description="")

    repair_descriptions.repair()

    assert s.query(Job).filter_by(id="thin").one().description == ""
    assert repair_descriptions.BACKOFF_S not in _mock_time     # no hard backoff
    s.close()


def test_repair_backs_off_hard_on_fetch_failure(Session, monkeypatch, _mock_time):
    # The branch that was dead before the guard fix: a throttle now RAISES, so
    # repair() must sleep BACKOFF_S (not the flat 2.5s) and count it an error.
    def _throttled(url):
        raise DescriptionFetchError("429 survived the retry budget")

    s = _run_repair(Session, monkeypatch, fetch=_throttled)
    _job(s, "throttled", description="")

    repair_descriptions.repair()

    assert s.query(Job).filter_by(id="throttled").one().description == ""
    assert repair_descriptions.BACKOFF_S in _mock_time         # hard backoff fired
    s.close()


def test_repair_skips_gone_posting_without_backing_off(Session, monkeypatch, _mock_time):
    # A 404 posting surfaces as ("", "") from fetch_job_description (not a raise),
    # so it lands in the fast "gone" branch — flat sleep, no 120s backoff. This is
    # the fix for the first live run, where every expired row would otherwise have
    # cost a 120s backoff.
    s = _run_repair(Session, monkeypatch, fetch=lambda url: ("", ""))
    _job(s, "expired", description="")

    repair_descriptions.repair()

    assert s.query(Job).filter_by(id="expired").one().description == ""
    assert repair_descriptions.BACKOFF_S not in _mock_time     # skipped fast
    s.close()


def test_repair_dry_run_changes_nothing(Session, monkeypatch, _mock_time):
    calls = []
    s = _run_repair(Session, monkeypatch,
                    fetch=lambda url: calls.append(url) or ("x" * 200, ""))
    _job(s, "blank", description="")

    repair_descriptions.repair(dry_run=True)

    assert calls == []                                          # never fetched
    assert s.query(Job).filter_by(id="blank").one().description == ""
    s.close()
