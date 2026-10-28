#!/usr/bin/env python3
"""Seal a fresh held-out label set from the DB — never tune against it.

Why this exists: the 200 labels in data/labels.json are training-contaminated.
Every rubric tweak was fit to them, so a number measured on them no longer
proves an improvement (a change is only better if it improves the number on
held-out labels the prompt wasn't tuned against). This script
reserves a clean test set from the 1775 jobs that were never hand-labeled.

Two sets, each doing exactly one job — see the reasoning logged with the plan:

  HEADLINE (100, pure random over the whole pool)
    The trustworthy number. It carries the REAL base rate of incoming traffic,
    so "FP dropped from X to Y" on this set means what it says. This is the one
    we report.

  STRESS (50, stratified — survivors of the objective hard gates only)
    A diagnostic, not a headline. It concentrates the S2/S3 stack-and-role
    judgment cases where the eligibility-only re-scope is supposed to act, so a
    flaw in the re-scope shows up loudly here with far less labeling than
    finding those cases at random. Its base rate is distorted BY DESIGN, so its
    counts are never reported as a headline rate.

The two sets are disjoint (a job in HEADLINE is excluded from STRESS) so the
stress draw cannot corrupt the headline base rate or waste a label.

The objective hard gates (india / inr_salary / unpaid / no_sponsorship) come
from v2/hard_gates.py — the same crisp S1/S4/S5 eligibility markers the gate
re-scope keeps as auto-skips. A job that fires one is objectively ineligible;
the survivors are the judgment zone.

Output: data/heldout_unlabeled.json — id/title/company/location/description +
fired_gates per job, and NO label field. The user fills `label` by hand; once
labeled it is SEALED: measure against it, never tune against it.

Reproducible: fixed SEED, fixed selection recorded in the artifact. Re-running
with the same DB and seed reproduces the same two sets.

    .venv/bin/python v2/seal_heldout.py            # write the artifact
    .venv/bin/python v2/seal_heldout.py --dry-run  # print stats, write nothing
"""

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from v2.hard_gates import fired_gates  # noqa: E402

ROOT = Path(__file__).parent.parent
DB = ROOT / "data" / "jobs.db"
LABELS = ROOT / "data" / "labels.json"
OUT = ROOT / "data" / "heldout_unlabeled.json"

SEED = 20260624          # the day we sealed; fixed so the draw is reproducible
HEADLINE_N = 100
STRESS_N = 50
MIN_DESC_CHARS = 40      # below this a description is a stub, not labelable

GATE_FIELDS = (
    "id", "title", "company", "location", "is_remote", "description",
    "source", "scraped_at",
)


def build_pool() -> list[dict]:
    """Never-labeled DB jobs that carry a real (non-stub) description."""
    labeled = {str(r["id"]) for r in json.loads(LABELS.read_text())}
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cols = ", ".join(GATE_FIELDS)
    rows = con.execute(f"SELECT {cols} FROM jobs").fetchall()
    con.close()
    pool = []
    for r in rows:
        if str(r["id"]) in labeled:
            continue
        desc = r["description"] or ""
        if len(desc.strip()) < MIN_DESC_CHARS:
            continue
        pool.append(dict(r))
    return pool


def select(pool: list[dict]) -> tuple[list[dict], list[dict]]:
    """Draw the disjoint headline (random) and stress (survivors) sets."""
    rng = random.Random(SEED)

    # Headline: pure random over the whole pool — the real base rate.
    pool_sorted = sorted(pool, key=lambda j: str(j["id"]))  # stable order in
    headline = rng.sample(pool_sorted, HEADLINE_N)          # → reproducible draw
    headline_ids = {j["id"] for j in headline}

    # Stress: survivors of the objective hard gates, not already in headline.
    survivors = [
        j for j in pool_sorted
        if not fired_gates(j) and j["id"] not in headline_ids
    ]
    stress = rng.sample(survivors, STRESS_N)
    return headline, stress


def to_record(job: dict, set_name: str) -> dict:
    """One sealed, unlabeled record. `label` is intentionally absent —
    the user fills it; its absence marks the set as not-yet-labeled."""
    return {
        "set": set_name,
        "id": str(job["id"]),
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "is_remote": bool(job["is_remote"]),
        "description": job["description"],
        "fired_gates": fired_gates(job),
        "label": None,
        "notes": "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print stats, write nothing")
    args = ap.parse_args()

    if OUT.exists() and not args.dry_run:
        sys.exit(
            f"refusing to overwrite {OUT} — it may already hold your hand "
            f"labels. Delete it deliberately if you mean to re-seal."
        )

    pool = build_pool()
    headline, stress = select(pool)
    gated = sum(1 for j in pool if fired_gates(j))

    print(f"pool (never-labeled, real description): {len(pool)}")
    print(f"  objectively hard-gated: {gated}   survivors: {len(pool) - gated}")
    print(f"headline (random, base-rate truth):     {len(headline)}")
    print(f"stress   (survivors, S2/S3 judgment):   {len(stress)}")
    overlap = {j["id"] for j in headline} & {j["id"] for j in stress}
    assert not overlap, f"sets overlap: {overlap}"
    print("disjoint: yes")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    records = (
        [to_record(j, "headline") for j in headline]
        + [to_record(j, "stress") for j in stress]
    )
    payload = {
        "sealed": True,
        "seed": SEED,
        "headline_n": HEADLINE_N,
        "stress_n": STRESS_N,
        "pool_size": len(pool),
        "note": (
            "Held-out test set sealed from never-labeled DB jobs. Fill `label` "
            "by hand (APPLY/CONSIDER/HOLD/SKIP). Once labeled: measure against "
            "it, NEVER tune against it. Headline = base-rate truth (report this);"
            " stress = S2/S3 diagnostic (do not report as a headline rate)."
        ),
        "jobs": records,
    }
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nsealed → {OUT}  ({len(records)} jobs, unlabeled)")


if __name__ == "__main__":
    main()
