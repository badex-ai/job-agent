"""Tests for the queue view's data + dispatch seams (SPEC §5b / §12 step 3).

The interactive Rich loop (run_queue) is not driven here — the load-bearing
logic is split out into two headless seams that ARE tested:

  * get_queue()          — the right rows, in the right order (surface_priority
                           first; decided jobs drop out).
  * apply_queue_action() — one keypress -> the correct decision writes:
                           a Decision row every time (source_view='queue'), plus
                           mark_applied ONLY on apply, with the option-1 ordering.

Everything runs against an isolated temp SQLite engine; data/jobs.db is untouched.
We point the modules' get_session at the temp engine via monkeypatch (the same
seam test_record_decision uses for its own-session path).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job
import shared.database.decisions as decisions_mod
import shared.database.tracking as tracking_mod
import v2.dashboard.cli as cli
from shared.database.models import Decision


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp DB wired into every module the seam touches. Yields the
    sessionmaker so a test can seed/inspect rows directly."""
    engine = create_engine(f"sqlite:///{tmp_path / 'q.db'}", echo=False)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # the queue's data access, the decision seam, the apply seam, and cli._load_job
    # all reach the DB through get_session — repoint every one at the temp engine.
    monkeypatch.setattr(cli, "get_session", SessionLocal)
    monkeypatch.setattr(decisions_mod, "get_session", SessionLocal)
    monkeypatch.setattr(tracking_mod, "get_session", SessionLocal)
    return SessionLocal


def _seed(session, job_id, *, status="new", verdict="surface", **overrides):
    base = dict(
        id=job_id,
        title="Platform Engineer",
        company="Acme",
        status=status,
        gate_verdict=verdict,
        gate_reason="solid infra fit",
        gate_tags='["S6"]',
        gate_model="qwen2.5:14b",
    )
    base.update(overrides)
    job = Job(**base)
    session.add(job)
    session.commit()
    return job


# --- get_queue: filtering + ordering (SPEC §5b) ----------------------------

def test_queue_includes_only_new_surfaced_jobs(env):
    s = env()
    _seed(s, "surf", verdict="surface")
    _seed(s, "prio", verdict="surface_priority")
    _seed(s, "skip-row", verdict="skip")                 # not surfaced -> excluded
    _seed(s, "already", verdict="surface", status="reviewed")  # decided -> excluded
    s.close()

    ids = [j.id for j in cli.get_queue()]
    assert set(ids) == {"surf", "prio"}


def test_queue_orders_priority_first(env):
    s = env()
    _seed(s, "surf", verdict="surface")
    _seed(s, "prio", verdict="surface_priority")
    s.close()

    ids = [j.id for j in cli.get_queue()]
    assert ids[0] == "prio", "surface_priority must sort ahead of surface"


def test_decided_job_drops_out_of_queue(env):
    s = env()
    _seed(s, "j1", verdict="surface")
    s.close()

    assert [j.id for j in cli.get_queue()] == ["j1"]
    ok, err = cli.apply_queue_action("j1", "h")          # hold -> status reviewed
    assert ok and err is None
    assert cli.get_queue() == [], "a decided job is no longer in the queue"


# --- apply_queue_action: the four-way writes a Decision --------------------

@pytest.mark.parametrize("key,label,status", [
    ("c", "CONSIDER", "reviewed"),
    ("h", "HOLD", "reviewed"),
    ("s", "SKIP", "ignored"),
])
def test_non_apply_keys_write_decision_and_status(env, key, label, status):
    s = env()
    _seed(s, "j1", verdict="surface")
    s.close()

    ok, err = cli.apply_queue_action("j1", key)
    assert ok and err is None

    verify = env()
    d = verify.query(Decision).one()
    assert d.human_label == label
    assert d.source_view == "queue"
    assert d.gate_verdict == "surface"        # snapshot off the job
    assert verify.get(Job, "j1").status == status
    verify.close()


