"""Server-side transport: token header, refusal messages, envelope formatting,
and the timeout message. No listener needed - requests is faked."""
import pytest
import requests


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


def envelope(**kw):
    d = {"protocol": 3, "status": "ok", "stdout": "", "traceback": "",
         "job_id": "5", "elapsed_ms": 10, "stdout_bytes": 0}
    d.update(kw)
    return d


def test_headers_read_the_token_fresh_each_time(srv, tmp_path, monkeypatch):
    tok = tmp_path / "listener_token"
    monkeypatch.setattr(srv, "TOKEN_PATH", str(tok))
    assert srv._headers() == {}
    tok.write_text("first\n", encoding="utf-8")
    assert srv._headers() == {"X-MCP-Token": "first"}
    tok.write_text("second", encoding="utf-8")        # add-in restarted
    assert srv._headers() == {"X-MCP-Token": "second"}


def test_refusals_are_explained(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "TOKEN_PATH", str(tmp_path / "missing"))
    assert "does not exist" in srv._refused(Resp(401))
    assert "Host" in srv._refused(Resp(403))
    assert srv._refused(Resp(200)) == ""


def test_old_listener_is_reported_as_a_protocol_mismatch(srv):
    assert srv._fmt_envelope({"protocol": 2, "status": "ok"}).startswith("PROTOCOL MISMATCH")


def test_budget_exceeded_while_running_is_not_a_failure(srv):
    out = srv._fmt_envelope(envelope(status="running", budget_exceeded=True, budget_s=600))
    assert "STATUS: RUNNING" in out and "STILL RUNNING" in out
    assert "Do NOT re-issue" in out


def test_budget_exceeded_then_finished_reports_the_real_result(srv):
    out = srv._fmt_envelope(envelope(status="ok", budget_exceeded=True, budget_s=600))
    assert "STATUS: OK" in out and "final result" in out
    assert "TIMEOUT" not in out


def test_read_timeout_does_not_name_the_previous_job(srv, monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return Resp(200, envelope(job_id="7"))
        raise requests.exceptions.ReadTimeout()

    monkeypatch.setattr(srv.requests, "post", fake_post)
    srv._send("print(1)")                  # job 7 completes normally
    out = srv._send("print(2)")            # this one's id never arrives
    assert "STILL EXECUTING" in out
    assert "job_status('7')" not in out
    assert "heartbeat()" in out


def test_ping_version_check_passes_for_matching_files(srv, monkeypatch):
    monkeypatch.setattr(srv.requests, "get", lambda *a, **k: Resp(200, {
        "protocol": srv.EXPECTED_PROTOCOL, "listener_version": srv.SERVER_VERSION,
        "alive": True, "busy": False, "output": "alive"}))
    out = srv.ping_fusion()
    assert out.startswith("SUCCESS") and "NOTE" not in out


@pytest.mark.parametrize("tool,args", [
    ("joint_at_point", dict(component_a="A", ax_mm=0, ay_mm=0, az_mm=0,
                            component_b="B", bx_mm=0, by_mm=0, bz_mm=0,
                            motion="slider")),
    ("revolute_joint", dict(component_a="A", component_b="B", axis="w")),
    ("construction_plane_offset", dict(offset_mm=5, base="front")),
    ("fillet_edges", dict(radius_mm=1, which="Top")),
    ("export_stl", dict(filepath="x.stl", quality="ultra")),
    ("box_in_component", dict(component_name="", length_mm=1, width_mm=1, height_mm=1)),
])
def test_bad_arguments_are_errors_not_silent_defaults(srv, captured, tool, args):
    res = getattr(srv, tool)(**args)
    assert res.startswith("ERROR")
    assert captured == []          # nothing was sent to Fusion
