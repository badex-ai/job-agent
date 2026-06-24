"""Tests for the eval harness (T3).

The harness's value is its metric math — verdict->label alignment, the
SKIP/surface confusion matrix, the false-negative/false-positive lists, and
the resilience that keeps one network blip from discarding completed work.
All of that is pinned here with injected transports; no network is touched.
"""

import json

import pytest

from v2 import eval_gate
from v2.qwen_gate import GateError


# --- transport factories ---------------------------------------------------

def transport_returning(verdict_for_id: dict, default="surface"):
    """A fake transport that returns a chosen verdict per job id. The job id
    is recoverable from the prompt because build_job_prompt embeds the title;
    we instead key on a sentinel we put in the title -> see make_jobs."""
    def _t(url, payload, timeout):
        prompt = payload.get("prompt", "")
        verdict = default
        for sentinel, v in verdict_for_id.items():
            if sentinel in prompt:
                verdict = v
                break
        # when skipping, emit an Evidence quote that IS in the description
        # (DISQUALIFYING_TOKEN), so the evidence-guard lets the skip stand.
        ev = "" if verdict != "skip" else f"\nEvidence: '{DISQUALIFYING_TOKEN}'"
        return {"response": json.dumps({
            "reasoning": f"Disqualifying rule: {verdict}.{ev}",
            "verdict": verdict, "confidence": 50, "tags": [verdict],
        })}
    return _t


DISQUALIFYING_TOKEN = "requires security clearance"


def make_jobs(spec):
    """spec: list of (sentinel_title, label). Sentinel is unique per job so a
    transport can identify it from the rendered prompt. Descriptions contain
    DISQUALIFYING_TOKEN so a skip verdict's Evidence quote verifies."""
    return [
        {"id": i, "title": title, "company": "C", "location": "",
         "description": f"role desc {DISQUALIFYING_TOKEN} here",
         "label": label, "notes": ""}
        for i, (title, label) in enumerate(spec)
    ]


# --- alignment helpers -----------------------------------------------------

def test_is_surface_covers_both_surface_verdicts():
    assert eval_gate.is_surface("surface")
    assert eval_gate.is_surface("surface_priority")
    assert not eval_gate.is_surface("skip")


def test_label_is_surface_is_the_act_set():
    for lbl in ("APPLY", "CONSIDER", "HOLD"):
        assert eval_gate.label_is_surface(lbl)
    assert not eval_gate.label_is_surface("SKIP")


# --- confusion math: the core of the harness -------------------------------

def test_perfect_agreement_scores_100():
    jobs = make_jobs([("SKIPME", "SKIP"), ("KEEPME", "APPLY")])
    t = transport_returning({"SKIPME": "skip", "KEEPME": "surface_priority"})
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["n"] == 2
    assert m["skip_recall"] == 100.0
    assert m["skip_precision"] == 100.0
    assert m["surface_recall"] == 100.0
    assert m["n_false_negatives"] == 0
    assert m["n_false_positives"] == 0


def test_false_negative_is_a_leaked_skip():
    # user said SKIP, gate surfaced it -> false negative (the quiet leak)
    jobs = make_jobs([("LEAK", "SKIP")])
    t = transport_returning({"LEAK": "surface"})
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["skip_recall"] == 0.0
    assert m["n_false_negatives"] == 1
    assert m["false_negatives"][0]["title"] == "LEAK"
    assert m["false_negatives"][0]["verdict"] == "surface"
    assert m["n_false_positives"] == 0


def test_false_positive_is_a_buried_good_job():
    # user said CONSIDER (act-set), gate skipped it -> false positive (buried)
    jobs = make_jobs([("BURIED", "CONSIDER")])
    t = transport_returning({"BURIED": "skip"})
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["n_false_positives"] == 1
    assert m["false_positives"][0]["title"] == "BURIED"
    assert m["false_positives"][0]["label"] == "CONSIDER"
    assert m["surface_recall"] == 0.0
    assert m["n_false_negatives"] == 0


def test_hold_counts_as_act_set_not_skip():
    # HOLD must be surfaced; skipping it is a false positive (rubric sec 0)
    jobs = make_jobs([("HOLDJOB", "HOLD")])
    t = transport_returning({"HOLDJOB": "skip"})
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["n_false_positives"] == 1
    assert m["surface_recall_n"] == 1


