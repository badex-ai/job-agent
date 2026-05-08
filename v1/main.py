"""
main.py — entry point for the Job Agent v1.

Usage:
  python v1/main.py                # Start RAPH loop (hourly scrape + evaluation)
  python v1/main.py --once         # Run one cycle only
  python v1/main.py --dashboard    # Start CLI dashboard (Rich TUI)
  python v1/main.py --batch        # Process new jobs in parallel
  python v1/main.py --profile      # Create/update profile questionnaire
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

if __name__ == "__main__":
    if "--dashboard" in sys.argv:
        # Start Rich TUI dashboard
        from v1.dashboard.cli import display_dashboard
        display_dashboard()

    elif "--batch" in sys.argv:
        # Process new jobs in parallel
        from v1.agents.batch_processor import process_new_jobs
        import json
        summary = process_new_jobs(max_parallel=4)
        print(json.dumps(summary, indent=2))

    elif "--profile" in sys.argv:
        # Run profile builder
        from v1.setup_profile import ProfileBuilder
        builder = ProfileBuilder()
        builder.run()

    elif "--once" in sys.argv:
        # Run one RAPH cycle
        from v1.agents.loop import raph_cycle
        from shared.database.models import init_db
        init_db()
        raph_cycle()

    else:
        # Default: start RAPH loop scheduler (hourly)
        from v1.agents.loop import start_agent
        start_agent()
