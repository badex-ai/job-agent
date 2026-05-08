"""
Application Tracking — Record when and how user applies to jobs.

Functions:
- mark_applied(): Record application with channel, version, tone
- get_applied_jobs(): Get all applied jobs
- get_application_stats(): Get application statistics
- get_jobs_needing_followup(): Get jobs due for follow-up
- mark_followup_sent(): Record that follow-up was sent
- mark_response_received(): Record response from company
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from shared.database.models import Job, get_session

# Follow-up schedule: days after initial application
# Day 3: First follow-up (gentle reminder)
# Day 7: Second follow-up (still interested)
# Day 14: Final follow-up (closing the loop)
FOLLOWUP_SCHEDULE_DAYS = [3, 7, 14]


def _calculate_next_followup(applied_at: datetime, followup_count: int) -> Optional[datetime]:
    """
    Calculate when the next follow-up is due.

    Args:
        applied_at: When the application was submitted
        followup_count: How many follow-ups have been sent (0, 1, 2)

    Returns:
        Datetime of next follow-up, or None if all follow-ups exhausted
    """
    if followup_count >= len(FOLLOWUP_SCHEDULE_DAYS):
        return None
    days_offset = FOLLOWUP_SCHEDULE_DAYS[followup_count]
    return applied_at + timedelta(days=days_offset)


def mark_applied(
    job_id: str,
    via: str,
    version: Optional[str] = None,
    tone: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    Record that user applied to a job.

    Automatically schedules the first follow-up for 3 days later.

    Args:
        job_id: The job ID to mark as applied
        via: Channel used - "website" | "linkedin" | "email"
        version: Message version for A/B testing - "A" | "B" | "C" (optional)
        tone: Message tone - "formal" | "casual" | "story" (optional)

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return False, f"Job {job_id} not found"

        now = datetime.now()

        # Core application tracking
        job.applied = True
        job.applied_at = now
        job.applied_via = via
        job.message_version = version
        job.message_tone = tone
        job.status = "applied"

        # Schedule first follow-up (3 days from now)
        job.followup_count = 0
        job.next_followup_due = _calculate_next_followup(now, 0)

        session.commit()
        return True, None
    except Exception as e:
        session.rollback()
        return False, str(e)
    finally:
        session.close()


def mark_followup_sent(job_id: str) -> Tuple[bool, Optional[str]]:
    """
    Record that a follow-up was sent for a job.

    Increments followup_count and schedules the next follow-up.

    Args:
        job_id: The job ID

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return False, f"Job {job_id} not found"

        if not job.applied:
            return False, "Cannot send follow-up for job not marked as applied"

        job.followup_count = (job.followup_count or 0) + 1
        job.last_followup_at = datetime.now()
        job.next_followup_due = _calculate_next_followup(
            job.applied_at, job.followup_count
        )

        session.commit()
        return True, None
    except Exception as e:
        session.rollback()
        return False, str(e)
    finally:
        session.close()


def mark_response_received(
    job_id: str,
    response_type: str,
    interview_scheduled: bool = False
) -> Tuple[bool, Optional[str]]:
    """
    Record that a response was received from the company.

    Clears next_followup_due since no more follow-ups needed.

    Args:
        job_id: The job ID
        response_type: "positive" | "negative" | "later"
        interview_scheduled: Whether an interview was scheduled

    Returns:
        Tuple of (success: bool, error_message: Optional[str])
    """
    valid_response_types = {"positive", "negative", "later"}
    if response_type not in valid_response_types:
        return False, f"Invalid response_type. Must be one of: {valid_response_types}"

    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if not job:
            return False, f"Job {job_id} not found"

        job.response_received = True
        job.response_type = response_type
        job.interview_scheduled = interview_scheduled
        job.next_followup_due = None  # No more follow-ups needed

        if interview_scheduled:
            job.status = "interview"
        elif response_type == "negative":
            job.status = "rejected"

        session.commit()
        return True, None
    except Exception as e:
        session.rollback()
        return False, str(e)
    finally:
        session.close()


