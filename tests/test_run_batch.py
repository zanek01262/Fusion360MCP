"""run_batch: validation, and the fused program's stop_on_error behaviour.

The ops here are run_fusion_code snippets of plain Python, so the fused
program can actually be executed without Fusion."""
import contextlib
import io
import json

import pytest


class FakeResponse:
    status_code = 200

    def json(self):
        return {"protocol": 3, "status": "ok", "stdout": "", "traceback": "",
                "job_id": "1", "elapsed_ms": 1, "stdout_bytes": 0}


@pytest.fixture
def fused(srv, monkeypatch):
    """Let batch collection run for real, but capture the ONE final request
    instead of sending it to a listener."""
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json["code"])
        return FakeResponse()

    monkeypatch.setattr(srv.requests, "post", fake_post)
    return sent


def ops(*snippets):
    return json.dumps([{"tool": "run_fusion_code", "args": {"code": s}}
                       for s in snippets])


def execute(code):
    ns = {"progress": lambda msg: print("[progress] " + msg)}
    out = io.StringIO()
    err = None
    with contextlib.redirect_stdout(out):
        try:
            exec(compile(code, "<batch>", "exec"), ns)
        except Exception as e:
            err = e
    return out.getvalue(), err


def test_rejects_non_batchable_tool_without_side_effects(srv, fused):
    res = srv.run_batch('[{"tool":"clear_document","args":{}}]')
    assert res.startswith("ERROR") and "cannot be batched" in res
    assert fused == []


def test_rejects_unknown_tool(srv, fused):
    assert "unknown tool" in srv.run_batch('[{"tool":"nope","args":{}}]')
    assert fused == []


def test_passes_a_tools_own_validation_message_through(srv, fused):
    res = srv.run_batch('[{"tool":"fillet_edges","args":{"radius_mm":1,"which":"sides"}}]')
    assert "rejected before anything ran" in res and "which must be" in res
    assert fused == []


def test_stops_on_a_printed_failure_not_only_on_exceptions(srv, fused):
    srv.run_batch(ops("print('fine')",
                      "print('*** JOINT NOT CREATED ***')",
                      "print('SHOULD NOT RUN')"))
    out, err = execute(fused[0])
    assert isinstance(err, RuntimeError) and "batch STOPPED at op 1" in str(err)
    assert "fine" in out and "SHOULD NOT RUN" not in out


def test_stops_on_an_exception(srv, fused):
    srv.run_batch(ops("raise ValueError('boom')", "print('SHOULD NOT RUN')"))
    out, err = execute(fused[0])
    assert isinstance(err, ValueError)
    assert "op 0 (run_fusion_code) FAILED" in out and "SHOULD NOT RUN" not in out


def test_continue_mode_runs_everything_and_lists_the_failures(srv, fused):
    srv.run_batch(ops("print('ERROR: no component')", "raise ValueError('x')",
                      "print('last')"), stop_on_error=False)
    out, err = execute(fused[0])
    assert err is None and "last" in out
    assert "op(s) [0, 1] FAILED or flagged" in out


def test_clean_summaries_do_not_trip_the_stop(srv, fused):
    # lowercase counters in a clean summary must not read as failures
    srv.run_batch(ops("print('wrap_cut done: ok=3 no-op=0 failed=0')",
                      "print('CONFIRMED: exported STEP')", "print('last')"))
    out, err = execute(fused[0])
    assert err is None and "all 3 op(s) ok" in out


def test_ops_are_not_reindented(srv, fused):
    srv.run_batch(ops('s = """line1\nline2"""\nprint(repr(s))'))
    out, _ = execute(fused[0])
    assert repr("line1\nline2") in out
