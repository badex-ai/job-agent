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
    # both get verdict skip with tag == the verdict ("skip"); craft tags via custom transport
    def t(url, payload, timeout):
        title_has = lambda s: s in payload["prompt"]
        tag = "S1" if title_has("J1") else "S2"
        return {"response": json.dumps({
            "reasoning": f"Disqualifying rule: {tag}.\nEvidence: '{DISQUALIFYING_TOKEN}'",
            "verdict": "skip", "confidence": 50, "tags": [tag]})}
    m = eval_gate.evaluate(jobs, "sys", transport=t)
    assert m["tier_slice"]["S1"] == {"n": 1, "correct": 1}  # J1 was a real SKIP
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
