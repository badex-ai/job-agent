"""Tests for _store_new_jobs content-dedup + merge semantics (v2/loop.py)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job
from v2.loop import _store_new_jobs


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(jid, title, company, desc="", **kw):
    return {"id": jid, "title": title, "company": company,
            "description": desc, "source": "linkedin", **kw}


def test_genuinely_new_jobs_are_stored(session):
    n = _store_new_jobs(session, [
        _job("a1", "DevOps Engineer", "Acme", "run kubernetes and terraform"),
        _job("b2", "Frontend Developer", "Beta", "build react dashboards"),
    ])
    assert n == 2
    assert session.query(Job).count() == 2


def test_exact_id_repost_is_not_stored_twice(session):
    _store_new_jobs(session, [_job("a1", "DevOps Engineer", "Acme", "kube terraform")])
    n = _store_new_jobs(session, [_job("a1", "DevOps Engineer", "Acme", "kube terraform")])
    assert n == 0
    assert session.query(Job).count() == 1


def test_same_role_fresh_id_is_merged_not_duplicated(session):
    # The 34x LinkedIn re-post leak: same title+company+desc, different id.
    desc = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("lnk_1", "Full Stack Developer", "DataAnnotation", desc)])
    n = _store_new_jobs(session, [_job("lnk_2", "Full Stack Developer", "DataAnnotation", desc)])
    assert n == 0
    assert session.query(Job).count() == 1  # merged, not a second row


def test_distinct_roles_same_key_are_kept_separate(session):
    # Aggregator failure mode: same title+company, genuinely different roles.
    _store_new_jobs(session, [_job("agg_1", "Software Engineer", "Haystack",
                                   "DevOps role: kubernetes, terraform, on-call.")])
    n = _store_new_jobs(session, [_job("agg_2", "Software Engineer", "Haystack",
                                       "Data scientist: python, pandas, ML models.")])
    assert n == 1  # different descriptions -> not a dup -> stored
    assert session.query(Job).count() == 2


def test_blank_incoming_merges_and_fills_from_existing(session):
    full = "Detailed JD: AWS, Kubernetes, Terraform, GitOps, on-call rotation."
    _store_new_jobs(session, [_job("x1", "SRE", "Zeta", full)])
    n = _store_new_jobs(session, [_job("x2", "SRE", "Zeta", "")])  # blank re-post
    assert n == 0
    row = session.query(Job).one()
    assert row.description == full  # stayed filled


def test_blank_existing_is_filled_from_incoming(session):
    _store_new_jobs(session, [_job("x1", "SRE", "Zeta", "")])          # blank first
    full = "Detailed JD: AWS, Kubernetes, Terraform, GitOps, on-call rotation."
    n = _store_new_jobs(session, [_job("x2", "SRE", "Zeta", full)])    # full second
    assert n == 0
    row = session.query(Job).one()
    assert row.description == full  # blank got filled from the incoming side


def test_merge_refreshes_apply_link_when_both_present(session):
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("m1", "DevOps", "Acme", d, apply_link="old")])
    _store_new_jobs(session, [_job("m2", "DevOps", "Acme", d, apply_link="new")])
    assert session.query(Job).one().apply_link == "new"


def test_reopening_past_window_is_kept_as_new(session):
    # A role scraped >30 days ago, re-posted today, is a genuine re-opening —
    # it must NOT be buried. (The expensive FP under the manual-apply workflow.)
    from datetime import datetime, timedelta
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("old", "DevOps", "Acme", d)])
    row = session.query(Job).one()
    row.scraped_at = datetime.utcnow() - timedelta(days=45)   # last seen 45d ago
    session.commit()

    n = _store_new_jobs(session, [_job("new", "DevOps", "Acme", d)])  # re-opened
    assert n == 1                                    # kept as a NEW opening
    assert session.query(Job).count() == 2


def test_recent_repost_within_window_is_merged(session):
    from datetime import datetime, timedelta
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("r1", "DevOps", "Acme", d)])
    row = session.query(Job).one()
    row.scraped_at = datetime.utcnow() - timedelta(days=10)   # seen 10d ago
    session.commit()

    n = _store_new_jobs(session, [_job("r2", "DevOps", "Acme", d)])  # re-spam
    assert n == 0                                    # merged (within 30d)
    assert session.query(Job).count() == 1


def test_merge_refreshes_scraped_at_to_now(session):
    # Window is measured since LAST seen: a merge must push scraped_at forward so
    # a continuously-spammed post never ages out into a false re-opening.
    from datetime import datetime, timedelta
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("s1", "DevOps", "Acme", d)])
    row = session.query(Job).one()
    old = datetime.utcnow() - timedelta(days=20)
    row.scraped_at = old
    session.commit()
    _store_new_jobs(session, [_job("s2", "DevOps", "Acme", d)])
    assert session.query(Job).one().scraped_at > old   # pushed to ~now


def test_merge_ignores_junk_date_posted(session):
    # A merge must never overwrite a real date with the string "None"/"nan".
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("d1", "DevOps", "Acme", d, date_posted="2026-05-07")])
    _store_new_jobs(session, [_job("d2", "DevOps", "Acme", d, date_posted="None")])
    assert session.query(Job).one().date_posted == "2026-05-07"  # kept the real date


def test_merge_never_mutates_status_or_gate_columns(session):
    # The gate-status invariant: dedup must not touch status/gate_*.
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines end to end."
    _store_new_jobs(session, [_job("g1", "DevOps", "Acme", d)])
    row = session.query(Job).one()
    row.status = "reviewed"
    row.gate_verdict = "skip"
    session.commit()
    _store_new_jobs(session, [_job("g2", "DevOps", "Acme", d, apply_link="new")])
    row = session.query(Job).one()
    assert row.status == "reviewed"       # untouched
    assert row.gate_verdict == "skip"     # untouched
    assert row.apply_link == "new"        # volatile field still refreshed
