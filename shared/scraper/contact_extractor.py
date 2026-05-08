"""
Contact Extractor — Extract company/recruiter contact info from job postings.

Uses regex (0 tokens). No LLM required.
"""
import re
from typing import Optional

# Emails to filter out (not useful for outreach)
EXCLUDED_EMAIL_PATTERNS = [
    'noreply', 'no-reply', 'donotreply', 'do-not-reply',
    'notifications', 'alerts', 'system', 'mailer-daemon',
    'bounce', 'postmaster', 'admin@', 'support@', 'info@',
    'hello@', 'contact@'  # Generic, not personal
]


def extract_company_email(text: str) -> Optional[str]:
    """
    Extract company email from job description.
    Filters out generic/noreply addresses.
    Returns first valid email found, or None.
    """
    if not text:
        return None

    # Email pattern
    pattern = r'[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}'
    matches = re.findall(pattern, text.lower())

    for email in matches:
        # Skip excluded patterns
        if any(excluded in email for excluded in EXCLUDED_EMAIL_PATTERNS):
            continue
        return email

    return None


def extract_recruiter_linkedin(text: str) -> Optional[str]:
    """
    Extract recruiter LinkedIn URL from job posting.
    Returns full URL or None.
    """
    if not text:
        return None

    # LinkedIn profile URL pattern
    pattern = r'linkedin\.com/in/([\w-]+)'
    match = re.search(pattern, text, re.IGNORECASE)

    if match:
        username = match.group(1)
        return f"https://linkedin.com/in/{username}"

    return None


def extract_recruiter_name(text: str) -> Optional[str]:
    """
    Extract recruiter name from common patterns.
    Examples: "Posted by John Doe", "Contact: Jane Smith"
    """
    if not text:
        return None

    # Common patterns for recruiter names
    patterns = [
        r'posted by[:\s]+([A-Z][a-z]+\s[A-Z][a-z]+)',
        r'contact[:\s]+([A-Z][a-z]+\s[A-Z][a-z]+)',
        r'recruiter[:\s]+([A-Z][a-z]+\s[A-Z][a-z]+)',
        r'hiring manager[:\s]+([A-Z][a-z]+\s[A-Z][a-z]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).title()

    return None


def extract_contacts(job: dict) -> dict:
    """
    Extract all contact info from a job posting.

    Args:
        job: Job dict with 'description' field

    Returns:
        dict with keys: company_email, recruiter_email,
                       recruiter_linkedin, recruiter_name
    """
    description = job.get('description', '')

    return {
        'company_email': extract_company_email(description),
        'recruiter_email': None,  # Would need additional scraping
        'recruiter_linkedin': extract_recruiter_linkedin(description),
        'recruiter_name': extract_recruiter_name(description),
    }


def enrich_job_with_contacts(job: dict) -> dict:
    """
    Add contact info to job dict.

    Args:
        job: Job dict

    Returns:
        Job dict with contact fields added
    """
    contacts = extract_contacts(job)
    return {**job, **contacts}
