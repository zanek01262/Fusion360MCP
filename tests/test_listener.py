"""MCPListener outside Fusion: the request guard over real HTTP, the job
store and watchdog, the token file, and the injected lookup helpers."""
import http.client
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

TOKEN = "test-token-123"


@pytest.fixture
def server(lst):
    """The real RequestHandler on a random localhost port."""
    lst._token[0] = TOKEN
    httpd = lst.ThreadingHTTPServer(("127.0.0.1", 0), lst.RequestHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    lst._token[0] = None


def call(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host or "127.0.0.1:%d" % port)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    data = body.encode("utf-8") if body is not None else b""
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    r = conn.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    conn.close()
    return out


AUTH = {"X-MCP-Token": TOKEN}
CODE = json.dumps({"code": "print(1)"})


def test_ping_needs_no_token(server):
    status, d = call(server, "GET", "/ping")
    assert status == 200 and d["protocol"] == 3


def test_state_and_jobs_need_the_token(server):
    assert call(server, "GET", "/state")[0] == 401
    assert call(server, "GET", "/job/1")[0] == 401
    assert call(server, "GET", "/state", headers=AUTH)[0] == 200


def test_browser_style_post_without_token_is_refused_and_not_queued(server, lst):
    """What a web page can send with no CORS preflight: text/plain, no custom
    header. It used to be executed."""
    n = len(lst._jobs)
    status, d = call(server, "POST", "/exec", CODE,
                     headers={"Content-Type": "text/plain;charset=UTF-8"})
    assert status == 401 and d["error"] == "bad_token"
    assert d["protocol"] == 3        # so the server can explain the refusal
    assert len(lst._jobs) == n


def test_wrong_token_is_refused(server):
    assert call(server, "POST", "/exec", CODE, headers={"X-MCP-Token": "guess"})[0] == 401


def test_dns_rebinding_host_is_refused_even_with_the_token(server):
    status, d = call(server, "POST", "/exec", CODE, headers=AUTH,
                     host="attacker.example:8765")
    assert status == 403 and d["error"] == "forbidden_host"
    assert call(server, "GET", "/ping", host="attacker.example")[0] == 403


def test_authorised_post_is_queued(server, lst):
    status, d = call(server, "POST", "/exec", CODE, headers=AUTH)
    assert status == 200 and d["status"] == "queued"
    assert d["job_id"] in lst._jobs
    status, _ = call(server, "POST", "/exec", CODE, headers=AUTH,
                     host="localhost:%d" % server)
    assert status == 200


def test_no_token_configured_refuses_everything(server, lst):
    lst._token[0] = None             # listener not fully started
    assert call(server, "POST", "/exec", CODE, headers=AUTH)[0] == 401


# ---------------------------------------------------------------- job store

def test_overrun_is_flagged_but_the_job_still_finishes_ok(lst):
    job = {"id": "t1", "status": "running", "code": "print('done')",
           "started": time.time() - 100, "budget_s": 1.0, "stdout": [],
           "traceback": "", "finished": None, "elapsed_ms": None}
    lst._jobs["t1"] = job
    try:
        lst._flag_overruns(time.time())
        assert job["budget_exceeded"] and job["status"] == "running"
        job["status"] = "queued"
        lst._execute_job(job)
        assert job["status"] == "ok"        # used to stay 'timeout' forever
        env = lst._envelope(job)
        assert env["status"] == "ok" and env["budget_exceeded"] is True
        assert "done" in env["stdout"]
    finally:
        lst._jobs.pop("t1", None)


def test_watchdog_exits_when_stopped(lst):
    ev = threading.Event()
    t = threading.Thread(target=lst._watchdog, args=(ev,), daemon=True)
    t.start()
    ev.set()
    t.join(timeout=3)
    assert not t.is_alive()


def test_token_file_is_written_and_removed(lst, tmp_path, monkeypatch):
    monkeypatch.setattr(lst, "TOKEN_DIR", str(tmp_path / ".fusion_mcp"))
    monkeypatch.setattr(lst, "TOKEN_PATH", str(tmp_path / ".fusion_mcp" / "listener_token"))
    tok = lst._write_token()
    with open(lst.TOKEN_PATH, encoding="utf-8") as f:
        assert f.read() == tok and len(tok) >= 32
    if sys.platform != "win32":
        assert (os.stat(lst.TOKEN_PATH).st_mode & 0o777) == 0o600
    lst._remove_token()
    assert not os.path.exists(lst.TOKEN_PATH) and lst._token[0] is None


# ---------------------------------------------------------------- helpers

def occ(name, comp, path=None):
    return SimpleNamespace(name=name, fullPathName=path or name,
                           component=SimpleNamespace(name=comp))


@pytest.fixture
def helpers(lst):
    """The helper source exactly as injected into every job, against a fake
    design with one Lid and two copies of Screw."""
    root = SimpleNamespace(name="root", allOccurrences=[
        occ("Lid:1", "Lid"), occ("Screw:1", "Screw"),
        occ("Screw:2", "Screw", path="Sub:1+Screw:2")])
    ns = {"adsk": None, "app": None}
    exec(compile(lst._HELPER_SOURCE, "<helpers>", "exec"), ns)
    ns["_require_design"] = lambda: SimpleNamespace(rootComponent=root)
    ns["root"] = root
    return ns


def test_find_occurrence_picks_one_copy_by_name(helpers):
    assert helpers["find_occurrence"]("Screw:2").name == "Screw:2"
    assert helpers["find_occurrence"]("Sub:1+Screw:2").name == "Screw:2"
    assert helpers["find_occurrence"]("Lid").name == "Lid:1"   # unambiguous


def test_find_occurrence_refuses_an_ambiguous_component_name(helpers):
    with pytest.raises(RuntimeError, match=r"2 occurrences \(Screw:1, Screw:2\)"):
        helpers["find_occurrence"]("Screw")


def test_find_occurrence_miss_lists_what_exists(helpers):
    with pytest.raises(RuntimeError, match="Available: Lid:1"):
        helpers["find_occurrence"]("Lidd")


def test_find_component_never_falls_back_to_root(helpers):
    assert helpers["find_component"]("") is helpers["root"]
    assert helpers["find_component"]("Screw:2").name == "Screw"
    with pytest.raises(RuntimeError, match="no component named 'lid'"):
        helpers["find_component"]("lid")


# ---------------------------------------------------------------- active doc

class Doc:
    """Identity equality, like distinct Fusion documents."""


def test_active_index_uses_names_when_unique(lst):
    docs = [Doc(), Doc()]
    ids = [{"name": "A"}, {"name": "B"}]
    assert lst._active_index(docs, ids, docs[1], "B") == 1


def test_active_index_breaks_a_name_tie_by_identity(lst):
    docs = [Doc(), Doc()]
    ids = [{"name": "Untitled"}, {"name": "Untitled"}]
    assert lst._active_index(docs, ids, docs[1], "Untitled") == 1


def test_active_index_reports_ambiguity_instead_of_guessing(lst):
    class NeverEqual:
        def __eq__(self, other):
            return False
    docs = [NeverEqual(), NeverEqual()]
    ids = [{"name": "Untitled"}, {"name": "Untitled"}]
    assert lst._active_index(docs, ids, object(), "Untitled") == -1
