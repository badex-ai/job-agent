"""Tests for the webapp bucket-grouping read (v2/webapp/queries.py).

Pins the one genuinely new query: group gated jobs by Job.gate_verdict into the
browse buckets (surface_priority->APPLY, surface->CONSIDER, skip->SKIP), with
HOLD sourced from the Application table and applied/dismissed/held jobs filtered
out. Read-only; runs against an isolated temp DB so data/jobs.db is untouched.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job, migrate_applications
from v2.webapp import applications
from v2.webapp.queries import (
    BUCKETS, get_classified_jobs, get_applications, bucket_counts, list_cvs,
    cv_text, gate_health, _tags,
)


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)
    migrate_applications(engine)
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
        gate_run_at=datetime.utcnow(),
    )
    base.update(overrides)
    job = Job(**base)
    session.add(job)
    session.commit()
    return job


# --- grouping by gate verdict ----------------------------------------------

def test_groups_by_gate_verdict(Session):
    s = Session()
    _seed_job(s, id="j-apply", gate_verdict="surface_priority")
    _seed_job(s, id="j-consider", gate_verdict="surface")
    _seed_job(s, id="j-skip", gate_verdict="skip")

    buckets = get_classified_jobs(session=s)
    assert set(buckets) == set(BUCKETS)
    assert [j["id"] for j in buckets["APPLY"]] == ["j-apply"]
    assert [j["id"] for j in buckets["CONSIDER"]] == ["j-consider"]
    assert [j["id"] for j in buckets["SKIP"]] == ["j-skip"]
    assert buckets["HOLD"] == []  # HOLD is a manual action, not a gate verdict
    s.close()


def test_held_job_moves_to_hold_bucket(Session):
    """A held job leaves its gate bucket and appears under HOLD with its note."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.hold_job("j1", note="wait for salary band", session=s)

    buckets = get_classified_jobs(session=s)
    assert buckets["CONSIDER"] == []          # pulled from its gate bucket
    assert [j["id"] for j in buckets["HOLD"]] == ["j1"]
    assert buckets["HOLD"][0]["note"] == "wait for salary band"
    s.close()


def test_applied_and_dismissed_jobs_are_filtered_out(Session):
    """Applied/dismissed jobs vanish from the browse buckets entirely."""
    s = Session()
    _seed_job(s, id="j-applied", gate_verdict="surface_priority")
    _seed_job(s, id="j-dismissed", gate_verdict="surface")
    _seed_job(s, id="j-live", gate_verdict="surface")
    applications.apply_to_job("j-applied", session=s)
    applications.dismiss_job("j-dismissed", session=s)

    buckets = get_classified_jobs(session=s)
    ids = [j["id"] for v in buckets.values() for j in v]
    assert ids == ["j-live"]
    s.close()


def test_restored_job_returns_to_its_gate_bucket(Session):
    """Dismiss then restore: the job is back in its gate bucket, out of HOLD."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.hold_job("j1", session=s)
    applications.restore_job("j1", session=s)

    buckets = get_classified_jobs(session=s)
    assert [j["id"] for j in buckets["CONSIDER"]] == ["j1"]
    assert buckets["HOLD"] == []
    s.close()


def test_cli_applied_job_is_hidden_from_buckets(Session):
    """A job applied to via the CLI sets Job.applied but writes NO Application
    row. It must still leave the browse buckets (A1) — otherwise it lingers as a
    zombie card whose Apply button 409s forever."""
    s = Session()
    _seed_job(s, id="cli-applied", gate_verdict="surface_priority", applied=True)
    _seed_job(s, id="live", gate_verdict="surface_priority")
    buckets = get_classified_jobs(session=s)
    ids = [j["id"] for v in buckets.values() for j in v]
    assert ids == ["live"]     # the applied job is gone from every bucket
    s.close()


def test_applied_job_never_lands_in_hold(Session):
    """Applied == out of the worklist entirely, including HOLD."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface", applied=True)
    buckets = get_classified_jobs(session=s)
    assert all(v == [] for v in buckets.values())
    s.close()


def test_ungated_jobs_do_not_appear(Session):
    """A job the gate hasn't scored (gate_verdict NULL) is in no bucket."""
    s = Session()
    _seed_job(s, id="gated", gate_verdict="surface")
    _seed_job(s, id="ungated", gate_verdict=None)
    buckets = get_classified_jobs(session=s)
    ids = [j["id"] for v in buckets.values() for j in v]
    assert ids == ["gated"]
    s.close()