def test_mixed_batch_confusion_counts():
    jobs = make_jobs([
        ("S_caught", "SKIP"),       # skip -> caught (tp)
        ("S_leaked", "SKIP"),       # surface -> false negative
        ("A_kept", "APPLY"),        # surface -> correct
        ("C_buried", "CONSIDER"),   # skip -> false positive
        ("H_kept", "HOLD"),         # surface -> correct
    ])
    t = transport_returning({
        "S_caught": "skip", "S_leaked": "surface", "A_kept": "surface_priority",
        "C_buried": "skip", "H_kept": "surface",
    })
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["n"] == 5
    assert m["skip_recall_n"] == 2          # two SKIP labels
    assert m["skip_recall"] == 50.0         # 1 of 2 caught
    assert m["skip_precision_n"] == 2       # gate emitted 2 skips
    assert m["skip_precision"] == 50.0      # 1 of 2 was a real SKIP
    assert m["surface_recall_n"] == 3       # APPLY+CONSIDER+HOLD
    assert m["n_false_negatives"] == 1
    assert m["n_false_positives"] == 1


def test_every_metric_carries_its_n():
    jobs = make_jobs([("X", "SKIP")])
    m = eval_gate.evaluate(jobs, "sys", transport=transport_returning({"X": "skip"}))
    for key in ("skip_recall_n", "skip_precision_n", "surface_recall_n",
                "n_false_negatives", "n_false_positives", "n", "n_errors"):
        assert key in m, key


def test_tier_slice_attributes_skips_to_cited_rule():
    jobs = make_jobs([("J1", "SKIP"), ("J2", "CONSIDER")])
    # both get verdict skip; craft tags via custom transport. Tags are S4/S2 (not
    # S1) because the clearance token carries no geographic-exclusion keyword and
    # the S1-only guard would (correctly) downgrade an S1 skip lacking one — this
    # test is about tier attribution, not S1 semantics.
    def t(url, payload, timeout):
        title_has = lambda s: s in payload["prompt"]
        tag = "S4" if title_has("J1") else "S2"
        return {"response": json.dumps({
            "reasoning": f"Disqualifying rule: {tag}.\nEvidence: '{DISQUALIFYING_TOKEN}'",
            "verdict": "skip", "confidence": 50, "tags": [tag]})}
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["tier_slice"]["S4"] == {"n": 1, "correct": 1}  # J1 was a real SKIP
    assert m["tier_slice"]["S2"] == {"n": 1, "correct": 0}  # J2 was CONSIDER


# --- resilience: the bug the live timeout exposed --------------------------

def test_transport_error_is_caught_not_fatal(monkeypatch):
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)  # no real backoff
    jobs = make_jobs([("A", "SKIP"), ("B", "APPLY")])

    def boom(url, payload, timeout):
        raise TimeoutError("simulated relay drop")

    m = eval_gate.evaluate(jobs, "sys", transport=boom)
    assert m["n"] == 0                       # no results, but no crash
    assert m["n_errors"] == 2
    assert all("transport" in e["error"] for e in m["errors"])


def test_malformed_output_recorded_as_malformed():
    jobs = make_jobs([("A", "SKIP")])

    def bad_json(url, payload, timeout):
        return {"response": "not json at all"}

    m = eval_gate.evaluate(jobs, "sys", transport=bad_json)
    assert m["n"] == 0
    assert m["n_errors"] == 1
    assert "malformed" in m["errors"][0]["error"]


def test_retry_recovers_after_transient_failure(monkeypatch):
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky(url, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("first attempt drops")
        return {"response": json.dumps({
            "reasoning": "x", "verdict": "skip", "confidence": 50, "tags": ["S1"]})}

    job = {"title": "T", "company": "", "location": "", "description": "d"}
    v = eval_gate._gate_with_retry(job, "sys", {"transport": flaky}, backoff=0)
    assert v["verdict"] == "skip"
    assert calls["n"] == 2  # failed once, succeeded on retry


def test_gate_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def always_malformed(url, payload, timeout):
        calls["n"] += 1
        return {"response": "{ broken"}

    job = {"title": "T", "company": "", "location": "", "description": "d"}
    with pytest.raises(GateError):
        eval_gate._gate_with_retry(job, "sys", {"transport": always_malformed}, backoff=0)
    assert calls["n"] == 1  # malformed output is deterministic -> no retry


# --- circuit breaker: abort a doomed batch when the tunnel drops -----------

def test_breaker_aborts_after_consecutive_transport_errors(monkeypatch):
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)
    jobs = make_jobs([(f"J{i}", "SKIP") for i in range(10)])

    def always_down(url, payload, timeout):
        raise TimeoutError("tunnel gone")

    with pytest.raises(eval_gate.TunnelDown) as ei:
        eval_gate.evaluate(jobs, "sys", transport=always_down,
                           abort_after_consecutive_errors=3)
    # aborted at the 3rd error, not after grinding all 10 jobs
    assert "3 consecutive" in str(ei.value)
    assert ei.value.partial == []  # no successful jobs before the drop


