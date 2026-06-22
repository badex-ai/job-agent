"""Export the append-only `decisions` table to a labels.json-shaped training
file (SPEC §7 / §12 step 6) — the last load-bearing piece of the apply/triage CLI.

The CLI captures one immutable Decision row per human triage action. This is the
seam that turns those rows back into ground truth the rubric-derivation process
can read: it joins each Decision to its Job (the Decision stores only a job_id +
a frozen gate snapshot, NOT the job text the rubric needs) and emits a list of
fat records keyed exactly like `data/labels.json` on the load-bearing fields
(`id, title, company, location, is_remote, description, label, notes`).

What this deliberately does NOT copy from labels.json: `v1_score`, `v2_score`,
`*_action`, `diff`, `tag`, `n` — v2 has no score (SPEC §3). Emitting them would
be fabricating data. Instead each record carries the *real* v2 signal frozen on
the Decision: `gate_verdict`, `gate_tags`, `gate_reason`, `source_view`,
`human_rule`, `decided_at`.

Three structural rules, each tied to a §7 failure mode:

  * De-dup by latest decision (C3, streaming-label noise). The table is
    append-only; re-deciding a job inserts a SECOND row. The training file must
    NOT contain a stale label and its correction as two contradictory examples,
    so we keep the latest `decided_at` per `job_id` and report how many older
    rows were superseded.
  * Surface the selection-context split (C1, selection bias). The export reports
    queue- vs rescue-sourced label counts. A surfaced-only set teaches the gate
    to confirm its own false-negatives; this is where that's visible BEFORE
    anyone retrains.
  * Held-out split, opt-in and deterministic (§6, no tuning on the test set).
    `--holdout FRACTION` partitions by a seeded hash of `job_id`, so a given job
    lands in the same split every run as the set grows — no leakage. Default off.

Export only; the actual rubric re-derivation is a separate, later task (§11).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import argparse
import hashlib
from typing import List, Dict, Optional, Tuple

from shared.database.models import Job, Decision, get_session

# The load-bearing keys the rubric tooling reads out of labels.json. The export
# guarantees these; the v2-specific keys below are added, the v1 score keys are
# intentionally dropped (v2 has no score — SPEC §3).
SHARED_LABEL_KEYS = (
    "id", "title", "company", "location", "is_remote", "description",
    "label", "notes",
)


def _latest_per_job(decisions: List[Decision]) -> Tuple[List[Decision], int]:
    """Collapse the append-only table to one row per job_id — the latest by
    decided_at (id breaks ties; autoincrement id is monotonic with insert order,
    so it's the correct tiebreaker when two rows share a timestamp). Returns the
    kept rows and the count of superseded (older) rows so the caller can report
    that corrections happened (SPEC §7/C3)."""
    latest: Dict[str, Decision] = {}
    superseded = 0
    for d in decisions:
        prev = latest.get(d.job_id)
        if prev is None:
            latest[d.job_id] = d
            continue
        # newer wins; "newer" = later decided_at, or higher id on a tie
        prev_key = (prev.decided_at, prev.id)
        this_key = (d.decided_at, d.id)
        if this_key >= prev_key:
            latest[d.job_id] = d
        superseded += 1
    return list(latest.values()), superseded


def _holdout_split(job_id: str, fraction: float) -> str:
    """Deterministic train/holdout assignment by a seeded hash of job_id. The
    same job always lands in the same split across runs (no leakage as the set
    grows). fraction is the holdout share in [0, 1)."""
    if fraction <= 0:
        return "train"
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    # map the first 8 hex digits to [0, 1)
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return "holdout" if bucket < fraction else "train"


def _record(decision: Decision, job: Optional[Job], holdout: float) -> Dict:
    """One labels.json-shaped record: the job text (joined) + the human label +
    the frozen v2 gate signal. Tolerates a missing Job (the decision's job_id no
    longer in the DB — keep the label, it's still ground truth) by emitting the
    shared keys as None rather than dropping the row."""
    gate_tags = decision.gate_tags
    if isinstance(gate_tags, str):
        try:
            gate_tags = json.loads(gate_tags)
        except (json.JSONDecodeError, TypeError):
            pass  # leave the raw string if it isn't valid JSON

    rec = {
        # --- shared labels.json keys (the rubric tooling reads these) ---
        "id": decision.job_id,
        "title": job.title if job else None,
        "company": job.company if job else None,
        "location": job.location if job else None,
        "is_remote": job.is_remote if job else None,
        "description": job.description if job else None,
        "label": decision.human_label,
        "notes": decision.note,
        # --- v2-specific signal frozen on the Decision (no score in v2) ---
        "gate_verdict": decision.gate_verdict,
        "gate_tags": gate_tags,
        "gate_reason": decision.gate_reason,
        "human_rule": decision.human_rule,
        "source_view": decision.source_view,
        "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
        "split": _holdout_split(decision.job_id, holdout),
    }
    return rec


def build_export(session=None, *, holdout: float = 0.0) -> Dict:
    """Headless core (no I/O): read the decisions table, join each to its Job,
    collapse to the latest decision per job, and return a dict with the records
    plus the summary the caller prints. Pure enough to unit-test against a temp
    DB; the CLI wrapper does the file write.

    Returns:
        {
          "records": [ <labels.json-shaped dict>, ... ],   # latest per job
          "summary": {
            "total_decisions": int,    # rows in the table
            "unique_jobs": int,        # records emitted
            "superseded": int,         # older rows dropped (re-decides)
            "by_source": {"queue": int, "rescue": int},  # selection context (C1)
            "by_label": {"APPLY": int, "CONSIDER": int, "HOLD": int, "SKIP": int},
            "by_split": {"train": int, "holdout": int},
            "missing_job": int,        # decisions whose job_id isn't in jobs
          },
        }
    """
    own = session is None
    session = session or get_session()
    try:
        decisions = session.query(Decision).all()
        kept, superseded = _latest_per_job(decisions)
        # stable, reviewable order: most recent decision first
        kept.sort(key=lambda d: (d.decided_at or 0, d.id), reverse=True)

        records = []
        missing_job = 0
        for d in kept:
            job = session.get(Job, d.job_id)
            if job is None:
                missing_job += 1
            records.append(_record(d, job, holdout))

        by_source = {"queue": 0, "rescue": 0}
        by_label = {"APPLY": 0, "CONSIDER": 0, "HOLD": 0, "SKIP": 0}
        by_split = {"train": 0, "holdout": 0}
        for r in records:
            if r["source_view"] in by_source:
                by_source[r["source_view"]] += 1
            if r["label"] in by_label:
                by_label[r["label"]] += 1
            by_split[r["split"]] = by_split.get(r["split"], 0) + 1

        return {
            "records": records,
            "summary": {
                "total_decisions": len(decisions),
                "unique_jobs": len(records),
                "superseded": superseded,
                "by_source": by_source,
                "by_label": by_label,
                "by_split": by_split,
                "missing_job": missing_job,
            },
        }
    finally:
        if own:
            session.close()


def _format_summary(summary: Dict) -> str:
    """Human-readable run report. The selection-context split (C1) is called out
    explicitly — a surfaced-only set is biased by construction, and this is the
    line that makes that visible before retraining."""
    s = summary
    lines = [
        f"decisions in table : {s['total_decisions']}",
        f"unique jobs exported: {s['unique_jobs']}"
        + (f"  ({s['superseded']} superseded by re-decides)" if s["superseded"] else ""),
        f"by source          : queue={s['by_source']['queue']}  "
        f"rescue={s['by_source']['rescue']}",
        f"by label           : APPLY={s['by_label']['APPLY']}  "
        f"CONSIDER={s['by_label']['CONSIDER']}  HOLD={s['by_label']['HOLD']}  "
        f"SKIP={s['by_label']['SKIP']}",
    ]
    if s["by_split"].get("holdout"):
        lines.append(
            f"split              : train={s['by_split'].get('train', 0)}  "
            f"holdout={s['by_split'].get('holdout', 0)}"
        )
    if s["missing_job"]:
        lines.append(
            f"⚠ {s['missing_job']} decision(s) reference a job_id no longer in "
            f"the jobs table (label kept, text emitted as null)."
        )
    if s["by_source"]["rescue"] == 0 and s["unique_jobs"] > 0:
        lines.append(
            "⚠ NO rescue-sourced labels — the set is surfaced-only and biased "
            "by construction (SPEC §7/C1). Audit the rescue view before retraining."
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the decisions table to a labels.json-shaped training file."
    )
    parser.add_argument(
        "-o", "--out", default="data/decisions_export.json",
        help="output path (default: data/decisions_export.json)",
    )
    parser.add_argument(
        "--holdout", type=float, default=0.0, metavar="FRACTION",
        help="deterministic held-out share in [0,1) by hashed job_id (default: 0, off)",
    )
    parser.add_argument(
        "--stdout", action="store_true",
        help="write the JSON to stdout instead of a file (summary still goes to stderr)",
    )
    args = parser.parse_args(argv)

    if not (0.0 <= args.holdout < 1.0):
        print("error: --holdout must be in [0, 1)", file=sys.stderr)
        return 2

    result = build_export(holdout=args.holdout)
    payload = json.dumps(result["records"], indent=2, ensure_ascii=False)

    if args.stdout:
        print(payload)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)

    report = _format_summary(result["summary"])
    sink = sys.stderr if args.stdout else sys.stdout
    if not args.stdout:
        print(f"wrote {result['summary']['unique_jobs']} records -> {args.out}", file=sink)
    print(report, file=sink)
    return 0


if __name__ == "__main__":
    sys.exit(main())
