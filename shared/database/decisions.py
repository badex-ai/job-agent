"""Decision recording — the single headless seam every triage action routes through.

SPEC §12 step 2. record_decision() does exactly two things, in one transaction:

1. INSERTS an immutable Decision row, SNAPSHOTTING the gate's verdict off the Job
   at write time (gate_verdict/reason/tags/model). The snapshot is frozen here so
   a later re-gate of the Job can never rewrite the label's decision-time context
   (SPEC §4a).
2. MUTATES Job.status per the §6 lifecycle map (the coarse queue bucket).

It deliberately does NOT own the apply side effects (channel/tone, follow-up
schedule). Those have a canonical owner — shared.database.tracking.mark_applied —
and the apply UI calls that separately (SPEC §5b/§5d). record_decision never
reimplements follow-up scheduling: one fact, one writer.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import datetime
from typing import Optional, Tuple

from shared.database.models import Job, Decision, get_session

# SPEC §6: the four-way human label collapses onto the coarse Job.status bucket
# used by the daily queue. CONSIDER and HOLD both leave the queue as 'reviewed'
# but stay DISTINCT in Decision.human_label — the training set keeps the
# distinction the rubric needs; the queue does not.
HUMAN_LABELS = ("APPLY", "CONSIDER", "HOLD", "SKIP")
_LABEL_TO_STATUS = {
    "APPLY":    "applied",
    "CONSIDER": "reviewed",
    "HOLD":     "reviewed",
    "SKIP":     "ignored",
}
SOURCE_VIEWS = ("queue", "rescue")


def record_decision(
    job: Job,
    human_label: str,
    *,
    rule: Optional[str] = None,
    note: Optional[str] = None,
    source_view: str = "queue",
    session=None,
) -> Tuple[bool, Optional[str]]:
    """Record one triage decision: append an immutable Decision row + advance
    Job.status. Returns (success, error_message).

    Args:
        job: the Job being decided (only job.id and the gate_* snapshot fields
             are read; a detached instance is fine).
        human_label: one of APPLY / CONSIDER / HOLD / SKIP (the rubric's space).
        rule: optional rule being corrected — S1..S6 on a skip, or the
              over-fired gate tag on a rescue (SPEC §4a human_rule).
        note: optional free text.
        source_view: 'queue' | 'rescue' — records the selection context so the
                     export can defend against selection bias (SPEC §7/C1).
        session: optional SQLAlchemy session to write within (the caller owns the
                 transaction); if omitted, a fresh session is opened and closed.

    Note: for APPLY, the caller is expected to also invoke mark_applied() for the
    channel/follow-up tracking. record_decision sets status='applied' idempotently
    here so the label and status are consistent even if recorded before that call.
    """
    if human_label not in HUMAN_LABELS:
        return False, f"Invalid human_label {human_label!r}; must be one of {HUMAN_LABELS}"
    if source_view not in SOURCE_VIEWS:
        return False, f"Invalid source_view {source_view!r}; must be one of {SOURCE_VIEWS}"

    own_session = session is None
    session = session or get_session()
    try:
        decision = Decision(
            job_id=job.id,
            decided_at=datetime.utcnow(),
            # snapshot the gate's call AT DECISION TIME, frozen (SPEC §4a):
            gate_verdict=job.gate_verdict,
            gate_reason=job.gate_reason,
            gate_tags=job.gate_tags,
            gate_model=job.gate_model,
            # the human ground truth:
            human_label=human_label,
            human_rule=rule,
            note=note,
            source_view=source_view,
        )
        session.add(decision)

        # advance the coarse queue bucket (SPEC §6). Re-attach the Job to this
        # session by id rather than trusting a possibly-detached instance, so the
        # status write lands on the persisted row.
        db_job = session.query(Job).filter(Job.id == job.id).first()
        if db_job is None:
            session.rollback()
            return False, f"Job {job.id} not found"
        db_job.status = _LABEL_TO_STATUS[human_label]

        session.commit()
        return True, None
    except Exception as e:
        session.rollback()
        return False, str(e)
    finally:
        if own_session:
            session.close()
