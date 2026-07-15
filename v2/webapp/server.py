"""The read-only local server for the apply UI.

Stdlib `http.server` only — no Flask, no dependency. It binds 127.0.0.1 and
serves two things: the single `index.html` page and a small JSON read endpoint
the page fetches. There are NO write routes: the handler implements `do_GET`
alone, so POST/PUT/DELETE fall through to the base class's 501. That absence IS
the read-only invariant (plan §6) — the server cannot record a Decision or move
a Job.status because it has no code path that writes, and the test pins it.

All DB access goes through `queries.get_classified_jobs`, which opens and closes
its own session. The server holds no session and constructs no query, so it
inherits the read-only guarantee from the one query module.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from v2.webapp.queries import get_classified_jobs, bucket_counts, list_cvs

# The page lives next to this module. It may not exist yet (wired in build step
# 3); the handler degrades to a plain placeholder rather than 500-ing so the
# server + API are testable before the mockup lands.
STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

HOST = "127.0.0.1"
PORT = 8765

_PLACEHOLDER = (
    "<!doctype html><meta charset='utf-8'><title>Apply UI</title>"
    "<p>The page is not wired yet. Data is live at "
    "<a href='/api/jobs'>/api/jobs</a>.</p>"
)


def _payload(jobs_provider):
    """Assemble the one read-only payload the page consumes: buckets + counts."""
    buckets = jobs_provider()
    return {"buckets": buckets, "counts": bucket_counts(buckets)}


def make_handler(jobs_provider=get_classified_jobs, cvs_provider=list_cvs):
    """Build the request-handler class, closing over the classified-jobs reader.

    `jobs_provider` is injectable so tests can point the server at a temp DB
    without touching data/jobs.db. It must return the same shape as
    `get_classified_jobs`: {bucket: [job_view, ...]}. `cvs_provider` is the
    same seam for the CV picker list (plan §4/§6).
    """

    class Handler(BaseHTTPRequestHandler):
        # Only GET is defined. POST/PUT/DELETE are intentionally absent so the
        # base class answers them 501 — the enforced read-only invariant.
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/" or path.startswith("/index.html"):
                self._serve_index()
            elif path == "/api/jobs":
                self._serve_json(_payload(jobs_provider))
            elif path == "/api/cvs":
                self._serve_json({"cvs": cvs_provider()})
            elif path == "/api/health":
                self._serve_json({"status": "ok"})
            elif path == "/favicon.ico":
                # No icon asset; answer 204 so the browser's automatic request
                # doesn't log a 404 on every page load.
                self.send_response(204)
                self.end_headers()
            else:
                self._serve_error(404, "not found")

        def _serve_index(self):
            if INDEX_FILE.exists():
                body = INDEX_FILE.read_bytes()
            else:
                body = _PLACEHOLDER.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")

        def _serve_json(self, obj):
            body = json.dumps(obj).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")

        def _serve_error(self, code, message):
            body = json.dumps({"error": message}).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # Quiet the default stderr access log; the app is a single-user local
        # tool and the per-request noise is not wanted.
        def log_message(self, *args):
            pass

    return Handler


def make_server(host=HOST, port=PORT, jobs_provider=get_classified_jobs,
                cvs_provider=list_cvs):
    """Create (but do not start) the HTTP server bound to host:port.

    Threading so a slow browser fetch never blocks the next request; single
    user, so this is comfort, not concurrency the DB has to survive. Pass
    port=0 to let the OS pick a free port (tests do this).
    """
    return ThreadingHTTPServer(
        (host, port), make_handler(jobs_provider, cvs_provider))


def serve(host=HOST, port=PORT):
    """Run the server forever (blocking). The entrypoint (step 4) calls this."""
    httpd = make_server(host, port)
    httpd.serve_forever()