def test_unknown_verdict_is_skipped_not_crash(Session):
    """A verdict outside the three-valued space is dropped, not crashed on."""
    s = Session()
    _seed_job(s, id="weird", gate_verdict="maybe")
    buckets = get_classified_jobs(session=s)
    assert buckets == {b: [] for b in BUCKETS}
    s.close()


def test_empty_buckets_are_clean_not_error(Session):
    """No gated jobs at all -> all four buckets present and empty (plan §3)."""
    s = Session()
    buckets = get_classified_jobs(session=s)
    assert buckets == {b: [] for b in BUCKETS}
    assert bucket_counts(buckets) == {b: 0 for b in BUCKETS}
    s.close()


def test_newest_scored_first_within_bucket(Session):
    s = Session()
    _seed_job(s, id="older", gate_verdict="surface",
              gate_run_at=datetime.utcnow() - timedelta(hours=1))
    _seed_job(s, id="newer", gate_verdict="surface",
              gate_run_at=datetime.utcnow())
    buckets = get_classified_jobs(session=s)
    assert [j["id"] for j in buckets["CONSIDER"]] == ["newer", "older"]
    s.close()


# --- the job view projection -----------------------------------------------

def test_job_view_fields_and_priority_flag(Session):
    s = Session()
    _seed_job(s, id="jp", gate_verdict="surface_priority")
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
    _seed_job(s, id="jl", gate_verdict="surface",
              apply_link=None, job_url="https://example.com/job/xyz")
    view = get_classified_jobs(session=s)["CONSIDER"][0]
    assert view["apply_link"] == "https://example.com/job/xyz"
    s.close()


def test_missing_gate_reason_degrades(Session):
    s = Session()
    _seed_job(s, id="jr", gate_verdict="skip", gate_reason=None, gate_tags=None)
    view = get_classified_jobs(session=s)["SKIP"][0]
    assert view["gate_reason"] is None
    assert view["tags"] == []
    s.close()


def test_bucket_counts(Session):
    s = Session()
    for i in range(3):
        _seed_job(s, id=f"a{i}", gate_verdict="surface_priority")
    _seed_job(s, id="h0", gate_verdict="surface")
    applications.hold_job("h0", session=s)
    counts = bucket_counts(get_classified_jobs(session=s))
    assert counts == {"APPLY": 3, "CONSIDER": 0, "HOLD": 1, "SKIP": 0}
    s.close()


# --- the tracking tables (Applied / Dismissed) -----------------------------

