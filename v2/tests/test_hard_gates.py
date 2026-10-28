"""The profile decides which hard gates run."""

import pytest

from v2.hard_gates import ALL_GATES, enabled_gates, fired_gates

INDIA_UNPAID = {"location": "Pune, India", "title": "Intern",
                "description": "This is an unpaid internship."}


def test_missing_profile_runs_every_gate(tmp_path):
    assert enabled_gates(tmp_path / "absent.yml") == frozenset(ALL_GATES)


def test_profile_without_the_key_runs_every_gate(tmp_path):
    profile = tmp_path / "p.yml"
    profile.write_text("location: Somewhere\n")
    assert enabled_gates(profile) == frozenset(ALL_GATES)


def test_profile_list_switches_gates_off(tmp_path):
    profile = tmp_path / "p.yml"
    profile.write_text("hard_gates: [unpaid]\n")
    on = enabled_gates(profile)
    assert fired_gates(INDIA_UNPAID, on) == ["unpaid"]
    assert fired_gates(INDIA_UNPAID, frozenset(ALL_GATES)) == ["india", "unpaid"]


def test_unknown_gate_name_is_refused(tmp_path):
    profile = tmp_path / "p.yml"
    profile.write_text("hard_gates: [unpaid, atlantis]\n")
    with pytest.raises(ValueError):
        enabled_gates(profile)
