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


def test_s1_guard_keeps_non_english_exclusion():
    # held-out (2026-07-08): the model correctly caught a Spanish residency
    # exclusion, but the English-only S1 regex read it as a bare place mention
    # and downgraded a CORRECT skip to surface — an FN leak. A residency
    # exclusion in any language must keep the skip.
    reasoning = ("Disqualifying rule: S1.\n"
                 "Evidence: 'Solo residentes en Perú, por la entrega de equipos.'")
    desc = "Analista Backend. Solo residentes en Perú, por la entrega de equipos."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "skip" and why is None


def test_s1_guard_keeps_based_locally_in_exclusion():
    # held-out (2026-07-08): 'based locally in the Philippines' is an explicit
    # exclusion, but the regex only had 'based in'/'located in', so 'based
    # locally in' slipped through and the correct skip was downgraded.
    reasoning = ("Disqualifying rule: S1.\n"
                 "Evidence: 'we require candidates to be based locally in the Philippines'")
    desc = ("Full Stack Engineer. This role is remote; however, we require "
            "candidates to be based locally in the Philippines.")
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "skip" and why is None


def test_s1_guard_still_downgrades_bare_place_after_extension():
    # guard the tripwire: the non-English/verb additions must NOT make a bare
    # place mention count as an exclusion. This is the FN->FP oscillation the
    # earlier rounds showed has no equilibrium — proving it stays inert.
    reasoning = "Disqualifying rule: S1.\nEvidence: 'Lima, Perú'"
    desc = "Backend Developer. Our office is in Lima, Perú. Fully remote worldwide."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "surface" and why is not None


def test_s1_guard_downgrades_bare_residentes():
    # regression (2026-07-08): the Spanish arm must be
    # noun+preposition ('residentes de/en'), NOT the bare noun 'residentes',
    # which matched benign inclusions ('residentes y no residentes bienvenidos')
    # and non-eligibility domains ('app para médicos residentes'). A bare
    # 'residentes' with no scoping preposition must stay a mention -> downgrade.
    reasoning = "Disqualifying rule: S1.\nEvidence: 'una app para residentes'"
    desc = "Full Stack Developer. Construimos una app para residentes. Remote."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
    assert eff == "surface" and why is not None


def test_s1_guard_keeps_solo_para_residentes_variant():
    # the tightened Spanish arm must still catch the 'solo PARA residentes en X'
    # phrasing, not only the bare 'solo residentes en X' — both are common in
    # Spanish job ads and both are real residency exclusions.
    reasoning = "Disqualifying rule: S1.\nEvidence: 'Solo para residentes en Perú'"
    desc = "Backend. Solo para residentes en Perú, por la entrega de equipos."
    eff, why = eval_gate.verify_evidence("skip", reasoning, desc, tags=["S1"])
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


# --- rescore: single-run/dual-score (2026-07-08) ---------------------------

def test_rescore_reproduces_a_live_run_exactly():
    # THE INVARIANT: re-scoring a saved run under the SAME guard must reproduce
    # that run's own metrics. This is what makes single-run/dual-score
    # trustworthy — the rescore path and the live path share one scorer, so an
    # unchanged guard cannot change FN/FP. If this ever fails, rescore has
    # diverged from evaluate() and no rescored delta can be believed.
    jobs = make_jobs([("A", "SKIP"), ("B", "APPLY"), ("C", "SKIP"), ("D", "HOLD")])
    t = transport_returning({"A": "skip", "B": "surface", "C": "surface", "D": "skip"})
    live = eval_gate.evaluate(jobs, "sys", transport=t)
    # feed the live run's own output back through rescore, same jobs/guard
    again = eval_gate.rescore(live, jobs)
    for k in ("n", "n_false_negatives", "n_false_positives",
              "skip_recall", "skip_precision", "surface_recall"):
        assert again[k] == live[k], f"{k}: rescore {again[k]} != live {live[k]}"


def test_rescore_reapplies_the_current_guard_to_raw_output():
    # rescore must re-derive the verdict from raw_verdict via the CURRENT
    # verify_evidence, not just copy the saved effective verdict. Here the saved
    # run has raw_verdict='skip' with an S1 quote that names a place but states
    # NO exclusion — the current guard downgrades it, so rescore must turn this
    # SKIP-labeled job into a leaked FN even though the saved 'verdict' was skip.
    jobs = [{"id": 1, "title": "Dev", "company": "", "location": "",
             "description": "Remote. Office in Lima, Peru.", "label": "SKIP",
             "notes": ""}]
    prior = {
        "results": [{
            "id": 1, "title": "Dev", "label": "SKIP",
            "verdict": "skip",                 # what the OLD guard recorded
            "raw_verdict": "skip",
            "reasoning": "Disqualifying rule: S1.\nEvidence: 'Lima, Peru'",
            "tags": ["S1"], "downgraded": None,
        }],
        "errors": [], "latency_avg_s": 5.0,
    }
    m = eval_gate.rescore(prior, jobs)
    # bare place mention -> current guard downgrades -> leaked SKIP
    assert m["n_downgraded_bad_evidence"] == 1
    assert m["n_false_negatives"] == 1