def test_applied_job_appears_in_applied_table(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface_priority")
    applications.apply_to_job("j1", note="sent with devops CV", session=s)

    apps = get_applications(session=s)
    assert [r["id"] for r in apps["applied"]] == ["j1"]
    assert apps["dismissed"] == []
    row = apps["applied"][0]
    assert row["status"] == "applied"
    assert row["note"] == "sent with devops CV"
    assert row["applied_at"] is not None   # date set by mark_applied dual-write
    s.close()


def test_applied_row_carries_the_cv(Session):
    """B2: the CV sent on apply surfaces in the Applied table row."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface_priority")
    applications.apply_to_job("j1", cv="cv_devops", session=s)
    apps = get_applications(session=s)
    assert apps["applied"][0]["cv"] == "cv_devops"
    s.close()


def test_applied_row_cv_survives_a_status_change(Session):
    """The (job, cv, outcome) triple must survive the lifecycle: a status change
    keeps the recorded CV visible in the table."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.apply_to_job("j1", cv="cv_frontend", session=s)
    applications.set_status("j1", "interview", session=s)
    apps = get_applications(session=s)
    row = apps["applied"][0]
    assert row["status"] == "interview"
    assert row["cv"] == "cv_frontend"
    s.close()


def test_applied_row_reflects_latest_status(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.apply_to_job("j1", session=s)
    applications.set_status("j1", "interview", session=s)

    apps = get_applications(session=s)
    assert apps["applied"][0]["status"] == "interview"
    s.close()


def test_dismissed_job_appears_in_dismissed_table(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.dismiss_job("j1", note="wrong stack", session=s)

    apps = get_applications(session=s)
    assert [r["id"] for r in apps["dismissed"]] == ["j1"]
    assert apps["dismissed"][0]["note"] == "wrong stack"
    assert apps["applied"] == []
    s.close()


def test_held_job_is_in_neither_tracking_table(Session):
    """Held jobs live in the HOLD browse bucket, not the tracking tables."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.hold_job("j1", session=s)

    apps = get_applications(session=s)
    assert apps["applied"] == []
    assert apps["dismissed"] == []
    s.close()


def test_restored_job_leaves_dismissed_table(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.dismiss_job("j1", session=s)
    applications.restore_job("j1", session=s)

    apps = get_applications(session=s)
    assert apps["dismissed"] == []   # back in its gate bucket, off the table
    s.close()


def test_applications_newest_action_first(Session):
    s = Session()
    _seed_job(s, id="old", gate_verdict="surface")
    _seed_job(s, id="new", gate_verdict="surface")
    applications.apply_to_job("old", session=s)
    applications.apply_to_job("new", session=s)

    apps = get_applications(session=s)
    assert [r["id"] for r in apps["applied"]] == ["new", "old"]
    s.close()


def test_application_row_tolerates_missing_job(Session):
    """A tracked action on a job no longer in the jobs table still shows (id +
    note), rather than crashing or silently dropping the action."""
    s = Session()
    applications.dismiss_job("ghost", note="left over", session=s)

    apps = get_applications(session=s)
    row = apps["dismissed"][0]
    assert row["id"] == "ghost"
    assert row["title"] == "ghost"      # falls back to the id
    assert row["company"] is None
    assert row["note"] == "left over"
    s.close()


def test_empty_applications_are_clean(Session):
    s = Session()
    assert get_applications(session=s) == {"applied": [], "dismissed": []}
    s.close()


# --- editable notes (set_note) ---------------------------------------------

def test_set_note_on_applied_keeps_placement_and_status(Session):
    """Editing an applied job's note re-appends its placement event, so it stays
    in Applied with its current status — the note is not a move."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.apply_to_job("j1", session=s)
    applications.set_status("j1", "interview", session=s)
    applications.set_note("j1", "recruiter emailed", session=s)

    apps = get_applications(session=s)
    assert [r["id"] for r in apps["applied"]] == ["j1"]
    row = apps["applied"][0]
    assert row["status"] == "interview"        # status carried forward
    assert row["note"] == "recruiter emailed"
    s.close()


def test_set_note_on_dismissed_keeps_it_dismissed(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.dismiss_job("j1", note="old", session=s)
    applications.set_note("j1", "reconsider in Q3", session=s)

    apps = get_applications(session=s)
    assert [r["id"] for r in apps["dismissed"]] == ["j1"]
    assert apps["dismissed"][0]["note"] == "reconsider in Q3"
    s.close()


def test_set_note_empty_clears_the_note(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.apply_to_job("j1", note="draft", session=s)
    applications.set_note("j1", "   ", session=s)  # whitespace -> cleared

    apps = get_applications(session=s)
    assert apps["applied"][0]["note"] is None
    s.close()


def test_set_note_does_not_pull_job_from_hold_bucket(Session):
    """A note on a held job keeps it in HOLD (held is annotatable, not moved)."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.hold_job("j1", session=s)
    applications.set_note("j1", "waiting on band", session=s)

    buckets = get_classified_jobs(session=s)
    assert [j["id"] for j in buckets["HOLD"]] == ["j1"]
    assert buckets["HOLD"][0]["note"] == "waiting on band"
    s.close()


def test_set_note_on_untouched_job_raises(Session):
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    with pytest.raises(ValueError):
        applications.set_note("j1", "nope", session=s)
    s.close()


def test_set_note_on_restored_job_raises(Session):
    """A restored job is back in its gate bucket with no placement — nothing to
    annotate."""
    s = Session()
    _seed_job(s, id="j1", gate_verdict="surface")
    applications.dismiss_job("j1", session=s)
    applications.restore_job("j1", session=s)
    with pytest.raises(ValueError):
        applications.set_note("j1", "nope", session=s)
    s.close()


def test_event_meaning_sets_are_the_single_source(Session):
    """Finding 7: queries derives its hidden/held sets from applications'
    exported constants — the taxonomy lives in exactly one module. Guard against
    a literal creeping back by asserting the partition is coherent."""
    a = applications
    # every hidden event is exactly one of applied/dismissed/held, nothing else.
    assert a.HIDDEN_FROM_BUCKETS == (
        a.APPLIED_EVENTS | a.DISMISSED_EVENTS | a.HELD_EVENTS)
    # the three tab sets are disjoint (a latest event lands in one place).
    assert a.APPLIED_EVENTS.isdisjoint(a.DISMISSED_EVENTS)
    assert a.APPLIED_EVENTS.isdisjoint(a.HELD_EVENTS)
    assert a.DISMISSED_EVENTS.isdisjoint(a.HELD_EVENTS)
    # 'restored' returns a job to its bucket, so it hides nothing.
    assert "restored" not in a.HIDDEN_FROM_BUCKETS
    # the dead helper is gone (finding 7).
    assert not hasattr(a, "actioned_job_ids")


# --- the CV picker list ----------------------------------------------------

def test_list_cvs_globs_and_sorts(tmp_path):
    (tmp_path / "cv_devops.txt").write_text("x")
    (tmp_path / "cv_frontend.txt").write_text("x")
    (tmp_path / "cv_fullstack.txt").write_text("x")
    assert list_cvs(tmp_path) == ["cv_devops", "cv_frontend", "cv_fullstack"]


def test_list_cvs_ignores_non_cv_and_non_txt(tmp_path):
    (tmp_path / "cv_devops.txt").write_text("x")
    (tmp_path / "cv.txt").write_text("x")          # no underscore -> not a picker CV
    (tmp_path / "cv_notes.md").write_text("x")      # wrong extension
    (tmp_path / "jobs.db").write_text("x")
    assert list_cvs(tmp_path) == ["cv_devops"]


def test_list_cvs_missing_dir_is_empty(tmp_path):
    assert list_cvs(tmp_path / "nope") == []


# --- cv_text: raw CV contents for the copy-CV-text button -------------------

def test_cv_text_returns_the_file_contents(tmp_path):
    (tmp_path / "cv_devops.txt").write_text("DevOps CV body", encoding="utf-8")
    assert cv_text("cv_devops", tmp_path) == "DevOps CV body"


def test_cv_text_unknown_name_is_none(tmp_path):
    (tmp_path / "cv_devops.txt").write_text("x")
    assert cv_text("cv_nope", tmp_path) is None


def test_cv_text_rejects_path_traversal(tmp_path):
    """A traversal / absolute name must never escape the data dir: it isn't in
    list_cvs(), so it resolves to None rather than reading an arbitrary file."""
    (tmp_path / "cv_devops.txt").write_text("x")
    (tmp_path.parent / "secret.txt").write_text("SECRET")
    assert cv_text("../secret", tmp_path) is None
    assert cv_text("/etc/passwd", tmp_path) is None
    # a bare 'cv' (no underscore) is not a picker CV either.
    (tmp_path / "cv.txt").write_text("y")
    assert cv_text("cv", tmp_path) is None


# --- gate health (staleness banner) ----------------------------------------

def test_gate_health_empty_db_is_stale(Session):
    """No jobs at all: nothing pending, never gated, stale (no signal is stale)."""
    s = Session()
    h = gate_health(session=s)
    assert h == {"pending": 0, "total": 0, "last_gate_at": None, "stale": True}
    s.close()


def test_gate_health_counts_ungated_backlog(Session):
    """Ungated (gate_verdict NULL) rows are the pending backlog the buckets hide."""
    s = Session()
    _seed_job(s, id="gated", gate_verdict="surface")
    _seed_job(s, id="null-1", gate_verdict=None, gate_run_at=None)
    _seed_job(s, id="null-2", gate_verdict=None, gate_run_at=None)

    h = gate_health(session=s)
    assert h["pending"] == 2
    assert h["total"] == 3
    assert h["stale"] is False          # the one gated row is fresh (utcnow)
    s.close()


def test_gate_health_excludes_applied_from_pending(Session):
    """A CLI-applied job (Job.applied set, never gated) is out of the worklist,
    so it must not inflate the 'awaiting gate' count."""
    s = Session()
    _seed_job(s, id="applied-ungated", gate_verdict=None, gate_run_at=None,
              applied=True)
    _seed_job(s, id="real-pending", gate_verdict=None, gate_run_at=None)

    h = gate_health(session=s)
    assert h["pending"] == 1            # only the genuinely-waiting row
    s.close()


def test_gate_health_stale_when_last_run_old(Session):
    """A last gate run older than the threshold marks the pipeline stale even
    with nothing pending — the gate has stopped keeping up."""
    s = Session()
    old = datetime.utcnow() - timedelta(hours=48)
    _seed_job(s, id="old-gated", gate_verdict="surface", gate_run_at=old)

    h = gate_health(session=s)
    assert h["pending"] == 0
    assert h["last_gate_at"] == old.isoformat()
    assert h["stale"] is True
    s.close()


def test_gate_health_fresh_run_not_stale(Session):
    """A recent gate run within the threshold is not stale."""
    s = Session()
    recent = datetime.utcnow() - timedelta(hours=1)
    _seed_job(s, id="fresh", gate_verdict="surface", gate_run_at=recent)

    h = gate_health(session=s, stale_after=timedelta(hours=24))
    assert h["stale"] is False
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
