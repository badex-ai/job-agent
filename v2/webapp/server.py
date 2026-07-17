"""The local server for the apply UI: read endpoints + one write seam.

Stdlib `http.server` only — no Flask, no dependency. It binds 127.0.0.1 and
serves the single `index.html` page, a small JSON read endpoint the page
fetches, and ONE write endpoint (`POST /api/action`) that records the Apply /
Dismiss / Hold / Restore / status actions.

The write surface is deliberately a single method on a single path so it can be
guarded in one place. GET remains read-only. The action handler NEVER queries or
mutates directly — it parses, guards, and dispatches to `v2.webapp.applications`,
which owns the append-only Application log and the (immutable) gate-verdict
invariant. There is no code path here that writes Job.gate_* or records a
Decision.

Write guards (a browser can reach this port, so localhost is not "trusted"):
  1. JSON-only — the body must be `application/json`. An HTML <form> cannot send
     that content-type cross-origin without a CORS preflight, so this alone
     defeats classic form-based CSRF against the write path.
  2. Host-header check — the Host must be 127.0.0.1/localhost:<port>. This blocks
     DNS-rebinding, where a hostile page resolves its own domain to 127.0.0.1 and
     POSTs to this server; the browser sends the attacker's Host, which we reject.
  3. Bounded body — Content-Length is required and capped (_MAX_BODY), so a
     missing or oversized body is refused rather than read unbounded.

Concurrency note: ThreadingHTTPServer can run two writes at once. This is a
single-human tool (one person clicking buttons), so in practice writes are
serial; SQLite's own file lock serialises anything that does overlap, worst case
a transient "database is locked". No app-level lock is added for that reason.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from v2.webapp import applications
from v2.webapp.queries import get_classified_jobs, bucket_counts, list_cvs

# The page lives next to this module. It may not exist yet (wired in build step
# 3); the handler degrades to a plain placeholder rather than 500-ing so the
# server + API are testable before the mockup lands.
STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

HOST = "127.0.0.1"
PORT = 8765

# Cap the write body: action payloads are a job id + a short note, never large.
_MAX_BODY = 64 * 1024
# Host values allowed on a write (host:port is stripped to host before compare).
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}

_PLACEHOLDER = (
    "<!doctype html><meta charset='utf-8'><title>Apply UI</title>"
    "<p>The page is not wired yet. Data is live at "
    "<a href='/api/jobs'>/api/jobs</a>.</p>"
)


def _payload(jobs_provider):
    """Assemble the one read-only payload the page consumes: buckets + counts."""
    buckets = jobs_provider()
    return {"buckets": buckets, "counts": bucket_counts(buckets)}


class _ActionError(Exception):
    """A write request the client got wrong. Carries the HTTP status to return
    (400 for bad input, 409 for a conflicting state like double-apply)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def dispatch_action(body: dict) -> dict:
    """Route one parsed write request to the applications write seam.

    `body` is the decoded JSON object. Returns the new Application row as a dict.
    Raises _ActionError(400) on a malformed request (unknown action, missing
    job_id, bad status, or an illegal state transition surfaced as ValueError by
    the applications layer) and _ActionError(409) when apply's mark_applied
    rejects (e.g. the job is already applied). This function does no I/O beyond
    delegating; applications.* owns the session and the commit.
    """
    action = body.get("action")
    job_id = body.get("job_id")
    note = body.get("note")
    if not isinstance(job_id, str) or not job_id:
        raise _ActionError(400, "missing or invalid job_id")
    # note reaches the DB column directly; a non-string (dict/list) would raise
    # a ProgrammingError inside the applications layer, not a ValueError, and
    # escape as an unanswered 500. Validate it here as str|None.
    if note is not None and not isinstance(note, str):
        raise _ActionError(400, "note must be a string")

    try:
        if action == "apply":
            row, err = applications.apply_to_job(job_id, note=note)
            if err is not None:
                # mark_applied refused (unknown job / already applied): a
                # conflict with existing state, not a malformed request.
                raise _ActionError(409, err)
            return row
        if action == "dismiss":
            return applications.dismiss_job(job_id, note=note)
        if action == "hold":
            return applications.hold_job(job_id, note=note)
        if action == "restore":
            return applications.restore_job(job_id)
        if action == "set_status":
            status = body.get("status")
            if not isinstance(status, str):
                raise _ActionError(400, "set_status requires a status string")
            return applications.set_status(job_id, status)
    except ValueError as exc:
        # Illegal transition (restore a non-held job, status on an unapplied
        # job, unknown status value) — the client asked for something the state
        # machine forbids.
        raise _ActionError(400, str(exc)) from exc

    raise _ActionError(400, f"unknown action {action!r}")


