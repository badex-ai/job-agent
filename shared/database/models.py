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


class Decision(Base):
    """Append-only training signal: one immutable row per human triage action.

    The load-bearing half of the apply/triage CLI (SPEC §4a). Each row pairs the
    human's four-way label with a SNAPSHOT of the gate's verdict AT DECISION
    TIME — frozen here so a later re-gate of the Job row can never corrupt the
    label's decision-time context. Rows are inserted, never updated or deleted;
    re-deciding the same job inserts a SECOND row and keeps both.
    """
    __tablename__ = "decisions"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    job_id        = Column(String)                     # FK -> jobs.id (Job.id is a String)
    decided_at    = Column(DateTime, default=datetime.utcnow)

    # the gate's call AT DECISION TIME, frozen (never re-read from the Job row):
    gate_verdict  = Column(String)                     # skip / surface / surface_priority
    gate_reason   = Column(Text)                       # snapshot
    gate_tags     = Column(Text)                       # JSON list snapshot (S1..S6 etc.)
    gate_model    = Column(String)                     # snapshot (e.g. qwen2.5:14b)

    # the human label (the new ground truth):
    human_label   = Column(String)                     # APPLY / CONSIDER / HOLD / SKIP
    human_rule    = Column(String)                     # optional: rule being corrected (S1..S6)
    note          = Column(Text)                       # optional free text
    source_view   = Column(String)                     # 'queue' | 'rescue' (selection context, §7)


class Application(Base):
    """Append-only application-lifecycle log — one row per web-UI action on a job.

    Deliberately SEPARATE from Decision: Decision.human_label is the gate's
    training signal (validated against APPLY/CONSIDER/HOLD/SKIP, exported for
    model comparison), so post-apply lifecycle events must never pollute it.
    This table owns "what I did about a job", Decision owns "what a job is worth".

    Latest row per job_id (by id) IS the current state. Rows are inserted, never
    updated or deleted — Restore appends a 'restored' event rather than deleting,
    a note edit appends a fresh row, so the whole history survives. The web-UI
    Applied/Dismissed tables render from this table exclusively; the gate verdict
    on the Job row is never mutated.
    """
    __tablename__ = "applications"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    job_id     = Column(String)                        # FK -> jobs.id (Job.id is a String)
    created_at = Column(DateTime, default=datetime.utcnow)

    # the action taken. 'held' parks a job (manual Hold bucket); 'restored'
    # un-dismisses/un-holds it back to its gate bucket.
    event      = Column(String)                         # applied / dismissed / held / restored
    # post-apply lifecycle, only meaningful once event has reached 'applied':
    status     = Column(String)                         # applied / interview / offer / rejected / ghosted
    note       = Column(Text)                           # optional free text (Notes column)


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


def migrate_decisions(engine=None) -> bool:
    """Create the append-only `decisions` table on an existing DB. Idempotent:
    create_all never touches a table that already exists, so re-running is a
    no-op. Mirrors migrate_gate_columns. Returns True if the table was absent
    before this call (i.e. it was just created), False if it already existed."""
    from sqlalchemy import inspect
    engine = engine or get_engine()
    existed = inspect(engine).has_table("decisions")
    Base.metadata.create_all(engine)  # creates `decisions` if missing; no-op otherwise
    return not existed


def migrate_applications(engine=None) -> bool:
    """Create the append-only `applications` table on an existing DB. Idempotent:
    create_all never touches an existing table, so re-running is a no-op. Mirrors
    migrate_decisions. Returns True if the table was just created, False if it
    already existed."""
    from sqlalchemy import inspect
    engine = engine or get_engine()
    existed = inspect(engine).has_table("applications")
    Base.metadata.create_all(engine)  # creates `applications` if missing; no-op otherwise
    return not existed


if __name__ == "__main__":
    added = migrate_gate_columns()
    print(f"[migrate] gate columns added: {added or 'none (already present)'}")
    created = migrate_decisions()
    print(f"[migrate] decisions table: {'created' if created else 'already present'}")
    created = migrate_applications()
    print(f"[migrate] applications table: {'created' if created else 'already present'}")