def test_breaker_resets_on_success(monkeypatch):
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)
    # outcome is keyed on the job's unique title, so the per-job retry loop
    # gives a consistent result within a job (retries don't shuffle a shared
    # iterator). Jobs J0,J1 fail, J2 succeeds (resets streak), J3,J4 fail,
    # J5 succeeds — never 3 errors in a row, so the breaker must NOT fire.
    jobs = make_jobs([(f"J{i}", "SKIP") for i in range(6)])
    fail = {"J0", "J1", "J3", "J4"}

    def flaky(url, payload, timeout):
        prompt = payload["prompt"]
        for name in fail:
            if name in prompt:
                raise TimeoutError("drop")
        return {"response": json.dumps({
            "reasoning": "Disqualifying rule: S2.\nEvidence: 'requires security clearance'",
            "verdict": "skip", "confidence": 50, "tags": ["S2"]})}

    m = eval_gate.evaluate(jobs, "sys", transport=flaky,
                           abort_after_consecutive_errors=3)
    assert m["n"] == 2  # J2 and J5 survived
    assert m["n_errors"] == 4


def test_breaker_ignores_malformed_output(monkeypatch):
    # GateError (model's fault) must NOT trip the tunnel breaker
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)
    jobs = make_jobs([(f"J{i}", "SKIP") for i in range(5)])

    def malformed(url, payload, timeout):
        return {"response": "{ not json"}

    m = eval_gate.evaluate(jobs, "sys", transport=malformed,
                           abort_after_consecutive_errors=2)
    assert m["n_errors"] == 5  # ran all 5, breaker never fired on malformed


def test_breaker_off_by_default(monkeypatch):
    monkeypatch.setattr(eval_gate.time, "sleep", lambda *_: None)
    jobs = make_jobs([(f"J{i}", "SKIP") for i in range(4)])

    def always_down(url, payload, timeout):
        raise TimeoutError("down")

    # default abort_after_consecutive_errors=None -> tolerate all, no raise
    m = eval_gate.evaluate(jobs, "sys", transport=always_down)
    assert m["n_errors"] == 4


# --- preflight reachability ------------------------------------------------

def test_preflight_ok_when_reachable(monkeypatch):
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(eval_gate.urllib.request, "urlopen", lambda *a, **k: Resp())
    ok, msg = eval_gate.preflight(probes=1)
    assert ok and "reachable" in msg


def test_preflight_fails_when_unreachable(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("no route")
    monkeypatch.setattr(eval_gate.urllib.request, "urlopen", boom)
    ok, msg = eval_gate.preflight(probes=2)
    assert not ok and "unreachable" in msg


# --- evidence-quote guard (the hallucinated-quote fix) ---------------------

def test_extract_evidence_pulls_quote():
    r = "Phase 1: S1 fire.\nDisqualifying rule: S1.\nEvidence: 'must be based in Europe'"
    assert eval_gate.extract_evidence(r) == "must be based in Europe"


def test_extract_evidence_none_when_absent():
    assert eval_gate.extract_evidence("no evidence line here") is None


def test_guard_downgrades_fabricated_quote():
    # model "quoted" a rubric example phrase that is NOT in this posting
    reasoning = "Disqualifying rule: S1.\nEvidence: 'only for LATAM'"
    desc = "Kubernetes Engineer (100% Remote). McLean VA. Join our team."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc)
    assert eff == "surface"
    assert why and "fabricated" in why


