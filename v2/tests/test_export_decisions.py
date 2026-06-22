"""Tests for the decisions -> labels.json export (SPEC §7 / §12 step 6).

The export is the seam that turns captured Decision rows back into ground truth
the rubric-derivation process reads. These pin the contract it must hold:

  * the JOIN — each record carries the Job's text (the Decision stores only a
    job_id), keyed exactly like labels.json on the load-bearing fields;
  * NO fabricated score keys — v2 has no score (SPEC §3), so v1_score/v2_action/
    diff must be ABSENT, not zeroed;
  * latest-per-job collapse — the append-only table can hold a stale label and
    its correction; the training file keeps only the latest and reports the
    supersession (SPEC §7/C3);
  * the selection-context split is surfaced (queue vs rescue — SPEC §7/C1);
  * the held-out split is deterministic and stable across runs (SPEC §6);
  * a decision whose job_id is gone is kept (label survives), text emitted null.

Isolated temp SQLite engine; data/jobs.db untouched. build_export takes a session
so it's driven headless with no file I/O.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job, Decision
import v2.export_decisions as ex


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'e.db'}", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _job(session, job_id, **overrides):
    base = dict(
        id=job_id,
        title="Platform Engineer",
        company="Acme",
        location="Remote",
        is_remote=True,
        description="We run Kubernetes on AWS. Node.js + Python.",
        status="reviewed",
    )
    base.update(overrides)
    job = Job(**base)
    session.add(job)
    session.commit()
    return job


def _decision(session, job_id, **overrides):
    base = dict(
        job_id=job_id,
        decided_at=datetime(2026, 6, 22, 10, 0, 0),
        gate_verdict="surface",
        gate_reason="solid infra fit",
        gate_tags='["S6"]',
        gate_model="qwen2.5:14b",
        human_label="CONSIDER",
        human_rule=None,
        note="looks good",
        source_view="queue",
    )
    base.update(overrides)
    d = Decision(**base)
    session.add(d)
    session.commit()
    return d


# --- the join: labels.json shape, job text pulled in -----------------------

def test_record_joins_job_text_and_matches_labels_json_keys(env):
    s = env()
    _job(s, "j1", title="SRE", company="Globex", location="Remote",
         description="infra role")
    _decision(s, "j1", human_label="APPLY", note="strong fit")
    s.close()

    out = ex.build_export(session=env())
    assert len(out["records"]) == 1
    r = out["records"][0]
    # the load-bearing labels.json keys the rubric tooling reads:
    for key in ex.SHARED_LABEL_KEYS:
        assert key in r, f"missing labels.json key: {key}"
    assert r["id"] == "j1"
    assert r["title"] == "SRE"           # joined off the Job, not the Decision
    assert r["company"] == "Globex"
    assert r["description"] == "infra role"
    assert r["label"] == "APPLY"         # the human ground truth
    assert r["notes"] == "strong fit"


def test_no_fabricated_v1_score_keys(env):
    """v2 has no score (SPEC §3). The export must not invent labels.json's
    score columns — they must be ABSENT, not zeroed (a zero is a lie a reader
    would treat as data)."""
    s = env()
    _job(s, "j1")
    _decision(s, "j1")
    s.close()

    r = ex.build_export(session=env())["records"][0]
    for forbidden in ("v1_score", "v2_score", "v1_action", "v2_action", "diff", "tag"):
        assert forbidden not in r, f"v2 export must not fabricate {forbidden}"
    # but it DOES carry the real v2 signal:
    assert r["gate_verdict"] == "surface"
    assert r["source_view"] == "queue"


def test_gate_tags_decoded_to_list(env):
    s = env()
    _job(s, "j1")
    _decision(s, "j1", gate_tags='["S2", "S6"]')
    s.close()
    r = ex.build_export(session=env())["records"][0]
    assert r["gate_tags"] == ["S2", "S6"]   # decoded from the JSON-string column


# --- latest-per-job collapse (append-only -> one training row) -------------

def test_redecide_keeps_only_latest_label_and_counts_superseded(env):
    """A stale label and its correction must NOT both appear as training
    examples — they'd contradict. Keep the latest, report the supersession."""
    s = env()
    _job(s, "j1")
    _decision(s, "j1", human_label="SKIP", note="agreed with gate",
              decided_at=datetime(2026, 6, 22, 9, 0, 0), source_view="queue")
    _decision(s, "j1", human_label="APPLY", note="rescued on second look",
              decided_at=datetime(2026, 6, 22, 11, 0, 0), source_view="rescue")
    s.close()

    out = ex.build_export(session=env())
    assert len(out["records"]) == 1, "one job -> one training record"
    assert out["records"][0]["label"] == "APPLY"   # the latest decision wins
    assert out["records"][0]["notes"] == "rescued on second look"
    assert out["summary"]["superseded"] == 1
    assert out["summary"]["unique_jobs"] == 1
    assert out["summary"]["total_decisions"] == 2


