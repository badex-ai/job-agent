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
        "- `skip` — ONLY when a rubric rule S1-S6 clearly applies AND you can\n"
        "  quote the disqualifying text. Name the rule (e.g. 'S1') in `tags`.\n"
        "- `surface` — the DEFAULT and the required answer under ANY uncertainty.\n"
        "  Ambiguous remote scope, learnable gaps, seniority (Senior/Sr./Lead),\n"
        "  secondary-stack mismatch, anything you are not sure about -> surface.\n"
        "- `surface_priority` — a clean fit against the APPLY profile: a DevOps/\n"
        "  platform/K8s/cloud-infra IC role, or full-stack/backend JS/TS-Node-\n"
        "  React, with no flags."
    )


def _procedure_block() -> str:
    return (
        "## How to decide (do these in order, do not skip phase 1)\n"
        "PHASE 1 - objective hard gates. BEFORE you assess role fit, check the\n"
        "DESCRIPTION TEXT for these facts only:\n"
        "  (1) S1: does the description explicitly state that applicants MUST be\n"
        "      located in, or authorized to work in, a specific country/region\n"
        "      that does NOT include the candidate's country or 'worldwide'? Fire S1 only on an\n"
        "      explicit exclusionary requirement, not on a mention of a place.\n"
        "  (2) S4: explicit security clearance or visa demanded? -> skip, tag S4.\n"
        "  (3) S5: unpaid / no stipend / India-based? -> skip, tag S5.\n"
        "If any phase-1 gate fires, STOP and skip. Do not let an interesting\n"
        "role talk you out of a hard eligibility failure.\n"
        "\n"
        "WHAT IS NOT AN S1 RESTRICTION (do NOT tag S1 for these):\n"
        "  - the 'Location metadata:' line above is NOT evidence of restriction;\n"
        "    only explicit exclusion text inside the description counts.\n"
        "  - a company being 'based in' a place, a city in the title, an office\n"
        "    location, 'remote', '100% remote', or a time-zone preference.\n"
        "  - a region named WITHOUT an explicit 'must be located/authorized'\n"
        "    requirement -> that is ambiguous, which means surface, not skip.\n"
        "  - CRITICAL: a wrong-field or wrong-stack role is NEVER S1. If you want\n"
        "    to skip because the ROLE doesn't fit (scrum, UI/UX, data, security,\n"
        "    SAP, Java-primary, etc.), that is S2 or S3 — NOT S1. S1 means ONE\n"
        "    thing only: an explicit geographic/work-authorization exclusion. Do\n"
        "    not tag S1 and quote a country name as a stand-in for 'I want to\n"
        "    skip this'. Tag the rule that matches your ACTUAL reason.\n"
        "\n"
        "PHASE 2 - role judgment (only if all phase-1 gates pass). Assess stack\n"
        "(S2), field/role-type (S3), and Staff/Principal level (S6). These are\n"
        "SURFACE-BIASED: skip only on a clear, quotable match; when unsure,\n"
        "surface. Senior/Sr./Lead is NOT a skip. JS/TS/React/Next/Node/Python/\n"
        "Kubernetes are THE CANDIDATE'S stack - never skip those as wrong-stack.\n"
        "  - S2 = the PRIMARY language/platform is one the candidate does not work in (Java,\n"
        "    C#, .NET, SAP, OutSystems, etc.). Quote the language/platform.\n"
        "    S2 PRE-CHECK (do BEFORE firing S2): list every concrete language/\n"
        "    platform in the requirement you would quote, then answer IN ORDER:\n"
        "    Q1 - does the role demand a HARD-NO language (Java, C#, .NET, SAP,\n"
        "      OutSystems) as its ONE primary/required language - a SINGULAR\n"
        "      demand like 'X as your primary language', 'must be a strong X\n"
        "      developer', 'X is required'? (A hard-no language as ONE option in\n"
        "      an 'A, B, or C' list is NOT a singular demand - that is Q2.) If\n"
        "      YES -> S2 fires; co-listed Python does not redeem a hard-no\n"
        "      primary.\n"
        "    Q2 - otherwise, is at least one of THE CANDIDATE'S languages (JavaScript,\n"
        "      TypeScript, React, Next, Node, Node.js, Python, Kubernetes,\n"
        "      Terraform, AWS, GCP, HTML, CSS, Bash) present ANYWHERE in that\n"
        "      list? If YES -> reachable with the candidate's stack -> surface, do NOT fire\n"
        "      S2. (Quoting a whole 'A, B, or C' list as S2 evidence when one\n"
        "      option is the candidate's stack is the mistake this check stops.) If NO\n"
        "      candidate-stack language appears anywhere in the required list -> S2\n"
        "      fires (wrong stack).\n"
        "  - S3 = wrong field/role type (data, mobile, security-specialist, QA,\n"
        "    UI/UX-led, DevRel, scrum, systems/field engineering). Quote the\n"
        "    phrase that shows the role's focus.\n"
        "\n"
        "## Required reasoning format\n"
        "Write `reasoning` as exactly three parts, in this order:\n"
        "  1. 'Phase 1:' - state the result of each hard gate (pass/fire).\n"
        "  2. 'Disqualifying rule:' - the single rule that fires, or 'none'. Pick\n"
        "     the rule that matches your REAL reason for skipping: geographic\n"
        "     exclusion=S1, wrong primary stack=S2, wrong field/role=S3,\n"
        "     clearance/visa=S4, unpaid/India=S5, Staff/Principal title=S6.\n"
        "  3. 'Evidence:' - copy a SHORT EXACT SUBSTRING of THIS posting's\n"
        "     description that proves the firing rule. It must be text that\n"
        "     actually appears in the description above, word for word, and it\n"
        "     must actually demonstrate THAT rule (for S1, real exclusion text;\n"
        "     for S2/S3, the language or role-focus phrase). Do NOT write an\n"
        "     example phrase, do NOT paraphrase, do NOT invent text. If no real\n"
        "     substring proves the rule, the rule does not fire -> surface.\n"
        "Then set `verdict`. This replaces open-ended deliberation: cite the rule\n"
        "that matches your real reason, with a real quote from THIS posting, or\n"
        "surface."
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
        _procedure_block(),
    ]
    return "\n\n".join(blocks)