def test_guard_keeps_real_quote():
    reasoning = "Disqualifying rule: S1.\nEvidence: 'must be based in Europe or Ukraine'"
    desc = "Backend role. Candidates must be based in Europe or Ukraine. NestJS."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc)
    assert eff == "skip"
    assert why is None


def test_guard_keeps_quote_from_title():
    # 14b seen-200 (2026-06-16): on wrong-field skips the model proves S3 by
    # quoting the job TITLE (a real field it was shown), which is absent from
    # the description body. The old guard called these fabricated and buried
    # 15 correct skips (SOAR/HPE NonStop/Data Engineer ...). The title must be
    # part of the haystack.
    reasoning = "Disqualifying rule: S3.\nEvidence: 'Remote SOAR Engineer'"
    desc = "Join our security operations team building SOAR playbooks."
    eff, why = eval_gate.verify_evidence(
        "skip", reasoning, desc, tags=["S3"], title="Remote SOAR Engineer")
    assert eff == "skip"
    assert why is None


def test_guard_keeps_one_real_fragment_of_multipart_quote():
    # 14b emits several quoted snippets joined by commas; any one real snippet
    # proves the skip. The joined string isn't a literal substring, so the old
    # single-string check failed all of them and called the skip fabricated.
    reasoning = ("Disqualifying rule: S3.\n"
                 "Evidence: 'Remote SOAR Engineer', 'SOAR technologies'")
    desc = "We operate SOAR technologies across the security stack."
    eff, why = eval_gate.verify_evidence(
        "skip", reasoning, desc, tags=["S3"], title="Remote SOAR Engineer")
    assert eff == "skip"
    assert why is None


def test_guard_still_downgrades_when_no_fragment_verifies():
    # the multipart leniency must not become a fabrication loophole: if NONE of
    # the fragments appear in title+description, the skip is still invalid.
    reasoning = ("Disqualifying rule: S3.\n"
                 "Evidence: 'only for LATAM', 'must reside in Brazil'")
    desc = "Kubernetes Engineer (100% Remote). McLean VA. Join our team."
    eff, why = eval_gate.verify_evidence(
        "skip", reasoning, desc, tags=["S3"], title="Kubernetes Engineer")
    assert eff == "surface"
    assert why and "fabricated" in why


def test_s1_guard_downgrades_bare_place_mention():
    # live n=100 (2026-06-15): the 7B fired S1 quoting bare 'United States' /
    # 'Remote' — a real substring but a mention, not an exclusion. Buried APPLYs.
    reasoning = "Phase 1: S1 fired.\nDisqualifying rule: S1.\nEvidence: 'United States'"
    desc = "Backend Developer. Remote. We hire across the United States and beyond."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "surface"
    assert why and "no exclusion" in why


def test_s1_guard_downgrades_bare_remote():
    reasoning = "Disqualifying rule: S1.\nEvidence: 'Location: Remote'"
    desc = "Platform Engineer. Location: Remote. Kubernetes, Terraform, AWS."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "surface" and why is not None


def test_s1_guard_downgrades_short_bare_mention():
    # regression: live 0-99 (2026-06-15) buried two APPLY jobs on a 6-char
    # 'Evidence: Remote'. The short-quote (<8) leniency was short-circuiting
    # the S1 exclusion check — a bare 'Remote' must still downgrade.
    reasoning = "Disqualifying rule: S1.\nEvidence: Remote"
    desc = "Software Developer. Remote. Build APIs in Node and Python."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "surface" and why is not None


def test_s1_guard_keeps_real_exclusion():
    reasoning = ("Disqualifying rule: S1.\n"
                 "Evidence: 'Only applicants in the United States will be considered'")
    desc = "Notes: Only applicants in the United States will be considered for this role."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "skip" and why is None


def test_s1_veto_skipped_when_another_rule_also_cited():
    # regression: the 7B mistagged wrong-field skips as S1 + the real rule.
    # If a non-geographic rule (S3) is also cited, the skip stands on THAT
    # rule and the S1 quote-quality veto must NOT override it.
    reasoning = ("Disqualifying rule: S3.\n"
                 "Evidence: 'United States'")  # bare place, but S3 is the real reason
    desc = "Scrum Engineer based in the United States. Agile ceremonies, sprints."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1", "S3"])
    assert eff == "skip" and why is None


