"""
Test to verify which columns exist in the database.
Run: python test_schema_check.py
"""
import sqlite3
import sys

# Columns we need for new features
REQUIRED_COLUMNS = {
    # contact-info-schema
    "company_email": False,
    "recruiter_email": False,
    "recruiter_linkedin": False,
    "recruiter_name": False,
    # application-tracking
    "applied_at": False,
    "applied_via": False,
    "message_version": False,
    "message_tone": False,
    # followup-system
    "followup_count": False,
    "last_followup_at": False,
    "next_followup_due": False,
    # ab-testing-framework
    "response_received": False,
    "response_type": False,
    "interview_scheduled": False,
    # rejection-analysis
    "rejection_data": False,
}

def check_schema():
    conn = sqlite3.connect('data/jobs.db')
    cursor = conn.cursor()
    cursor.execute('PRAGMA table_info(jobs)')
    existing = {row[1] for row in cursor.fetchall()}
    conn.close()

    print("=" * 50)
    print("SCHEMA CHECK RESULTS")
    print("=" * 50)

    missing = []
    present = []

    for col in REQUIRED_COLUMNS:
        if col in existing:
            present.append(col)
            REQUIRED_COLUMNS[col] = True
        else:
            missing.append(col)

    if present:
        print(f"\n✓ PRESENT ({len(present)}):")
        for col in present:
            print(f"  - {col}")

    if missing:
        print(f"\n✗ MISSING ({len(missing)}):")
        for col in missing:
            print(f"  - {col}")

    print("\n" + "=" * 50)
    if missing:
        print(f"RESULT: Need to add {len(missing)} columns")
        return False
    else:
        print("RESULT: All columns present ✓")
        return True

if __name__ == "__main__":
    success = check_schema()
    sys.exit(0 if success else 1)
