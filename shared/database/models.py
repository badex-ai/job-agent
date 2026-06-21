from sqlalchemy import (
    create_engine, Column, String, Integer, Float,
    DateTime, Text, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from shared.config import DB_PATH

Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"

    id            = Column(String, primary_key=True)   # LinkedIn job ID
    title         = Column(String)
    company       = Column(String)
    location      = Column(String)
    is_remote     = Column(Boolean, default=True)
    description   = Column(Text)
    apply_link    = Column(String)
    job_url       = Column(String)
    date_posted   = Column(String)
    source        = Column(String)                     # linkedin / indeed / wellfound
    job_type      = Column(String, default="fullstack")  # devops / frontend / fullstack
    scraped_at    = Column(DateTime, default=datetime.utcnow)

    # AI analysis fields (old format - keeping for migration)
    match_score       = Column(Float, default=0.0)
    missing_skills    = Column(Text)                   # JSON list stored as text
    strengths         = Column(Text)                   # JSON list stored as text
    drafted_message   = Column(Text)                   # personalised outreach message
    drafted_email     = Column(Text)                   # full email body
    fit_summary       = Column(Text)                   # brief summary

    # AI analysis fields (new format - dimensions + detailed report)
    overall_score     = Column(Float, default=0.0)
    cv_match          = Column(Float, default=0.0)
    level_fit         = Column(Float, default=0.0)
    culture_fit       = Column(Float, default=0.0)
    red_flags         = Column(Text)                   # JSON array of red flags
    top_strengths     = Column(Text)                   # JSON array of strengths
    gaps              = Column(Text)                   # JSON array of skill gaps
    why_good_fit      = Column(Text)                   # Paragraph explaining fit
    why_concerns      = Column(Text)                   # Paragraph explaining concerns
    recommended_action = Column(String, default="SKIP") # APPLY/CONSIDER/HOLD/SKIP
    report_markdown   = Column(Text)                   # Full markdown report

    # Outreach tracking
    email_sent        = Column(Boolean, default=False)
    email_sent_at     = Column(DateTime)
    linkedin_queued   = Column(Boolean, default=False)
    linkedin_sent     = Column(Boolean, default=False)
    applied           = Column(Boolean, default=False)
    status            = Column(String, default="new")  # new / reviewed / applied / ignored

    # v2 gate verdict (the measured Qwen gate — replaces v1 overall_score)
    gate_verdict       = Column(String)               # skip / surface / surface_priority
    gate_reason        = Column(Text)                 # model reasoning (self-diagnosing row)
    gate_model         = Column(String)               # qwen2.5:14b / hard_gate / offline
    gate_tags          = Column(Text)                 # JSON list of cited rubric rules
    gate_confidence    = Column(Integer)              # 0-100
    gate_source        = Column(String)               # hard_gate / qwen / offline / error
    gate_run_at        = Column(DateTime)             # when the gate scored this row

    # Contact info (extracted via regex, 0 tokens)
    company_email      = Column(String)               # careers@company.com
    recruiter_email    = Column(String)               # john.doe@company.com
    recruiter_linkedin = Column(String)               # linkedin.com/in/johndoe
    recruiter_name     = Column(String)               # John Doe

    # Application tracking
    applied_at         = Column(DateTime)             # When user applied
    applied_via        = Column(String)               # website / linkedin / email
    message_version    = Column(String)               # A / B / C for A/B testing
    message_tone       = Column(String)               # formal / casual / story

    # Follow-up tracking
    followup_count     = Column(Integer, default=0)   # 0, 1, 2, 3
    last_followup_at   = Column(DateTime)             # Last follow-up sent
    next_followup_due  = Column(DateTime)             # When next follow-up is due

    # Response tracking (for A/B testing)
    response_received  = Column(Boolean, default=False)
    response_type      = Column(String)               # positive / negative / later
    interview_scheduled = Column(Boolean, default=False)

    # Rejection analysis
    rejection_data     = Column(Text)                 # JSON: {reason, date, lessons}


def get_engine():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


def get_session():
    """Get a new SQLAlchemy session."""
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


def init_db():
    """Initialize database schema."""
    engine = get_engine()
    Base.metadata.create_all(engine)


def get_session():
    engine = get_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("[DB] Tables created.")


# SQLite ADD COLUMN is the only ALTER it supports, and create_all() never
# touches an existing table. So new columns on an already-populated jobs.db
# need an explicit, idempotent migration. Type strings are SQLite affinities.
_GATE_COLUMNS = {
    "gate_verdict": "VARCHAR",
    "gate_reason": "TEXT",
    "gate_model": "VARCHAR",
    "gate_tags": "TEXT",
    "gate_confidence": "INTEGER",
    "gate_source": "VARCHAR",
    "gate_run_at": "DATETIME",
}


def migrate_gate_columns(engine=None) -> list:
    """Add any missing gate_* column to an existing jobs table. Idempotent:
    re-running is a no-op once columns exist. Returns the columns it added."""
    from sqlalchemy import text
    engine = engine or get_engine()
    Base.metadata.create_all(engine)  # fresh DB: table+columns exist already
    added = []
    with engine.begin() as conn:
        existing = {row[1] for row in
                    conn.execute(text("PRAGMA table_info(jobs)"))}
        for col, sqltype in _GATE_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {sqltype}"))
                added.append(col)
    return added


if __name__ == "__main__":
    added = migrate_gate_columns()
    print(f"[migrate] gate columns added: {added or 'none (already present)'}")
