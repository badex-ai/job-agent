"""Route smoke tests for the read-only apply server (v2/webapp/server.py).

Starts the REAL ThreadingHTTPServer on an OS-picked port, pointed at an isolated
temp DB via the injectable jobs_provider, and drives it over HTTP with urllib.
data/jobs.db is never touched.

The load-bearing test is `test_no_write_path_reachable`: POST/PUT/DELETE must NOT
be served (handler defines do_GET only → base class answers 501). That absence is
the read-only invariant from plan §6, pinned here so a future edit that adds a
write verb fails loudly.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.database.models import Base, Job, migrate_decisions
from shared.database.decisions import record_decision
from v2.webapp.queries import get_classified_jobs
from v2.webapp.server import make_server


@pytest.fixture
def Session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test_jobs.db'}", echo=False)
    Base.metadata.create_all(engine)
    migrate_decisions(engine)
    return sessionmaker(bind=engine)


def _seed_decided(Session, job_id, label):
    s = Session()
    s.add(Job(
        id=job_id, title=f"Role {job_id}", company="Acme", location="Remote",
        description="Build the thing.", apply_link=f"https://ex.com/{job_id}",
        status="new", gate_verdict="surface", gate_reason="fit",
        gate_tags='["S6"]', gate_model="qwen2.5:14b",
    ))
    s.commit()
    record_decision(s.get(Job, job_id), label, session=s)
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

    httpd = make_server(host="127.0.0.1", port=0, jobs_provider=temp_provider,
                        cvs_provider=lambda: ["cv_devops", "cv_frontend"])
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


def test_health(server):
    status, _, data = _get_json(server, "/api/health")
    assert status == 200
    assert data == {"status": "ok"}


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, "/api/nope")
    assert exc.value.code == 404


# --- the read-only invariant (load-bearing) --------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_no_write_path_reachable(server, method):
    """Write verbs must not be served: the handler defines do_GET only, so the
    base class answers 501. This is the plan §6 read-only invariant."""
    req = urllib.request.Request(server + "/api/jobs", method=method, data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 501


def test_post_to_root_also_rejected(server):
    req = urllib.request.Request(server + "/", method="POST", data=b"x")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 501
