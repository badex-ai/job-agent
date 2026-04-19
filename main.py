"""
main.py — entry point for the Job Agent.

Usage:
  python main.py                # Start RAPH loop (hourly scrape + evaluation)
  python main.py --once         # Run one cycle only
  python main.py --dashboard    # Start CLI dashboard (Rich TUI)
  python main.py --batch        # Process new jobs in parallel
  python main.py --profile      # Create/update profile questionnaire
"""
import sys
import os

if __name__ == "__main__":
    if "--dashboard" in sys.argv:
        # Start Rich TUI dashboard
        from dashboard.cli import display_dashboard
        display_dashboard()
    
    elif "--batch" in sys.argv:
        # Process new jobs in parallel
        from agents.batch_processor import process_new_jobs
        import json
        summary = process_new_jobs(max_parallel=4)
        print(json.dumps(summary, indent=2))
    
    elif "--profile" in sys.argv:
        # Run profile builder
        from setup_profile import ProfileBuilder
        builder = ProfileBuilder()
        builder.run()
    
    elif "--once" in sys.argv:
        # Run one RAPH cycle
        from agents.loop import raph_cycle
        from database.models import init_db
        init_db()
        raph_cycle()
    
    else:
        # Default: start RAPH loop scheduler (hourly)
        from agents.loop import start_agent
        start_agent()
