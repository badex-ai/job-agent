"""
Email Sender — sends cold emails automatically via Gmail SMTP.
Uses app password (not main password) — set up in Google Account > Security.
Only sends if job has an apply email address or the job warrants cold outreach.
"""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from shared.config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD

logger = logging.getLogger(__name__)


def send_email(to_address: str, subject: str, body: str) -> bool:
    """
    Send a plain text email via Gmail SMTP.
    Returns True on success, False on failure.
    """
    if not GMAIL_APP_PASSWORD:
        logger.error("GMAIL_APP_PASSWORD not set in .env — cannot send email")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = to_address
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_address, msg.as_string())

        logger.info(f"Email sent to {to_address} | Subject: {subject}")
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error("Gmail auth failed — check GMAIL_APP_PASSWORD in .env")
        return False
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


def send_job_application(job: dict) -> bool:
    """
    Send a cold email application for a job.
    Parses subject and body from job.drafted_email field.
    """
    drafted = job.get("drafted_email", "")
    if not drafted:
        logger.warning(f"No drafted email for job {job.get('id')} — skipping")
        return False

    apply_email = job.get("apply_email", "")
    if not apply_email:
        logger.info(f"No email apply address for {job.get('title')} — skipping email send")
        return False

    lines = drafted.strip().split("\n")
    subject = ""
    body_lines = []

    for i, line in enumerate(lines):
        if line.startswith("Subject:"):
            subject = line.replace("Subject:", "").strip()
        elif i > 0 and subject:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not subject or not body:
        logger.warning(f"Could not parse subject/body for job {job.get('id')}")
        return False

    return send_email(apply_email, subject, body)