def test_skip_captures_rule_tag(env):
    s = env()
    _seed(s, "j1", verdict="surface")
    s.close()

    ok, _ = cli.apply_queue_action("j1", "s", rule="S2", note="java co-listed")
    assert ok
    verify = env()
    d = verify.query(Decision).one()
    assert d.human_rule == "S2"
    assert d.note == "java co-listed"
    verify.close()


# --- apply: Decision + mark_applied, in that order (the option-1 seam) ------

def test_apply_writes_decision_and_marks_applied(env):
    s = env()
    _seed(s, "j1", verdict="surface_priority")
    s.close()

    ok, err = cli.apply_queue_action("j1", "a", channel="email", version="B", tone="story")
    assert ok and err is None

    verify = env()
    d = verify.query(Decision).one()
    assert d.human_label == "APPLY"
    job = verify.get(Job, "j1")
    assert job.status == "applied"
    # mark_applied owns these — proves the seam delegated rather than reimplementing:
    assert job.applied is True
    assert job.applied_via == "email"
    assert job.message_version == "B"
    assert job.message_tone == "story"
    assert job.next_followup_due is not None   # follow-up scheduled by mark_applied
    verify.close()


def test_apply_defaults_channel_to_website(env):
    s = env()
    _seed(s, "j1", verdict="surface")
    s.close()
    ok, _ = cli.apply_queue_action("j1", "a")    # no channel given
    assert ok
    verify = env()
    assert verify.get(Job, "j1").applied_via == "website"
    verify.close()


def test_apply_surfaces_mark_applied_failure_but_keeps_decision(env, monkeypatch):
    """If mark_applied fails, the (append-only) Decision is already written and we
    surface the tracking failure — we never silently lose the label."""
    s = env()
    _seed(s, "j1", verdict="surface")
    s.close()

    monkeypatch.setattr(cli, "mark_applied",
                        lambda *a, **k: (False, "boom"))
    ok, err = cli.apply_queue_action("j1", "a", channel="website")
    assert not ok
    assert "mark_applied failed" in err and "boom" in err

    verify = env()
    assert verify.query(Decision).count() == 1   # the label survived
    verify.close()


# --- navigation keys are not decisions -------------------------------------

@pytest.mark.parametrize("key", ["o", "n", "b", "x"])
def test_nav_and_unknown_keys_rejected_by_seam(env, key):
    s = env()
    _seed(s, "j1", verdict="surface")
    s.close()
    ok, err = cli.apply_queue_action("j1", key)
    assert not ok
    assert "not a decision key" in err
    verify = env()
    assert verify.query(Decision).count() == 0
    verify.close()


# --- rendering never crashes on a missing reason (SPEC §9) -----------------

def test_render_job_tolerates_missing_reason(env):
    s = env()
    _seed(s, "j1", verdict="surface", gate_reason=None, gate_tags=None,
          location="Remote", is_remote=True, source="linkedin")
    s.close()
    # exercise render on a job pulled through the real queue path (fully loaded,
    # then detached) — the same shape run_queue() hands to render_job.
    job = cli.get_queue()[0]
    panel = cli.render_job(job, 1, 1, {"surface_priority": 0, "surface": 1})
    assert panel is not None     # built a Panel without raising


# --- menu counts (SPEC §5a dashboard) --------------------------------------

def test_menu_counts_partitions_by_verdict_and_status(env):
    s = env()
    _seed(s, "prio", verdict="surface_priority")
    _seed(s, "surf", verdict="surface")
    _seed(s, "buried1", verdict="skip")
    _seed(s, "buried2", verdict="skip")
    _seed(s, "done", verdict="surface", status="applied")
    _seed(s, "decided-skip", verdict="skip", status="ignored")  # not buried anymore
    s.close()

    counts = cli.menu_counts()
    assert counts["queue"] == 2      # prio + surf, both still 'new'
    assert counts["buried"] == 2     # only the undecided skips
    assert counts["applied"] == 1
