"""Tests for the rescue view — the selection-bias defence (SPEC §5c / §7 / §12.4).

The rescue view is the ONLY channel that produces labels on gate-skipped jobs,
so the training set isn't surfaced-only (SPEC §7/C1). These pin the headless
seams:

  * get_rescue_queue()    — undecided gate-skipped rows only; decided ones drop out.
  * apply_rescue_action() — 'c' confirm-skip writes a SKIP Decision; 'r' rescue
                            writes the chosen act-set label; both source_view='rescue';
                            a rescue-APPLY delegates to mark_applied exactly like
                            the queue does (a real apply is a real apply).

Isolated temp SQLite engine; data/jobs.db untouched. get_session is repointed at
the temp engine in every module the seam reaches (same pattern as the queue tests).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job, Decision
import shared.database.decisions as decisions_mod
import shared.database.tracking as tracking_mod
import v2.dashboard.cli as cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(cli, "get_session", SessionLocal)
    monkeypatch.setattr(decisions_mod, "get_session", SessionLocal)
    monkeypatch.setattr(tracking_mod, "get_session", SessionLocal)
    return SessionLocal


def _seed(session, job_id, *, status="new", verdict="skip", **overrides):
    base = dict(
        id=job_id,
        title="Backend Engineer",
        company="Globex",
        status=status,
        gate_verdict=verdict,
        gate_reason="S2: primary stack reads as Java",
        gate_tags='["S2"]',
        gate_model="qwen2.5:14b",
    )
    base.update(overrides)
    job = Job(**base)
    session.add(job)
    session.commit()
    return job


# --- get_rescue_queue: only undecided buried jobs --------------------------

def test_rescue_queue_is_skipped_and_new_only(env):
    s = env()
    _seed(s, "buried", verdict="skip")
    _seed(s, "surfaced", verdict="surface")               # not skipped -> excluded
    _seed(s, "decided", verdict="skip", status="ignored")  # decided -> excluded
    s.close()

    assert [j.id for j in cli.get_rescue_queue()] == ["buried"]


def test_confirmed_or_rescued_job_drops_out_of_rescue_queue(env):
    s = env()
    _seed(s, "b1", verdict="skip")
    s.close()

    assert [j.id for j in cli.get_rescue_queue()] == ["b1"]
    ok, err = cli.apply_rescue_action("b1", "c")          # confirm skip
    assert ok and err is None
    assert cli.get_rescue_queue() == []


# --- confirm-skip: a SKIP decision agreeing with the gate ------------------

def test_confirm_skip_writes_skip_decision_from_rescue(env):
    s = env()
    _seed(s, "b1", verdict="skip")
    s.close()

    ok, err = cli.apply_rescue_action("b1", "c", note="agree, Java shop")
    assert ok and err is None

    v = env()
    d = v.query(Decision).one()
    assert d.human_label == "SKIP"
    assert d.source_view == "rescue"        # the selection-context marker (§7)
    assert d.gate_verdict == "skip"         # frozen snapshot off the buried job
    assert d.note == "agree, Java shop"
    assert v.get(Job, "b1").status == "ignored"
    v.close()


# --- rescue: the false-negative-correcting label ---------------------------

@pytest.mark.parametrize("sub_label,status", [
    ("CONSIDER", "reviewed"),
    ("HOLD", "reviewed"),
])
def test_rescue_to_act_set_writes_decision(env, sub_label, status):
    s = env()
    _seed(s, "b1", verdict="skip")
    s.close()

    ok, err = cli.apply_rescue_action("b1", "r", sub_label=sub_label, rule="S2")
    assert ok and err is None

    v = env()
    d = v.query(Decision).one()
    assert d.human_label == sub_label
    assert d.source_view == "rescue"
    assert d.human_rule == "S2"             # the over-fired rule being corrected
    assert v.get(Job, "b1").status == status
    v.close()


def test_rescue_apply_delegates_to_mark_applied(env):
    """A rescue-APPLY is a real application — it must go through mark_applied
    exactly like the queue's apply, scheduling the follow-up."""
    s = env()
    _seed(s, "b1", verdict="skip")
    s.close()

    ok, err = cli.apply_rescue_action(
        "b1", "r", sub_label="APPLY", rule="S2", channel="linkedin"
    )
    assert ok and err is None

    v = env()
    d = v.query(Decision).one()
    assert d.human_label == "APPLY"
    assert d.source_view == "rescue"
    job = v.get(Job, "b1")
    assert job.status == "applied"
    assert job.applied is True
    assert job.applied_via == "linkedin"
    assert job.next_followup_due is not None
    v.close()


# --- validation ------------------------------------------------------------

def test_rescue_requires_valid_sub_label(env):
    s = env()
    _seed(s, "b1", verdict="skip")
    s.close()
    ok, err = cli.apply_rescue_action("b1", "r", sub_label="SKIP")  # not act-set
    assert not ok and "sub_label" in err
    v = env()
    assert v.query(Decision).count() == 0
    v.close()


def test_rescue_rejects_unknown_key(env):
    s = env()
    _seed(s, "b1", verdict="skip")
    s.close()
    ok, err = cli.apply_rescue_action("b1", "x")
    assert not ok and "not a rescue decision key" in err
    v = env()
    assert v.query(Decision).count() == 0
    v.close()


# --- rendering tolerates a missing reason (SPEC §9) ------------------------

def test_render_skipped_tolerates_missing_reason(env):
    s = env()
    _seed(s, "b1", verdict="skip", gate_reason=None, gate_tags=None,
          location="Remote", is_remote=True, source="linkedin")
    s.close()
    job = cli.get_rescue_queue()[0]
    assert cli.render_skipped(job, 1, 1) is not None


# --- the cross-module invariant the rescue queue silently depends on -------

def test_gate_skip_leaves_status_new_so_rescue_queue_is_not_empty():
    """SPEC §7/C1 precondition: the gate must NEVER mutate Job.status. The rescue
    queue is `status=='new' AND gate_verdict=='skip'`; if _apply_verdict marked
    skips as 'ignored', the rescue queue would be empty BY CONSTRUCTION and the
    whole bias defence would silently do nothing. This pins that invariant so a
    future change to the gate fails here loudly instead of in production."""
    from v2.loop import _apply_verdict
    job = Job(id="g1", status="new")
    _apply_verdict(job, {
        "gate_verdict": "skip",
        "gate_reason": "S2: Java shop",
        "gate_model": "qwen2.5:14b",
        "gate_tags": ["S2"],
        "gate_confidence": 90,
        "gate_source": "qwen",
        "gate_run_at": None,
    })
    assert job.gate_verdict == "skip"
    assert job.status == "new", (
        "the gate must not touch Job.status — a skip row must stay 'new' so the "
        "rescue queue can surface it (SPEC §7/C1)"
    )
