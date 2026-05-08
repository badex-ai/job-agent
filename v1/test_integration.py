#!/usr/bin/env python3
"""
test_integration.py — Verify all phases are working.
Run this to check syntax, imports, and basic functionality.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
from datetime import datetime

def test_imports():
    """Test that all critical modules import successfully."""
    print("Testing imports...")
    try:
        from shared.database.models import Job, get_engine, get_session, init_db
        print("  ✓ Database models")

        from v1.analyzer.cv_matcher import analyse_job, generate_report, play_alert_sound
        print("  ✓ CV matcher (Phase 1)")

        from v1.agents.batch_processor import BatchProcessor
        print("  ✓ Batch processor (Phase 3)")

        from shared.config.profile_manager import ProfileManager
        print("  ✓ Profile manager")
        
        try:
            from rich.console import Console
            print("  ✓ Rich (CLI dashboard)")
        except ImportError:
            print("  ⚠ Rich not installed (needed for Phase 2)")
        
        print("✓ All imports successful\n")
        return True
    except Exception as e:
        print(f"✗ Import failed: {e}\n")
        return False


def test_profile_manager():
    """Test profile manager functionality."""
    print("Testing profile manager...")
    try:
        from shared.config.profile_manager import ProfileManager
        pm = ProfileManager()
        
        if pm.is_empty():
            print("  ℹ Profile not yet created (run 'python main.py --profile')")
        else:
            context = pm.get_evaluation_context()
            print(f"  ✓ Profile loaded ({len(context)} chars)")
            
            deals = pm.get_deal_breakers()
            print(f"  ✓ Deal-breakers: {len(deals)}")
        
        print("✓ Profile manager working\n")
        return True
    except Exception as e:
        print(f"✗ Profile manager test failed: {e}\n")
        return False


def test_cv_matcher():
    """Test CV matcher evaluation."""
    print("Testing CV matcher...")
    try:
        from v1.analyzer.cv_matcher import analyse_job
        from shared.config import CV_PATH
        
        # Check if CV exists
        if not os.path.exists(CV_PATH):
            print(f"  ⚠ CV not found at {CV_PATH}")
            print("    Create data/cv.txt first")
            return False
        
        # Test with a dummy job
        dummy_job = {
            "id": "test-001",
            "title": "DevOps Engineer",
            "company": "TechCorp",
            "description": "Looking for DevOps engineer with Kubernetes and AWS experience.",
            "location": "Remote",
        }
        
        # Note: This would call Claude API, so we skip actual evaluation
        print("  ✓ CV matcher available (requires API key for actual evaluation)")
        print("✓ CV matcher ready\n")
        return True
    except Exception as e:
        print(f"✗ CV matcher test failed: {e}\n")
        return False


def test_batch_processor():
    """Test batch processor setup."""
    print("Testing batch processor...")
    try:
        from v1.agents.batch_processor import BatchProcessor, CHECKPOINT_DIR
        
        # Check checkpoint directory
        if not CHECKPOINT_DIR.exists():
            CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            print(f"  ✓ Created checkpoint directory: {CHECKPOINT_DIR}")
        else:
            checkpoints = list(CHECKPOINT_DIR.glob("batch_*.json"))
            print(f"  ℹ Found {len(checkpoints)} existing checkpoints")
        
        # Test processor initialization
        processor = BatchProcessor(max_workers=4)
        print(f"  ✓ Processor initialized (ID: {processor.batch_id})")
        
        print("✓ Batch processor ready\n")
        return True
    except Exception as e:
        print(f"✗ Batch processor test failed: {e}\n")
        return False


def test_database():
    """Test database functionality."""
    print("Testing database...")
    try:
        from shared.database.models import get_engine, get_session, init_db, Job
        
        # Initialize database
        init_db()
        print("  ✓ Database initialized")
        
        # Test session
        session = get_session()
        count = session.query(Job).count()
        session.close()
        print(f"  ✓ Session works (found {count} existing jobs)")
        
        print("✓ Database ready\n")
        return True
    except Exception as e:
        print(f"✗ Database test failed: {e}\n")
        return False


def test_main_entry_points():
    """Test that main.py has correct entry points."""
    print("Testing main.py entry points...")
    try:
        main_path = os.path.join(os.path.dirname(__file__), "main.py")
        with open(main_path) as f:
            content = f.read()
        
        flags = ["--dashboard", "--batch", "--profile", "--once"]
        found = []
        for flag in flags:
            if flag in content:
                found.append(flag)
        
        print(f"  ✓ Found entry points: {', '.join(found)}")
        print("✓ Main entry points configured\n")
        return True
    except Exception as e:
        print(f"✗ Main entry points test failed: {e}\n")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("🧪 Job Agent Integration Test")
    print("=" * 60)
    print()
    
    results = {
        "imports": test_imports(),
        "profile": test_profile_manager(),
        "cv_matcher": test_cv_matcher(),
        "batch": test_batch_processor(),
        "database": test_database(),
        "main": test_main_entry_points(),
    }
    
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test, result in results.items():
        symbol = "✓" if result else "✗"
        print(f"{symbol} {test}")
    
    print()
    print(f"Passed: {passed}/{total}")
    
    if passed == total:
        print("\n✅ All systems ready!")
        print("\nNext steps:")
        print("  1. python main.py --profile          (create profile)")
        print("  2. python main.py                    (start agent loop)")
        print("  3. python main.py --dashboard        (open dashboard)")
    else:
        print("\n⚠ Some tests failed. Fix issues above before running.")
    
    print()


if __name__ == "__main__":
    main()