def test_rescore_fails_loudly_on_id_mismatch():
    prior = {"results": [{"id": 999, "title": "X", "label": "SKIP",
                          "verdict": "skip", "raw_verdict": "skip",
                          "reasoning": "", "tags": []}],
             "errors": [], "latency_avg_s": None}
    with pytest.raises(SystemExit):
        eval_gate.rescore(prior, make_jobs([("A", "SKIP")]))  # id 0, not 999


# --- eligibility-only re-scope (2026-07-14) --------------------------------
# The gate stops adjudicating stack/role TASTE (S2): a skip whose only basis is
# S2 is demoted to surface for the human. S3 is NOT demoted (17/17 on held-out).

def test_apply_scope_is_a_no_op_when_off():
    # the DEFAULT must be byte-identical to before: eligibility_only off leaves
    # every verdict — including a sole-S2 skip — untouched. This is what keeps
    # the baseline scoring path unchanged until the flag is explicitly set.
    v, why = eval_gate.apply_scope("skip", ["S2"], eligibility_only=False)
    assert v == "skip" and why is None


def test_apply_scope_demotes_sole_s2_skip():
    v, why = eval_gate.apply_scope("skip", ["S2"], eligibility_only=True)
    assert v == "surface" and "S2" in why


def test_apply_scope_keeps_eligibility_skips():
    # a skip citing ANY eligibility rule stands — the re-scope only sheds taste.
    for tag in ("S1", "S3", "S4", "S5", "S6"):
        v, why = eval_gate.apply_scope("skip", [tag], eligibility_only=True)
        assert v == "skip" and why is None, f"{tag} must not be demoted"


def test_apply_scope_keeps_s2_when_an_eligibility_rule_co_fires():
    # a skip tagged S2 AND S3 stands on S3 — S3 is a surviving eligibility basis,
    # so the taste rule co-occurring must not pull the whole skip down.
    v, why = eval_gate.apply_scope("skip", ["S2", "S3"], eligibility_only=True)
    assert v == "skip" and why is None


def test_apply_scope_never_touches_a_surface_verdict():
    v, why = eval_gate.apply_scope("surface", ["S2"], eligibility_only=True)
    assert v == "surface" and why is None


def test_eligibility_only_flips_a_buried_taste_job_to_a_recovered_surface():
    # end-to-end through evaluate(): a CONSIDER job the gate skips on S2 (a buried
    # good job = FP) is recovered to surface under eligibility_only, dropping FP.
    jobs = make_jobs([("KEEP", "CONSIDER"), ("GEO", "SKIP")])
    def t(url, payload, timeout):
        tag = "S2" if "KEEP" in payload["prompt"] else "S1"
        ev = "must be authorized to work in the US" if tag == "S1" else DISQUALIFYING_TOKEN
        desc_ev = f"\nEvidence: '{ev}'"
        return {"response": json.dumps({
            "reasoning": f"Disqualifying rule: {tag}.{desc_ev}",
            "verdict": "skip", "confidence": 50, "tags": [tag]})}
    base = eval_gate.evaluate(jobs, "sys", transport=t)
    resc = eval_gate.evaluate(jobs, "sys", transport=t, eligibility_only=True)
    # baseline buries the CONSIDER (S2) job -> 1 FP; eligibility-only recovers it
    assert base["n_false_positives"] == 1
    assert resc["n_false_positives"] == 0


def test_rescore_eligibility_only_reproduces_a_live_eligibility_only_run():
    # THE COMPOSITION INVARIANT: judging the re-scope OFFLINE (rescore a saved
    # baseline run with eligibility_only) must equal running the gate live with
    # eligibility_only. This is what lets us test the re-scope against a saved
    # held-out run with zero M4 heat — if it ever fails, the offline number is a
    # lie and we'd have to spend a live run to trust it.
    jobs = make_jobs([("KEEP", "CONSIDER"), ("GEO", "SKIP"), ("OK", "APPLY")])
    def t(url, payload, timeout):
        p = payload["prompt"]
        if "KEEP" in p: tag, verdict = "S2", "skip"
        elif "GEO" in p: tag, verdict = "S1", "skip"
        else: return {"response": json.dumps({
            "reasoning": "clean", "verdict": "surface", "confidence": 50, "tags": []})}
        ev = "must be authorized to work in the US" if tag == "S1" else DISQUALIFYING_TOKEN
        return {"response": json.dumps({
            "reasoning": f"Disqualifying rule: {tag}.\nEvidence: '{ev}'",
            "verdict": verdict, "confidence": 50, "tags": [tag]})}
    # one live baseline run (no re-scope), then judge the re-scope two ways
    baseline = eval_gate.evaluate(jobs, "sys", transport=t)
    live_rescope = eval_gate.evaluate(jobs, "sys", transport=t, eligibility_only=True)
    offline_rescope = eval_gate.rescore(baseline, jobs, eligibility_only=True)
    for k in ("n", "n_false_negatives", "n_false_positives",
              "skip_recall", "skip_precision", "surface_recall"):
        assert offline_rescope[k] == live_rescope[k], (
            f"{k}: offline {offline_rescope[k]} != live {live_rescope[k]}")


