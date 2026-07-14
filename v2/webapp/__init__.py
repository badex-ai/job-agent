"""Read-only local web layer over the already-built triage backend.

This package NEVER writes: no Decision row, no Job.status mutation. It reads the
jobs the user has already classified in the CLI and renders them by bucket so the
user can act (copy a message, open the posting, pick a CV). See v2/APPLY_UI_PLAN.md.
"""
