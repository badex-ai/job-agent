#!/usr/bin/env python3
"""
migrate_schema.py — Migrate database to new Phase 1-3 schema.

This script:
1. Backs up existing jobs.db
2. Creates new columns for Phase 1-3 features
3. Maintains all existing job data
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from database.models import get_engine, Job
from sqlalchemy import text, inspect
from datetime import datetime
from pathlib import Path
import shutil

def backup_database():
    """Create backup of current database."""
    db_path = Path("data/jobs.db")
    if db_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = db_path.parent / f"jobs_backup_{timestamp}.db"
        shutil.copy2(db_path, backup_path)
        print(f"✓ Backed up database to: {backup_path}")
        return backup_path
    return None


def check_columns():
    """Check if new columns already exist."""
    engine = get_engine()
    inspector = inspect(engine)

    existing_cols = {col['name'] for col in inspector.get_columns('jobs')}

    # Phase 1-3 columns (existing)
    phase_1_3_cols = {
        'job_type', 'overall_score', 'cv_match', 'level_fit', 'culture_fit',
        'red_flags', 'top_strengths', 'gaps', 'why_good_fit',
        'why_concerns', 'recommended_action', 'report_markdown'
    }

    # Top 1% system columns (new)
    top_1pct_cols = {
        # Contact info
        'company_email', 'recruiter_email', 'recruiter_linkedin', 'recruiter_name',
        # Application tracking
        'applied_at', 'applied_via', 'message_version', 'message_tone',
        # Follow-up tracking
        'followup_count', 'last_followup_at', 'next_followup_due',
        # Response tracking
        'response_received', 'response_type', 'interview_scheduled',
        # Rejection analysis
        'rejection_data'
    }

    all_new_cols = phase_1_3_cols | top_1pct_cols
    missing = all_new_cols - existing_cols
    return missing, existing_cols


def migrate_schema():
    """Add new columns if they don't exist."""
    engine = get_engine()
    
    missing, existing = check_columns()
    
    if not missing:
        print("✓ Database already has all Phase 1-3 columns")
        return True
    
    print(f"\nAdding {len(missing)} new columns...")
    
    with engine.connect() as conn:
        # Add missing columns one by one
        for col in missing:
            if col == 'job_type':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN job_type TEXT DEFAULT 'fullstack'"))
                print("  ✓ Added job_type")
            elif col == 'overall_score':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN overall_score FLOAT DEFAULT 0.0"))
                print("  ✓ Added overall_score")
            elif col == 'cv_match':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN cv_match FLOAT DEFAULT 0.0"))
                print("  ✓ Added cv_match")
            elif col == 'level_fit':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN level_fit FLOAT DEFAULT 0.0"))
                print("  ✓ Added level_fit")
            elif col == 'culture_fit':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN culture_fit FLOAT DEFAULT 0.0"))
                print("  ✓ Added culture_fit")
            elif col == 'red_flags':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN red_flags TEXT"))
                print("  ✓ Added red_flags")
            elif col == 'top_strengths':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN top_strengths TEXT"))
                print("  ✓ Added top_strengths")
            elif col == 'gaps':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN gaps TEXT"))
                print("  ✓ Added gaps")
            elif col == 'why_good_fit':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN why_good_fit TEXT"))
                print("  ✓ Added why_good_fit")
            elif col == 'why_concerns':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN why_concerns TEXT"))
                print("  ✓ Added why_concerns")
            elif col == 'recommended_action':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN recommended_action TEXT DEFAULT 'SKIP'"))
                print("  ✓ Added recommended_action")
            elif col == 'report_markdown':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN report_markdown TEXT"))
                print("  ✓ Added report_markdown")
            # Contact info columns
            elif col == 'company_email':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN company_email TEXT"))
                print("  ✓ Added company_email")
            elif col == 'recruiter_email':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN recruiter_email TEXT"))
                print("  ✓ Added recruiter_email")
            elif col == 'recruiter_linkedin':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN recruiter_linkedin TEXT"))
                print("  ✓ Added recruiter_linkedin")
            elif col == 'recruiter_name':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN recruiter_name TEXT"))
                print("  ✓ Added recruiter_name")
            # Application tracking columns
            elif col == 'applied_at':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN applied_at DATETIME"))
                print("  ✓ Added applied_at")
            elif col == 'applied_via':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN applied_via TEXT"))
                print("  ✓ Added applied_via")
            elif col == 'message_version':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN message_version TEXT"))
                print("  ✓ Added message_version")
            elif col == 'message_tone':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN message_tone TEXT"))
                print("  ✓ Added message_tone")
            # Follow-up tracking columns
            elif col == 'followup_count':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN followup_count INTEGER DEFAULT 0"))
                print("  ✓ Added followup_count")
            elif col == 'last_followup_at':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN last_followup_at DATETIME"))
                print("  ✓ Added last_followup_at")
            elif col == 'next_followup_due':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN next_followup_due DATETIME"))
                print("  ✓ Added next_followup_due")
            # Response tracking columns
            elif col == 'response_received':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN response_received BOOLEAN DEFAULT 0"))
                print("  ✓ Added response_received")
            elif col == 'response_type':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN response_type TEXT"))
                print("  ✓ Added response_type")
            elif col == 'interview_scheduled':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN interview_scheduled BOOLEAN DEFAULT 0"))
                print("  ✓ Added interview_scheduled")
            # Rejection analysis column
            elif col == 'rejection_data':
                conn.execute(text("ALTER TABLE jobs ADD COLUMN rejection_data TEXT"))
                print("  ✓ Added rejection_data")

        conn.commit()
    
    print("\n✓ Schema migration complete")
    return True


def verify_migration():
    """Verify all new columns exist."""
    missing, existing = check_columns()
    
    if missing:
        print(f"✗ Migration incomplete. Missing columns: {missing}")
        return False
    
    print("✓ All Phase 1-3 columns present")
    
    # Count jobs
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=get_engine())
    session = Session()
    count = session.query(Job).count()
    session.close()
    
    print(f"✓ Database contains {count} jobs")
    return True


def main():
    """Run migration."""
    print("=" * 60)
    print("Database Migration: Old Schema → Phase 1-3 Schema")
    print("=" * 60)
    print()
    
    # Backup
    backup_database()
    print()
    
    # Check current state
    print("Checking current schema...")
    missing, existing = check_columns()
    
    if not missing:
        print("✓ Database already migrated")
        print()
        return
    
    print(f"ℹ Found {len(missing)} missing columns")
    print(f"  Missing: {', '.join(sorted(missing))}")
    print()
    
    # Migrate
    print("Starting migration...")
    if migrate_schema():
        print()
        print("Verifying migration...")
        if verify_migration():
            print()
            print("=" * 60)
            print("✅ Migration successful!")
            print("=" * 60)
            print()
            print("You can now:")
            print("  • python main.py --profile    (create profile)")
            print("  • python main.py               (start RAPH loop)")
            print("  • python main.py --dashboard   (view jobs)")
        else:
            print()
            print("✗ Verification failed")
    else:
        print()
        print("✗ Migration failed")


if __name__ == "__main__":
    main()
