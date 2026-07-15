"""Read-only local web layer over the already-built triage backend.

This package NEVER writes: no Decision row, no Job.status mutation. It reads the
jobs the user has already classified in the CLI and renders them by bucket so the
user can act (copy a message, open the posting, pick a CV).

Run it from the repo root:  python -m v2.webapp
(opens http://127.0.0.1:8765 in the browser; Ctrl-C to stop. Must run from the
repo root, or with the root on PYTHONPATH, so `v2`/`shared` import.)
"""