def test_id_breaks_decided_at_tie(env):
    """Two decisions sharing a timestamp: the higher autoincrement id (= later
    insert) wins, so a same-second correction still resolves deterministically."""
    s = env()
    _job(s, "j1")
    ts = datetime(2026, 6, 22, 10, 0, 0)
    _decision(s, "j1", human_label="HOLD", decided_at=ts)
    _decision(s, "j1", human_label="CONSIDER", decided_at=ts)  # inserted later
    s.close()
    r = ex.build_export(session=env())["records"][0]
    assert r["label"] == "CONSIDER"


# --- selection-context split (SPEC §7/C1) ----------------------------------

def test_summary_partitions_by_source_view(env):
    s = env()
    _job(s, "q1"); _job(s, "q2"); _job(s, "r1")
    _decision(s, "q1", source_view="queue")
    _decision(s, "q2", source_view="queue")
    _decision(s, "r1", source_view="rescue", human_label="SKIP")
    s.close()

    summ = ex.build_export(session=env())["summary"]
    assert summ["by_source"] == {"queue": 2, "rescue": 1}
    assert summ["by_label"]["CONSIDER"] == 2
    assert summ["by_label"]["SKIP"] == 1


def test_surfaced_only_set_is_flagged_in_report(env):
    """A set with zero rescue labels is biased by construction — the report must
    say so before anyone retrains on it (SPEC §7/C1)."""
    s = env()
    _job(s, "q1")
    _decision(s, "q1", source_view="queue")
    s.close()
    report = ex._format_summary(ex.build_export(session=env())["summary"])
    assert "surfaced-only" in report.lower()
    assert "C1" in report


# --- held-out split: deterministic + stable (SPEC §6) ----------------------

def test_holdout_zero_is_all_train(env):
    s = env()
    _job(s, "j1"); _decision(s, "j1")
    s.close()
    out = ex.build_export(session=env(), holdout=0.0)
    assert out["records"][0]["split"] == "train"
    assert out["summary"]["by_split"]["train"] == 1


def test_holdout_split_is_deterministic_across_runs(env):
    """The same job_id must land in the same split every run — no leakage as the
    label set grows. Two builds at the same fraction must agree per job."""
    s = env()
    for i in range(40):
        jid = f"job-{i}"
        _job(s, jid); _decision(s, jid)
    s.close()

    a = {r["id"]: r["split"] for r in ex.build_export(session=env(), holdout=0.3)["records"]}
    b = {r["id"]: r["split"] for r in ex.build_export(session=env(), holdout=0.3)["records"]}
    assert a == b, "holdout assignment must be stable run-to-run"
    # and it must actually hold some out (not degenerate to all-train)
    assert "holdout" in set(a.values())


def test_holdout_assignment_is_function_of_job_id_only(env):
    """A given job_id hashes to the same bucket regardless of label/order, so a
    re-decided job never crosses the train/holdout boundary."""
    assert ex._holdout_split("stable-id", 0.5) == ex._holdout_split("stable-id", 0.5)


# --- missing job tolerance --------------------------------------------------

def test_decision_for_deleted_job_keeps_label_with_null_text(env):
    """If the Job row is gone, the label is still ground truth — keep it, emit
    the joined text fields as null rather than dropping the row."""
    s = env()
    _decision(s, "ghost", human_label="SKIP")   # no matching Job
    s.close()

    out = ex.build_export(session=env())
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["id"] == "ghost"
    assert r["label"] == "SKIP"
    assert r["title"] is None and r["description"] is None
    assert out["summary"]["missing_job"] == 1


# --- empty table ------------------------------------------------------------

def test_empty_table_exports_nothing_without_crashing(env):
    out = ex.build_export(session=env())
    assert out["records"] == []
    assert out["summary"]["unique_jobs"] == 0
    # an empty set isn't flagged as surfaced-only-biased (nothing to be biased)
    report = ex._format_summary(out["summary"])
    assert "surfaced-only" not in report.lower()
