"""Tests for the gate system-prompt builder (T2).

These run against the committed example profile and rubric (a made-up
candidate), so they pass on a fresh clone and pin how each profile field
reaches the prompt.
"""

import pytest

from v2.gate_prompt import REPO_ROOT, build_system_prompt

EXAMPLE_PROFILE = REPO_ROOT / "shared" / "config" / "_profile.example.yml"
EXAMPLE_RUBRIC = REPO_ROOT / "v2" / "rubric.example.md"


@pytest.fixture(scope="module")
def prompt() -> str:
    return build_system_prompt(profile_path=EXAMPLE_PROFILE,
                               rubric_path=EXAMPLE_RUBRIC)


# --- candidate facts from the profile ---------------------------------------

def test_includes_location(prompt):
    assert "Bogotá" in prompt and "Colombia" in prompt


def test_s1_names_the_candidates_country(prompt):
    s1 = prompt[prompt.index("(1) S1:"):prompt.index("(2) S4:")]
    assert "Colombia" in s1


def test_includes_full_remote_nonnegotiable(prompt):
    assert "Full remote" in prompt and "non-negotiable" in prompt


def test_includes_real_stack(prompt):
    for token in ("Go", "Python", "PostgreSQL"):
        assert token in prompt, token


def test_marks_java_as_not_the_candidates_stack(prompt):
    # Java must appear specifically in the "NOT ... stack" framing, not as a skill.
    assert "NOT the candidate's stack" in prompt
    not_stack_line = next(
        line for line in prompt.splitlines() if "NOT the candidate's stack" in line
    )
    assert "Java" in not_stack_line


def test_includes_ic_level(prompt):
    assert "IC" in prompt and "Individual Contributor" in prompt


def test_includes_salary_floor(prompt):
    assert "40000" in prompt


def test_includes_deal_breakers(prompt):
    assert "equity-only" in prompt


def test_excluded_locations_reach_s5(tmp_path):
    profile = tmp_path / "p.yml"
    profile.write_text(EXAMPLE_PROFILE.read_text().replace(
        "excluded_locations: []", "excluded_locations: [Atlantis]"))
    out = build_system_prompt(profile_path=profile, rubric_path=EXAMPLE_RUBRIC)
    assert "S5: unpaid / no stipend / Atlantis-based?" in out
    assert "unpaid/Atlantis=S5" in out


def test_no_excluded_locations_leaves_s5_unpaid_only(prompt):
    assert "S5: unpaid / no stipend? -> skip" in prompt


# --- rubric tiers (§0–§3) injected -----------------------------------------

def test_includes_rubric_skip_tiers(prompt):
    for tier in ("S1", "S2", "S3"):
        assert tier in prompt, tier


def test_includes_surface_rules(prompt):
    # the surface-for-user cluster identifiers
    assert "C1" in prompt and "H1" in prompt


def test_includes_apply_profile_section(prompt):
    assert "APPLY profile" in prompt


# --- excludes rubric §4 (label conflicts) and §5 (meta-notes) ---------------

def test_excludes_label_conflicts_section(prompt):
    assert "Known label conflicts" not in prompt
    assert "Job 12" not in prompt  # a §4-only detail


def test_excludes_meta_section_about_this_prompt(prompt):
    assert "What this means for the Qwen prompt" not in prompt


# --- verdict semantics + steel-man -----------------------------------------

def test_includes_three_verdict_literals(prompt):
    for verdict in ("skip", "surface", "surface_priority"):
        assert verdict in prompt, verdict


def test_uncertainty_routes_to_surface(prompt):
    lowered = prompt.lower()
    assert "uncertainty" in lowered or "unsure" in lowered or "not sure" in lowered
    assert "when unsure, surface" in prompt.lower() or "-> surface" in prompt


def test_two_phase_procedure_checks_hard_gates_first(prompt):
    # the core fix: S1/S4/S5 are an explicit first pass before role judgment
    assert "PHASE 1" in prompt and "PHASE 2" in prompt
    p1 = prompt.index("PHASE 1")
    p2 = prompt.index("PHASE 2")
    assert p1 < p2, "phase 1 (hard gates) must come before phase 2 (judgment)"
    # S1 must be named in the phase-1 hard-gate checklist
    assert prompt.index("S1") < p2


def test_requires_evidence_quote_to_skip(prompt):
    # replaces the steel-man: must quote disqualifying text or surface
    lowered = prompt.lower()
    assert "quote" in lowered
    assert "cannot quote" in lowered or "do not skip" in lowered or \
        "does not fire" in lowered


def test_seniority_senior_lead_not_a_skip(prompt):
    # the S6 misfire fix must be visible in the prompt
    assert "Senior" in prompt and "Lead" in prompt
    assert "NOT a skip" in prompt or "not a skip" in prompt.lower()


def test_no_open_ended_steel_man(prompt):
    # the backfiring instruction is gone
    assert "argue the strongest case for the\nOPPOSITE" not in prompt
    assert "reasoning" in prompt


