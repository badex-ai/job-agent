"""Tests for the webapp bucket-grouping read (v2/webapp/queries.py).

Pins the one genuinely new query: group classified jobs by the LATEST
Decision.human_label per job, into the four buckets, read-only. Runs against an
isolated temp DB; data/jobs.db is untouched.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job, Decision, migrate_decisions
from shared.database.decisions import record_decision
from v2.webapp.queries import (
    BUCKETS, get_classified_jobs, bucket_counts, _tags,
)


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)
    migrate_decisions(engine)
    return sessionmaker(bind=engine)


def _seed_job(session, **overrides):
    base = dict(
        id="job-1",
        title="Senior Platform Engineer",
        company="Acme",
        location="Remote",
        description="Build the thing.",
        apply_link="https://example.com/apply/1",
        status="new",
        gate_verdict="surface",
        gate_reason="infra fit",
        gate_tags='["S6"]',
        gate_model="qwen2.5:14b",
    )
    base.update(overrides)
    job = Job(**base)
    session.add(job)
    session.commit()
    return job


# --- grouping into the four buckets ----------------------------------------

def test_groups_by_latest_human_label(Session):
    s = Session()
    _seed_job(s, id="j-apply")
    _seed_job(s, id="j-consider")
    _seed_job(s, id="j-hold")
    _seed_job(s, id="j-skip")
    record_decision(s.get(Job, "j-apply"), "APPLY", session=s)
    record_decision(s.get(Job, "j-consider"), "CONSIDER", session=s)
    record_decision(s.get(Job, "j-hold"), "HOLD", session=s)
    record_decision(s.get(Job, "j-skip"), "SKIP", session=s)

    buckets = get_classified_jobs(session=s)
    assert set(buckets) == set(BUCKETS)
    assert [j["id"] for j in buckets["APPLY"]] == ["j-apply"]
    assert [j["id"] for j in buckets["CONSIDER"]] == ["j-consider"]
    assert [j["id"] for j in buckets["HOLD"]] == ["j-hold"]
    assert [j["id"] for j in buckets["SKIP"]] == ["j-skip"]
    s.close()


def test_redecided_job_latest_wins(Session):
    """Append-only: re-deciding inserts a second row; the latest label wins and the
    job appears in exactly one bucket (plan §3)."""
    s = Session()
    _seed_job(s, id="j1")
    # First HOLD, then CONSIDER later. Force ordered timestamps so the tiebreak
    # isn't the only thing under test.
    d1 = Decision(job_id="j1", human_label="HOLD",
                  decided_at=datetime.utcnow() - timedelta(hours=1))
    d2 = Decision(job_id="j1", human_label="CONSIDER",
                  decided_at=datetime.utcnow())
    s.add_all([d1, d2])
    s.commit()

    buckets = get_classified_jobs(session=s)
    assert [j["id"] for j in buckets["CONSIDER"]] == ["j1"]
    assert buckets["HOLD"] == []
    # appears in exactly one bucket
    total = sum(len(v) for v in buckets.values())
    assert total == 1
    s.close()


def test_id_tiebreak_when_timestamps_collide(Session):
    """When two decisions share decided_at, the higher id (latest insert) wins."""
    s = Session()
    _seed_job(s, id="j1")
    ts = datetime.utcnow()
    s.add(Decision(job_id="j1", human_label="SKIP", decided_at=ts))
    s.commit()
    s.add(Decision(job_id="j1", human_label="APPLY", decided_at=ts))
    s.commit()

    buckets = get_classified_jobs(session=s)
    assert [j["id"] for j in buckets["APPLY"]] == ["j1"]
    assert buckets["SKIP"] == []
    s.close()


def test_empty_buckets_are_clean_not_error(Session):
    """No decisions at all -> all four buckets present and empty (plan §3)."""
    s = Session()
    buckets = get_classified_jobs(session=s)
    assert buckets == {b: [] for b in BUCKETS}
    assert bucket_counts(buckets) == {b: 0 for b in BUCKETS}
    s.close()


def test_undecided_jobs_do_not_appear(Session):
    s = Session()
    _seed_job(s, id="decided")
    _seed_job(s, id="undecided")
    record_decision(s.get(Job, "decided"), "APPLY", session=s)
    buckets = get_classified_jobs(session=s)
    ids = [j["id"] for v in buckets.values() for j in v]
    assert ids == ["decided"]
    s.close()


def test_decision_for_deleted_job_is_skipped_not_crash(Session):
    """A Decision whose Job row is gone must not crash the view (plan §3)."""
    s = Session()
    s.add(Decision(job_id="ghost", human_label="APPLY", decided_at=datetime.utcnow()))
    s.commit()
    buckets = get_classified_jobs(session=s)
    assert buckets == {b: [] for b in BUCKETS}
    s.close()


# --- the job view projection -----------------------------------------------

def test_job_view_fields_and_priority_flag(Session):
    s = Session()
    _seed_job(s, id="jp", gate_verdict="surface_priority")
    record_decision(s.get(Job, "jp"), "APPLY", session=s)
    view = get_classified_jobs(session=s)["APPLY"][0]
    assert view["id"] == "jp"
    assert view["title"] == "Senior Platform Engineer"
    assert view["company"] == "Acme"
    assert view["location"] == "Remote"
    assert view["apply_link"] == "https://example.com/apply/1"
    assert view["tags"] == ["S6"]
    assert view["priority"] is True
    s.close()


def test_apply_link_falls_back_to_job_url(Session):
    s = Session()
    _seed_job(s, id="jl", apply_link=None, job_url="https://example.com/job/xyz")
    record_decision(s.get(Job, "jl"), "CONSIDER", session=s)
    view = get_classified_jobs(session=s)["CONSIDER"][0]
    assert view["apply_link"] == "https://example.com/job/xyz"
    s.close()


def test_missing_gate_reason_degrades(Session):
    s = Session()
    _seed_job(s, id="jr", gate_reason=None, gate_tags=None)
    record_decision(s.get(Job, "jr"), "HOLD", session=s)
    view = get_classified_jobs(session=s)["HOLD"][0]
    assert view["gate_reason"] is None
    assert view["tags"] == []
    s.close()


def test_bucket_counts(Session):
    s = Session()
    for i in range(3):
        _seed_job(s, id=f"a{i}")
        record_decision(s.get(Job, f"a{i}"), "APPLY", session=s)
    _seed_job(s, id="h0")
    record_decision(s.get(Job, "h0"), "HOLD", session=s)
    counts = bucket_counts(get_classified_jobs(session=s))
    assert counts == {"APPLY": 3, "CONSIDER": 0, "HOLD": 1, "SKIP": 0}
    s.close()


# --- pure helpers ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ('["S1", "S2"]', ["S1", "S2"]),
    ("[]", []),
    (None, []),
    ("not json", []),
    ('{"a": 1}', []),   # non-list JSON -> []
])
def test_tags_parsing(raw, expected):
    assert _tags(raw) == expected