# --- stub transport (the offline VERIFY path) ------------------------------

def test_stub_transport_skips_on_hard_marker():
    r = eval_gate.stub_transport("u", {"prompt": "this is an unpaid role", "system": ""}, 1)
    assert json.loads(r["response"])["verdict"] == "skip"


def test_stub_transport_surfaces_clean_job():
    r = eval_gate.stub_transport("u", {"prompt": "remote devops role", "system": ""}, 1)
    assert json.loads(r["response"])["verdict"] == "surface"


# --- CLI smoke (offline, via stub) -----------------------------------------

def test_main_stub_writes_valid_json(tmp_path, monkeypatch):
    # the stub never reads the prompt; keep the test free of a personal profile
    monkeypatch.setattr(eval_gate, "build_system_prompt", lambda: "stub prompt")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        {"id": str(i), "title": f"Role {i}", "company": "Acme",
         "location": "Remote", "description": "Backend role, remote.",
         "label": label, "notes": ""}
        for i, label in enumerate(["APPLY", "CONSIDER", "HOLD", "SKIP", "SKIP"])
    ]))
    out = tmp_path / "r.json"
    rc = eval_gate.main(["--stub-model", "--labels", str(labels),
                         "--limit", "5", "--out", str(out)])
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


# --- pacing ----------------------------------------------------------------
# Pacing only engages on the live path (transport is None), so these patch
# gate_job directly rather than injecting a transport. The interval must be
# FIXED: an earlier adaptive arm keyed on latency-vs-running-minimum ratcheted
# to the cap on ordinary latency variance and cost 3.5x wall clock.

def _paced_run(monkeypatch, latencies, cooldown_s=60.0):
    """Run evaluate() on the live path with faked latencies; never sleeps.
    Returns the list of intervals actually passed to time.sleep()."""
    slept = []
    monkeypatch.setattr(eval_gate.time, "sleep", lambda s: slept.append(s))

    seq = iter(latencies)
    clock = [0.0]

    def fake_time():
        return clock[0]

    def fake_gate_job(**kw):
        clock[0] += next(seq)  # advance the clock => that job's measured dt
        return {"verdict": "surface", "reason": "r", "tags": [],
                "confidence": 90, "raw": {}}

    monkeypatch.setattr(eval_gate.time, "time", fake_time)
    monkeypatch.setattr(eval_gate, "gate_job", fake_gate_job)
    jobs = make_jobs([(f"J{i}", "APPLY") for i in range(len(latencies))])
    eval_gate.evaluate(jobs, "sys", cooldown_s=cooldown_s)
    return slept


def test_pacing_interval_is_fixed_regardless_of_latency_swings(monkeypatch):
    # The exact shape that pinned the old adaptive arm at the cap: one fast job
    # anchors a running-minimum baseline, then normal (slower) jobs trip
    # dt > 1.5*baseline forever. Interval must not move.
    slept = _paced_run(monkeypatch, [4.0, 8.0, 9.0, 12.0, 8.0, 30.0])
    assert slept == [60.0] * 6


def test_pacing_does_not_ratchet_on_a_long_run(monkeypatch):
    # 40 jobs of realistic jitter: the old arm reached 240s here.
    lat = [6.0, 9.0, 7.5, 11.0, 8.0] * 8
    slept = _paced_run(monkeypatch, lat)
    assert set(slept) == {60.0}
    assert max(slept) < eval_gate.EVAL_COOLDOWN_MAX_S


def test_pacing_honours_the_requested_interval(monkeypatch):
    # 70 jobs/hr => ~43s. The knob must be obeyed literally.
    slept = _paced_run(monkeypatch, [8.0, 8.0, 8.0], cooldown_s=43.0)
    assert slept == [43.0, 43.0, 43.0]


def test_pacing_off_by_default(monkeypatch):
    assert _paced_run(monkeypatch, [8.0, 8.0], cooldown_s=0.0) == []


def test_on_pace_reports_every_sleep(monkeypatch):
    # The adaptation was silent, which is why a 3.5x regression needed a
    # hardware thermal probe to find. Pacing must be observable in-band.
    seen = []
    monkeypatch.setattr(eval_gate.time, "sleep", lambda s: None)
    monkeypatch.setattr(eval_gate, "gate_job", lambda **kw: {
        "verdict": "surface", "reason": "r", "tags": [], "confidence": 90,
        "raw": {}})
    jobs = make_jobs([("A", "APPLY"), ("B", "APPLY")])
    eval_gate.evaluate(jobs, "sys", cooldown_s=30.0, on_pace=seen.append)
    assert len(seen) == 2
    assert all(e["cooldown_s"] == 30.0 for e in seen)
    assert all("latency_s" in e for e in seen)