def test_does_not_repeat_json_only_instruction(prompt):
    # That caveat is owned by qwen_gate and appended there, not here.
    assert "Respond only in JSON" not in prompt


# --- S2 PRE-CHECK: hard-no-aware OR-list guard ------------------------------
# Live 14b 7-case probe (2026-06-21): the bare S2 rule over-fired on OR-lists
# that contained one of the candidate's languages (it quoted the whole 'PHP, Golang, or
# Node.js' list as evidence) — 17 of 42 baseline FPs were S2. The guard is two
# ordered questions, NOT looser prose: Q1 fires S2 only on a SINGULAR hard-no
# primary (so co-listed Python can't redeem a Java-primary role); Q2 surfaces
# when any of the candidate's languages appears anywhere in an OR-list.

def test_s2_precheck_present(prompt):
    assert "S2 PRE-CHECK" in prompt
    # it must sit inside the S2 rule, before S3 (where the model judges stack)
    assert prompt.index("S2 PRE-CHECK") < prompt.index("S3 =")


def test_s2_precheck_q1_fires_on_singular_hardno_primary(prompt):
    # Q1: a hard-no language as the ONE primary/required language fires S2,
    # and a co-listed Python must NOT redeem it (the Scotiabank correction).
    assert "primary language" in prompt
    assert "does not redeem" in prompt
    # Java/C#/.NET named as the hard-no set the guard reasons about
    pre = prompt[prompt.index("S2 PRE-CHECK"):prompt.index("S3 =")]
    for hardno in ("Java", "C#", ".NET"):
        assert hardno in pre, hardno


def test_s2_precheck_q2_surfaces_candidate_stack_in_or_list(prompt):
    # Q2: if any of the candidate's languages appears anywhere in the list ->
    # surface, explicitly naming the OR-list mistake the guard exists to stop.
    pre = prompt[prompt.index("S2 PRE-CHECK"):prompt.index("S3 =")]
    assert "Python" in pre  # a stack keyword from the profile
    assert "do NOT fire" in pre or "surface" in pre
    # the all-hard-no escape hatch stays: no candidate-stack language -> S2 fires
    assert "candidate-stack language appears anywhere" in pre


# --- the builder really reads the paths it is given -------------------------

def test_reads_given_paths(tmp_path):
    profile = tmp_path / "p.yml"
    profile.write_text(
        "location: SENTINEL_LOCATION\n"
        "country: SENTINEL_COUNTRY\n"
        "stack: SENTINEL_STACK\n"
        "stack_keywords: [SENTINEL_KEYWORD]\n"
        "not_stack: [SENTINEL_NOT_STACK]\n"
        "wrong_fields: SENTINEL_FIELDS\n"
        "priority_roles: SENTINEL_PRIORITY\n"
        "remote_requirement: SENTINEL_REMOTE\n"
        "relocation: SENTINEL_RELO\n"
        "salary_min: '99999'\n"
        "level_goal: SENTINEL_LEVEL\n"
        "deal_breakers: [SENTINEL_DEALBREAKER]\n"
    )
    rubric = tmp_path / "r.md"
    rubric.write_text(
        "## 0. head\nSENTINEL_RUBRIC\n\n"
        "## 4. conflicts\nSHOULD_NOT_APPEAR\n"
    )
    out = build_system_prompt(profile_path=profile, rubric_path=rubric)
    for sentinel in ("LOCATION", "COUNTRY", "STACK", "KEYWORD", "NOT_STACK",
                     "FIELDS", "PRIORITY", "REMOTE"):
        assert f"SENTINEL_{sentinel}" in out, sentinel
    assert "99999" in out
    assert "SENTINEL_RUBRIC" in out
    assert "SHOULD_NOT_APPEAR" not in out


def test_raises_on_missing_rubric_structure(tmp_path):
    profile = EXAMPLE_PROFILE
    bad = tmp_path / "bad.md"
    bad.write_text("# rubric with no numbered sections\n")
    with pytest.raises(ValueError):
        build_system_prompt(profile_path=profile, rubric_path=bad)


def test_example_files_exist():
    assert EXAMPLE_PROFILE.exists()
    assert EXAMPLE_RUBRIC.exists()


def test_firing_rule_forces_skip_verdict(prompt):
    # The measured FN class: model names a firing disqualifying rule, then
    # surfaces anyway. The prompt must hard-link a fired rule to a skip verdict.
    assert "Verdict MUST match your reasoning" in prompt
    assert "the\n`verdict` MUST be `skip`" in prompt
    # The rule must anchor to the 'Disqualifying rule:' line, not a bare mention,
    # and must only bind once the reasoning format that defines it is stated.
    assert "'Disqualifying rule:' line binds the verdict" in prompt
    assert prompt.index("Required reasoning format") < prompt.index(
        "Verdict MUST match your reasoning"
    )