def get_jobs_needing_followup() -> List[Job]:
    """
    Get all jobs that are due for a follow-up.

    Returns jobs where:
    - User has applied
    - No response received yet
    - next_followup_due is in the past or today
    - followup_count < 3 (max follow-ups not reached)

    Returns:
        List of Job objects needing follow-up, ordered by due date
    """
    session = get_session()
    try:
        now = datetime.now()
        jobs = session.query(Job).filter(
            Job.applied == True,
            Job.response_received == False,
            Job.next_followup_due <= now,
            Job.followup_count < len(FOLLOWUP_SCHEDULE_DAYS)
        ).order_by(Job.next_followup_due.asc()).all()

        # Detach from session so they can be used after session closes
        for job in jobs:
            session.expunge(job)

        return jobs
    finally:
        session.close()


def get_applied_jobs(include_responded: bool = False) -> List[Job]:
    """
    Get all jobs marked as applied.

    Args:
        include_responded: If True, include jobs that received responses.
                          If False (default), only return jobs awaiting response.

    Returns:
        List of Job objects, ordered by applied_at descending
    """
    session = get_session()
    try:
        query = session.query(Job).filter(Job.applied == True)

        if not include_responded:
            query = query.filter(Job.response_received == False)

        jobs = query.order_by(Job.applied_at.desc()).all()

        # Detach from session
        for job in jobs:
            session.expunge(job)

        return jobs
    finally:
        session.close()


def get_application_stats() -> Dict:
    """
    Get application statistics for analytics and A/B testing.

    Returns:
        Dict with counts by channel, version, tone, and response rates
    """
    session = get_session()
    try:
        applied_jobs = session.query(Job).filter(Job.applied == True).all()

        stats = {
            "total_applied": len(applied_jobs),
            "awaiting_response": 0,
            "responses_received": 0,
            "interviews_scheduled": 0,
            "by_channel": {"website": 0, "linkedin": 0, "email": 0, "other": 0},
            "by_version": {"A": 0, "B": 0, "C": 0, "none": 0},
            "by_tone": {"formal": 0, "casual": 0, "story": 0, "none": 0},
            "response_rate_by_channel": {},
            "response_rate_by_version": {},
        }

        channel_responses = {"website": 0, "linkedin": 0, "email": 0, "other": 0}
        version_responses = {"A": 0, "B": 0, "C": 0, "none": 0}

        for job in applied_jobs:
            # Count by channel
            channel = job.applied_via if job.applied_via in stats["by_channel"] else "other"
            stats["by_channel"][channel] += 1

            # Count by version
            version = job.message_version if job.message_version in ["A", "B", "C"] else "none"
            stats["by_version"][version] += 1

            # Count by tone
            tone = job.message_tone if job.message_tone in ["formal", "casual", "story"] else "none"
            stats["by_tone"][tone] += 1

            # Count responses
            if job.response_received:
                stats["responses_received"] += 1
                channel_responses[channel] += 1
                version_responses[version] += 1
                if job.interview_scheduled:
                    stats["interviews_scheduled"] += 1
            else:
                stats["awaiting_response"] += 1

        # Calculate response rates (avoid division by zero)
        for channel, count in stats["by_channel"].items():
            if count > 0:
                rate = (channel_responses[channel] / count) * 100
                stats["response_rate_by_channel"][channel] = round(rate, 1)

        for version, count in stats["by_version"].items():
            if count > 0:
                rate = (version_responses[version] / count) * 100
                stats["response_rate_by_version"][version] = round(rate, 1)

        return stats
    finally:
        session.close()


def get_followup_summary() -> Dict:
    """
    Get a summary of follow-up status across all applied jobs.

    Returns:
        Dict with follow-up counts and due dates
    """
    session = get_session()
    try:
        now = datetime.now()
        applied_jobs = session.query(Job).filter(
            Job.applied == True,
            Job.response_received == False
        ).all()

        summary = {
            "total_awaiting": len(applied_jobs),
            "due_today": 0,
            "overdue": 0,
            "upcoming_3_days": 0,
            "by_followup_count": {0: 0, 1: 0, 2: 0, 3: 0},
        }

        for job in applied_jobs:
            count = job.followup_count or 0
            summary["by_followup_count"][min(count, 3)] += 1

            if job.next_followup_due:
                days_until = (job.next_followup_due - now).days
                if days_until < 0:
                    summary["overdue"] += 1
                elif days_until == 0:
                    summary["due_today"] += 1
                elif days_until <= 3:
                    summary["upcoming_3_days"] += 1

        return summary
    finally:
        session.close()