def test_s1_veto_still_fires_when_s1_is_sole_basis():
    reasoning = "Disqualifying rule: S1.\nEvidence: 'United States'"
    desc = "Engineer. We hire across the United States and elsewhere. Node, Python."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "surface" and why is not None


def test_s1_exclusion_check_only_applies_to_s1():
    # an S2 wrong-stack skip legitimately quotes a language name with no
    # exclusion keyword — the S1-only check must not touch it.
    reasoning = "Disqualifying rule: S2.\nEvidence: 'primary language is Java'"
    desc = "Senior Engineer. The primary language is Java across the platform."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S2"])
    assert eff == "skip" and why is None


def test_guard_ignores_surface_verdicts():
    eff, why = eval_gate.verify_evidence("surface", "anything", "anything")
    assert eff == "surface" and why is None


def test_guard_downgrades_skip_with_no_evidence():
    eff, why = eval_gate.verify_evidence("skip", "Disqualifying rule: none.", "desc")
    assert eff == "surface" and "no Evidence" in why


def test_fabricated_quote_does_not_count_as_caught_skip():
    # end-to-end: a SKIP-labeled job the model skips on a FAKE quote should
    # become a false negative (surfaced), not a true positive — the guard
    # must not let hallucination inflate skip_recall.
    jobs = [{"id": 1, "title": "Kubernetes engineer", "company": "", "location": "",
             "description": "100% Remote. McLean VA.", "label": "SKIP", "notes": ""}]
    def t(url, payload, timeout):
        return {"response": json.dumps({
            "reasoning": "Disqualifying rule: S1.\nEvidence: 'only for LATAM'",
            "verdict": "skip", "confidence": 1, "tags": ["S1"]})}
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["n_downgraded_bad_evidence"] == 1
    # downgraded to surface -> this SKIP is now a leaked false negative
    assert m["n_false_negatives"] == 1


# --- stub transport (the offline VERIFY path) ------------------------------

def test_stub_transport_skips_on_hard_marker():
    r = eval_gate.stub_transport("u", {"prompt": "this is an unpaid role", "system": ""}, 1)
    assert json.loads(r["response"])["verdict"] == "skip"


def test_stub_transport_surfaces_clean_job():
    r = eval_gate.stub_transport("u", {"prompt": "remote devops role", "system": ""}, 1)
    assert json.loads(r["response"])["verdict"] == "surface"


# --- CLI smoke (offline, via stub) -----------------------------------------

def test_main_stub_writes_valid_json(tmp_path):
    out = tmp_path / "r.json"
    rc = eval_gate.main(["--stub-model", "--limit", "5", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    for key in ("n", "skip_recall", "false_negatives"):
        assert key in data
    assert data["n"] == 5
    assert data["mode"] == "STUB"


# --- labeled-file loader (--labels held-out support) -----------------------

def test_load_labeled_jobs_reads_bare_list(tmp_path):
    p = tmp_path / "bare.json"
    p.write_text(json.dumps([{"id": "1", "label": "SKIP"},
                             {"id": "2", "label": "APPLY"}]))
    jobs = eval_gate.load_labeled_jobs(p)
    assert [j["label"] for j in jobs] == ["SKIP", "APPLY"]


def test_load_labeled_jobs_reads_jobs_wrapper(tmp_path):
    # the sealed held-out shape: {"jobs": [...]}
    p = tmp_path / "wrap.json"
    p.write_text(json.dumps({"sealed": True,
                             "jobs": [{"id": "1", "label": "HOLD"}]}))
    jobs = eval_gate.load_labeled_jobs(p)
    assert len(jobs) == 1 and jobs[0]["label"] == "HOLD"


def test_load_labeled_jobs_refuses_unlabeled(tmp_path):
    # held-out file before labeling carries label: null -> must fail loudly,
    # never silently score a null label
    p = tmp_path / "unl.json"
    p.write_text(json.dumps({"jobs": [{"id": "1", "label": None},
                                      {"id": "2", "label": "SKIP"}]}))
    with pytest.raises(SystemExit) as e:
        eval_gate.load_labeled_jobs(p)
    assert "no valid label" in str(e.value)


def test_load_labeled_jobs_refuses_unknown_label(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([{"id": "1", "label": "MAYBE"}]))
    with pytest.raises(SystemExit):
        eval_gate.load_labeled_jobs(p)
