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
