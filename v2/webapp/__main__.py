"""Entrypoint: `python -m v2.webapp` from the repo root.

Binds 127.0.0.1:8765, opens the page in the default browser, and serves until
Ctrl-C. Localhost only — the apply UI is a single-user tool and never listens on
a public interface.

Must be run from the repo root (or with the repo root on PYTHONPATH); a bare run
from another directory can't import the `v2` / `shared` packages.
"""
import sys
import threading
import webbrowser

from shared.database.models import (
    migrate_applications, migrate_application_columns,
)
from v2.webapp.server import HOST, PORT, make_server


def main():
    # Bring the applications schema up to date before serving. The read queries
    # touch columns (e.g. `cv`) that create_all() never adds to a table that
    # already exists, so on a DB predating a column the first /api/jobs query
    # would raise "no such column" and surface as a misleading "server down" in
    # the UI. Both calls are idempotent no-ops once the schema is current.
    migrate_applications()
    migrate_application_columns()

    try:
        httpd = make_server(HOST, PORT)
    except OSError as exc:
        # Almost always "address already in use" — a previous run is still up.
        print(f"Could not bind http://{HOST}:{PORT} — {exc}.\n"
              f"Is the apply UI already running? Close it, or free the port.",
              file=sys.stderr)
        return 1

    url = f"http://{HOST}:{PORT}/"
    print(f"Apply UI on {url}  (read-only · Ctrl-C to stop)")

    # Open the browser once the server is actually accepting connections. Fire it
    # on a short timer so serve_forever() is already looping when the tab loads.
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
