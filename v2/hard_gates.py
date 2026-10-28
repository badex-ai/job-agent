#!/usr/bin/env python3
"""
Deterministic hard-skip gates — the thin safety net in front of the Qwen gate.

Measured fact (2026-06-11): only 7 of the 95 hand-labeled SKIPs are
mechanical-keyword decidable; the rest need semantic judgment. So these
gates only encode markers that are unambiguous on sight:

    india          — Location field names India / an Indian city
    inr_salary     — ₹ / LPA / INR / Rs amounts anywhere in the text
    unpaid         — explicit no-stipend / unpaid / no-compensation phrasing
    no_sponsorship — explicit refusal: "no sponsorship", "unable to sponsor",
                     "citizens only", "security clearance", "no visa"

Deliberately NOT gated here (Qwen's job, needs context):
    - "right to work" / "work authorization" — appears in benign boilerplate
    - onsite outside the candidate's country — soft skip when relocation is
      conditional
    - country-restricted remote ("LATAM only") — phrased a hundred ways
    - salary below the profile's floor — needs real parsing, not worth a
      brittle regex
    - stack/role mismatch — pure judgment

Which gates run is set by `hard_gates:` in shared/config/_profile.yml (all
four when the profile or the key is missing). A candidate based in India
would drop `india` and `inr_salary`, for example.

A gate firing on a job the user labeled APPLY or CONSIDER is a bug in the
gate, never in the label. Audit mode checks exactly that:

    .venv/bin/python v2/hard_gates.py        # audit against data/labels.json
"""

import re
from functools import cache
from pathlib import Path

import yaml

PROFILE_PATH = Path(__file__).resolve().parent.parent / "shared" / "config" / "_profile.yml"
ALL_GATES = ("india", "inr_salary", "unpaid", "no_sponsorship")

INDIA_LOC_RE = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|mumbai|pune|chennai|"
    r"noida|gurgaon|gurugram|delhi|kolkata|ahmedabad|kochi)\b", re.I)

INR_SALARY_RE = re.compile(
    r"₹\s?[\d,]+|\b\d+\s*LPA\b|\bINR\b|\bRs\.?\s*[\d,]+", re.I)

UNPAID_RE = re.compile(
    r"no\s+stipend|not\s+stipend\s+based|\bunpaid\b|no\s+compensation|"
    r"without\s+pay\b|volunteer\s+(?:position|role|basis|opportunity)", re.I)

NO_SPONSOR_RE = re.compile(
    r"no\s+sponsor\w*|unable\s+to\s+sponsor|not\s+able\s+to\s+sponsor|"
    r"without\s+sponsor\w*|sponsorship\s+(?:is\s+)?not\s+(?:available|provided|offered)|"
    r"(?:u\.?s\.?\s+)?citizens?\s+only|security\s+clearance|no\s+visa\b", re.I)


@cache
def enabled_gates(profile_path: Path = PROFILE_PATH) -> frozenset[str]:
    """Gates switched on in the profile; all of them if it says nothing."""
    if not profile_path.exists():
        return frozenset(ALL_GATES)
    gates = (yaml.safe_load(profile_path.read_text()) or {}).get("hard_gates")
    if gates is None:
        return frozenset(ALL_GATES)
    unknown = set(gates) - set(ALL_GATES)
    if unknown:
        raise ValueError(f"unknown hard_gates in {profile_path}: {sorted(unknown)}")
    return frozenset(gates)


def fired_gates(job: dict, enabled: frozenset[str] | None = None) -> list[str]:
    """Return the list of gate names that fire for a job.

    `job` needs `location`, `title`, `description` (labels.json schema or
    the production scraper's row — same field names).
    """
    on = enabled_gates() if enabled is None else enabled
    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    fired = []
    if "india" in on and INDIA_LOC_RE.search(job.get("location", "")):
        fired.append("india")
    if "inr_salary" in on and INR_SALARY_RE.search(text):
        fired.append("inr_salary")
    if "unpaid" in on and UNPAID_RE.search(text):
        fired.append("unpaid")
    if "no_sponsorship" in on and NO_SPONSOR_RE.search(text):
        fired.append("no_sponsorship")
    return fired


def hard_skip(job: dict) -> str | None:
    """First gate that fires, or None if the job survives."""
    fired = fired_gates(job)
    return fired[0] if fired else None


# ---- audit against the 200 hand labels ----------------------------------
def audit():
    import json
    from collections import Counter
    from pathlib import Path

    jobs = json.loads(
        (Path(__file__).parent.parent / "data" / "labels.json").read_text())
    by_gate, by_label = Counter(), Counter()
    bad = []
    survivors = Counter()
    for j in jobs:
        fired = fired_gates(j)
        if fired:
            by_label[j["label"]] += 1
            for g in fired:
                by_gate[g] += 1
            if j["label"] in ("APPLY", "CONSIDER"):
                bad.append((j["label"], fired, j["title"], j["notes"][:60]))
        else:
            survivors[j["label"]] += 1

    n_gated = sum(by_label.values())
    print(f"gated: {n_gated}/200   by gate: {dict(by_gate)}")
    print(f"gated jobs' labels:    {dict(by_label)}")
    print(f"survivors' labels:     {dict(survivors)}")
    skips = sum(1 for j in jobs if j["label"] == "SKIP")
    caught = by_label["SKIP"]
    print(f"SKIP coverage: {caught}/{skips} ({caught/skips:.0%}) — rest is Qwen's job")
    if bad:
        print(f"\n*** {len(bad)} FALSE FIRES on APPLY/CONSIDER — gate bug: ***")
        for label, fired, title, notes in bad:
            print(f"  [{label}] {fired} {title}")
            print(f"          note: {notes}")
    else:
        print("zero false fires on APPLY/CONSIDER — gates are safe")


if __name__ == "__main__":
    audit()