def make_handler(jobs_provider=get_classified_jobs, cvs_provider=list_cvs):
    """Build the request-handler class, closing over the classified-jobs reader.

    `jobs_provider` is injectable so tests can point the server at a temp DB
    without touching data/jobs.db. It must return the same shape as
    `get_classified_jobs`: {bucket: [job_view, ...]}. `cvs_provider` is the
    same seam for the CV picker list (plan §4/§6).
    """

    class Handler(BaseHTTPRequestHandler):
        # Bound a stalled request: without this, a client that declares a large
        # Content-Length but never sends the body would block a handler thread
        # forever on rfile.read (BaseHTTPRequestHandler sets no default timeout).
        timeout = 10

        # GET is read-only; POST is the single guarded write seam (/api/action).
        # PUT/DELETE stay undefined so the base class answers them 501.
        def do_GET(self):
            # The Host check is a DNS-rebind defence and it must guard READS too:
            # rebinding's payoff against a localhost service is reading /api/jobs
            # (the whole job DB + hold notes) from a hostile origin. Reject any
            # request whose Host is not localhost, GET or POST alike.
            if not self._host_is_local():
                self._serve_error(403, "bad host")
                return
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

        def do_POST(self):
            if not self._host_is_local():
                self._serve_error(403, "bad host")
                return
            path = self.path.split("?", 1)[0]
            if path != "/api/action":
                # No write path here; a POST elsewhere is a plain 404, not 501,
                # now that the server does serve one write route.
                self._serve_error(404, "not found")
                return
            body = self._read_write_body()
            if body is None:
                return  # _read_write_body already sent the error response
            try:
                row = dispatch_action(body)
            except _ActionError as exc:
                self._serve_error(exc.status, exc.message)
                return
            except Exception:
                # Catch-all: any non-ValueError from the applications layer (a
                # SQLite OperationalError 'database is locked', a schema error,
                # etc.) must become an honest 500 the page can show, not an
                # unanswered dropped connection. Deliberately generic message —
                # no str(exc) — so internal detail never leaks to the client.
                self._serve_error(500, "internal error")
                return
            self._serve_json({"ok": True, "row": row})

        def _host_is_local(self):
            """True if the Host header names localhost (host:port -> host). The
            DNS-rebind guard shared by GET and POST; a missing Host fails it."""
            host = self.headers.get("Host", "").rsplit(":", 1)[0]
            return host in _LOCAL_HOSTS

        def _read_write_body(self):
            """Guard + parse a write body. Returns the decoded dict, or None
            after having sent the appropriate error response.

            The Host check ran in do_POST already. Guards here, in order:
            Content-Type (JSON-only, anti CSRF), bounded Content-Length, valid
            JSON object.
            """
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if ctype != "application/json":
                self._serve_error(415, "expected application/json")
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                # Absent or non-numeric Content-Length on a body we must size.
                self._serve_error(400, "missing or invalid content-length")
                return None
            if length <= 0:
                self._serve_error(400, "empty body")
                return None
            if length > _MAX_BODY:
                self._serve_error(413, "body too large")
                return None
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except ValueError:
                self._serve_error(400, "invalid json")
                return None
            if not isinstance(body, dict):
                self._serve_error(400, "json body must be an object")
                return None
            return body

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
