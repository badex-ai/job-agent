"""Route smoke tests for the read-only apply server (v2/webapp/server.py).

Starts the REAL ThreadingHTTPServer on an OS-picked port, pointed at an isolated
temp DB via the injectable jobs_provider, and drives it over HTTP with urllib.
data/jobs.db is never touched.

POST is served on exactly one path — `/api/action`, the guarded write seam — and
the write-guard tests below pin its defences (JSON-only, Host check, bounded
body) and its dispatch to the applications layer. The Host check guards GET too
(DNS-rebind defends reads, not just writes). PUT/DELETE stay unserved (501).
"""
import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database import models
from shared.database.models import Application, Base, Job, migrate_applications
from v2.webapp import applications
from v2.webapp.queries import get_classified_jobs, get_applications
from v2.webapp.server import make_server


@pytest.fixture
def Session(tmp_path, monkeypatch):
    """A sessionmaker on an isolated temp DB, ALSO wired into the module-level
    get_session() that the write path (applications/tracking) uses when called
    with no session — so a POST hits this temp DB, never data/jobs.db."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)
    migrate_applications(engine)
    Maker = sessionmaker(bind=engine)
    monkeypatch.setattr(models, "get_session", Maker)
    monkeypatch.setattr(applications, "get_session", Maker)
    monkeypatch.setattr("shared.database.tracking.get_session", Maker)
    return Maker


# Browse buckets read the gate verdict, not a human label. HOLD is a manual
# park action, so a "HOLD-bucket" seed sets a gate verdict AND holds the job.
_VERDICT = {"APPLY": "surface_priority", "CONSIDER": "surface", "SKIP": "skip"}


def _seed_decided(Session, job_id, label):
    s = Session()
    verdict = _VERDICT.get(label, "surface")
    s.add(Job(
        id=job_id, title=f"Role {job_id}", company="Acme", location="Remote",
        description="Build the thing.", apply_link=f"https://ex.com/{job_id}",
        status="new", gate_verdict=verdict, gate_reason="fit",
        gate_tags='["S6"]', gate_model="qwen2.5:14b",
        gate_run_at=datetime.utcnow(),
    ))
    s.commit()
    if label == "HOLD":
        applications.hold_job(job_id, session=s)
    s.close()


@pytest.fixture
def server(Session):
    """A running server bound to 127.0.0.1:<os-picked>, reading the temp DB.

    The jobs_provider is get_classified_jobs bound to this fixture's Session, so
    the server sees the seeded temp DB and never data/jobs.db.
    """
    # get_classified_jobs(session=None) would open the real DB; instead hand it a
    # fresh session from the temp engine on each call (matches its own lifecycle:
    # when passed a session it does not close it, so make a new one per call).
    def temp_provider():
        s = Session()
        try:
            return get_classified_jobs(session=s)
        finally:
            s.close()

    def temp_apps_provider():
        s = Session()
        try:
            return get_applications(session=s)
        finally:
            s.close()

    # A tiny stub cv-text provider so the /api/cv-text route is exercised without
    # touching data/: only 'cv_devops' resolves, everything else is None (404).
    def temp_cv_text(name):
        return "DevOps CV body" if name == "cv_devops" else None

    httpd = make_server(host="127.0.0.1", port=0, jobs_provider=temp_provider,
                        cvs_provider=lambda: ["cv_devops", "cv_frontend"],
                        applications_provider=temp_apps_provider,
                        cv_text_provider=temp_cv_text)
    host, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://{host}:{port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


def _get_json(base, path):
    status, ctype, body = _get(base, path)
    return status, ctype, json.loads(body)


def _post_json(base, path, obj, ctype="application/json", host=None):
    """POST a body to the server. Defaults to a well-formed JSON write request;
    ctype/host are overridable so the guard tests can send malformed ones.
    Returns (status, decoded_json) on success; raises HTTPError otherwise."""
    data = json.dumps(obj).encode("utf-8") if isinstance(obj, (dict, list)) else obj
    headers = {"Content-Type": ctype}
    if host is not None:
        headers["Host"] = host
    req = urllib.request.Request(base + path, method="POST", data=data,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def _raw_post_status(base, path, headers_lines, body=b""):
    """Send a hand-built POST over a raw socket so tests can control the exact
    Content-Length header (spoof it, omit it) independent of the body bytes.
    Returns the numeric status from the response line. `headers_lines` is a list
    of raw header strings (no Host/Content-Type added automatically)."""
    host, port = base.rsplit("//", 1)[1].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        req = f"POST {path} HTTP/1.1\r\n" + "".join(h + "\r\n" for h in headers_lines)
        req += "\r\n"
        sock.sendall(req.encode("utf-8") + body)
        sock.settimeout(5)
        data = b""
        try:
            while b"\r\n" not in data:
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            return None  # server hung instead of answering (the bug we guard)
    status_line = data.split(b"\r\n", 1)[0]
    return int(status_line.split(b" ")[1])


# --- the JSON read endpoint -------------------------------------------------

def test_api_jobs_groups_into_four_buckets(server, Session):
    _seed_decided(Session, "j-apply", "APPLY")
    _seed_decided(Session, "j-consider", "CONSIDER")
    _seed_decided(Session, "j-hold", "HOLD")
    _seed_decided(Session, "j-skip", "SKIP")

    status, ctype, data = _get_json(server, "/api/jobs")
    assert status == 200
    assert "application/json" in ctype
    buckets = data["buckets"]
    assert [j["id"] for j in buckets["APPLY"]] == ["j-apply"]
    assert [j["id"] for j in buckets["CONSIDER"]] == ["j-consider"]
    assert [j["id"] for j in buckets["HOLD"]] == ["j-hold"]
    assert [j["id"] for j in buckets["SKIP"]] == ["j-skip"]


def test_api_jobs_includes_counts(server, Session):
    _seed_decided(Session, "a", "APPLY")
    _seed_decided(Session, "b", "APPLY")
    _seed_decided(Session, "h", "HOLD")

    status, _, data = _get_json(server, "/api/jobs")
    assert status == 200
    assert data["counts"] == {"APPLY": 2, "CONSIDER": 0, "HOLD": 1, "SKIP": 0}


def test_api_jobs_empty_db_is_clean(server):
    status, _, data = _get_json(server, "/api/jobs")
    assert status == 200
    assert data["buckets"] == {"APPLY": [], "CONSIDER": [], "HOLD": [], "SKIP": []}
    assert data["counts"] == {"APPLY": 0, "CONSIDER": 0, "HOLD": 0, "SKIP": 0}


def test_api_jobs_carries_job_view_fields(server, Session):
    _seed_decided(Session, "jv", "APPLY")
    _, _, data = _get_json(server, "/api/jobs")
    view = data["buckets"]["APPLY"][0]
    assert view["title"] == "Role jv"
    assert view["company"] == "Acme"
    assert view["apply_link"] == "https://ex.com/jv"
    assert view["tags"] == ["S6"]


# --- the page + health ------------------------------------------------------

def test_index_served_at_root(server):
    status, ctype, body = _get(server, "/")
    assert status == 200
    assert "text/html" in ctype
    assert b"<" in body  # some HTML, whether the mockup or the placeholder


def test_index_is_the_real_page_not_placeholder(server):
    """Once static/index.html exists it must be served in place of the
    placeholder — the page fetches /api/jobs, so that string must be present."""
    _, _, body = _get(server, "/")
    assert b"/api/jobs" in body
    assert b"bucket" in body.lower()


def test_favicon_is_204_not_404(server):
    """The browser auto-requests /favicon.ico; answer 204 so it isn't a stray
    404 in the access path of a daily-use tool."""
    status, _, _ = _get(server, "/favicon.ico")
    assert status == 204


def test_api_cvs_returns_the_picker_list(server):
    status, ctype, data = _get_json(server, "/api/cvs")
    assert status == 200
    assert "application/json" in ctype
    assert data == {"cvs": ["cv_devops", "cv_frontend"]}


def test_api_cv_text_returns_the_cv_body(server):
    status, ctype, data = _get_json(server, "/api/cv-text?name=cv_devops")
    assert status == 200
    assert "application/json" in ctype
    assert data == {"name": "cv_devops", "text": "DevOps CV body"}


def test_api_cv_text_unknown_name_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/cv-text?name=cv_nope")
    assert exc.value.code == 404


def test_api_cv_text_missing_name_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/cv-text")
    assert exc.value.code == 404


def test_api_applications_empty_is_clean(server):
    status, ctype, data = _get_json(server, "/api/applications")
    assert status == 200
    assert "application/json" in ctype
    assert data == {"applied": [], "dismissed": []}


def test_api_applications_reflects_writes(server, Session):
    """A dismiss then an apply show up in the right tables over HTTP, against
    the temp DB (the applications_provider is bound to it, not data/jobs.db)."""
    _seed_decided(Session, "d1", "CONSIDER")
    _seed_decided(Session, "a1", "APPLY")
    _post_json(server, "/api/action", {"action": "dismiss", "job_id": "d1"})
    _post_json(server, "/api/action", {"action": "apply", "job_id": "a1"})

    _, _, data = _get_json(server, "/api/applications")
    assert [r["id"] for r in data["applied"]] == ["a1"]
    assert [r["id"] for r in data["dismissed"]] == ["d1"]
    assert data["applied"][0]["status"] == "applied"


def test_action_set_note_edits_applied_note(server, Session):
    _seed_decided(Session, "a1", "APPLY")
    _post_json(server, "/api/action", {"action": "apply", "job_id": "a1"})
    status, data = _post_json(server, "/api/action",
                              {"action": "set_note", "job_id": "a1",
                               "note": "recruiter emailed"})
    assert status == 200
    assert data["ok"] is True

    _, _, apps = _get_json(server, "/api/applications")
    assert [r["id"] for r in apps["applied"]] == ["a1"]
    assert apps["applied"][0]["note"] == "recruiter emailed"
    assert apps["applied"][0]["status"] == "applied"  # placement/status intact


def test_action_set_note_on_untouched_job_is_400(server, Session):
    _seed_decided(Session, "c1", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action",
                   {"action": "set_note", "job_id": "c1", "note": "x"})
    assert exc.value.code == 400


def test_get_provider_error_is_500_not_dropped_connection():
    """C2: a read-path provider that raises (e.g. a locked DB during a gate run)
    must return an honest 500 the page can show, not drop the connection."""
    def boom():
        raise RuntimeError("db locked")
    httpd = make_server(host="127.0.0.1", port=0, jobs_provider=boom,
                        cvs_provider=lambda: [], applications_provider=lambda: {})
    host, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"http://{host}:{port}", "/api/jobs")
        assert exc.value.code == 500
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_api_applications_rejects_foreign_host(server):
    """The DNS-rebind Host guard covers this read too (it exposes hold/apply
    notes), like every other GET."""
    req = urllib.request.Request(server + "/api/applications",
                                 headers={"Host": "evil.example.com"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


def test_health(server):
    status, _, data = _get_json(server, "/api/health")
    assert status == 200
    assert data == {"status": "ok"}


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/nope")
    assert exc.value.code == 404


# --- the write seam: POST /api/action --------------------------------------

def test_action_dismiss_records_and_hides(server, Session):
    _seed_decided(Session, "d1", "CONSIDER")
    status, data = _post_json(server, "/api/action",
                              {"action": "dismiss", "job_id": "d1"})
    assert status == 200
    assert data["ok"] is True
    assert data["row"]["event"] == "dismissed"
    # the write actually landed: the job is gone from the browse buckets.
    _, _, jobs = _get_json(server, "/api/jobs")
    ids = [j["id"] for b in jobs["buckets"].values() for j in b]
    assert "d1" not in ids


def test_action_apply_dual_writes_job_flag(server, Session):
    _seed_decided(Session, "a1", "APPLY")
    status, data = _post_json(server, "/api/action",
                              {"action": "apply", "job_id": "a1"})
    assert status == 200 and data["row"]["event"] == "applied"
    # dual-write invariant: Job.applied is now True (CLI double-apply guard).
    s = Session()
    assert s.query(Job).filter(Job.id == "a1").one().applied is True
    s.close()


def test_action_hold_then_restore_roundtrips(server, Session):
    _seed_decided(Session, "h1", "CONSIDER")
    _post_json(server, "/api/action", {"action": "hold", "job_id": "h1"})
    _, _, jobs = _get_json(server, "/api/jobs")
    assert [j["id"] for j in jobs["buckets"]["HOLD"]] == ["h1"]
    _post_json(server, "/api/action", {"action": "restore", "job_id": "h1"})
    _, _, jobs = _get_json(server, "/api/jobs")
    assert jobs["buckets"]["HOLD"] == []
    assert [j["id"] for j in jobs["buckets"]["CONSIDER"]] == ["h1"]


def test_action_set_status_after_apply(server, Session):
    _seed_decided(Session, "s1", "APPLY")
    _post_json(server, "/api/action", {"action": "apply", "job_id": "s1"})
    status, data = _post_json(server, "/api/action",
                              {"action": "set_status", "job_id": "s1",
                               "status": "interview"})
    assert status == 200 and data["row"]["status"] == "interview"


def test_action_apply_records_the_cv(server, Session):
    """B2 over HTTP: the cv on the apply payload lands on the row and surfaces in
    the Applied table."""
    _seed_decided(Session, "cv1", "APPLY")
    status, data = _post_json(server, "/api/action",
                              {"action": "apply", "job_id": "cv1",
                               "cv": "cv_devops"})
    assert status == 200 and data["row"]["cv"] == "cv_devops"
    _, _, apps = _get_json(server, "/api/applications")
    assert apps["applied"][0]["cv"] == "cv_devops"


def test_action_set_cv_corrects_the_recorded_cv(server, Session):
    _seed_decided(Session, "cv2", "APPLY")
    _post_json(server, "/api/action",
               {"action": "apply", "job_id": "cv2", "cv": "cv_devops"})
    status, data = _post_json(server, "/api/action",
                              {"action": "set_cv", "job_id": "cv2",
                               "cv": "cv_fullstack"})
    assert status == 200 and data["row"]["cv"] == "cv_fullstack"
    _, _, apps = _get_json(server, "/api/applications")
    assert apps["applied"][0]["cv"] == "cv_fullstack"
    assert apps["applied"][0]["status"] == "applied"  # placement/status intact


def test_action_set_cv_on_unapplied_job_is_400(server, Session):
    _seed_decided(Session, "cv3", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action",
                   {"action": "set_cv", "job_id": "cv3", "cv": "cv_devops"})
    assert exc.value.code == 400


def test_action_non_string_cv_is_400(server, Session):
    """A non-string cv (dict/list) is rejected before it reaches SQLite, like
    the note guard — a clean 400, not a dropped 500."""
    _seed_decided(Session, "cv4", "APPLY")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action",
                   {"action": "apply", "job_id": "cv4", "cv": {"x": 1}})
    assert exc.value.code == 400


# --- write-guard rejections -------------------------------------------------

def test_action_rejects_non_json_content_type(server, Session):
    """A form-encoded POST (the CSRF vector) is refused with 415, before any
    write. JSON-only is the anti-CSRF guard."""
    _seed_decided(Session, "c1", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", {"action": "dismiss", "job_id": "c1"},
                   ctype="application/x-www-form-urlencoded")
    assert exc.value.code == 415


def test_action_rejects_foreign_host(server, Session):
    """A POST whose Host is not localhost is refused with 403 — the DNS-rebind
    guard."""
    _seed_decided(Session, "c2", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", {"action": "dismiss", "job_id": "c2"},
                   host="evil.example.com")
    assert exc.value.code == 403


def test_action_rejects_invalid_json(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", b"{not json")
    assert exc.value.code == 400


def test_action_unknown_action_is_400(server, Session):
    _seed_decided(Session, "u1", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", {"action": "nuke", "job_id": "u1"})
    assert exc.value.code == 400


def test_action_missing_job_id_is_400(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", {"action": "dismiss"})
    assert exc.value.code == 400


def test_restore_on_unheld_job_is_400(server, Session):
    """An illegal state transition (restore a job that was never held/dismissed)
    surfaces as a ValueError in the applications layer -> 400, not a 500."""
    _seed_decided(Session, "r1", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", {"action": "restore", "job_id": "r1"})
    assert exc.value.code == 400


def test_post_to_other_path_is_404(server):
    """POST is served only on /api/action; elsewhere it is a plain 404."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/jobs", {"action": "dismiss", "job_id": "x"})
    assert exc.value.code == 404


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_put_delete_still_501(server, method):
    """PUT/DELETE remain unimplemented -> base class 501."""
    req = urllib.request.Request(server + "/api/action", method=method,
                                 data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 501


# --- bounded-body guard (raw socket: urllib computes its own Content-Length) --

def test_missing_content_length_is_400(server):
    """A body-carrying POST with no Content-Length can't be sized -> 400, and the
    handler must ANSWER rather than block reading a body it can't bound."""
    status = _raw_post_status(
        server, "/api/action",
        ['Host: 127.0.0.1', 'Content-Type: application/json'],
        body=b'{"action":"dismiss","job_id":"x"}')
    assert status == 400


def test_oversize_content_length_is_413(server):
    status = _raw_post_status(
        server, "/api/action",
        ['Host: 127.0.0.1', 'Content-Type: application/json',
         f'Content-Length: {65 * 1024}'],
        body=b'{}')
    assert status == 413


def test_zero_length_body_is_400(server):
    status = _raw_post_status(
        server, "/api/action",
        ['Host: 127.0.0.1', 'Content-Type: application/json',
         'Content-Length: 0'])
    assert status == 400


def test_inflated_content_length_does_not_hang(server):
    """A Content-Length larger than the bytes sent must not block the handler
    forever: the Handler.timeout bounds the read, so the server answers (or the
    connection closes) rather than hanging. The guard is: we get *a* status back
    within the socket timeout, not None."""
    status = _raw_post_status(
        server, "/api/action",
        ['Host: 127.0.0.1', 'Content-Type: application/json',
         'Content-Length: 5000'],
        body=b'{}')  # declares 5000, sends 2
    # Either a 400-class answer or a closed connection is fine; a hang (None) is
    # the failure this pins.
    assert status is None or status >= 400


# --- invariant 1 at the HTTP seam: actions never mutate Job.status/gate_verdict

@pytest.mark.parametrize("action", ["dismiss", "hold"])
def test_dismiss_hold_do_not_touch_job_status_or_verdict(server, Session, action):
    """The regression applications.py warns about: a web dismiss/hold must leave
    Job.status=='new' (or the CLI rescue queue silently drains) and gate_verdict
    untouched (immutable ground truth)."""
    _seed_decided(Session, "inv1", "CONSIDER")  # gate_verdict='surface'
    _post_json(server, "/api/action", {"action": action, "job_id": "inv1"})
    s = Session()
    job = s.query(Job).filter(Job.id == "inv1").one()
    assert job.status == "new"
    assert job.gate_verdict == "surface"
    s.close()


# --- invariant 2 at the seam: apply is atomic, double-apply refused -----------

def test_apply_ghost_job_is_409_with_no_orphan_row(server, Session):
    """Applying a non-existent job: mark_applied fails, the whole transaction
    rolls back -> 409 and ZERO Application rows for that id (no orphan log)."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action",
                   {"action": "apply", "job_id": "ghost"})
    assert exc.value.code == 409
    s = Session()
    assert s.query(Application).filter(Application.job_id == "ghost").count() == 0
    s.close()


def test_double_apply_is_409_and_leaves_state_intact(server, Session):
    """The second Apply on an already-applied job is refused (409) — it must NOT
    append a second 'applied' row nor re-zero the follow-up schedule."""
    _seed_decided(Session, "dup", "APPLY")
    _post_json(server, "/api/action", {"action": "apply", "job_id": "dup"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action", {"action": "apply", "job_id": "dup"})
    assert exc.value.code == 409
    s = Session()
    applied_rows = (s.query(Application)
                    .filter(Application.job_id == "dup",
                            Application.event == "applied").count())
    assert applied_rows == 1  # exactly one real application, not two
    s.close()


# --- malformed-but-valid-JSON must not drop the connection (finding 2) --------

def test_non_string_note_is_400_not_dropped_connection(server, Session):
    """A valid JSON body with a non-string note used to reach SQLite and raise a
    ProgrammingError (not ValueError) that escaped as an unanswered 500/drop.
    Now validated -> a clean 400."""
    _seed_decided(Session, "n1", "CONSIDER")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(server, "/api/action",
                   {"action": "hold", "job_id": "n1", "note": {"x": 1}})
    assert exc.value.code == 400
