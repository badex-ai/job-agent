"""Qwen gate system-prompt builder (v2 Phase 4, T2).

Builds the system prompt the gate runs on, from two authored sources:

- `shared/config/_profile.yml` — the objective candidate fields (remote
  requirement, relocation, salary floor, level, deal-breakers). Read at
  build time so they cannot drift from the profile.
- `v2/rubric.md` — the eligibility rules derived from the 200 labels.
  Sections 0–3 are injected verbatim (rubric §5: "Inject §0 verbatim").
  Sections 4 (label conflicts awaiting the user) and 5 (meta-notes about
  this prompt) are excluded — they are notes to us, not rules for the model.

The §2 stack facts (real stack JS/TS + Python + infra/K8s; Java/C#/.NET etc.
are NOT the candidate's stack) are authored here because the
profile is interview prose and the design notes forbids inferring the stack
from CV-style text. The rendered prompt is pinned by tests so any drift
breaks the build instead of shipping silently.

The JSON-only instruction is owned by qwen_gate (the documented Ollama
caveat) and appended there; this builder does not repeat it.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_PATH = REPO_ROOT / "shared" / "config" / "_profile.yml"
DEFAULT_RUBRIC_PATH = REPO_ROOT / "v2" / "rubric.md"

# Stack facts authored from the design notes — the profile cannot express these
# (it is interview prose; §2 forbids deriving the stack from CV text).
NOT_HIS_STACK = ["Java", "C#", ".NET", "Kotlin", "Scala", "Ruby", "PHP"]


def _read_profile(profile_path: Path) -> dict:
    with open(profile_path) as f:
        return yaml.safe_load(f)


def _read_rubric_sections_0_to_3(rubric_path: Path) -> str:
    """Return rubric sections 0–3 verbatim, excluding §4 (label conflicts)
    and §5 (meta-notes about this prompt). Raise if the structure is missing
    — never ship a prompt with no rubric."""
    text = Path(rubric_path).read_text()
    start = re.search(r"^## 0\. ", text, re.MULTILINE)
    end = re.search(r"^## 4\. ", text, re.MULTILINE)
    if start is None or end is None:
        raise ValueError(
            f"rubric structure not found in {rubric_path}: "
            f"expected '## 0.' and '## 4.' section markers"
        )
    return text[start.start() : end.start()].rstrip()


def _candidate_block(profile: dict) -> str:
    not_stack = ", ".join(NOT_HIS_STACK)
    return (
        "## The candidate you are gating for\n"
        "You decide whether ONE specific person should see a job. A wrongly\n"
        "skipped good job is invisible to them forever — so when in doubt, surface.\n"
        "\n"
        "- Location: the candidate's city, the candidate's country.\n"
        f"- Remote: {profile['remote_requirement']}. "
        "A job 'remote' only inside countries the candidate is not in is a SKIP.\n"
        f"- Relocation: {profile['relocation']} — so onsite-abroad is a soft\n"
        "  concern, not an automatic skip.\n"
        "- Real working stack: JavaScript/TypeScript (React/Next/Node), Python\n"
        " , and infrastructure/Kubernetes, Terraform,\n"
        "  AWS/GCP. Moving toward platform/infra.\n"
        f"- NOT the candidate's stack: {not_stack}. A role whose PRIMARY\n"
        "  language is one of these is a wrong-stack skip; the same language merely\n"
        "  listed among others is not.\n"
        f"- Level: {profile['level_goal']}. Manager/director/VP is a soft skip.\n"
        f"- Salary floor: about ${profile['salary_min']}.\n"
        f"- Hard deal-breakers: {', '.join(profile['deal_breakers'])}; "
        "no-stipend/volunteer; India-based (location or ₹/LPA salary); explicit\n"
        "  'no sponsorship / must have work authorization'."
    )


def _verdict_block() -> str:
    return (
        "## Your three verdicts\n"
        "- `skip` — ONLY when a rubric rule S1–S6 clearly applies. Name the rule\n"
        "  (e.g. 'S1', 'S2') in `tags`. You may not skip on a guess.\n"
        "- `surface` — the DEFAULT and the required answer under ANY uncertainty.\n"
        "  Ambiguous remote scope, learnable gaps, seniority stretch, secondary-\n"
        "  stack mismatch, anything you are not sure about → surface, not skip.\n"
        "- `surface_priority` — a clean fit against the APPLY profile (§3): a\n"
        "  DevOps/platform/K8s/cloud-infra IC role, or full-stack/backend JS/TS-\n"
        "  Node-React, with no flags."
    )


def _steel_man_block() -> str:
    return (
        "## How to reason (steel-man, in this order)\n"
        "In the `reasoning` field, FIRST argue the strongest case for the\n"
        "OPPOSITE of the verdict you are leaning toward: before a skip, say why\n"
        "this job might be worth surfacing; before surfacing, say why it might be\n"
        "a trap. THEN commit to a verdict. If the opposite case is not decisively\n"
        "defeated, the verdict is `surface`."
    )


def build_system_prompt(
    profile_path: Path = DEFAULT_PROFILE_PATH,
    rubric_path: Path = DEFAULT_RUBRIC_PATH,
) -> str:
    """Render the full gate system prompt from the profile and rubric files."""
    profile = _read_profile(profile_path)
    rubric = _read_rubric_sections_0_to_3(rubric_path)
    blocks = [
        "You are an eligibility gate for a job-application pipeline.",
        _candidate_block(profile),
        "## Eligibility rubric (derived from 200 hand-labeled jobs)\n" + rubric,
        _verdict_block(),
        _steel_man_block(),
    ]
    return "\n\n".join(blocks)
