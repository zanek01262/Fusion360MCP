#!/usr/bin/env python3
"""
fusion_mcp_server.py
====================
An MCP (Model Context Protocol) server that lets Claude drive Fusion 360.

It forwards tool calls to the MCPListener add-in running inside Fusion
(http://127.0.0.1:8765), which executes the code on Fusion's main thread
and returns stdout / tracebacks.

Run by Claude Desktop via claude_desktop_config.json (see README.md).
Requires: pip install "mcp[cli]" requests

UNIT CONVENTION
---------------
Every tool argument is in MILLIMETERS. Tools convert to Fusion's internal
unit (centimeters) by dividing by 10. When writing raw code via
run_fusion_code / run_fusion_code_safe, remember the API is in CM.
"""

import os
import re
import tempfile
import requests
from mcp.server.fastmcp import FastMCP, Image

LISTENER_URL = "http://127.0.0.1:8765"
# Bounded wait for the sync path. The listener keeps RUNNING the job even if we
# stop waiting here, and the job stays pollable via job_status(). Kept under the
# HTTP client timeout so we always get a structured envelope back, never a bare
# socket timeout.
SYNC_WAIT_S = 100.0
HTTP_TIMEOUT_S = 115.0
_last_job_id = [None]   # most recent job, so a timeout can name it
_terse = [True]         # compact output on clean success; full detail on trouble
_compact_next = [True]  # per-call: report tools opt OUT (their body IS the detail)

_TROUBLE = ("***", "WARNING", "ERROR", "FAILED", "UNCONFIRMED", "MISMATCH",
            "REFUSED", "Traceback", "NOT CREATED", "no-op", "STALE")


def _compact(text: str) -> str:
    """Drop indented continuation detail when nothing went wrong.

    Every marker in _TROUBLE disables compaction for the whole payload, so any
    failure is reported in full. On a clean run this removes the explanatory
    second/third lines that a correct result does not need."""
    if not _terse[0] or not text or not _compact_next[0]:
        return text
    if any(m in text for m in _TROUBLE):
        return text
    kept, dropped = [], 0
    for line in text.splitlines():
        if line[:2] == "  " and line.strip():
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        kept.append("({} detail line(s) hidden - set_verbosity('full') to show)"
                    .format(dropped))
    return "\n".join(kept)
# The one version number. MCPListener.LISTENER_VERSION must match (the
# listener lives in a separate folder, so it cannot import this);
# tests/test_versions.py fails the build if any copy drifts.
SERVER_VERSION = "1.9.1"
EXPECTED_PROTOCOL = 3

# Auth token the listener writes at startup (see MCPListener.py). Read fresh on
# every request: the listener makes a new one each time the add-in starts, so a
# cached copy would go stale after a Stop -> Run.
TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".fusion_mcp", "listener_token")
TOKEN_HEADER = "X-MCP-Token"


def _headers() -> dict:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return {TOKEN_HEADER: f.read().strip()}
    except OSError:
        return {}


def _refused(r) -> str:
    """Explain a 401/403 from the listener; '' if the request was accepted."""
    if r.status_code == 403:
        return ("AUTH ERROR: the listener refused this request's Host header. "
                "LISTENER_URL must point at 127.0.0.1 or localhost.")
    if r.status_code == 401:
        where = ("exists but does not match - it is from an earlier run"
                 if os.path.exists(TOKEN_PATH) else "does not exist")
        return ("AUTH ERROR: the listener rejected the auth token. The token "
                "file {} {}. FIX: in Fusion press Shift+S > Add-Ins > "
                "MCPListener > Stop > Run (that rewrites the token), and make "
                "sure Fusion and Claude Desktop run as the same OS user."
                .format(TOKEN_PATH, where))
    return ""

MISMATCH_MSG = (
    "PROTOCOL MISMATCH: the MCPListener add-in running inside Fusion is an OLD "
    "version that does not match this server (expected protocol {}). Payloads "
    "will read as empty/None until it is updated. FIX: copy the new "
    "MCPListener.py into Fusion's AddIns folder "
    "(%appdata%/Autodesk/Autodesk Fusion 360/API/AddIns/MCPListener/), then in "
    "Fusion press Shift+S > Add-Ins > MCPListener > Stop > Run."
    .format(EXPECTED_PROTOCOL))


def _check_protocol(data: dict) -> str:
    """Return a loud mismatch warning if the listener did not report the "
    "expected protocol, else empty string. An old listener omits the field "
    "entirely - that is the failure this catches."""
    if data.get("protocol") != EXPECTED_PROTOCOL:
        return MISMATCH_MSG
    return ""

mcp = FastMCP("fusion360")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------
def _fmt_envelope(data: dict) -> str:
    """Format the listener's structured envelope into a readable string that
    always tells the agent status, elapsed time, stdout, and any traceback."""
    pm = _check_protocol(data)
    if pm:
        return pm
    status = data.get("status", "ok" if data.get("ok") else "error")
    elapsed = data.get("elapsed_ms")
    jid = data.get("job_id")
    over = data.get("budget_exceeded")
    head = "STATUS: {}".format(status.upper())
    if elapsed is not None:
        head += "  ({} ms)".format(elapsed)
    if jid:
        head += "  [job {}]".format(jid)
    if status in ("ok", "error") and data.get("stdout_bytes") == 0 and not data.get("traceback"):
        head += "  (no output captured)"
    parts = [head]
    out = data.get("stdout", "")
    if out:
        parts.append(_compact(out).rstrip())
    tb = data.get("traceback", "")
    if tb:
        parts.append("TRACEBACK:\n" + tb.rstrip())
    if over and status == "running":
        parts.append("NOTE: this job has run past its {}s budget and is STILL "
                     "RUNNING in Fusion - it has not failed. Poll job_status('{}'):"
                     " the status changes to OK or ERROR when it ends. Do NOT "
                     "re-issue it.".format(data.get("budget_s"), jid))
    elif over:
        parts.append("NOTE: finished after running past its {}s budget. The "
                     "status above is the final result.".format(data.get("budget_s")))
    elif status == "running":
        parts.append("NOTE: still running. Poll job_status('{}') for the final "
                     "result; partial stdout shown above.".format(jid))
    return "\n".join(parts)


import threading as _threading

_batch_mode = [False]
_batch_buf = []
_batch_lock = _threading.RLock()   # a concurrent call must not be swallowed

# ONLY these may be batched. Allowlist, not denylist: a tool omitted here simply
# cannot be batched, whereas a tool missing from a denylist would EXECUTE FOR
# REAL during collection - which, for a destructive tool, means data loss.
BATCHABLE = {
    "create_box", "create_cylinder", "drill_hole", "hole_on_face",
    "sketch_on_face", "tapped_hole", "fillet_edges", "shell_body",
    "cut_prisms", "wrap_cut", "project_to_surface", "set_parameter",
    "create_component", "box_in_component", "move_component",
    "copy_component", "ground_component", "rigid_joint", "revolute_joint",
    "joint_at_point", "construction_plane_offset", "construction_plane_tangent",
    "export_step", "export_stl", "export_dxf", "set_view", "run_fusion_code",
}


# Output text that means an op FAILED even though it did not raise - most tools
# report failure by printing. Case-sensitive on purpose: lowercase counters
# such as "failed=0" or "no-op=0" in a clean summary must not trip it.
_BATCH_STOP_MARKERS = ("ERROR", "WARNING", "***", "FAILED", "NOT CREATED",
                       "REFUSED", "MISMATCH", "UNCONFIRMED", "Traceback")


def _send(code: str, compact: bool = True) -> str:
    """Run code via the listener's sync path (bounded wait). Returns a structured
    envelope string. If the job outlives SYNC_WAIT_S it stays pollable via
    job_status(); we still return immediately with partial output + a job id."""
    _compact_next[0] = compact
    if _batch_mode[0]:
        # Batch capture: collect this tool's generated code instead of sending
        # it, so N operations become ONE main-thread job / ONE round trip.
        _batch_buf.append(code)
        return "BATCHED"
    # If this request times out, the NEW job's id never arrives. Clear the old
    # one so the timeout message cannot point job_status at the previous job.
    _last_job_id[0] = None
    try:
        r = requests.post(LISTENER_URL + "/exec_sync",
                          json={"code": code, "wait_s": SYNC_WAIT_S},
                          headers=_headers(), timeout=HTTP_TIMEOUT_S)
        refused = _refused(r)
        if refused:
            return refused
        data = r.json()
        if data.get("job_id"):
            _last_job_id[0] = data["job_id"]
        return _fmt_envelope(data)
    except requests.exceptions.ConnectionError:
        return ("CONNECTION ERROR: could not reach Fusion listener at "
                + LISTENER_URL
                + "\nIs Fusion open with the MCPListener add-in running?")
    except requests.exceptions.ReadTimeout:
        # The job is STILL RUNNING - it may even have completed (including a
        # save). Never present this as a failure, and never re-run the call.
        jid = _last_job_id[0]
        return ("STATUS: RUNNING  (transport wait exceeded {}s)\n"
                "The operation is STILL EXECUTING in Fusion and may already have "
                "completed - do NOT re-issue this call (that stacks a second "
                "job).\n"
                "{}"
                "get_state() and heartbeat() stay responsive during a long op."
                .format(HTTP_TIMEOUT_S,
                        "Poll job_status('%s') for the result.\n" % jid
                        if jid else
                        "Call heartbeat() for the current job id, then "
                        "job_status(<id>).\n"))
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)


# Camera presets shared by view_model / set_view. 'bottom' included; an
# unrecognised orientation is reported as an ERROR rather than silently
# falling back to iso.
# NOTE: braces are doubled because these fragments are concatenated into
# templates that are then passed through .format().
VIEWS = ("eyes = {{'iso':(10,-10,10),'top':(0,0,10),'bottom':(0,0,-10),"
         "'front':(0,-10,0),'back':(0,10,0),'right':(10,0,0),'left':(-10,0,0)}}\n")
UPS = ("ups = {{'iso':(0,0,1),'top':(0,1,0),'bottom':(0,-1,0),"
       "'front':(0,0,1),'back':(0,0,1),'right':(0,0,1),'left':(0,0,1)}}\n")


def _send_async(code: str) -> str:
    """Start code as an async job and return the job_id immediately. Used by
    tools that can outlive the wire (clear_document, big patterns)."""
    if _batch_mode[0]:
        # Never execute during batch collection - that would fire a real (often
        # destructive) operation while the caller believes it is only being
        # queued for later.
        raise RuntimeError("this tool uses the async path and cannot be batched")
    try:
        r = requests.post(LISTENER_URL + "/exec", json={"code": code},
                          headers=_headers(), timeout=10)
        refused = _refused(r)
        if refused:
            return refused
        d = r.json()
        pm = _check_protocol(d)
        if pm:
            return pm
        jid = d.get("job_id")
        return ("STATUS: QUEUED  [job {}]\n"
                "Started as a background job so the channel is not blocked.\n"
                "Poll job_status('{}') for incremental progress and the final "
                "result.".format(jid, jid))
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)


# ==========================================================================
# CORE
# ==========================================================================
@mcp.tool()
def ping_fusion() -> str:
    """Check that the Fusion listener is reachable AND whether it's busy. Served
    on a separate channel from geometry execution, so it STAYS RESPONSIVE even
    while a long operation is running on Fusion's main thread (a hung op no
    longer blocks ping). Reports busy state and which job is current."""
    try:
        r = requests.get(LISTENER_URL + "/ping", headers=_headers(), timeout=5)
        refused = _refused(r)
        if refused:
            return refused
        d = r.json()
        pm = _check_protocol(d)
        if pm:
            return pm
        lv = d.get("listener_version")
        warn = ""
        if lv and lv != SERVER_VERSION:
            warn = ("\nNOTE: server v{} vs listener v{}. These are separate files "
                    "in separate folders - the listener lives in Fusion's AddIns "
                    "folder. Update whichever is older, then Stop/Run the add-in."
                    .format(SERVER_VERSION, lv))
        return ("SUCCESS:\nserver v{} | listener v{} | alive={} busy={} "
                "current_job={} idle_seconds={}\n{}{}"
                .format(SERVER_VERSION, lv, d.get("alive"), d.get("busy"),
                        d.get("current_job"), d.get("idle_seconds"),
                        d.get("output", ""), warn))
    except Exception as e:
        return ("CONNECTION ERROR: {}\nOpen Fusion 360 and start the MCPListener "
                "add-in (Utilities > Add-Ins > MCPListener > Run).".format(e))


@mcp.tool()
def get_state() -> str:
    """CHEAP, always-responsive state from the listener cache: the active
    document, EVERY open document, and body/sketch/timeline counts. Served
    without the main thread, so it is safe during a heavy op or after a hang.

    IMPORTANT: this is a CACHE. It reports its own age - if `fresh=False`, the
    user may have switched documents in the Fusion UI since the snapshot. Call
    document_report() for an authoritative live read before any save or clear."""
    try:
        r = requests.get(LISTENER_URL + "/state", headers=_headers(), timeout=5)
        refused = _refused(r)
        if refused:
            return refused
        d = r.json()
        pm = _check_protocol(d)
        if pm:
            return pm
        lines = []
        age = d.get("snapshot_age_s")
        fresh = d.get("snapshot_fresh")
        lines.append("snapshot age: {}s  fresh={}".format(age, fresh))
        if fresh is False:
            lines.append("*** STALE - the active document may have changed in the "
                         "Fusion UI. Use document_report() before saving/clearing. ***")
        lines.append("active_document: {}".format(d.get("active_document")))
        if any(x.get("active") is None for x in (d.get("open_documents") or [])):
            lines.append("NOTE: several open documents share the active name (marked ?) "
                         "- run document_report() before saving or clearing.")
        lines.append("lineage_id: {}".format(d.get("lineage_id")))
        lines.append("is_saved_flag={} is_modified={} version={} data_ok={}".format(
            d.get("is_saved_flag"), d.get("is_modified"),
            d.get("version"), d.get("data_ok")))
        if d.get("data_ok") is False:
            lines.append("NOTE: app.data is unavailable, so lineage CANNOT be "
                         "read. Do not treat this document as unsaved on that "
                         "basis - it may well have a lineage.")
        docs = d.get("open_documents") or []
        if docs:
            lines.append("open documents ({}):".format(len(docs)))
            for x in docs:
                act = x.get("active")
                lines.append("  {} {}  saved_flag={} modified={} lineage={}".format(
                    "*" if act else ("?" if act is None else " "), x.get("name"),
                    x.get("is_saved_flag"), x.get("is_modified"),
                    x.get("lineage_id")))
        lines.append("bodies: {}  sketches: {}  components: {}  timeline: {}".format(
            d.get("bodies"), d.get("sketches"), d.get("components"),
            d.get("timeline_features")))
        lines.append("busy: {}  current_job: {}".format(
            d.get("busy"), d.get("current_job")))
        return "SUCCESS:\n" + "\n".join(lines)
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)


@mcp.tool()
def document_report() -> str:
    """AUTHORITATIVE live read of document identity - run before ANY save, clear
    or destructive edit, and whenever unsure what you are working in.

    Reports, for the active document AND every other open one: name, isSaved and
    isModified read independently, lineage, version, and whether app.data was
    reachable. Ends with a verdict that distinguishes 'never saved' from 'cannot
    tell' - conflating those is what creates duplicate documents."""
    code = (
        "app_ = app\n"
        "def ident(d):\n"
        "    o = {}\n"
        "    try:\n"
        "        o['name'] = d.name\n"
        "    except Exception:\n"
        "        o['name'] = '(unreadable)'\n"
        "    for a, k in (('isSaved','saved_flag'), ('isModified','modified')):\n"
        "        try:\n"
        "            o[k] = bool(getattr(d, a))\n"
        "        except Exception:\n"
        "            o[k] = None\n"
        "    try:\n"
        "        df = d.dataFile\n"
        "        o['data_ok'] = True\n"
        "        if df is None:\n"
        "            o['lineage'] = None\n"
        "            o['version'] = None\n"
        "            o['folder'] = None\n"
        "        else:\n"
        "            o['lineage'] = df.id\n"
        "            try:\n"
        "                o['version'] = df.versionNumber\n"
        "            except Exception:\n"
        "                o['version'] = None\n"
        "            try:\n"
        "                o['folder'] = df.parentFolder.name\n"
        "            except Exception:\n"
        "                o['folder'] = None\n"
        "    except Exception as _e:\n"
        "        o['data_ok'] = False\n"
        "        o['lineage'] = 'UNKNOWN'\n"
        "        o['version'] = None\n"
        "        o['folder'] = None\n"
        "        o['data_err'] = str(_e)[:60]\n"
        "    return o\n"
        "act = app_.activeDocument\n"
        "ai = ident(act)\n"
        "print('ACTIVE DOCUMENT: ' + str(ai.get('name')))\n"
        "print('  isSaved flag : ' + str(ai.get('saved_flag')))\n"
        "print('  isModified   : ' + str(ai.get('modified')))\n"
        "print('  lineage      : ' + str(ai.get('lineage')))\n"
        "print('  version      : ' + str(ai.get('version')))\n"
        "print('  folder       : ' + str(ai.get('folder')))\n"
        "print('  app.data ok  : ' + str(ai.get('data_ok')))\n"
        "if ai.get('data_ok') is False:\n"
        "    print('  >> app.data is UNAVAILABLE (' + str(ai.get('data_err')) + ')')\n"
        "    print('     Lineage cannot be read. This document may well be SAVED;')\n"
        "    print('     do NOT infer UNSAVED from a missing lineage, and do NOT')\n"
        "    print('     saveAs on that basis - that creates a duplicate document.')\n"
        "verdict = 'UNKNOWN'\n"
        "if ai.get('data_ok') and ai.get('lineage'):\n"
        "    verdict = 'SAVED (has lineage)'\n"
        "elif ai.get('data_ok') and not ai.get('lineage'):\n"
        "    verdict = 'NEVER SAVED (no lineage, app.data readable)'\n"
        "elif ai.get('saved_flag') is True:\n"
        "    verdict = 'PROBABLY SAVED (isSaved=True; lineage unreadable)'\n"
        "elif str(ai.get('name','')).lower().startswith('untitled'):\n"
        "    verdict = 'PROBABLY NEVER SAVED (named Untitled)'\n"
        "print('  VERDICT      : ' + verdict)\n"
        "print('--')\n"
        "try:\n"
        "    n = app_.documents.count\n"
        "    print('OPEN DOCUMENTS (' + str(n) + '):')\n"
        "    _docs = [app_.documents.item(i) for i in range(n)]\n"
        "    _ids = [ident(d) for d in _docs]\n"
        "    # names decide when unique; object equality breaks a tie between\n"
        "    # same-named documents; otherwise say we cannot tell\n"
        "    _named = [i for i in range(n) if _ids[i].get('name') == ai.get('name')]\n"
        "    _act = _named[0] if len(_named) == 1 else None\n"
        "    if len(_named) > 1:\n"
        "        _same = []\n"
        "        for i in _named:\n"
        "            try:\n"
        "                if _docs[i] == act:\n"
        "                    _same.append(i)\n"
        "            except Exception:\n"
        "                pass\n"
        "        _act = _same[0] if len(_same) == 1 else None\n"
        "    for i in range(n):\n"
        "        o = _ids[i]\n"
        "        if _act is not None:\n"
        "            mark = '*' if i == _act else ' '\n"
        "        else:\n"
        "            mark = '?' if i in _named else ' '\n"
        "        print('  %s [%d] %s  saved=%s modified=%s lineage=%s'\n"
        "              % (mark, i, o.get('name'), o.get('saved_flag'),\n"
        "                 o.get('modified'), o.get('lineage')))\n"
        "    print('  (* = active; ? = shares the active name, cannot tell which;'\n"
        "          ' use activate_document to switch)')\n"
        "except Exception as _e:\n"
        "    print('could not enumerate open documents: ' + str(_e)[:70])\n"
    )
    return _send(code, compact=False)


@mcp.tool()
def activate_document(name_or_index: str) -> str:
    """Make a specific OPEN document active, by name (substring, case-insensitive)
    or by the index shown in document_report. Every other tool operates on the
    ACTIVE document, so if the user is working in one design and Fusion has
    another active, work silently lands in the wrong file. Use this to bind to
    the user's document instead of assuming. Confirms the switch by reporting the
    active document afterwards."""
    code = (
        "sel = {sel!r}\n"
        "target = None\n"
        "n = app.documents.count\n"
        "if sel.strip().isdigit():\n"
        "    i = int(sel.strip())\n"
        "    if 0 <= i < n:\n"
        "        target = app.documents.item(i)\n"
        "    else:\n"
        "        print('ERROR: index %d out of range (0..%d)' % (i, n-1))\n"
        "else:\n"
        "    for i in range(n):\n"
        "        d = app.documents.item(i)\n"
        "        try:\n"
        "            if sel.lower() in d.name.lower():\n"
        "                target = d\n"
        "                break\n"
        "        except Exception:\n"
        "            pass\n"
        "    if target is None:\n"
        "        print('ERROR: no open document matching %r' % sel)\n"
        "        print('open: ' + ', '.join(\n"
        "            [app.documents.item(i).name for i in range(n)]))\n"
        "if target is not None:\n"
        "    try:\n"
        "        target.activate()\n"
        "    except Exception as _e:\n"
        "        print('activate() failed: ' + str(_e)[:80])\n"
        "    print('active document is now: ' + app.activeDocument.name)\n"
    ).format(sel=name_or_index)
    return _send(code, compact=False)


@mcp.tool()
def run_async(code: str) -> str:
    """Start a long-running Fusion operation WITHOUT waiting for it. Returns a
    job_id immediately. Poll job_status(job_id) to get progress (partial stdout)
    and the final result. Use this for operations you expect to exceed ~90s
    (large patterns, heavy booleans, full-assembly rebuilds) so the call never
    blocks. Same injected helpers and units rules as run_fusion_code. Emit
    progress(msg) in your code to mark stages visible while it runs."""
    try:
        r = requests.post(LISTENER_URL + "/exec", json={"code": code},
                          headers=_headers(), timeout=10)
        refused = _refused(r)
        if refused:
            return refused
        d = r.json()
        pm = _check_protocol(d)
        if pm:
            return pm
        jid = d.get("job_id")
        return ("SUCCESS:\nstarted job {} (status: {}). Poll job_status('{}') "
                "for progress and the final result.".format(jid, d.get("status"), jid))
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)


@mcp.tool()
def job_status(job_id: str) -> str:
    """Structured result of an async job: status (queued/running/ok/error),
    elapsed time, stdout collected SO FAR (incremental), and traceback on
    failure. Poll until status is ok or error. A job that runs past its budget
    is flagged budget_exceeded but keeps running and still ends in ok/error -
    the flag is not a failure.

    POLL WITH BACKOFF, not tightly: each poll is a round trip and returns the
    whole accumulated stdout again. Roughly 5s, 10s, 20s, then every 30s. The
    response tells you the elapsed time so you can judge. get_state() stays
    responsive meanwhile and is cheaper if you only need liveness."""
    try:
        r = requests.get(LISTENER_URL + "/job/" + str(job_id),
                         headers=_headers(), timeout=5)
        refused = _refused(r)
        if refused:
            return refused
        if r.status_code == 404:
            return "ERROR: no such job_id {}".format(job_id)
        return _fmt_envelope(r.json())
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)




@mcp.tool()
def run_fusion_code(code: str) -> str:
    """Execute arbitrary Python inside the running Fusion 360 session.
    [server v1.9.1 - if this looks old, Claude Desktop is showing a CACHED tool
     schema; fully restart it.]

    `adsk`, `app`, `ui` are in scope, plus ~30 injected helpers. Fusion's API is
    in CENTIMETRES: never hand-divide by 10 - use the injected mm(x) for every
    length, and P_xy/P_xz/P_yz(world mm) for sketch points. Call
    fusion_help('helpers') for the full helper list and fusion_help('planes')
    for the measured sketch-plane mapping table.

    For any cut/join: _b = capture_state(target); ...; report_volume_delta(_b,
    'label', target). Call assert_scale() after building.

    Anything you print() is returned; tracebacks are returned on error. Prefer
    run_fusion_code_safe for multi-step work, and run_batch for several known
    operations (one round trip instead of N).

    REFUSED by the listener: defineAsFreeMove (hard-crashes the host) and
    defineAsOffset (missing on this build).
    """
    return _send(code)


@mcp.tool()
def run_fusion_code_safe(code: str) -> str:
    """Transactional version of run_fusion_code.

    Records the timeline position first; if the code raises, deletes every
    feature the call created and VERIFIES the timeline is back where it
    started - it prints ROLLED BACK only when it is, and ROLLBACK INCOMPLETE
    otherwise. Use for multi-step / risky operations so partial failures don't
    leave orphaned geometry. Parametric documents only (design history on, the
    default): a direct-modelling document has no timeline, and says so.

    Same injected helpers as run_fusion_code (mm(), assert_scale(),
    total_volume(), report_volume_delta(), sliver_face_count(),
    log_sketch_areas(), delete_face()). Use mm() for inputs; never hand-divide.
    """
    # The user's code runs as a compiled string, not re-indented into a try:
    # block - indenting rewrote multi-line string literals and shifted every
    # traceback line number. Wrapper names are _mcp_-prefixed so the user's
    # code cannot clobber them.
    wrapped = (
        "import traceback as _mcp_tb\n"
        "_mcp_tl = None\n"
        "try:\n"
        "    _mcp_tl = _require_design().timeline\n"
        "    _mcp_count0 = _mcp_tl.count\n"
        "    _mcp_marker0 = _mcp_tl.markerPosition\n"
        "except Exception:\n"
        "    _mcp_tl = None\n"
        "_MCP_USER_CODE = " + repr(code) + "\n"
        "try:\n"
        "    exec(compile(_MCP_USER_CODE, '<mcp>', 'exec'), globals())\n"
        "    print('[safe] committed')\n"
        "except Exception:\n"
        "    _mcp_msg = _mcp_tb.format_exc()\n"
        "    if _mcp_tl is None:\n"
        "        print('[safe] *** NO ROLLBACK: this document has no timeline '\n"
        "              '(direct modelling) - any partial changes remain. ***')\n"
        "    else:\n"
        "        # New features are inserted AT the marker, so this call's own\n"
        "        # features occupy [marker0, marker0 + n). Anything that was\n"
        "        # already past a rolled-back marker is not ours and is kept.\n"
        "        _mcp_n = _mcp_tl.count - _mcp_count0\n"
        "        _mcp_fail = []\n"
        "        for _mcp_i in range(_mcp_marker0 + _mcp_n - 1, _mcp_marker0 - 1, -1):\n"
        "            try:\n"
        "                # TimelineObject has no deleteMe - delete its entity\n"
        "                _mcp_ent = _mcp_tl.item(_mcp_i).entity\n"
        "                if _mcp_ent is None:\n"
        "                    _mcp_fail.append('index %d: no entity' % _mcp_i)\n"
        "                else:\n"
        "                    _mcp_ent.deleteMe()\n"
        "            except Exception as _mcp_e:\n"
        "                _mcp_fail.append('index %d: %s' % (_mcp_i, str(_mcp_e)[:60]))\n"
        "        _mcp_left = _mcp_tl.count - _mcp_count0\n"
        "        if _mcp_n < 0:\n"
        "            print('[safe] *** this call DELETED %d feature(s) before failing;'\n"
        "                  ' rollback cannot restore deleted features. ***' % -_mcp_n)\n"
        "        elif _mcp_n == 0:\n"
        "            print('[safe] nothing to roll back - the error came before any '\n"
        "                  'feature was created')\n"
        "        elif _mcp_left <= 0:\n"
        "            print('[safe] ROLLED BACK after error: removed %d feature(s), '\n"
        "                  'timeline is back to %d item(s)' % (_mcp_n, _mcp_tl.count))\n"
        "        else:\n"
        "            print('[safe] *** ROLLBACK INCOMPLETE: %d of %d feature(s) '\n"
        "                  'created by this call are still in the timeline. Inspect '\n"
        "                  'with get_model_state before continuing. ***'\n"
        "                  % (_mcp_left, _mcp_n))\n"
        "            for _mcp_f in _mcp_fail[:5]:\n"
        "                print('       ' + _mcp_f)\n"
        "    raise Exception(_mcp_msg)\n"
    )
    return _send(wrapped)


@mcp.tool()
def clear_document(background: bool = True) -> str:
    """Delete all bodies, sketches and timeline features in the ACTIVE document.
    Prints the target document name and lineage before destroying anything.

    Reverse-order timeline deletion via item(i).entity.deleteMe(); background=True
    (default) returns a job_id - poll job_status. SAFER ALTERNATIVE: to replace
    one body, build the new one then retire_body(old) - it keeps history and
    cannot destroy unrelated work."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "# Identity FIRST - this is the most destructive tool in the toolkit and\n"
        "# it acts on whatever document is active, which may not be the one the\n"
        "# caller thinks. Always state the target before destroying anything.\n"
        "try:\n"
        "    _d = app.activeDocument\n"
        "    _lin = 'UNKNOWN (app.data unavailable)'\n"
        "    try:\n"
        "        _df = _d.dataFile\n"
        "        _lin = _df.id if _df else '(never saved)'\n"
        "    except Exception:\n"
        "        pass\n"
        "    print('CLEARING DOCUMENT: %s   lineage=%s' % (_d.name, _lin))\n"
        "    print('  bodies=%d sketches=%d timeline=%d'\n"
        "          % (root.bRepBodies.count, root.sketches.count,\n"
        "             design.timeline.count))\n"
        "except Exception as _ie:\n"
        "    print('WARNING: could not read document identity (%s) - clearing '\n"
        "          'anyway was requested.' % str(_ie)[:60])\n"
        "progress('starting reverse-order timeline clear')\n"
        "removed = 0\n"
        "try:\n"
        "    tl = design.timeline\n"
        "    n = tl.count\n"
        "    progress('timeline has %d feature(s)' % n)\n"
        "    for i in range(n - 1, -1, -1):\n"
        "        try:\n"
        "            obj = tl.item(i)\n"
        "            ent = obj.entity          # TimelineObject has no deleteMe\n"
        "            if ent is not None:\n"
        "                ent.deleteMe()\n"
        "                removed += 1\n"
        "                if removed % 10 == 0:\n"
        "                    progress('deleted %d feature(s)' % removed)\n"
        "        except Exception as _e:\n"
        "            progress('skip feature %d: %s' % (i, str(_e)[:60]))\n"
        "except Exception as _te:\n"
        "    print('no usable timeline (%s) - falling back to direct deletes'\n"
        "          % str(_te)[:60])\n"
        "# Fallback sweep ONLY for direct-modelling docs with no timeline.\n"
        "# Bulk bRepBodies.deleteMe() on a PARAMETRIC doc creates a delete\n"
        "# feature per body and is exactly the path that hung for 10+ minutes,\n"
        "# so it is never used when a timeline exists.\n"
        "_has_timeline = False\n"
        "try:\n"
        "    _has_timeline = design.timeline.count >= 0\n"
        "except Exception:\n"
        "    _has_timeline = False\n"
        "if _has_timeline and (root.bRepBodies.count or root.sketches.count):\n"
        "    print('NOTE: %d body/ies and %d sketch(es) remain after clearing the'\n"
        "          % (root.bRepBodies.count, root.sketches.count))\n"
        "    print('      timeline. NOT falling back to bulk deleteMe() - that is'\n"
        "          ' the path that hangs on parametric documents.')\n"
        "    print('      Retire individual bodies with retire_body(body), or'\n"
        "          ' delete them in the Fusion UI.')\n"
        "elif not _has_timeline:\n"
        "    progress('no timeline (direct-modelling doc) - sweeping directly')\n"
        "    guard = 0\n"
        "    while root.bRepBodies.count > 0 and guard < 200:\n"
        "        try:\n"
        "            root.bRepBodies.item(0).deleteMe()\n"
        "        except Exception:\n"
        "            break\n"
        "        guard += 1\n"
        "    while root.sketches.count > 0 and guard < 400:\n"
        "        try:\n"
        "            root.sketches.item(0).deleteMe()\n"
        "        except Exception:\n"
        "            break\n"
        "        guard += 1\n"
        "print('cleared: %d timeline feature(s) deleted; bodies=%d sketches=%d'\n"
        "      % (removed, root.bRepBodies.count, root.sketches.count))\n"
    )
    if background:
        return _send_async(code)
    return _send(code, compact=False)


@mcp.tool()
def set_parameter(name: str, value_mm: float) -> str:
    """Set (or create) a user parameter by name, in millimeters. Driving a
    model with named parameters lets the dimensions be tweaked later without
    rebuilding. Build parametric-first whenever a value might change."""
    code = (
        "design = _require_design()\n"
        "ups = design.userParameters\n"
        "expr = '{v} mm'\n"
        "p = ups.itemByName({n!r})\n"
        "_was = p.expression if p else None\n"
        "if p:\n"
        "    p.expression = expr\n"
        "else:\n"
        "    val = adsk.core.ValueInput.createByString(expr)\n"
        "    ups.add({n!r}, val, 'mm', '')\n"
        "# READ BACK - Fusion silently rejects or clamps an expression it does\n"
        "# not accept, so an unverified 'updated' message can be simply false.\n"
        "p2 = ups.itemByName({n!r})\n"
        "if p2 is None:\n"
        "    print('*** FAILED: parameter %r does not exist after the write. ***'\n"
        "          % {n!r})\n"
        "else:\n"
        "    got = p2.expression\n"
        "    try:\n"
        "        val_mm = p2.value * 10.0\n"
        "    except Exception:\n"
        "        val_mm = None\n"
        "    if got.replace(' ', '') == expr.replace(' ', ''):\n"
        "        print('CONFIRMED: %s = %s (%.4f mm)'\n"
        "              % ({n!r}, got, val_mm if val_mm is not None else float('nan')))\n"
        "    else:\n"
        "        print('*** MISMATCH: asked for %r, Fusion stored %r (%.4f mm). '\n"
        "              'The expression was rejected or clamped - do NOT assume '\n"
        "              'the requested value is in effect. ***'\n"
        "              % (expr, got, val_mm if val_mm is not None else float('nan')))\n"
        "    if _was is not None and _was != got:\n"
        "        print('  (was %r)' % _was)\n"
    ).format(v=value_mm, n=name)
    return _send(code)


@mcp.tool()
def list_parameters() -> str:
    """List all user parameters and their current expressions."""
    code = (
        "design = _require_design()\n"
        "ups = design.userParameters\n"
        "lines = []\n"
        "for i in range(ups.count):\n"
        "    p = ups.item(i)\n"
        "    lines.append(p.name + ' = ' + p.expression)\n"
        "print('\\n'.join(lines) if lines else 'no user parameters')\n"
    )
    return _send(code, compact=False)


# ==========================================================================
# PRIMITIVES (built on the root component's XY plane)
# ==========================================================================
@mcp.tool()
def create_box(length_mm: float, width_mm: float, height_mm: float,
               origin_x_mm: float = 0, origin_y_mm: float = 0) -> str:
    """Create a rectangular box: sketches an L x W rectangle on XY at the given
    origin and extrudes upward by height. Returns the new body name."""
    L, W, H = length_mm / 10.0, width_mm / 10.0, height_mm / 10.0
    ox, oy = origin_x_mm / 10.0, origin_y_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "sk = root.sketches.add(root.xYConstructionPlane)\n"
        "p0 = adsk.core.Point3D.create({ox}, {oy}, 0)\n"
        "p1 = adsk.core.Point3D.create({x1}, {y1}, 0)\n"
        "sk.sketchCurves.sketchLines.addTwoPointRectangle(p0, p1)\n"
        "prof = sk.profiles.item(0)\n"
        "ext = root.features.extrudeFeatures\n"
        "dist = adsk.core.ValueInput.createByReal({H})\n"
        "feat = ext.addSimple(prof, dist, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        "print('created body: ' + feat.bodies.item(0).name)\n"
    ).format(ox=ox, oy=oy, x1=ox + L, y1=oy + W, H=H)
    return _send(code)


@mcp.tool()
def create_cylinder(diameter_mm: float, height_mm: float,
                    origin_x_mm: float = 0, origin_y_mm: float = 0) -> str:
    """Create a cylinder: sketches a circle on XY and extrudes upward."""
    R, H = diameter_mm / 20.0, height_mm / 10.0
    ox, oy = origin_x_mm / 10.0, origin_y_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "sk = root.sketches.add(root.xYConstructionPlane)\n"
        "ctr = adsk.core.Point3D.create({ox}, {oy}, 0)\n"
        "sk.sketchCurves.sketchCircles.addByCenterRadius(ctr, {R})\n"
        "prof = sk.profiles.item(0)\n"
        "ext = root.features.extrudeFeatures\n"
        "dist = adsk.core.ValueInput.createByReal({H})\n"
        "feat = ext.addSimple(prof, dist, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        "print('created body: ' + feat.bodies.item(0).name)\n"
    ).format(ox=ox, oy=oy, R=R, H=H)
    return _send(code)


# Generated-code fragment shared by the XY hole tools. Expects `root` and
# `body`; leaves `sk` (a sketch on a plane at the body's top) and `_top` (cm).
_TOP_DOWN_SKETCH = (
    "# Sketch at the TARGET body's top and cut DOWN from there. These tools\n"
    "# used to sketch at z=0 and cut toward -Z, which missed every body built\n"
    "# upward from XY - e.g. by create_box (verified live against 1.9.0).\n"
    "_top = body.boundingBox.maxPoint.z\n"
    "_pname = 'mcp_hole_plane_z%gmm' % round(_top * 10, 4)\n"
    "_pl = root.constructionPlanes.itemByName(_pname)\n"
    "if _pl is None:\n"
    "    _ci = root.constructionPlanes.createInput()\n"
    "    _ci.setByOffset(root.xYConstructionPlane, adsk.core.ValueInput.createByReal(_top))\n"
    "    _pl = root.constructionPlanes.add(_ci)\n"
    "    _pl.name = _pname\n"
    "    _pl.isLightBulbOn = False   # a drilling reference, not geometry\n"
    "sk = root.sketches.add(_pl)   # measured: sketch +X/+Y = world +X/+Y\n"
)


@mcp.tool()
def drill_hole(diameter_mm: float, x_mm: float, y_mm: float,
               depth_mm: float, body_index: int = 0) -> str:
    """Cut a circular hole straight down at (x, y), depth_mm deep measured from
    the TOP of body `body_index` (its highest point). Only that body is cut -
    other bodies the hole passes through are left alone. The drilling plane is
    a hidden construction plane named mcp_hole_plane_z<height>mm, reused by
    later holes at the same height. For holes on other faces use
    sketch_on_face + hole_on_face."""
    R, D = diameter_mm / 20.0, depth_mm / 10.0
    x, y = x_mm / 10.0, y_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "_before = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        + _TOP_DOWN_SKETCH +
        "ctr = adsk.core.Point3D.create({x}, {y}, 0)\n"
        "sk.sketchCurves.sketchCircles.addByCenterRadius(ctr, {R})\n"
        "prof = sk.profiles.item(0)\n"
        "ext = root.features.extrudeFeatures\n"
        "inp = ext.createInput(prof, adsk.fusion.FeatureOperations.CutFeatureOperation)\n"
        "inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-{D}))\n"
        "# Without this Fusion cuts EVERY body the hole crosses (lid and\n"
        "# enclosure together), and body_index was silently ignored.\n"
        "inp.participantBodies = [body]\n"
        "ext.add(inp)\n"
        "_after = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "_delta = _after - _before\n"
        "print('hole cut at ({xm}, {ym}) mm, {dpm} mm deep from the top of %s (z=%.2f mm) | volume delta: %+.4f cm^3'\n"
        "      % (body.name, _top * 10, _delta))\n"
        "if abs(_delta) < 1e-7:\n"
        "    print('WARNING: volume delta ~ZERO - the cut had NO effect (missed the body or wrong depth/location).')\n"
    ).format(x=x, y=y, R=R, D=D, xm=x_mm, ym=y_mm, bi=body_index, dpm=depth_mm)
    return _send(code)


@mcp.tool()
def fillet_edges(radius_mm: float, body_index: int = 0, which: str = "all") -> str:
    """Round edges of a body. which='all' fillets every edge; 'vertical' only
    edges with vertical run; 'top' only edges lying on the top face plane."""
    if which not in ("all", "vertical", "top"):
        return "ERROR: which must be 'all', 'vertical' or 'top' (got {!r})".format(which)
    R = radius_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "maxz = body.boundingBox.maxPoint.z\n"
        "edges = adsk.core.ObjectCollection.create()\n"
        "for e in body.edges:\n"
        "    sp = e.startVertex.geometry\n"
        "    ep = e.endVertex.geometry\n"
        "    is_vertical = abs(sp.z - ep.z) > 1e-6\n"
        "    on_top = (abs(sp.z - maxz) < 1e-6) and (abs(ep.z - maxz) < 1e-6)\n"
        "    if {w!r} == 'all':\n"
        "        edges.add(e)\n"
        "    elif {w!r} == 'vertical' and is_vertical:\n"
        "        edges.add(e)\n"
        "    elif {w!r} == 'top' and on_top:\n"
        "        edges.add(e)\n"
        "if edges.count == 0:\n"
        "    print('WARNING: no edges matched which={w} - nothing was filleted')\n"
        "else:\n"
        "    _len_mm = 0.0\n"
        "    for _e in edges:\n"
        "        try:\n"
        "            _len_mm += _e.length * 10.0\n"
        "        except Exception:\n"
        "            pass\n"
        "    _before = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "    fil = root.features.filletFeatures\n"
        "    inp = fil.createInput()\n"
        "    inp.addConstantRadiusEdgeSet(edges, adsk.core.ValueInput.createByReal({R}), True)\n"
        "    fil.add(inp)\n"
        "    _after = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "    _delta = _after - _before\n"
        "    print('filleted ' + str(edges.count) + ' edges (which={w}, r={rmm}mm)')\n"
        "    # A fillet of radius r along length L changes volume by at most\n"
        "    # ~0.1*r^2*L. A big overshoot means the blend propagated past the\n"
        "    # intended edges (tangent chain running into adjacent geometry).\n"
        "    # Fusion reports SUCCESS and the body stays valid, so this is the\n"
        "    # only cheap detector.\n"
        "    _exp = 0.1 * ({rmm} ** 2) * _len_mm\n"
        "    _act = abs(_delta) * 1000.0\n"
        "    print('  volume delta %.2f mm^3 (expected <= ~%.2f for r=%s, L=%.1fmm)'\n"
        "          % (_act, _exp, {rmm}, _len_mm))\n"
        "    if _exp > 0 and _act > 10.0 * _exp:\n"
        "        print('  *** WARNING: %.1fx the expected maximum - the fillet '\n"
        "              'likely propagated BEYOND the selected edges and may have '\n"
        "              'filled in geometry elsewhere. Verify with cross_section.'\n"
        "              % (_act / _exp))\n"
    ).format(bi=body_index, w=which, R=R, rmm=radius_mm)
    return _send(code)


@mcp.tool()
def shell_body(thickness_mm: float, body_index: int = 0, open_top: bool = True) -> str:
    """Hollow a body to a wall thickness. open_top=True removes the top face to
    make an open enclosure; False makes a fully closed hollow body."""
    T = thickness_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "_before = body.volume if body.isValid else 0\n"
        "shells = root.features.shellFeatures\n"
        "faces = adsk.core.ObjectCollection.create()\n"
        "if {ot}:\n"
        "    top = None\n"
        "    for f in body.faces:\n"
        "        if top is None or f.centroid.z > top.centroid.z:\n"
        "            top = f\n"
        "    faces.add(top)\n"
        "else:\n"
        "    faces.add(body)\n"
        "inp = shells.createInput(faces)\n"
        "inp.insideThickness = adsk.core.ValueInput.createByReal({T})\n"
        "shells.add(inp)\n"
        "_body2 = root.bRepBodies.item({bi})\n"
        "_after = _body2.volume if _body2.isValid else 0\n"
        "_delta = _after - _before\n"
        "_sliv = 0\n"
        "for f in _body2.faces:\n"
        "    try:\n"
        "        if f.area < 0.0005:   # 0.05 mm^2, as sliver_face_count()\n"
        "            _sliv += 1\n"
        "    except Exception:\n"
        "        pass\n"
        "print('shelled to {tmm}mm (open_top={ot}) | volume delta: {{:+.4f}} cm^3 | slivers: {{}}'.format(_delta, _sliv))\n"
        "if abs(_delta) < 1e-7:\n"
        "    print('WARNING: volume delta ~ZERO - shell had NO effect (thickness too large for the body?).')\n"
        "if _sliv:\n"
        "    print('WARNING: ' + str(_sliv) + ' sliver face(s) - shell may have produced bad geometry.')\n"
    ).format(bi=body_index, ot=open_top, T=T, tmm=thickness_mm)
    return _send(code)


# Metric coarse pitches (ISO 261), and the standard coarse tap drills. A size
# or pitch not in the drill table uses major - pitch, the standard formula -
# which is what makes fine pitches come out right (M8x1 -> 7.0, not 6.8).
_METRIC_COARSE_PITCH = {
    1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 3.5: 0.6, 4: 0.7, 5: 0.8, 6: 1.0,
    7: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5,
    22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5,
}
_METRIC_COARSE_DRILL = {
    1.6: 1.25, 2: 1.6, 2.5: 2.05, 3: 2.5, 3.5: 2.9, 4: 3.3, 5: 4.2, 6: 5.0,
    8: 6.8, 10: 8.5, 12: 10.2,
}
# Unified (UNC/UNF) tap drills in mm, ~75% thread (standard number/letter/
# fractional drills). #8-32, #10-24 and #10-32 keep the metric-drill
# substitutes earlier releases used, so existing designs do not change.
_UNIFIED_DRILL = {
    "#0-80": 1.19, "#1-64": 1.51, "#1-72": 1.51, "#2-56": 1.78, "#2-64": 1.78,
    "#3-48": 1.99, "#3-56": 2.08, "#4-40": 2.26, "#4-48": 2.37, "#5-40": 2.58,
    "#5-44": 2.64, "#6-32": 2.7, "#6-40": 2.87, "#8-32": 3.4, "#8-36": 3.45,
    "#10-24": 3.9, "#10-32": 4.1, "#12-24": 4.5, "#12-28": 4.62,
    "1/4-20": 5.1, "1/4-28": 5.4, "5/16-18": 6.53, "5/16-24": 6.91,
    "3/8-16": 7.94, "3/8-24": 8.43, "7/16-14": 9.35, "7/16-20": 9.92,
    "1/2-13": 10.72, "1/2-20": 11.51, "5/8-11": 13.49, "5/8-18": 14.68,
    "3/4-10": 16.67, "3/4-16": 17.46,
}


def _tap_drill(thread_size: str):
    """Parse a thread designation -> (drill_mm, canonical, kind, size, pitch).

    Case-insensitive; accepts 'M3', 'm4', 'M4X0.7', 'M8 x 1', '#10-32',
    '10-32', '1/4-20', '1/4-20 UNC'. For metric, `pitch` is in mm; for
    unified it is threads per inch. Raises ValueError for anything it cannot
    size - it never guesses. (It used to fall back to M3's 2.5 mm drill, so
    an M20 or a lowercase 'm4' silently got a 2.5 mm hole.)"""
    s = thread_size.strip().upper().replace(" ", "").replace('"', "")
    for suffix in ("UNEF", "UNC", "UNF"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    m = re.fullmatch(r"M(\d+(?:\.\d+)?)(?:[X\u00d7](\d+(?:\.\d+)?))?", s)
    if m:
        d = float(m.group(1))
        coarse = _METRIC_COARSE_PITCH.get(d)
        p = float(m.group(2)) if m.group(2) else coarse
        if p is None:
            raise ValueError("M{:g} has no standard coarse pitch in the table - "
                             "give the pitch, e.g. 'M{:g}x1.5'".format(d, d))
        if not 0 < p < d / 2:
            raise ValueError("pitch {:g} mm is implausible for M{:g}".format(p, d))
        if coarse is not None and abs(p - coarse) < 1e-9 and d in _METRIC_COARSE_DRILL:
            drill = _METRIC_COARSE_DRILL[d]
        else:
            drill = round(d - p, 3)
        return drill, "M{:g}x{:g}".format(d, p), "metric", d, p
    m = re.fullmatch(r"(#?\d+|\d+/\d+)-(\d+)", s)
    if m:
        size = m.group(1)
        if "/" not in size and not size.startswith("#"):
            size = "#" + size
        key = "{}-{}".format(size, m.group(2))
        if key in _UNIFIED_DRILL:
            return _UNIFIED_DRILL[key], key, "unified", size, int(m.group(2))
        raise ValueError("unified size {} is not in the tap-drill table".format(key))
    raise ValueError("unrecognised thread size {!r}".format(thread_size))


@mcp.tool()
def tapped_hole(thread_size: str, x_mm: float, y_mm: float, depth_mm: float,
                body_index: int = 0, tap_drill_only: bool = True,
                tap_drill_mm: float = None) -> str:
    """Create a tapped hole at (x, y), cutting straight down from the TOP of
    body `body_index` (its highest point); only that body is cut.

    thread_size: e.g. 'M3', 'M4x0.7', 'M8x1' (fine pitch), '#6-32', '10-32',
    '1/4-20' - case-insensitive. Metric drills are major minus pitch, so any
    pitch works; unified sizes come from a table. An unrecognised size is an
    ERROR and nothing is cut - never a guess. Pass tap_drill_mm to size the
    hole yourself. tap_drill_only=True (default) cuts only the tap-drill hole,
    for threading with a real tap (best for 3D-printed and hand-tapped parts).
    False also applies a MODELED thread (visualization / CNC), and prints a
    WARNING, with the reason, if Fusion has no matching designation.
    """
    try:
        drill_mm, canon, kind, size, pitch = _tap_drill(thread_size)
    except ValueError as e:
        if tap_drill_mm is None:
            return ("ERROR: {}. Nothing was cut. Supported: metric 'M<d>' or "
                    "'M<d>x<pitch>' (any pitch), and unified: {}. Or pass "
                    "tap_drill_mm to size the hole yourself."
                    .format(e, ", ".join(sorted(_UNIFIED_DRILL))))
        drill_mm, canon, kind, size, pitch = None, thread_size, None, None, None
    if tap_drill_mm is not None:
        drill_mm = float(tap_drill_mm)
    drill = drill_mm / 20.0  # radius in cm
    D = depth_mm / 10.0
    x, y = x_mm / 10.0, y_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "_ts = {ts!r}\n"
        "_before = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        + _TOP_DOWN_SKETCH +
        "ctr = adsk.core.Point3D.create({x}, {y}, 0)\n"
        "sk.sketchCurves.sketchCircles.addByCenterRadius(ctr, {R})\n"
        "prof = sk.profiles.item(0)\n"
        "ext = root.features.extrudeFeatures\n"
        "inp = ext.createInput(prof, adsk.fusion.FeatureOperations.CutFeatureOperation)\n"
        "inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-{D}))\n"
        "inp.participantBodies = [body]   # only the chosen body\n"
        "hole = ext.add(inp)\n"
        "_after = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "_delta = _after - _before\n"
        "print('tap-drill hole (%s, dia {dmm}mm) cut at ({xm},{ym})mm, {dpm}mm deep from the top of %s (z=%.2fmm) | volume delta: %+.4f cm^3'\n"
        "      % (_ts, body.name, _top * 10, _delta))\n"
        "if abs(_delta) < 1e-7:\n"
        "    print('WARNING: volume delta ~ZERO - tap-drill cut had NO effect (missed body / wrong location / depth).')\n"
    ).format(bi=body_index, x=x, y=y, R=drill, D=D, ts=canon, dmm=drill_mm,
             xm=x_mm, ym=y_mm, dpm=depth_mm)
    if not tap_drill_only:
        code += _modeled_thread_code(canon, kind, size, pitch)
    return _send(code)


def _modeled_thread_code(canon, kind, size, pitch) -> str:
    """Generated code that puts a modeled internal thread on the side face of
    `hole` (an ExtrudeFeature in `root`).

    Written against the ThreadDataQuery API as it actually behaves (checked
    live): allSizes / allDesignations / allClasses return sequences directly.
    The getAll* methods called before 1.9.1 do not exist, so modeled threads
    never worked. Fusion lists inch sizes as decimal inches, some labelled
    ('0.19(#10)', '0.25'), and inch designations drop the '#' ('10-32 UNF')."""
    return (
        "_ts = " + repr(canon) + "\n"
        "KIND = " + repr(kind) + "\n"
        "MAJOR = " + repr(size) + "\n"
        "PITCH = " + repr(pitch) + "\n"
        "_applied = None\n"
        "_why = 'unknown'\n"
        "try:\n"
        "    import re as _re\n"
        "    threads = root.features.threadFeatures\n"
        "    q = threads.threadDataQuery\n"
        "    tclass = None\n"
        "    if KIND == 'metric':\n"
        "        tclass = q.defaultMetricThreadType\n"
        "        want_d = MAJOR\n"
        "    elif KIND == 'unified':\n"
        "        tclass = q.defaultInchThreadType\n"
        "        # nominal diameter in inches: #n -> 0.060 + 0.013n; a/b -> a/b\n"
        "        if MAJOR.startswith('#'):\n"
        "            want_d = 0.060 + 0.013 * int(MAJOR[1:])\n"
        "        else:\n"
        "            _n, _dn = MAJOR.split('/')\n"
        "            want_d = float(_n) / float(_dn)\n"
        "    else:\n"
        "        _why = 'designation %r cannot be matched to a Fusion thread' % _ts\n"
        "    size = None\n"
        "    if tclass:\n"
        "        for s in q.allSizes(tclass):\n"
        "            _m = _re.match(r'[\\d.]+', s)\n"
        "            if _m and abs(float(_m.group(0)) - want_d) < 1e-3:\n"
        "                size = s\n"
        "                break\n"
        "        if size is None:\n"
        "            _why = 'thread type %r has no size for %s' % (tclass, _ts)\n"
        "    desig = None\n"
        "    if size is not None:\n"
        "        desigs = list(q.allDesignations(tclass, size))\n"
        "        for dsg in desigs:\n"
        "            du = dsg.replace(' ', '').upper()\n"
        "            if KIND == 'metric':\n"
        "                _m = _re.match(r'M([\\d.]+)X([\\d.]+)', du)\n"
        "                if _m and abs(float(_m.group(2)) - PITCH) < 1e-6:\n"
        "                    desig = dsg\n"
        "                    break\n"
        "            else:\n"
        "                want = '%s-%d' % (MAJOR.lstrip('#'), PITCH)\n"
        "                if du.startswith(want) and not du[len(want):len(want) + 1].isdigit():\n"
        "                    desig = dsg\n"
        "                    break\n"
        "        if desig is None:\n"
        "            _why = 'none of %s matches %s' % (desigs, _ts)\n"
        "    side = None\n"
        "    for f in hole.sideFaces:\n"
        "        side = f\n"
        "        break\n"
        "    if desig and side is None:\n"
        "        _why = 'the cut produced no side face to thread'\n"
        "    if desig and side is not None:\n"
        "        classes = list(q.allClasses(True, tclass, desig))\n"
        "        # the usual default fit: 6H metric, 2B unified\n"
        "        cls = next((c for c in classes if c in ('6H', '2B')),\n"
        "                   classes[0] if classes else '')\n"
        "        # createThreadInfo(isInternal, threadType, designation, class)\n"
        "        tinfo = threads.createThreadInfo(True, tclass, desig, cls)\n"
        "        tin = threads.createInput(side, tinfo)\n"
        "        tin.isModeled = True\n"
        "        threads.add(tin)\n"
        "        _applied = '%s %s' % (desig, cls)\n"
        "except Exception as _te:\n"
        "    _why = str(_te)[:120]\n"
        "if _applied:\n"
        "    print('modeled thread applied: ' + _applied)\n"
        "else:\n"
        "    print('WARNING: modeled thread NOT applied (%s). The tap-drill hole '\n"
        "          'above was cut.' % _why)\n"
    )


# ==========================================================================
# FACE-REFERENCED FEATURES
# ==========================================================================
@mcp.tool()
def sketch_on_face(component_name: str = "", which: str = "top",
                   body_index: int = 0) -> str:
    """Select an existing face (by orientation) and stash it for the next
    face-referenced operation. which: top/bottom (max/min Z), front/back
    (min/max Y), right/left (max/min X). Leave component_name empty for the
    root component. Call hole_on_face immediately after (single-slot selection;
    don't interleave other operations). Verify with view_model on complex parts."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "body = host.bRepBodies.item({bi})\n"
        "sel = {{'top':('z','max'),'bottom':('z','min'),'front':('y','min'),"
        "'back':('y','max'),'right':('x','max'),'left':('x','min')}}[{w!r}]\n"
        "axis, mode = sel\n"
        "best = None\n"
        "bestv = None\n"
        "for f in body.faces:\n"
        "    v = getattr(f.centroid, axis)\n"
        "    if best is None or (mode=='max' and v>bestv) or (mode=='min' and v<bestv):\n"
        "        best, bestv = f, v\n"
        "import __main__ as _m\n"
        "_m._mcp_face = best\n"
        "c = best.centroid\n"
        "print('selected ' + str({w!r}) + ' face: centroid=({{:.2f}},{{:.2f}},{{:.2f}})cm area={{:.3f}}'.format(c.x,c.y,c.z,best.area))\n"
    ).format(cn=component_name, bi=body_index, w=which)
    return _send(code)


@mcp.tool()
def hole_on_face(diameter_mm: float, u_mm: float, v_mm: float,
                 depth_mm: float, through: bool = False) -> str:
    """Drill a hole into the face most recently selected by sketch_on_face.
    (u, v) are mm offsets from that face's centroid along the face's own axes.
    through=True cuts all the way; otherwise cuts to depth. Run sketch_on_face first."""
    R, D = diameter_mm / 20.0, depth_mm / 10.0
    u, v = u_mm / 10.0, v_mm / 10.0
    code = (
        "import __main__ as _m\n"
        "face = getattr(_m, '_mcp_face', None)\n"
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "if face is None:\n"
        "    print('ERROR: no face selected - call sketch_on_face first')\n"
        "else:\n"
        "    _before = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "    comp = face.body.parentComponent\n"
        "    sk = comp.sketches.add(face)\n"
        "    ctr2d = sk.modelToSketchSpace(face.centroid)\n"
        "    cpt = adsk.core.Point3D.create(ctr2d.x + {u}, ctr2d.y + {v}, 0)\n"
        "    sk.sketchCurves.sketchCircles.addByCenterRadius(cpt, {R})\n"
        "    prof = sk.profiles.item(0)\n"
        "    ext = comp.features.extrudeFeatures\n"
        "    inp = ext.createInput(prof, adsk.fusion.FeatureOperations.CutFeatureOperation)\n"
        "    inp.participantBodies = [face.body]   # only the body that owns the face\n"
        "    if {thru}:\n"
        "        ext_dir = adsk.fusion.ExtentDirections.NegativeExtentDirection\n"
        "        inp.setAllExtent(ext_dir)\n"
        "    else:\n"
        "        inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-{D}))\n"
        "    ext.add(inp)\n"
        "    _after = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "    _delta = _after - _before\n"
        "    print('hole on face at u={um} v={vm} mm | volume delta: {{:+.4f}} cm^3'.format(_delta))\n"
        "    if abs(_delta) < 1e-7:\n"
        "        print('WARNING: volume delta ~ZERO - cut had NO effect (missed body / wrong face / depth).')\n"
    ).format(u=u, v=v, R=R, D=D, thru=through, um=u_mm, vm=v_mm)
    return _send(code)


# ==========================================================================
# MEASUREMENT & STATE
# ==========================================================================
@mcp.tool()
def get_model_state() -> str:
    """Comprehensive design state in ONE call - replaces hand-rolled introspection
    and makes crash recovery a single query. ALWAYS leads with the active
    DOCUMENT identity (name + lineage id + saved/unsaved) so you know which
    document you're about to clear/save/overwrite. Then per body: name, validity,
    volume (cm^3), bbox (mm), face/edge counts, and sub-threshold ("sliver") face
    count. Plus sketch count and timeline feature names."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "lines = []\n"
        "# --- document identity FIRST (prevents wrong-doc overwrites) ---\n"
        "try:\n"
        "    doc = app.activeDocument\n"
        "    dname = doc.name\n"
        "    try:\n"
        "        if doc.dataFile:\n"
        "            lineage = doc.dataFile.id\n"
        "            saved = 'SAVED'\n"
        "        else:\n"
        "            lineage = '(none)'\n"
        "            saved = 'UNSAVED'\n"
        "    except Exception:\n"
        "        # app.data unavailable: the lineage CANNOT be read. Saying\n"
        "        # UNSAVED here is what leads to saveAs -> a duplicate design.\n"
        "        lineage = 'UNKNOWN (app.data unavailable)'\n"
        "        saved = 'UNKNOWN - run document_report() before saving'\n"
        "    lines.append('ACTIVE DOCUMENT: ' + dname + '  [' + saved + ']  lineage=' + str(lineage))\n"
        "except Exception:\n"
        "    lines.append('ACTIVE DOCUMENT: (could not read)')\n"
        "lines.append('--')\n"
        "for b in root.bRepBodies:\n"
        "    bb = b.boundingBox\n"
        "    dx = (bb.maxPoint.x-bb.minPoint.x)*10\n"
        "    dy = (bb.maxPoint.y-bb.minPoint.y)*10\n"
        "    dz = (bb.maxPoint.z-bb.minPoint.z)*10\n"
        "    try:\n"
        "        valid = b.isValid\n"
        "    except Exception:\n"
        "        valid = '?'\n"
        "    slivers = 0\n"
        "    _fcount = b.faces.count\n"
        "    _scan = b.faces if _fcount <= 4000 else []\n"
        "    for f in _scan:\n"
        "        try:\n"
        "            if f.area < 0.0005:   # 0.05 mm^2, as sliver_face_count()\n"
        "                slivers += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "    _sl = str(slivers) if _fcount <= 4000 else 'skipped(>4000 faces)'\n"
        "    lines.append('{}: valid={} vol={:.3f}cm^3 bbox={:.1f}x{:.1f}x{:.1f}mm faces={} edges={} slivers={}'.format(\n"
        "        b.name, valid, b.volume, dx, dy, dz, _fcount, b.edges.count, _sl))\n"
        "lines.append('--')\n"
        "lines.append('bodies: ' + str(root.bRepBodies.count) + '   sketches: ' + str(root.sketches.count))\n"
        "try:\n"
        "    tl = design.timeline\n"
        "    feats = []\n"
        "    for i in range(tl.count):\n"
        "        try:\n"
        "            feats.append(tl.item(i).name)\n"
        "        except Exception:\n"
        "            feats.append('<?>')\n"
        "    lines.append('timeline (' + str(tl.count) + '): ' + ', '.join(feats))\n"
        "except Exception:\n"
        "    lines.append('timeline: (none / direct-modeling doc)')\n"
        "print('\\n'.join(lines))\n"
    )
    return _send(code, compact=False)


@mcp.tool()
def measure_distance(which_a: str, which_b: str, body_index: int = 0) -> str:
    """Measure true distance (mm) between two reference faces of a body, each
    selected by orientation: top/bottom/front/back/left/right. Centroid to
    centroid. More trustworthy than the bounding box for verifying features."""
    code = (
        "import math\n"
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "def pick(which):\n"
        "    sel = {{'top':('z','max'),'bottom':('z','min'),'front':('y','min'),"
        "'back':('y','max'),'right':('x','max'),'left':('x','min')}}[which]\n"
        "    axis, mode = sel\n"
        "    best=None; bv=None\n"
        "    for f in body.faces:\n"
        "        v=getattr(f.centroid,axis)\n"
        "        if best is None or (mode=='max' and v>bv) or (mode=='min' and v<bv):\n"
        "            best,bv=f,v\n"
        "    return best\n"
        "fa=pick({a!r}); fb=pick({b!r})\n"
        "ca,cb=fa.centroid,fb.centroid\n"
        "d=math.sqrt((ca.x-cb.x)**2+(ca.y-cb.y)**2+(ca.z-cb.z)**2)*10\n"
        "print('distance ' + str({a!r}) + '<->' + str({b!r}) + ' = {{:.3f}} mm'.format(d))\n"
    ).format(bi=body_index, a=which_a, b=which_b)
    return _send(code, compact=False)


@mcp.tool()
def cross_section(body_index: int = 0, axis: str = "z", at_mm: float = None,
                  span_axis: str = "x", samples: int = 41,
                  at_a_mm: float = None, at_b_mm: float = None) -> str:
    """Scan a line of points through a body, printing an ASCII solid/void row
    (#=solid .=void) plus the mm coordinates of every boundary. The fastest way
    to check wall thickness, trapped voids or a missed cut without a screenshot.

    Sweeps `span_axis` across the bbox, holding the other two axes at their mid
    value (or at `at_mm` along `axis`). Pin the held axes with at_a_mm/at_b_mm
    to scan through a specific feature, e.g. span_axis='y', at_a_mm=3.8."""
    at_expr = "None" if at_mm is None else str(at_mm / 10.0)
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "bb = body.boundingBox\n"
        "mn, mx = bb.minPoint, bb.maxPoint\n"
        "ax = {ax!r}\n"
        "span = {sp!r}\n"
        "at = {at}\n"
        "N = {n}\n"
        "def axval(p, a):\n"
        "    return getattr(p, a)\n"
        "# fixed coords for the two non-span axes\n"
        "all_axes = ['x','y','z']\n"
        "others = [a for a in all_axes if a != span]\n"
        "fixed = {{}}\n"
        "PINS = {pins!r}\n"
        "for idx, a in enumerate(others):\n"
        "    if PINS[idx] is not None:\n"
        "        fixed[a] = PINS[idx] / 10.0     # mm -> cm\n"
        "    elif a == ax and at is not None:\n"
        "        fixed[a] = at\n"
        "    else:\n"
        "        fixed[a] = (axval(mn,a)+axval(mx,a))/2.0\n"
        "lo = axval(mn, span); hi = axval(mx, span)\n"
        "_INSIDE = adsk.fusion.PointContainment.PointInsidePointContainment\n"
        "_queries = [0]\n"
        "def probe(t):\n"
        "    coord = {{span:t}}\n"
        "    coord.update(fixed)\n"
        "    pt = adsk.core.Point3D.create(coord['x'],coord['y'],coord['z'])\n"
        "    _queries[0] += 1\n"
        "    return body.pointContainment(pt) == _INSIDE\n"
        "row = []\n"
        "states = []\n"
        "for i in range(N):\n"
        "    t = lo + (hi-lo)*i/(N-1)\n"
        "    solid = probe(t)\n"
        "    row.append('#' if solid else '.')\n"
        "    states.append((t, solid))\n"
        "print('cross-section of ' + body.name + ' along ' + span + ' (#=solid .=void):')\n"
        "print(''.join(row))\n"
        "print('span ' + span + ': {{:.1f}}mm .. {{:.1f}}mm at '.format(lo*10,hi*10) + \n"
        "      ', '.join(a+'={{:.1f}}mm'.format(fixed[a]*10) for a in others))\n"
        "# boundaries\n"
        "# Refine ONLY at transitions by bisection. A uniform scan wastes kernel\n"
        "# queries deep inside solid regions while giving boundary precision of\n"
        "# only span/N; ~8 bisections per transition costs a handful of extra\n"
        "# queries and improves precision by ~256x.\n"
        "bounds = []\n"
        "for i in range(1,len(states)):\n"
        "    if states[i][1] != states[i-1][1]:\n"
        "        a, b_ = states[i-1][0], states[i][0]\n"
        "        a_solid = states[i-1][1]\n"
        "        for _ in range(8):\n"
        "            mid = (a + b_) / 2.0\n"
        "            if probe(mid) == a_solid:\n"
        "                a = mid\n"
        "            else:\n"
        "                b_ = mid\n"
        "        edge_mm = (a + b_) / 2.0 * 10\n"
        "        kind = 'solid->void' if a_solid else 'void->solid'\n"
        "        bounds.append('{{:.3f}}mm {{}}'.format(edge_mm, kind))\n"
        "print('boundaries: ' + ('; '.join(bounds) if bounds else 'none (uniform)'))\n"
        "if len(bounds) >= 2:\n"
        "    try:\n"
        "        _w = []\n"
        "        for _k in range(0, len(bounds)-1, 2):\n"
        "            _a = float(bounds[_k].split('mm')[0])\n"
        "            _b = float(bounds[_k+1].split('mm')[0])\n"
        "            _w.append(abs(_b-_a))\n"
        "        if _w:\n"
        "            print('spans: ' + ', '.join('%.3fmm' % x for x in _w))\n"
        "    except Exception:\n"
        "        pass\n"
        "print('(%d kernel queries)' % _queries[0])\n"
    ).format(bi=body_index, ax=axis, sp=span_axis, at=at_expr, n=max(5, samples),
             pins=[at_a_mm, at_b_mm])
    return _send(code, compact=False)


@mcp.tool()
def list_faces(body_index: int = 0, max_faces: int = 40) -> str:
    """List a body's faces with a stable tempId, centroid (mm), area (mm^2), and
    surface type. The tempId lets you re-select the same face on a later call
    (pass it to face-targeted raw code via body.findByTempId). Essential for
    multi-step face work; sorted largest-area first."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item(%d)\n"
        "rows = []\n"
        "_cap = 4000   # bound kernel area/centroid queries on dense bodies\n"
        "_seen = 0\n"
        "for f in body.faces:\n"
        "    _seen += 1\n"
        "    if _seen > _cap:\n"
        "        break\n"
        "    try:\n"
        "        c = f.centroid\n"
        "        st = type(f.geometry).__name__\n"
        "        rows.append((f.area, f.tempId, c.x*10, c.y*10, c.z*10, st))\n"
        "    except Exception:\n"
        "        pass\n"
        "rows.sort(key=lambda r: -r[0])\n"
        "lines = []\n"
        "for r in rows[:%d]:\n"
        "    lines.append('tempId={} area={:.2f}mm^2 centroid=({:.1f},{:.1f},{:.1f})mm {}'.format(\n"
        "        r[1], r[0]*100, r[2], r[3], r[4], r[5]))\n"
        "print('faces of ' + body.name + ' (' + str(body.faces.count) + ' total'\n"
        "      + (', measured first ' + str(_cap) if _seen > _cap else '') + '):')\n"
        "print('\\n'.join(lines))\n"
    ) % (body_index, max_faces)
    return _send(code, compact=False)


@mcp.tool()
def list_edges(body_index: int = 0, max_edges: int = 40) -> str:
    """List a body's edges with a stable tempId, length (mm), midpoint (mm), and
    the ADJACENT SURFACE-TYPE PAIR (e.g. Cylinder|Cone) - which is how you
    actually target a fillet. Sorted longest first. Degenerate edges (zero
    length or <2 faces) are skipped and counted, so iteration never raises
    'Bad index parameter'. Feed the tempIds to select_edges/fillet_edges_smart."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item(%d)\n"
        "rows = []\n"
        "skipped = 0\n"
        "for e in body.edges:\n"
        "    try:\n"
        "        # guard: tangential lofts can leave zero-length edges with <2\n"
        "        # faces; e.faces.item(1) then raises 'Bad index parameter'\n"
        "        if e.faces.count < 2 or e.length*10 < 1e-4:\n"
        "            skipped += 1\n"
        "            continue\n"
        "        ev = e.evaluator\n"
        "        ok, sp, ep = ev.getEndPoints()\n"
        "        mid = ((sp.x+ep.x)/2*10, (sp.y+ep.y)/2*10, (sp.z+ep.z)/2*10)\n"
        "        L = e.length*10\n"
        "        fa = type(e.faces.item(0).geometry).__name__\n"
        "        fb = type(e.faces.item(1).geometry).__name__\n"
        "        pair = '|'.join(sorted((fa, fb)))\n"
        "        rows.append((L, e.tempId, mid, pair))\n"
        "    except Exception:\n"
        "        skipped += 1\n"
        "rows.sort(key=lambda r: -r[0])\n"
        "lines = []\n"
        "for r in rows[:%d]:\n"
        "    lines.append('tempId={} len={:.2f}mm mid=({:.1f},{:.1f},{:.1f})mm  {}'.format(\n"
        "        r[1], r[0], r[2][0], r[2][1], r[2][2], r[3]))\n"
        "print('edges of ' + body.name + ' (' + str(body.edges.count) + ' total, '\n"
        "      + str(skipped) + ' degenerate skipped):')\n"
        "print('\\n'.join(lines))\n"
    ) % (body_index, max_edges)
    return _send(code, compact=False)


@mcp.tool()
def heartbeat() -> str:
    """Crash-vs-busy check with explicit recovery guidance. Reads the listener's
    /ping channel (separate from geometry execution, so it answers even during a
    hung op). If it responds: Fusion is alive - check busy/current_job to see if
    an op is still running. If it errors/times out: Fusion likely crashed -
    restart the MCPListener add-in. (ping_fusion returns the same liveness data;
    this adds the recovery instructions.)"""
    try:
        r = requests.get(LISTENER_URL + "/ping", headers=_headers(), timeout=5)
        refused = _refused(r)
        if refused:
            return refused
        d = r.json()
        pm = _check_protocol(d)
        if pm:
            return pm
        return ("SUCCESS (Fusion ALIVE):\nbusy={} current_job={} idle_seconds={}\n"
                "If busy=True a job is still running - poll job_status. If you "
                "expected a result and busy=False, the op finished; call "
                "get_state() or get_model_state().".format(
                    d.get('busy'), d.get('current_job'), d.get('idle_seconds')))
    except Exception as e:
        return ("LISTENER NOT RESPONDING ({}).\nFusion likely crashed or the "
                "add-in stopped. Restart it: Shift+S > Add-Ins > MCPListener > "
                "Run. Then call get_state() to see what survived.".format(e))


# ==========================================================================
# VISUAL FEEDBACK
# ==========================================================================
@mcp.tool()
def view_model(orientation: str = "iso",
               zoom_x_mm: float = None, zoom_y_mm: float = None,
               zoom_z_mm: float = None, zoom_radius_mm: float = None,
               width: int = 800, height: int = 600) -> Image:
    """Orient the camera, fit, screenshot the viewport, and RETURN the image.

    orientation: iso / top / bottom / front / back / left / right. An
    unrecognised value is an ERROR (it used to silently return the previous
    view, which reads as "nothing changed" and wastes a round trip).

    ZOOM: pass zoom_x_mm/zoom_y_mm/zoom_z_mm plus zoom_radius_mm to frame a
    small detail instead of fitting the whole model. Inspecting a 4 mm feature
    on a 76 mm part is impossible with a whole-model fit.

    COST: an image costs roughly (width*height)/750 tokens - 800x600 is ~640,
    1200x900 is ~1440. The 800x600 default is legible for shape and placement.
    Raise it only when judging a fine detail; drop to 640x480 (~410) for a
    quick sanity look.
    """
    # A fresh temp file per call. The old fixed ~/fusion_view.png was shared
    # by concurrent calls and left behind in the home folder.
    fd, tmp = tempfile.mkstemp(prefix="fusion_view_", suffix=".png")
    os.close(fd)
    zoom = None
    if None not in (zoom_x_mm, zoom_y_mm, zoom_z_mm) and zoom_radius_mm:
        zoom = (zoom_x_mm / 10.0, zoom_y_mm / 10.0, zoom_z_mm / 10.0,
                zoom_radius_mm / 10.0)
    code = (
        "vp = app.activeViewport\n"
        "cam = vp.camera\n"
        "o = {o!r}\n"
        + VIEWS + UPS +
        "if o not in eyes:\n"
        "    print('ERROR: unknown orientation %r. Valid: %s'\n"
        "          % (o, ', '.join(sorted(eyes))))\n"
        "else:\n"
        "    eye = eyes[o]; up = ups[o]\n"
        "    ZOOM = {z!r}\n"
        "    if ZOOM:\n"
        "        cx, cy, cz, rad = ZOOM\n"
        "        d = max(rad * 4.0, 0.1)\n"
        "        import math\n"
        "        L = math.sqrt(eye[0]**2 + eye[1]**2 + eye[2]**2) or 1.0\n"
        "        ux, uy, uz = eye[0]/L, eye[1]/L, eye[2]/L\n"
        "        cam.target = adsk.core.Point3D.create(cx, cy, cz)\n"
        "        cam.eye = adsk.core.Point3D.create(cx+ux*d, cy+uy*d, cz+uz*d)\n"
        "        cam.upVector = adsk.core.Vector3D.create(up[0],up[1],up[2])\n"
        "        cam.isSmoothTransition = False\n"
        "        cam.viewExtents = rad * 2.2\n"
        "        vp.camera = cam\n"
        "        print('framed detail at (%.1f, %.1f, %.1f) mm, radius %.1f mm'\n"
        "              % (cx*10, cy*10, cz*10, rad*10))\n"
        "    else:\n"
        "        cam.eye = adsk.core.Point3D.create(eye[0],eye[1],eye[2])\n"
        "        cam.target = adsk.core.Point3D.create(0,0,0)\n"
        "        cam.upVector = adsk.core.Vector3D.create(up[0],up[1],up[2])\n"
        "        cam.isSmoothTransition = False\n"
        "        vp.camera = cam\n"
        "        vp.fit()\n"
        "    adsk.doEvents()\n"
        "    res = vp.saveAsImageFile({path!r}, {w}, {h})\n"
        "    print('screenshot ok' if res else 'screenshot FAILED')\n"
    ).format(o=orientation, z=zoom, path=tmp,
             w=max(320, int(width)), h=max(240, int(height)))
    try:
        result = _send(code)
        if "unknown orientation" in result:
            raise RuntimeError(result)
        if "screenshot ok" not in result or os.path.getsize(tmp) == 0:
            raise RuntimeError("view_model failed: " + result)
        with open(tmp, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return Image(data=data, format="png")


@mcp.tool()
def set_view(orientation: str = "iso") -> str:
    """Orient the camera without returning an image (iso/top/bottom/front/back/
    right/left), then fit. Unknown orientations are reported as an ERROR rather
    than silently falling back to iso."""
    code = (
        "vp = app.activeViewport\n"
        "cam = vp.camera\n"
        "o = {o!r}\n"
        + VIEWS + UPS +
        "if o not in eyes:\n"
        "    print('ERROR: unknown orientation %r. Valid: %s'\n"
        "          % (o, ', '.join(sorted(eyes))))\n"
        "else:\n"
        "    eye = eyes[o]; up = ups[o]\n"
        "    cam.eye = adsk.core.Point3D.create(eye[0],eye[1],eye[2])\n"
        "    cam.target = adsk.core.Point3D.create(0,0,0)\n"
        "    cam.upVector = adsk.core.Vector3D.create(up[0],up[1],up[2])\n"
        "    cam.isSmoothTransition = False\n"
        "    vp.camera = cam\n"
        "    vp.fit()\n"
        "    print('view set to ' + str(o))\n"
    ).format(o=orientation)
    return _send(code)


@mcp.tool()
def create_component(name: str) -> str:
    """Create a new empty component placed as an occurrence at the assembly
    origin. Returns its fullPathName. One component per distinct part.
    Needs an Assembly design: Fusion refuses a second component in a Part
    design ("add this Part to an Assembly")."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "m = adsk.core.Matrix3D.create()\n"
        "occ = root.occurrences.addNewComponent(m)\n"
        "occ.component.name = {n!r}\n"
        "print('created component occurrence: ' + occ.fullPathName)\n"
    ).format(n=name)
    return _send(code)


@mcp.tool()
def list_components() -> str:
    """List all occurrences with component name, origin position (mm) and
    grounding: grounded / grounded to parent / free. Only a free part can be
    moved by a joint."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "lines = []\n"
        "for occ in root.allOccurrences:\n"
        "    t = occ.transform.translation\n"
        "    if occ.isGrounded:\n"
        "        _g = 'grounded'\n"
        "    elif getattr(occ, 'isGroundToParent', False):\n"
        "        _g = 'grounded to parent'\n"
        "    else:\n"
        "        _g = 'free'\n"
        "    lines.append(occ.fullPathName + '  [' + occ.component.name + ']  pos=(' +\n"
        "        '{:.1f},{:.1f},{:.1f}'.format(t.x*10,t.y*10,t.z*10) + ')mm  ' + _g)\n"
        "print('\\n'.join(lines) if lines else 'no components (geometry is in root)')\n"
    )
    return _send(code, compact=False)


@mcp.tool()
def box_in_component(component_name: str, length_mm: float, width_mm: float,
                     height_mm: float, ox_mm: float = 0, oy_mm: float = 0,
                     oz_mm: float = 0) -> str:
    """Create a box INSIDE a named component (on its own XY plane at the given
    local origin). This is how an assembly part gets its geometry.
    component_name: the component, or any occurrence of it ('Screw:3')."""
    if not component_name:
        return "ERROR: component_name is required (use create_box for the root component)"
    L, W, H = length_mm / 10.0, width_mm / 10.0, height_mm / 10.0
    ox, oy, oz = ox_mm / 10.0, oy_mm / 10.0, oz_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "comp = find_component({cn!r})   # raises, listing what exists\n"
        "if comp is not None:\n"
        "    sk = comp.sketches.add(comp.xYConstructionPlane)\n"
        "    p0 = adsk.core.Point3D.create({ox}, {oy}, {oz})\n"
        "    p1 = adsk.core.Point3D.create({x1}, {y1}, {oz})\n"
        "    sk.sketchCurves.sketchLines.addTwoPointRectangle(p0, p1)\n"
        "    prof = sk.profiles.item(0)\n"
        "    ext = comp.features.extrudeFeatures\n"
        "    dist = adsk.core.ValueInput.createByReal({H})\n"
        "    feat = ext.addSimple(prof, dist, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        "    print('added box to ' + str({cn!r}) + ': ' + feat.bodies.item(0).name)\n"
    ).format(cn=component_name, ox=ox, oy=oy, oz=oz, x1=ox + L, y1=oy + W, H=H)
    return _send(code)


@mcp.tool()
def move_component(component_name: str, x_mm: float, y_mm: float, z_mm: float) -> str:
    """Set an occurrence's position (origin translated to x,y,z in mm), keeping
    its rotation. component_name: an occurrence name ('Screw:3') to pick one
    copy, or a component name used by exactly one occurrence - an ambiguous
    name is an ERROR listing the occurrence names. Position parts BEFORE
    jointing; joints override transforms afterward."""
    x, y, z = x_mm / 10.0, y_mm / 10.0, z_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "# an occurrence name ('Screw:3') picks one copy; a bare component\n"
        "# name is accepted only if exactly one occurrence uses it\n"
        "target = find_occurrence({cn!r})\n"
        "if target is not None:\n"
        "    # Change ONLY the translation. Building a fresh identity matrix\n"
        "    # silently dropped any rotation the occurrence had.\n"
        "    m = target.transform\n"
        "    m.translation = adsk.core.Vector3D.create({x}, {y}, {z})\n"
        "    target.transform = m\n"
        "    try:\n"
        "        if design.snapshots.hasPendingSnapshot:\n"
        "            design.snapshots.add()\n"
        "    except Exception:\n"
        "        pass\n"
        "    print('moved ' + str({cn!r}) + ' to ({xm},{ym},{zm})mm')\n"
    ).format(cn=component_name, x=x, y=y, z=z, xm=x_mm, ym=y_mm, zm=z_mm)
    return _send(code)


@mcp.tool()
def copy_component(component_name: str, count: int, dx_mm: float = 0,
                   dy_mm: float = 0, dz_mm: float = 0) -> str:
    """Place `count` additional occurrences of an existing component in a linear
    pattern, each offset (dx,dy,dz) mm from the previous, with the source's
    rotation. Reuses one definition (edit once, all update) - ideal for
    repeated hardware. The copies are occurrences named like 'Screw:2',
    'Screw:3' (see list_components); pass those names to move_component,
    ground_component and the joint tools to address one copy. Once copied, the
    bare component name is ambiguous there, so pass an occurrence name here
    too if you copy again."""
    dx, dy, dz = dx_mm / 10.0, dy_mm / 10.0, dz_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "src = find_occurrence({cn!r})   # the copy to pattern from\n"
        "if src is not None:\n"
        "    base = src.transform.translation\n"
        "    made = 0\n"
        "    for i in range(1, {cnt}+1):\n"
        "        m = src.transform   # copies keep the source's rotation\n"
        "        m.translation = adsk.core.Vector3D.create(base.x+{dx}*i, base.y+{dy}*i, base.z+{dz}*i)\n"
        "        root.occurrences.addExistingComponent(src.component, m)\n"
        "        made += 1\n"
        "    print('placed ' + str(made) + ' more occurrences of ' + str({cn!r}))\n"
    ).format(cn=component_name, cnt=count, dx=dx, dy=dy, dz=dz)
    return _send(code)


@mcp.tool()
def ground_component(component_name: str, grounded: bool = True) -> str:
    """Ground (lock in space) or unground an occurrence. An assembly needs
    exactly one grounded base so other parts joint relative to it. Ground base
    first. Accepts an occurrence name ('Base:1') or an unambiguous component
    name. Ungrounding also clears Fusion's separate 'ground to parent' flag,
    which otherwise keeps the part fixed. Reads both flags back and prints
    CONFIRMED only if the part really is in the requested state."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "# an occurrence name ('Screw:3') picks one copy; a bare component\n"
        "# name is accepted only if exactly one occurrence uses it\n"
        "target = find_occurrence({cn!r})\n"
        "want = {g}\n"
        "target.isGrounded = want\n"
        "# Fusion also has a separate 'ground to parent' flag. A part can carry\n"
        "# it with isGrounded False and still be fixed: this tool used to clear\n"
        "# isGrounded only and print grounded=False (observed live).\n"
        "if not want and hasattr(target, 'isGroundToParent'):\n"
        "    target.isGroundToParent = False\n"
        "g = target.isGrounded\n"
        "gp = getattr(target, 'isGroundToParent', False)\n"
        "fixed = bool(g or gp)\n"
        "if fixed == want:\n"
        "    print('CONFIRMED: %s is %s (isGrounded=%s, isGroundToParent=%s)'\n"
        "          % (target.name, 'grounded' if fixed else 'free to move', g, gp))\n"
        "else:\n"
        "    print('*** NOT APPLIED: asked for grounded=%s, but %s reads '\n"
        "          'isGrounded=%s, isGroundToParent=%s. ***' % (want, target.name, g, gp))\n"
    ).format(cn=component_name, g=grounded)
    return _send(code)


# ==========================================================================
# JOINTS
# ==========================================================================
_JOINT_AXES = {"x": "XAxisJointDirection", "y": "YAxisJointDirection",
               "z": "ZAxisJointDirection"}


def _joint_args_error(motion: str, axis: str) -> str:
    """'' if valid. Unknown values used to be replaced silently - 'slider'
    built a RIGID joint and still printed 'CONFIRMED: slider joint created'."""
    if motion not in ("rigid", "revolute"):
        return ("ERROR: motion must be 'rigid' or 'revolute' (got {!r}). Other "
                "joint types need run_fusion_code.".format(motion))
    if axis not in _JOINT_AXES:
        return "ERROR: axis must be 'x', 'y' or 'z' (got {!r})".format(axis)
    return ""


@mcp.tool()
def rigid_joint(component_a: str, component_b: str) -> str:
    """Create a RIGID joint between two components' origin points. Fusion moves
    component_a onto component_b - pass the grounded base as component_b.
    CONFIRMED only when the joint solves and its points coincide."""
    code = _joint_code(component_a, component_b, "rigid", "z",
                       use_origin=True, ax=0, ay=0, az=0, bx=0, by=0, bz=0)
    return _send(code)


@mcp.tool()
def revolute_joint(component_a: str, component_b: str, axis: str = "z") -> str:
    """Create a REVOLUTE joint (one rotational DOF) about axis (x/y/z) at the
    components' origins. For hinges, lids, swinging arms. Fusion moves
    component_a onto component_b - pass the grounded base as component_b."""
    err = _joint_args_error("revolute", axis)
    if err:
        return err
    code = _joint_code(component_a, component_b, "revolute", axis,
                       use_origin=True, ax=0, ay=0, az=0, bx=0, by=0, bz=0)
    return _send(code)


@mcp.tool()
def joint_at_point(component_a: str, ax_mm: float, ay_mm: float, az_mm: float,
                   component_b: str, bx_mm: float, by_mm: float, bz_mm: float,
                   motion: str = "rigid", axis: str = "z") -> str:
    """Joint two components at explicit LOCAL points (each component's own
    coordinate space, mm) rather than their origins. Creates a hidden sketch
    point (sketch 'mcp_joint_point') in each component, then joins. motion:
    'rigid' or 'revolute'; axis for revolute. Fusion moves component_a onto
    component_b - pass the grounded base as component_b. Needs an Assembly
    design - a Part design can hold only one component."""
    err = _joint_args_error(motion, axis)
    if err:
        return err
    code = _joint_code(component_a, component_b, motion, axis, use_origin=False,
                       ax=ax_mm / 10.0, ay=ay_mm / 10.0, az=az_mm / 10.0,
                       bx=bx_mm / 10.0, by=by_mm / 10.0, bz=bz_mm / 10.0)
    return _send(code)


@mcp.tool()
def list_joints() -> str:
    """List all joints with their motion type."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "types = {0:'rigid',1:'revolute',2:'slider',3:'cylindrical',4:'pin-slot',5:'planar',6:'ball'}\n"
        "lines = []\n"
        "for j in root.joints:\n"
        "    try:\n"
        "        jt = types.get(j.jointMotion.jointType, '?')\n"
        "    except Exception:\n"
        "        jt = '?'\n"
        "    lines.append(j.name + ': ' + jt)\n"
        "print('\\n'.join(lines) if lines else 'no joints')\n"
    )
    return _send(code, compact=False)


def _joint_code(ca, cb, motion, axis, use_origin, ax, ay, az, bx, by, bz):
    """Shared joint builder. Not a tool (no decorator) - internal helper.

    Fusion moves component_a onto component_b (verified live: "First
    component to move is grounded, it can not move"). So:
    - an explicitly grounded component_a is refused - it cannot move;
    - a component_a that is only 'grounded to parent' (a separate Fusion flag,
      observed live on a part whose isGrounded was False) is freed, and the
      output says so - otherwise the joint is created but can never solve;
    - CONFIRMED requires the joint to be healthy AND its two points to
      coincide in world space. A joint count going up is not enough: Fusion
      adds an unsolvable joint and only flags its health."""
    axdir = _JOINT_AXES[axis]   # validated by the calling tool
    if use_origin:
        geo_setup = (
            "_pa_local = (0.0, 0.0, 0.0)\n"
            "_pb_local = (0.0, 0.0, 0.0)\n"
            "ga = adsk.fusion.JointGeometry.createByPoint(\n"
            "    a.component.originConstructionPoint.createForAssemblyContext(a))\n"
            "gb = adsk.fusion.JointGeometry.createByPoint(\n"
            "    b.component.originConstructionPoint.createForAssemblyContext(b))\n"
        )
    else:
        geo_setup = (
            "# A construction point built from a raw coordinate raises\n"
            "# 'Environment is not supported' in parametric designs (verified\n"
            "# live). A sketch point works, and JointGeometry takes it directly.\n"
            "def addpt(comp, x, y, z):\n"
            "    sk = comp.sketches.add(comp.xYConstructionPlane)\n"
            "    sk.name = 'mcp_joint_point'\n"
            "    _helpers.append(sk)\n"
            "    sp = sk.sketchPoints.add(adsk.core.Point3D.create(x, y, z))\n"
            "    sk.isVisible = False\n"
            "    return sp\n"
            "_pa_local = ({ax}, {ay}, {az})\n"
            "_pb_local = ({bx}, {by}, {bz})\n"
            "pa = addpt(a.component, *_pa_local)\n"
            "pb = addpt(b.component, *_pb_local)\n"
            "ga = adsk.fusion.JointGeometry.createByPoint(pa.createForAssemblyContext(a))\n"
            "gb = adsk.fusion.JointGeometry.createByPoint(pb.createForAssemblyContext(b))\n"
        ).format(ax=ax, ay=ay, az=az, bx=bx, by=by, bz=bz)

    motion_setup = (
        "ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.{ad})\n".format(ad=axdir)
        if motion == "revolute" else
        "ji.setAsRigidJointMotion()\n"
    )

    return (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "# occurrence names ('Screw:3') or unambiguous component names; a miss\n"
        "# or an ambiguous name raises with the list of valid names\n"
        "a = find_occurrence({ca!r})\n"
        "b = find_occurrence({cb!r})\n"
        "if a.isGrounded:\n"
        "    raise RuntimeError(\n"
        "        '%s is grounded, so it cannot move - and a joint moves component_a '\n"
        "        'onto component_b. Pass the grounded base as component_b, or '\n"
        "        'unground this part first.' % a.name)\n"
        "if getattr(a, 'isGroundToParent', False):\n"
        "    a.isGroundToParent = False\n"
        "    print('note: %s was grounded to its parent; freed it so the joint can '\n"
        "          'move it' % a.name)\n"
        "_helpers = []\n"
        + geo_setup +
        "_jbefore = root.joints.count\n"
        "ji = root.joints.createInput(ga, gb)\n"
        + motion_setup +
        "try:\n"
        "    j = root.joints.add(ji)\n"
        "except Exception:\n"
        "    for _s in _helpers:        # leave no helper sketches behind\n"
        "        try:\n"
        "            _s.deleteMe()\n"
        "        except Exception:\n"
        "            pass\n"
        "    raise\n"
        "def _world(occ, p):\n"
        "    q = adsk.core.Point3D.create(*p)\n"
        "    q.transformBy(occ.transform2)\n"
        "    return q\n"
        "_gap = _world(a, _pa_local).distanceTo(_world(b, _pb_local)) * 10.0\n"
        "if root.joints.count <= _jbefore:\n"
        "    print('*** JOINT NOT CREATED: add() returned without error but the '\n"
        "          'joint count did not change (%d). Do NOT report these parts as '\n"
        "          'jointed. ***' % _jbefore)\n"
        "elif j.healthState != 0:\n"
        "    _msg = (getattr(j, 'errorOrWarningMessage', '') or '').split('\\n')[0]\n"
        "    print('*** JOINT CREATED BUT NOT SOLVED (health %s): %s Do NOT report '\n"
        "          'these parts as jointed. ***' % (j.healthState, _msg))\n"
        "elif _gap > 1e-3:\n"
        "    print('*** JOINT UNCONFIRMED: %s reports healthy, but its two points are '\n"
        "          '%.4f mm apart. ***' % (j.name, _gap))\n"
        "else:\n"
        "    print('CONFIRMED: {motion} joint %s solved - %s <-> %s, joint points '\n"
        "          'coincide (%.4f mm)' % (j.name, a.name, b.name, _gap))\n"
    ).format(ca=ca, cb=cb, motion=motion)


# ==========================================================================
# EXPORT
# ==========================================================================
def _export_verify(kind: str, indent: int = 0) -> str:
    """Generated-code tail shared by the export tools; expects _os, _p, _m0 and
    _ok to be set. CONFIRMED needs the call to succeed AND the file to have
    been written by THIS call: a file left at the same path by an earlier
    export used to pass the size check even when this export failed."""
    lines = [
        "_sz = _os.path.getsize(_p) if _os.path.exists(_p) else -1",
        "_m1 = _os.stat(_p).st_mtime_ns if _os.path.exists(_p) else None",
        "_fresh = _m1 is not None and _m1 != _m0",
        "if _ok and _sz > 0 and _fresh:",
        "    print('CONFIRMED: exported " + kind + " to %s (%d bytes)' % (_p, _sz))",
        "else:",
        "    if _m0 is not None and not _fresh:",
        "        _state = 'NOT rewritten - what is there is from an earlier export'",
        "    elif _sz >= 0:",
        "        _state = '%d bytes' % _sz",
        "    else:",
        "        _state = 'does not exist'",
        "    print('*** EXPORT FAILED: the call returned %s; file %s. Do NOT report '",
        "          'this as exported. Check the folder exists and is writable. ***'",
        "          % (_ok, _state))",
    ]
    pad = " " * indent
    return "".join(pad + ln + "\n" for ln in lines)


_EXPORT_HEAD = (
    "import os as _os\n"
    "_p = {fp!r}\n"
    "_m0 = _os.stat(_p).st_mtime_ns if _os.path.exists(_p) else None\n"
)


@mcp.tool()
def export_step(filepath: str) -> str:
    """Export the active design to a STEP file at an absolute path (.step).
    CONFIRMED only if the export succeeded AND wrote the file."""
    code = (_EXPORT_HEAD + (
        "design = _require_design()\n"
        "em = design.exportManager\n"
        "opts = em.createSTEPExportOptions(_p)\n"
        "_ok = em.execute(opts)\n"
    )).format(fp=filepath) + _export_verify("STEP")
    return _send(code)


@mcp.tool()
def export_stl(filepath: str, body_index: int = 0, quality: str = "medium") -> str:
    """Export a body to STL for 3D printing. quality: low/medium/high.
    CONFIRMED only if the export succeeded AND wrote the file."""
    # Actual members on this build are MeshRefinementLow/Medium/High/Custom.
    # The *QualityTriangleMeshRefinement names do NOT exist and raise
    # AttributeError at every quality setting.
    qmap = {"low": "MeshRefinementLow",
            "medium": "MeshRefinementMedium",
            "high": "MeshRefinementHigh"}
    if quality not in qmap:
        return "ERROR: quality must be 'low', 'medium' or 'high' (got {!r})".format(quality)
    ref = qmap[quality]
    code = (_EXPORT_HEAD + (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "em = design.exportManager\n"
        "opts = em.createSTLExportOptions(body, _p)\n"
        "try:\n"
        "    opts.meshRefinement = getattr(adsk.fusion.MeshRefinementSettings, {ref!r})\n"
        "except AttributeError:\n"
        "    _avail = [a for a in dir(adsk.fusion.MeshRefinementSettings) if 'Refinement' in a]\n"
        "    print('WARN: {ref} missing; available: ' + str(_avail))\n"
        "_ok = em.execute(opts)\n"
    )).format(bi=body_index, fp=filepath, ref=ref) + _export_verify("STL")
    return _send(code)


@mcp.tool()
def export_dxf(filepath: str, sketch_index: int = 0,
               component_name: str = "") -> str:
    """Export a sketch's profile to DXF (flat-stock / laser / waterjet). Selects
    sketch by index within the root (or a named component). filepath absolute
    (.dxf). CONFIRMED only if saveAsDXF succeeded AND wrote the file."""
    code = (_EXPORT_HEAD + (
        "design = _require_design()\n"
        "host = find_component({cn!r})   # raises on a miss; never falls back to root\n"
        "if host.sketches.count == 0:\n"
        "    raise RuntimeError('no sketches to export in component ' + host.name)\n"
        "sk = host.sketches.item({si})\n"
        "_ok = sk.saveAsDXF(_p)   # this return value used to be ignored\n"
    )).format(cn=component_name, si=sketch_index, fp=filepath) + _export_verify("DXF")
    return _send(code)


# ==========================================================================
# DOCUMENT PERSISTENCE
# ==========================================================================
@mcp.tool()
def save_document(name: str = "", create_new: bool = False,
                  force_new_lineage: bool = False) -> str:
    """Save the active document and VERIFY what happened. Never guesses.

    Lineage readable+present -> doc.save() (version the OPEN doc). Readable+
    absent -> saveAs `name`. UNREADABLE (app.data broken) -> try doc.save()
    FIRST (non-destructive) and never silently saveAs, which would duplicate an
    already-saved design; use force_new_lineage=True to override.
    create_new=True always saveAs. Reports version before/after and prints
    CONFIRMED only when an independent signal agrees, else UNCONFIRMED - do not
    report an UNCONFIRMED save as done."""
    code = (
        "doc = app.activeDocument\n"
        "want_name = {n!r}\n"
        "create_new = {cn}\n"
        "force_new = {fn}\n"
        "def ident():\n"
        "    o = {{}}\n"
        "    try:\n"
        "        o['name'] = doc.name\n"
        "    except Exception:\n"
        "        o['name'] = '(unreadable)'\n"
        "    for a, k in (('isSaved','saved_flag'), ('isModified','modified')):\n"
        "        try:\n"
        "            o[k] = bool(getattr(doc, a))\n"
        "        except Exception:\n"
        "            o[k] = None\n"
        "    try:\n"
        "        df = doc.dataFile\n"
        "        o['data_ok'] = True\n"
        "        o['lineage'] = df.id if df else None\n"
        "        try:\n"
        "            o['version'] = df.versionNumber if df else None\n"
        "        except Exception:\n"
        "            o['version'] = None\n"
        "    except Exception as _e:\n"
        "        o['data_ok'] = False\n"
        "        o['lineage'] = 'UNKNOWN'\n"
        "        o['version'] = None\n"
        "    return o\n"
        "before = ident()\n"
        "print('BEFORE: name=%s saved_flag=%s modified=%s lineage=%s version=%s data_ok=%s'\n"
        "      % (before['name'], before.get('saved_flag'), before.get('modified'),\n"
        "         before.get('lineage'), before.get('version'), before.get('data_ok')))\n"
        "action = None\n"
        "err = None\n"
        "if create_new:\n"
        "    if not want_name:\n"
        "        print('ERROR: create_new=True requires a name')\n"
        "    else:\n"
        "        try:\n"
        "            proj = app.data.activeProject\n"
        "            doc.saveAs(want_name, proj.rootFolder, 'created via MCP', '')\n"
        "            action = 'saveAs (new lineage)'\n"
        "        except Exception as _e:\n"
        "            err = str(_e)[:120]\n"
        "            print('saveAs FAILED: ' + err)\n"
        "            print('app.data is required for saveAs. Ask the user to')\n"
        "            print('create/save the document in the Fusion UI instead.')\n"
        "elif before.get('data_ok') and before.get('lineage'):\n"
        "    try:\n"
        "        doc.save('saved via MCP')\n"
        "        action = 'save (new version of OPEN document)'\n"
        "    except Exception as _e:\n"
        "        err = str(_e)[:120]\n"
        "        print('save FAILED: ' + err)\n"
        "elif before.get('data_ok') and not before.get('lineage'):\n"
        "    if not want_name:\n"
        "        print('ERROR: this document has never been saved - provide a name')\n"
        "    else:\n"
        "        try:\n"
        "            proj = app.data.activeProject\n"
        "            doc.saveAs(want_name, proj.rootFolder, 'created via MCP', '')\n"
        "            action = 'saveAs (first save)'\n"
        "        except Exception as _e:\n"
        "            err = str(_e)[:120]\n"
        "            print('saveAs FAILED: ' + err)\n"
        "else:\n"
        "    # lineage UNKNOWN: try the non-destructive path first\n"
        "    print('lineage is UNREADABLE (app.data unavailable).')\n"
        "    print('Trying doc.save() first - it versions the OPEN document and')\n"
        "    print('needs no app.data. It cannot create a duplicate.')\n"
        "    try:\n"
        "        doc.save('saved via MCP (lineage unreadable)')\n"
        "        action = 'save (new version of OPEN document)'\n"
        "    except Exception as _e:\n"
        "        err = str(_e)[:120]\n"
        "        print('doc.save() failed: ' + err)\n"
        "        if force_new and want_name:\n"
        "            try:\n"
        "                proj = app.data.activeProject\n"
        "                doc.saveAs(want_name, proj.rootFolder, 'forced via MCP', '')\n"
        "                action = 'saveAs (FORCED new lineage)'\n"
        "            except Exception as _e2:\n"
        "                print('forced saveAs also failed: ' + str(_e2)[:100])\n"
        "        else:\n"
        "            print('NOT falling back to saveAs. If this document already had')\n"
        "            print('a lineage, saveAs would create a DUPLICATE design - the')\n"
        "            print('exact failure this guard exists to prevent.')\n"
        "            print('Ask the user to save in the Fusion UI, or re-call with')\n"
        "            print('force_new_lineage=True if a new document is truly wanted.')\n"
        "after = ident()\n"
        "print('--')\n"
        "print('AFTER : name=%s saved_flag=%s modified=%s lineage=%s version=%s'\n"
        "      % (after['name'], after.get('saved_flag'), after.get('modified'),\n"
        "         after.get('lineage'), after.get('version')))\n"
        "print('action: %s' % (action or 'NONE'))\n"
        "confirmed = False\n"
        "if action:\n"
        "    bv, av = before.get('version'), after.get('version')\n"
        "    if bv is not None and av is not None and av > bv:\n"
        "        confirmed = True\n"
        "        print('CONFIRMED: version %s -> %s' % (bv, av))\n"
        "    elif before.get('modified') and after.get('modified') is False:\n"
        "        confirmed = True\n"
        "        print('CONFIRMED: modified flag cleared')\n"
        "    elif after.get('data_ok') and after.get('lineage') and not before.get('lineage'):\n"
        "        confirmed = True\n"
        "        print('CONFIRMED: document now has a lineage')\n"
        "if action and not confirmed:\n"
        "    print('*** UNCONFIRMED: the save call returned without error, but no')\n"
        "    print('    independent signal (version bump / modified cleared /')\n"
        "    print('    lineage appeared) confirms it landed. Do NOT report this')\n"
        "    print('    as saved. Ask the user to check the Fusion title bar. ***')\n"
        "if not action:\n"
        "    print('*** NOTHING WAS SAVED. ***')\n"
    ).format(n=name, cn=create_new, fn=force_new_lineage)
    return _send(code)


@mcp.tool()
def list_recent_designs(limit: int = 15) -> str:
    """List designs in the active project's root folder (name :: id). Use the id
    with open_document to resume earlier work, or with list_versions to inspect
    version history."""
    code = (
        "try:\n"
        "    proj = app.data.activeProject\n"
        "    folder = proj.rootFolder\n"
        "    items = folder.dataFiles\n"
        "    lines = []\n"
        "    n = min({lim}, items.count)\n"
        "    for i in range(n):\n"
        "        f = items.item(i)\n"
        "        lines.append(f.name + '  ::  ' + f.id)\n"
        "    print('\\n'.join(lines) if lines else 'no designs in active project root')\n"
        "except Exception as _e:\n"
        "    print('ERROR: ' + str(_e))\n"
        "    print('NOTE: this build can raise InternalValidationError on ALL '\n"
        "          'app.data access, which disables list/open/saveAs for the '\n"
        "          'whole session. Versioning an already-open document still '\n"
        "          'works via save_document.')\n"
    ).format(lim=limit)
    return _send(code, compact=False)


@mcp.tool()
def list_versions(design_id: str = "") -> str:
    """READ-ONLY: enumerate the saved versions of a design lineage so you can
    confirm what's recoverable after a bad save, and guide the user to the right
    version to promote in the UI. Pass a lineage id from list_recent_designs /
    get_model_state, or leave empty to use the ACTIVE document's lineage. Lists
    version number, id, and save date. (Promotion/restore is done by the user in
    the Fusion Data Panel - this tool tells them exactly which version.)"""
    code = (
        "did = {did!r}\n"
        "try:\n"
        "    if did:\n"
        "        df = app.data.findFileById(did)\n"
        "    else:\n"
        "        df = app.activeDocument.dataFile\n"
        "    if df is None:\n"
        "        print('ERROR: no data file (unsaved doc or bad id)')\n"
        "    else:\n"
        "        print('lineage: ' + df.name + '  id=' + df.id)\n"
        "        vers = df.versions\n"
        "        lines = []\n"
        "        for i in range(vers.count):\n"
        "            v = vers.item(i)\n"
        "            try:\n"
        "                vn = v.versionNumber\n"
        "            except Exception:\n"
        "                vn = '?'\n"
        "            try:\n"
        "                import datetime as _dt\n"
        "                dd = _dt.datetime.fromtimestamp(v.dateCreated).strftime('%Y-%m-%d %H:%M')\n"
        "            except Exception:\n"
        "                dd = '?'\n"
        "            lines.append('  v' + str(vn) + '  ' + dd + '  id=' + v.id)\n"
        "        print('versions (' + str(vers.count) + '):')\n"
        "        print('\\n'.join(lines))\n"
        "except Exception as _e:\n"
        "    print('ERROR: ' + str(_e))\n"
    ).format(did=design_id)
    return _send(code, compact=False)


@mcp.tool()
def open_document(design_id: str) -> str:
    """Open a design by its id token (from list_recent_designs) for editing.
    NOTE: this CHANGES the active document - subsequent clear_document /
    save_document act on THIS document. Confirm with get_state() afterward."""
    code = (
        "try:\n"
        "    df = app.data.findFileById({did!r})\n"
        "    if df is None:\n"
        "        print('ERROR: no design with that id')\n"
        "    else:\n"
        "        d = app.documents.open(df)\n"
        "        lin = d.dataFile.id if (d and d.dataFile) else '(?)'\n"
        "        print('opened: ' + df.name + '  lineage=' + str(lin) + '  (now the ACTIVE document)')\n"
        "except Exception as _e:\n"
        "    print('ERROR opening: ' + str(_e))\n"
    ).format(did=design_id)
    return _send(code)


# ==========================================================================
# CURVED-SURFACE FEATURES (wrap / emboss / project)
# ==========================================================================
@mcp.tool()
def wrap_cut(sketch_index: int = -1, depth_mm: float = 1.0,
             face_tempids: str = "", protrude: bool = False,
             component_name: str = "") -> str:
    """Wrap a flat sketch onto a NON-PLANAR face and cut (or emboss) to depth -
    the primitive for patterns/text/texture on curved and revolved bodies. Do
    not hand-roll it; the spline/ruled-surface/loft alternatives are unreliable.

    sketch_index -1 = newest sketch. OMIT face_tempids to auto-target the
    largest curved face (recommended - tempIds die when faces split). Cuts
    per-profile with individual volume checks and reports ok/no-op/failed. A
    no-op profile fell outside the face's angular sector: one flat sketch wraps
    onto only ~70-100 deg, so split large patterns into sectors."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "if host.sketches.count == 0:\n"
        "    print('ERROR: no sketches to wrap')\n"
        "else:\n"
        "    si = {si}\n"
        "    sk = host.sketches.item(host.sketches.count - 1 if si < 0 else si)\n"
        "    ids = {fid!r}\n"
        "    faces = []\n"
        "    if ids.strip():\n"
        "        for b in host.bRepBodies:\n"
        "            for tok in ids.split(','):\n"
        "                tok = tok.strip()\n"
        "                if not tok:\n"
        "                    continue\n"
        "                try:\n"
        "                    f = b.findByTempId(int(tok))\n"
        "                    if f:\n"
        "                        faces.extend(list(f) if hasattr(f,'__iter__') else [f])\n"
        "                except Exception:\n"
        "                    pass\n"
        "    if not faces:\n"
        "        # auto-target: largest NON-planar face (tempIds are unstable)\n"
        "        best = None\n"
        "        for b in host.bRepBodies:\n"
        "            for f in b.faces:\n"
        "                try:\n"
        "                    if 'Plane' in type(f.geometry).__name__:\n"
        "                        continue\n"
        "                    if best is None or f.area > best.area:\n"
        "                        best = f\n"
        "                except Exception:\n"
        "                    pass\n"
        "        if best is not None:\n"
        "            faces = [best]\n"
        "            print('auto-targeted largest curved face: area=%.2f mm^2' % (best.area*100))\n"
        "    if not faces:\n"
        "        print('ERROR: no curved face found to wrap onto')\n"
        "    else:\n"
        "        embosses = host.features.embossFeatures\n"
        "        depth = {d}\n"
        "        if not {pro}:\n"
        "            depth = -depth      # NEGATIVE depth cuts, positive protrudes\n"
        "        ok = 0; noop = 0; failed = 0\n"
        "        n = sk.profiles.count\n"
        "        print('wrapping %d profile(s)...' % n)\n"
        "        for i in range(n):\n"
        "            prof = sk.profiles.item(i)\n"
        "            _b = sum((bd.volume if bd.isValid else 0) for bd in host.bRepBodies)\n"
        "            try:\n"
        "                # createInput takes EXACTLY 3 positional args:\n"
        "                # (profiles_list, faces_list, depth_ValueInput)\n"
        "                ei = embosses.createInput(\n"
        "                    [prof], faces,\n"
        "                    adsk.core.ValueInput.createByReal(depth))\n"
        "                embosses.add(ei)\n"
        "                _a = sum((bd.volume if bd.isValid else 0) for bd in host.bRepBodies)\n"
        "                if abs(_a - _b) < 1e-9:\n"
        "                    noop += 1\n"
        "                else:\n"
        "                    ok += 1\n"
        "            except Exception as _e:\n"
        "                failed += 1\n"
        "                if failed <= 3:\n"
        "                    print('  profile %d failed: %s' % (i, str(_e)[:90]))\n"
        "        print('wrap_cut done: ok=%d no-op=%d failed=%d' % (ok, noop, failed))\n"
        "        if noop:\n"
        "            print('NOTE: no-op profiles almost always fall OUTSIDE the '\n"
        "                  'face angular sector (~70-100 deg per flat sketch). '\n"
        "                  'Split the pattern into sectors and wrap each.')\n"
    ).format(cn=component_name, si=sketch_index, fid=face_tempids,
             d=depth_mm / 10.0, pro=protrude)
    return _send(code)


@mcp.tool()
def project_to_surface(sketch_index: int = -1, face_tempid: int = 0,
                       component_name: str = "") -> str:
    """Project a sketch's curves onto a face, standalone (no cut). Useful to
    check where a pattern will actually land before committing to wrap_cut.
    face_tempid 0 = auto-target the largest curved face."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "sk = host.sketches.item(host.sketches.count - 1 if {si} < 0 else {si})\n"
        "tid = {tid}\n"
        "face = None\n"
        "if tid:\n"
        "    for b in host.bRepBodies:\n"
        "        try:\n"
        "            f = b.findByTempId(tid)\n"
        "            if f:\n"
        "                face = f[0] if hasattr(f,'__iter__') else f\n"
        "                break\n"
        "        except Exception:\n"
        "            pass\n"
        "if face is None:\n"
        "    best = None\n"
        "    for b in host.bRepBodies:\n"
        "        for f in b.faces:\n"
        "            if 'Plane' in type(f.geometry).__name__:\n"
        "                continue\n"
        "            if best is None or f.area > best.area:\n"
        "                best = f\n"
        "    face = best\n"
        "if face is None:\n"
        "    print('ERROR: no target face')\n"
        "else:\n"
        "    cnt = 0\n"
        "    for c in sk.sketchCurves:\n"
        "        try:\n"
        "            sk.projectToSurface([face], c, adsk.fusion.SurfaceProjectTypes.ClosestPointSurfaceProjectType)\n"
        "            cnt += 1\n"
        "        except Exception as _e:\n"
        "            pass\n"
        "    print('projected %d curve(s) onto face (area=%.2f mm^2)' % (cnt, face.area*100))\n"
    ).format(cn=component_name, si=sketch_index, tid=face_tempid)
    return _send(code)


@mcp.tool()
def construction_plane_offset(offset_mm: float, base: str = "xy",
                              component_name: str = "") -> str:
    """Create an offset construction plane from a principal plane (xy/xz/yz).
    Use this INSTEAD of raw ConstructionPlaneInput.setByPlane, which raises
    'Environment is not supported' on this build. Echoes the plane's world
    position so a unit slip is visible immediately (setByOffset takes INTERNAL
    cm - passing mm puts the plane 10x too far and embosses silently no-op)."""
    if base.lower() not in ("xy", "xz", "yz"):
        # used to fall back to XY silently while reporting the requested base
        return "ERROR: base must be 'xy', 'xz' or 'yz' (got {!r})".format(base)
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "base = {b!r}.lower()\n"
        "planes = {{'xy': host.xYConstructionPlane,\n"
        "          'xz': host.xZConstructionPlane,\n"
        "          'yz': host.yZConstructionPlane}}\n"
        "bp = planes[base]\n"
        "ci = host.constructionPlanes.createInput()\n"
        "ci.setByOffset(bp, adsk.core.ValueInput.createByReal({off}))\n"
        "pl = host.constructionPlanes.add(ci)\n"
        "g = pl.geometry\n"
        "print('created offset plane from %s at %.3f mm' % (base, {offmm}))\n"
        "print('  world origin: (%.3f, %.3f, %.3f) mm' % (g.origin.x*10, g.origin.y*10, g.origin.z*10))\n"
        "print('  normal: (%.3f, %.3f, %.3f)' % (g.normal.x, g.normal.y, g.normal.z))\n"
    ).format(cn=component_name, b=base, off=offset_mm / 10.0, offmm=offset_mm)
    return _send(code)


@mcp.tool()
def construction_plane_tangent(angle_deg: float = 0.0, body_index: int = 0,
                               component_name: str = "") -> str:
    """Create a construction plane TANGENT to the largest cylindrical/curved
    face at a given angle around its axis. This is the reliable way to place
    sketches for multi-sector wraps (0/90/180/270 deg) on revolved bodies.
    Echoes world position so unit/orientation slips surface immediately.
    The plane is built from a sketch point, so each call leaves one hidden
    reference sketch named mcp_tangent_ref_<angle>deg - the plane depends on
    it, so do not delete it."""
    code = (
        "import math\n"
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "body = host.bRepBodies.item({bi})\n"
        "best = None\n"
        "for f in body.faces:\n"
        "    if 'Cylinder' in type(f.geometry).__name__:\n"
        "        if best is None or f.area > best.area:\n"
        "            best = f\n"
        "if best is None:\n"
        "    for f in body.faces:\n"
        "        if 'Plane' not in type(f.geometry).__name__:\n"
        "            if best is None or f.area > best.area:\n"
        "                best = f\n"
        "if best is None:\n"
        "    print('ERROR: no curved face found')\n"
        "else:\n"
        "    bb = body.boundingBox\n"
        "    cx = (bb.minPoint.x + bb.maxPoint.x) / 2.0\n"
        "    cy = (bb.minPoint.y + bb.maxPoint.y) / 2.0\n"
        "    cz = (bb.minPoint.z + bb.maxPoint.z) / 2.0\n"
        "    r = max(bb.maxPoint.x - cx, bb.maxPoint.y - cy)\n"
        "    th = math.radians({ang})\n"
        "    px = cx + r * math.cos(th)\n"
        "    py = cy + r * math.sin(th)\n"
        "    sk3 = host.sketches.add(host.xYConstructionPlane)\n"
        "    pt = sk3.sketchPoints.add(adsk.core.Point3D.create(px, py, cz))\n"
        "    # The plane is defined BY this sketch point, so the sketch must stay\n"
        "    # (deleting it breaks the plane). Name and hide it so it reads as a\n"
        "    # deliberate reference rather than stray geometry.\n"
        "    try:\n"
        "        sk3.name = 'mcp_tangent_ref_%gdeg' % {ang}\n"
        "        sk3.isVisible = False\n"
        "    except Exception:\n"
        "        pass\n"
        "    ci = host.constructionPlanes.createInput()\n"
        "    try:\n"
        "        ci.setByTangentAtPoint(best, pt)\n"
        "        pl = host.constructionPlanes.add(ci)\n"
        "        g = pl.geometry\n"
        "        print('tangent plane at %.1f deg' % {ang})\n"
        "        print('  reference sketch: ' + sk3.name + ' (hidden; the plane depends on it)')\n"
        "        print('  world origin: (%.3f, %.3f, %.3f) mm' % (g.origin.x*10, g.origin.y*10, g.origin.z*10))\n"
        "        print('  normal: (%.3f, %.3f, %.3f)' % (g.normal.x, g.normal.y, g.normal.z))\n"
        "    except Exception as _e:\n"
        "        print('ERROR creating tangent plane: ' + str(_e))\n"
    ).format(cn=component_name, bi=body_index, ang=angle_deg)
    return _send(code)


@mcp.tool()
def sketch_axis_map(sketch_index: int = -1, component_name: str = "") -> str:
    """Probe a sketch's local->world axis mapping via sketchToModelSpace and
    report it. Run this BEFORE placing geometry on any offset or tangent plane -
    local axes rarely match world (e.g. XZ-plane sketch Y maps to world -Z, and
    an offset plane from YZ maps local +X to world -Z). Prints where local
    (0,0), (1,0) and (0,1) land in world mm so you can calibrate empirically."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "if host.sketches.count == 0:\n"
        "    print('ERROR: no sketches')\n"
        "else:\n"
        "    si = {si}\n"
        "    sk = host.sketches.item(host.sketches.count - 1 if si < 0 else si)\n"
        "    print('sketch: ' + sk.name)\n"
        "    for label, lx, ly in (('origin (0,0)', 0.0, 0.0),\n"
        "                          ('local +X (1,0)', 1.0, 0.0),\n"
        "                          ('local +Y (0,1)', 0.0, 1.0)):\n"
        "        p = sk.sketchToModelSpace(adsk.core.Point3D.create(lx, ly, 0))\n"
        "        print('  %-16s -> world (%8.3f, %8.3f, %8.3f) mm'\n"
        "              % (label, p.x*10, p.y*10, p.z*10))\n"
        "    o = sk.sketchToModelSpace(adsk.core.Point3D.create(0,0,0))\n"
        "    xv = sk.sketchToModelSpace(adsk.core.Point3D.create(1,0,0))\n"
        "    yv = sk.sketchToModelSpace(adsk.core.Point3D.create(0,1,0))\n"
        "    def axis_of(a, b):\n"
        "        d = (b.x-a.x, b.y-a.y, b.z-a.z)\n"
        "        names = ('X','Y','Z')\n"
        "        i = max(range(3), key=lambda k: abs(d[k]))\n"
        "        return ('+' if d[i] >= 0 else '-') + names[i]\n"
        "    print('  MAPPING: local +X -> world %s ; local +Y -> world %s'\n"
        "          % (axis_of(o, xv), axis_of(o, yv)))\n"
    ).format(cn=component_name, si=sketch_index)
    return _send(code, compact=False)


@mcp.tool()
def cut_prisms(prisms: str, body_index: int = 0, component_name: str = "") -> str:
    """Cut MANY rectangular prisms out of a body in ONE timeline feature using
    TemporaryBRepManager. Far more robust than repeated sketch+extrude cuts: it
    needs no construction planes and no face tempIds (both of which are fragile
    here), and it survives face-splitting because nothing is re-selected.
    Hundreds of cuts complete in one operation.

    prisms: JSON list of cutters, each
      {"x":mm,"y":mm,"z":mm,"w":mm,"d":mm,"h":mm}
    where x/y/z is the CENTER and w/d/h are full sizes along X/Y/Z.
    Example: '[{"x":0,"y":0,"z":5,"w":4,"d":4,"h":20}]'

    Reports volume delta and warns on a no-op.
    """
    import json as _json
    try:
        items = _json.loads(prisms)
        if not isinstance(items, list) or not items:
            return "ERROR: prisms must be a non-empty JSON list"
    except Exception as e:
        return "ERROR: could not parse prisms JSON: {}".format(e)
    # normalise to cm on the server side; pass as a plain numeric literal list
    cutters = []
    for p in items:
        try:
            cutters.append((float(p["x"]) / 10.0, float(p["y"]) / 10.0,
                            float(p["z"]) / 10.0, float(p["w"]) / 10.0,
                            float(p["d"]) / 10.0, float(p["h"]) / 10.0))
        except Exception as e:
            return "ERROR: bad prism entry {!r}: {}".format(p, e)

    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "body = host.bRepBodies.item({bi})\n"
        "_before = body.volume if body.isValid else 0\n"
        "CUTTERS = {cut!r}\n"
        "tbm = adsk.fusion.TemporaryBRepManager.get()\n"
        "tool = None\n"
        "made = 0\n"
        "for (x, y, z, w, d, h) in CUTTERS:\n"
        "    try:\n"
        "        obb = adsk.core.OrientedBoundingBox3D.create(\n"
        "            adsk.core.Point3D.create(x, y, z),\n"
        "            adsk.core.Vector3D.create(1, 0, 0),\n"
        "            adsk.core.Vector3D.create(0, 1, 0),\n"
        "            w, d, h)\n"
        "        blk = tbm.createBox(obb)\n"
        "        if tool is None:\n"
        "            tool = blk\n"
        "        else:\n"
        "            tbm.booleanOperation(tool, blk,\n"
        "                adsk.fusion.BooleanTypes.UnionBooleanType)\n"
        "        made += 1\n"
        "    except Exception as _e:\n"
        "        print('  cutter failed: ' + str(_e)[:80])\n"
        "if tool is None:\n"
        "    print('ERROR: no cutter bodies built')\n"
        "else:\n"
        "    print('built %d cutter prism(s) as one temp tool body' % made)\n"
        "    bf = host.features.baseFeatures.add()\n"
        "    bf.startEdit()\n"
        "    tool_body = host.bRepBodies.add(tool, bf)\n"
        "    _tname = tool_body.name\n"
        "    bf.finishEdit()\n"
        "    # The handle from bRepBodies.add() is STALE after finishEdit():\n"
        "    # reusing it raises ALL_TOOL_BODY_REFERENCE_LOST. Re-fetch by name.\n"
        "    tool_body = None\n"
        "    for _b in host.bRepBodies:\n"
        "        if _b.name == _tname:\n"
        "            tool_body = _b\n"
        "            break\n"
        "    if tool_body is None:\n"
        "        print('ERROR: could not re-fetch tool body %r after finishEdit'\n"
        "              % _tname)\n"
        "        raise RuntimeError('tool body reference lost')\n"
        "    combines = host.features.combineFeatures\n"
        "    tools_col = adsk.core.ObjectCollection.create()\n"
        "    tools_col.add(tool_body)\n"
        "    ci = combines.createInput(body, tools_col)\n"
        "    ci.operation = adsk.fusion.FeatureOperations.CutFeatureOperation\n"
        "    ci.isKeepToolBodies = False\n"
        "    combines.add(ci)\n"
        "    _b2 = host.bRepBodies.item({bi})\n"
        "    _after = _b2.volume if _b2.isValid else 0\n"
        "    _delta = _after - _before\n"
        "    print('cut_prisms done | volume delta: %+.4f cm^3 (%+.1f mm^3)'\n"
        "          % (_delta, _delta * 1000.0))\n"
        "    if abs(_delta) < 1e-9:\n"
        "        print('WARNING: volume delta ~ZERO - cutters may not intersect '\n"
        "              'the body (check coordinates and units).')\n"
    ).format(cn=component_name, bi=body_index, cut=cutters)
    return _send(code)


# ==========================================================================
# FILLET DIAGNOSIS
# ==========================================================================
_ASM_HINTS = {
    "ASM_BL_END_TOO_CMPLX":
        "the blend has no run-out at the edge ends. Try isTangentChain=True, or "
        "include the adjacent edges so the blend has somewhere to terminate.",
    "ASM_ABL_CAP_COMPLEX":
        "the blend cap geometry is too complex where edges meet. Reduce the "
        "radius, or fillet the meeting edges in a separate later feature.",
    "ASM_BL_CAP_COMPLEX":
        "same as ABL_CAP_COMPLEX: the end cap cannot be built. Smaller radius, "
        "or split the edge set.",
    "ASM_ABL_NO_GEOM_EE":
        "no valid blend surface exists for this edge/edge combination - usually "
        "the radius exceeds the available face width. Step the radius down.",
    "ASM_BL_UNFIN_SHEET":
        "the blend left an unfinished sheet - the surrounding faces cannot heal. "
        "Try tangent chaining, or fillet a shorter sub-chain.",
    "ALL_TOOL_BODY_REFERENCE_LOST":
        "a body handle went stale (typical after BaseFeature.finishEdit). "
        "Re-fetch the body from root.bRepBodies by name before reusing it.",
    "ASM_ABL_NO_GEOM_EF":
        "no blend geometry exists for this edge/face combination - the radius "
        "exceeds what the adjacent face can absorb. Step the radius down, or "
        "fillet the edge/face pair in a separate later feature.",
    "ASM_ABL_CVXTY_VIOLATION":
        "convexity changes along the edge (it goes from convex to concave), so "
        "one constant radius cannot work. Split the edge set at the inflection, "
        "or use a variable-radius fillet.",
    "ASM_BL_NO_VTX_GEOM":
        "no valid geometry at a vertex where blends meet. Fillet the edges "
        "separately, or include the whole vertex cluster so the corner can be "
        "built as one patch.",
    "ASM_BL_CHG_PT_COMPLEX_VERT":
        "a radius-change point lands on a complex vertex. Move the change point "
        "away from the vertex, or fillet with a constant radius first.",
    "ASM_BL_CANNOT_REORDER":
        "the blend cannot be reordered into the timeline at this position - it "
        "depends on geometry created later. Move the fillet later in the "
        "timeline, or roll the marker forward before adding it.",
    "ASM_INCONS_VERT_ATT":
        "inconsistent vertex attachment - the edge chain is ambiguous where "
        "several edges meet. Reduce the selection to one unambiguous chain.",
    "ASM_SELF_INTER":
        "the result would self-intersect. For a SWEEP this is usually the PATH, "
        "not the profile: the path's minimum curvature radius is smaller than "
        "the tube radius. Check with sweep_tube_check(), which reports curvature.",
    "FILLET_NO_EDGE_FOUND":
        "the edge selection resolved to nothing - typically stale tempIds after "
        "an operation split faces. Re-query with list_edges / select_edges.",
    "EDGE_REFERENCE_LOST":
        "a feature's edge references no longer resolve after an upstream edit. "
        "If two features each blame the other, they are in a mutual-reference "
        "deadlock - use force_delete_feature(name), which rolls the timeline "
        "marker back before deleting (what the Fusion UI does).",
}


@mcp.tool()
def explain_fillet_error(message: str) -> str:
    """Translate a raw ACIS/ASM fillet error into an actionable cause. Fusion
    surfaces codes like ASM_BL_END_TOO_CMPLX with no guidance; this maps the
    common ones to what actually fixes them."""
    hits = [(k, v) for k, v in _ASM_HINTS.items() if k in (message or "")]
    if not hits:
        return ("No known ASM code found in that message. General fillet ladder: "
                "(1) try isTangentChain=True - a blend with no run-out at its "
                "ends often succeeds immediately once chained; (2) step the "
                "radius down; (3) include adjacent edges; (4) fillet sub-chains "
                "separately. Always verify the volume delta afterwards - a "
                "chained fillet can succeed AND silently propagate.")
    return "\n".join("{}\n  -> {}".format(k, v) for k, v in hits)


@mcp.tool()
def fillet_edges_smart(radius_mm: float, body_index: int = 0,
                       which: str = "all", min_radius_mm: float = 0.2,
                       edge_tempids: str = "", component_name: str = "") -> str:
    """Fillet that searches for a working configuration AND verifies the result.

    Pass `edge_tempids` (from select_edges/list_edges) for real selection;
    `which` (all/vertical/top) is a crude fallback. Tries tangent-chain on/off
    across descending radii, and after each success checks volume (delta vs
    ~0.1*r^2*L) AND topology (faces consumed smaller than 4r^2 - an over-large
    radius eats narrow features while volume still looks fine). Overshoots roll
    back. Zero delta at every radius = the surfaces already meet tangentially
    and need no fillet, reported as a result not a failure.

    component_name: the component that owns the body (empty = root) - pass
    the same one you gave select_edges, or its tempIds will not resolve."""
    if not edge_tempids and which not in ("all", "vertical", "top"):
        return "ERROR: which must be 'all', 'vertical' or 'top' (got {!r})".format(which)
    R0 = radius_mm / 10.0
    RMIN = min_radius_mm / 10.0
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "host = find_component({cn!r})\n"
        "body = host.bRepBodies.item({bi})\n"
        "maxz = body.boundingBox.maxPoint.z\n"
        "IDS = [int(t) for t in {ids!r}.replace(' ','').split(',') if t]\n"
        "def pick_edges():\n"
        "    col = adsk.core.ObjectCollection.create()\n"
        "    L = 0.0\n"
        "    b = host.bRepBodies.item({bi})\n"
        "    if IDS:\n"
        "        for tid in IDS:\n"
        "            try:\n"
        "                e = b.findByTempId(tid)\n"
        "                e = e[0] if hasattr(e, '__iter__') else e\n"
        "                if e is None or e.faces.count < 2 or e.length*10 < 1e-4:\n"
        "                    continue\n"
        "                col.add(e)\n"
        "                L += e.length * 10.0\n"
        "            except Exception:\n"
        "                pass\n"
        "        return col, L\n"
        "    _sel = {w!r}\n"
        "    for e in b.edges:\n"
        "        try:\n"
        "            if e.faces.count < 2 or e.length*10 < 1e-4:\n"
        "                continue\n"
        "            sp = e.startVertex.geometry\n"
        "            ep = e.endVertex.geometry\n"
        "            vert = abs(sp.z - ep.z) > 1e-6\n"
        "            top = (abs(sp.z - maxz) < 1e-6) and (abs(ep.z - maxz) < 1e-6)\n"
        "            take = (_sel=='all') or (_sel=='vertical' and vert) or (_sel=='top' and top)\n"
        "            if take:\n"
        "                col.add(e)\n"
        "                L += e.length * 10.0\n"
        "        except Exception:\n"
        "            pass\n"
        "    return col, L\n"
        "def face_map():\n"
        "    m = {{}}\n"
        "    try:\n"
        "        b = host.bRepBodies.item({bi})\n"
        "        for f in b.faces:\n"
        "            try:\n"
        "                m[f.tempId] = f.area * 100.0\n"
        "            except Exception:\n"
        "                pass\n"
        "    except Exception:\n"
        "        pass\n"
        "    return m\n"
        "r = {R0}\n"
        "rmin = {RMIN}\n"
        "attempt = 0\n"
        "solved = None\n"
        "zero_delta_seen = 0\n"
        "while r >= rmin and solved is None:\n"
        "    for chain in (True, False):\n"
        "        attempt += 1\n"
        "        edges, Lmm = pick_edges()\n"
        "        if edges.count == 0:\n"
        "            print('no edges selected (tempids=%s which=%s)' % (IDS, {w!r}))\n"
        "            break\n"
        "        vol_before = sum((b.volume if b.isValid else 0)\n"
        "                         for b in host.bRepBodies)\n"
        "        faces_before = face_map()\n"
        "        marker = design.timeline.markerPosition\n"
        "        try:\n"
        "            fil = host.features.filletFeatures\n"
        "            inp = fil.createInput()\n"
        "            inp.isRollingBallCorner = True\n"
        "            inp.addConstantRadiusEdgeSet(\n"
        "                edges, adsk.core.ValueInput.createByReal(r), chain)\n"
        "            fil.add(inp)\n"
        "            vol_after = sum((b.volume if b.isValid else 0)\n"
        "                            for b in host.bRepBodies)\n"
        "            delta_mm3 = abs(vol_after - vol_before) * 1000.0\n"
        "            exp_mm3 = 0.1 * ((r*10.0) ** 2) * Lmm\n"
        "            faces_after = face_map()\n"
        "            gone = [(tid, a) for tid, a in faces_before.items()\n"
        "                    if tid not in faces_after]\n"
        "            thresh = 4.0 * (r*10.0) ** 2\n"
        "            eaten = [(tid, a) for tid, a in gone if a < thresh]\n"
        "            vol_sane = not (exp_mm3 > 0 and delta_mm3 > 10.0 * exp_mm3)\n"
        "            topo_sane = (len(eaten) == 0)\n"
        "            print('attempt %d: r=%.2fmm chain=%s -> SUCCESS, delta %.2f mm^3 '\n"
        "                  '(expect <= ~%.2f), %d face(s) removed'\n"
        "                  % (attempt, r*10, chain, delta_mm3, exp_mm3, len(gone)))\n"
        "            if not vol_sane:\n"
        "                print('  *** VOLUME OVERSHOOT %.1fx - blend propagated '\n"
        "                      'beyond the selection ***' % (delta_mm3/exp_mm3))\n"
        "            if not topo_sane:\n"
        "                print('  *** CONSUMED %d small face(s) (areas %s mm^2, all '\n"
        "                      '< 4r^2=%.2f) - radius too big for the feature it '\n"
        "                      'landed on; it rounded that feature AWAY even though '\n"
        "                      'the volume delta looked fine ***'\n"
        "                      % (len(eaten),\n"
        "                         ', '.join('%.3f' % a for _t, a in eaten[:6]), thresh))\n"
        "            if delta_mm3 < 1e-6:\n"
        "                zero_delta_seen += 1\n"
        "                print('  NOTE: ZERO volume change is a RESULT, not a '\n"
        "                      'failure - these surfaces already meet tangentially '\n"
        "                      'and need no fillet.')\n"
        "            if vol_sane and topo_sane:\n"
        "                solved = (r*10, chain, delta_mm3)\n"
        "                break\n"
        "            print('  rolling back')\n"
        "            cur = design.timeline.markerPosition\n"
        "            for i in range(cur - 1, marker - 1, -1):\n"
        "                try:\n"
        "                    design.timeline.item(i).entity.deleteMe()\n"
        "                except Exception:\n"
        "                    pass\n"
        "        except Exception as _e:\n"
        "            print('attempt %d: r=%.2fmm chain=%s -> FAILED: %s'\n"
        "                  % (attempt, r*10, chain, str(_e)[:100]))\n"
        "    if solved is None:\n"
        "        r = r / 2.0\n"
        "if solved:\n"
        "    print('RESOLVED: radius=%.2fmm tangent_chain=%s (volume delta %.2f mm^3)'\n"
        "          % solved)\n"
        "elif zero_delta_seen:\n"
        "    print('ALREADY TANGENT: every attempt produced zero volume change. '\n"
        "          'These surfaces already meet smoothly - no fillet is needed.')\n"
        "else:\n"
        "    print('NO WORKING CONFIGURATION down to %.2fmm.' % ({RMIN}*10))\n"
        "    print('Next: narrow the selection with select_edges (face_pair / z '\n"
        "          'range / min_len), run fillet_preview to spot faces smaller than '\n"
        "          '4r^2, or pass the raw error to explain_fillet_error.')\n"
    ).format(bi=body_index, w=which, R0=R0, RMIN=RMIN, ids=edge_tempids,
             cn=component_name)
    return _send(code)


@mcp.tool()
def select_edges(body_index: int = 0, face_pair: str = "", z_min_mm: float = None,
                 z_max_mm: float = None, min_len_mm: float = None,
                 max_len_mm: float = None, component_name: str = "") -> str:
    """Select edges by PREDICATE and return their tempIds - the selector real
    fillets need. `which=all/vertical/top` cannot express "the edges between
    surface type A and B, within a z range, longer than L", which is the shape
    of essentially every real fillet.

    face_pair: substring match against the adjacent surface-type pair, e.g.
      "Cylinder|Cone", or just "Cone" to match any edge touching a cone.
    z_min_mm / z_max_mm: edge MIDPOINT z range.
    min_len_mm / max_len_mm: edge length range.

    Returns a comma-separated tempId list you can paste straight into
    fillet_edges_smart(edge_tempids=...), plus a per-edge listing. Degenerate
    edges (zero length / <2 faces) are skipped."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "body = host.bRepBodies.item({bi})\n"
        "want_pair = {fp!r}\n"
        "zmin = {zmin!r}\n"
        "zmax = {zmax!r}\n"
        "lmin = {lmin!r}\n"
        "lmax = {lmax!r}\n"
        "hits = []\n"
        "skipped = 0\n"
        "for e in body.edges:\n"
        "    try:\n"
        "        if e.faces.count < 2 or e.length*10 < 1e-4:\n"
        "            skipped += 1\n"
        "            continue\n"
        "        L = e.length * 10.0\n"
        "        ok, sp, ep = e.evaluator.getEndPoints()\n"
        "        mz = (sp.z + ep.z) / 2.0 * 10.0\n"
        "        fa = type(e.faces.item(0).geometry).__name__\n"
        "        fb = type(e.faces.item(1).geometry).__name__\n"
        "        pair = '|'.join(sorted((fa, fb)))\n"
        "        if want_pair and want_pair.lower() not in pair.lower():\n"
        "            continue\n"
        "        if zmin is not None and mz < zmin:\n"
        "            continue\n"
        "        if zmax is not None and mz > zmax:\n"
        "            continue\n"
        "        if lmin is not None and L < lmin:\n"
        "            continue\n"
        "        if lmax is not None and L > lmax:\n"
        "            continue\n"
        "        hits.append((e.tempId, L, mz, pair))\n"
        "    except Exception:\n"
        "        skipped += 1\n"
        "hits.sort(key=lambda r: -r[1])\n"
        "for h in hits:\n"
        "    print('tempId=%d len=%.2fmm mid_z=%.2fmm  %s' % h)\n"
        "print('--')\n"
        "print('matched %d edge(s) (%d degenerate skipped)' % (len(hits), skipped))\n"
        "print('edge_tempids=' + ','.join(str(h[0]) for h in hits))\n"
    ).format(cn=component_name, bi=body_index, fp=face_pair,
             zmin=z_min_mm, zmax=z_max_mm, lmin=min_len_mm, lmax=max_len_mm)
    return _send(code, compact=False)


@mcp.tool()
def fillet_preview(radius_mm: float, edge_tempids: str, body_index: int = 0,
                   component_name: str = "") -> str:
    """Report what a fillet WOULD affect before you commit it: the selected
    edges, their adjacent faces, and - critically - any adjacent face whose area
    is smaller than ~4r^2, which is the signature of a radius too big for the
    feature it lands on. That is the case where a fillet silently CONSUMES a
    small face (e.g. a 1.3 mm rim annulus) and rounds the feature away while the
    volume delta still looks reasonable. component_name: the component that
    owns the body (empty = root), as passed to select_edges."""
    R = radius_mm
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "body = find_component({cn!r}).bRepBodies.item({bi})\n"
        "ids = [int(t) for t in {ids!r}.replace(' ','').split(',') if t]\n"
        "r = {R}\n"
        "thresh = 4.0 * r * r\n"
        "print('fillet_preview: r=%.2fmm, at-risk face area threshold ~%.2f mm^2'\n"
        "      % (r, thresh))\n"
        "risky = 0\n"
        "total_len = 0.0\n"
        "for tid in ids:\n"
        "    try:\n"
        "        e = body.findByTempId(tid)\n"
        "        e = e[0] if hasattr(e, '__iter__') else e\n"
        "        if e is None or e.faces.count < 2:\n"
        "            print('  tempId=%d: MISSING or degenerate' % tid)\n"
        "            continue\n"
        "        L = e.length * 10.0\n"
        "        total_len += L\n"
        "        for k in (0, 1):\n"
        "            f = e.faces.item(k)\n"
        "            a = f.area * 100.0\n"
        "            flag = ''\n"
        "            if a < thresh:\n"
        "                flag = '  *** AT RISK: face area %.2f mm^2 < 4r^2 - this '\n"
        "                flag = (flag + 'fillet may CONSUME it entirely ***') % a\n"
        "                risky += 1\n"
        "            print('  tempId=%d len=%.2fmm face%d %s area=%.3f mm^2%s'\n"
        "                  % (tid, L, k, type(f.geometry).__name__, a, flag))\n"
        "    except Exception as _e:\n"
        "        print('  tempId=%d: error %s' % (tid, str(_e)[:60]))\n"
        "print('--')\n"
        "print('total edge length %.2f mm; expected volume change <= ~%.2f mm^3'\n"
        "      % (total_len, 0.1 * r * r * total_len))\n"
        "if risky:\n"
        "    print('WARNING: %d adjacent face(s) smaller than 4r^2. Reduce the '\n"
        "          'radius or narrow the edge selection before committing.' % risky)\n"
    ).format(bi=body_index, ids=edge_tempids, R=R, cn=component_name)
    return _send(code, compact=False)


@mcp.tool()
def force_delete_feature(feature_name: str) -> str:
    """Delete a timeline feature that refuses to go, including a MUTUAL-REFERENCE
    DEADLOCK where two fillets each report the other as the blocking failure and
    neither suppress nor deleteMe works in either order.

    Does what the Fusion UI does: rolls the timeline marker to just BEFORE the
    feature, deletes it there, then rolls forward and reports what else broke.
    This is the escape hatch for an otherwise-stuck model."""
    code = (
        "design = _require_design()\n"
        "name = {fn!r}\n"
        "tl = design.timeline\n"
        "orig = tl.markerPosition\n"
        "idx = -1\n"
        "for i in range(tl.count):\n"
        "    try:\n"
        "        if tl.item(i).name == name:\n"
        "            idx = i\n"
        "            break\n"
        "    except Exception:\n"
        "        pass\n"
        "if idx < 0:\n"
        "    print('ERROR: no timeline feature named %r' % name)\n"
        "    print('available: ' + ', '.join(\n"
        "        [tl.item(i).name for i in range(tl.count)][:40]))\n"
        "else:\n"
        "    print('found %r at timeline index %d (marker was %d)'\n"
        "          % (name, idx, orig))\n"
        "    try:\n"
        "        tl.markerPosition = idx          # roll to just BEFORE it\n"
        "        progress('rolled marker back to %d' % idx)\n"
        "    except Exception as _e:\n"
        "        print('WARN: could not roll marker: %s' % str(_e)[:70])\n"
        "    deleted = False\n"
        "    try:\n"
        "        ent = tl.item(idx).entity\n"
        "        if ent is not None:\n"
        "            ent.deleteMe()\n"
        "            deleted = True\n"
        "            print('deleted %r' % name)\n"
        "    except Exception as _e:\n"
        "        print('delete failed even rolled back: %s' % str(_e)[:120])\n"
        "    try:\n"
        "        tl.moveToEnd()\n"
        "        progress('rolled marker forward')\n"
        "    except Exception:\n"
        "        pass\n"
        "    broken = []\n"
        "    for i in range(tl.count):\n"
        "        try:\n"
        "            it = tl.item(i)\n"
        "            hs = it.healthState\n"
        "            if hs != 0:\n"
        "                broken.append('%s(health=%s)' % (it.name, hs))\n"
        "        except Exception:\n"
        "            pass\n"
        "    print('--')\n"
        "    print('deleted=%s; %d feature(s) now unhealthy%s'\n"
        "          % (deleted, len(broken),\n"
        "             (': ' + ', '.join(broken[:12])) if broken else ''))\n"
    ).format(fn=feature_name)
    return _send(code)


@mcp.tool()
def sweep_tube_check(path_sketch_index: int = -1, tube_radius_mm: float = 1.0,
                     samples: int = 60, component_name: str = "") -> str:
    """Check a sweep PATH before sweeping. ASM_SELF_INTER on a circular-profile
    sweep is almost always the path, not the profile: if the path's minimum
    curvature radius is smaller than the tube radius, the swept body
    self-intersects. Reports minimum curvature radius and where it occurs, plus
    the path's segment count and total length (a mis-chained path silently
    produces a short sweep)."""
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "si = {si}\n"
        "sk = host.sketches.item(host.sketches.count - 1 if si < 0 else si)\n"
        "R = {R}\n"
        "N = {n}\n"
        "print('path sketch: %s (%d curve(s))' % (sk.name, sk.sketchCurves.count))\n"
        "worst = None\n"
        "total = 0.0\n"
        "_cerr = []\n"
        "for ci in range(sk.sketchCurves.count):\n"
        "    try:\n"
        "        c = sk.sketchCurves.item(ci)\n"
        "        ev = c.geometry.evaluator if hasattr(c.geometry,'evaluator') else c.evaluator\n"
        "        ok, p0, p1 = ev.getParameterExtents()\n"
        "        try:\n"
        "            okl, L = ev.getLengthAtParameter(p0, p1)\n"
        "            total += (L * 10.0) if okl else 0.0\n"
        "        except Exception:\n"
        "            pass\n"
        "        for k in range(N + 1):\n"
        "            t = p0 + (p1 - p0) * k / float(N)\n"
        "            try:\n"
        "                # returns (ok, direction, curvature in 1/cm). It used to be\n"
        "                # unpacked as two values, so EVERY sample raised and the\n"
        "                # tool reported 'could not measure' on any path.\n"
        "                okc, _cdir, curv = ev.getCurvature(t)\n"
        "            except Exception as _ce:\n"
        "                _cerr.append(str(_ce)[:80])\n"
        "                continue\n"
        "            if not okc:\n"
        "                continue\n"
        "            mag = abs(float(curv))\n"
        "            if mag and mag > 1e-9:\n"
        "                rad_mm = (1.0 / mag) * 10.0\n"
        "                if worst is None or rad_mm < worst[0]:\n"
        "                    worst = (rad_mm, ci, t)\n"
        "    except Exception as _e:\n"
        "        print('  curve %d: could not evaluate (%s)' % (ci, str(_e)[:50]))\n"
        "print('total path length ~%.2f mm' % total)\n"
        "if worst is None and _cerr:\n"
        "    print('*** WARNING: curvature could not be evaluated (%d sample error(s), '\n"
        "          'first: %s). The path was NOT checked. ***' % (len(_cerr), _cerr[0]))\n"
        "elif worst is None:\n"
        "    print('no curved segments - straight lines only. A sharp corner between '\n"
        "          'segments acts as zero radius and WILL self-intersect a tube: '\n"
        "          'fillet the corners first.')\n"
        "else:\n"
        "    print('minimum curvature radius %.3f mm (curve %d, parameter %.4f)'\n"
        "          % (worst[0], worst[1], worst[2]))\n"
        "    if worst[0] < R:\n"
        "        print('*** WILL SELF-INTERSECT: path curvature radius %.3f mm < '\n"
        "              'tube radius %.3f mm. Smooth the path near that parameter '\n"
        "              'or reduce the tube radius. ASM_SELF_INTER blames the '\n"
        "              'profile/taper, but the PATH is the cause. ***'\n"
        "              % (worst[0], R))\n"
        "    elif worst[0] < R * 1.5:\n"
        "        print('CAUTION: curvature radius only %.2fx the tube radius - '\n"
        "              'marginal.' % (worst[0] / R))\n"
        "    else:\n"
        "        print('path curvature is comfortable for a %.2f mm tube.' % R)\n"
    ).format(cn=component_name, si=path_sketch_index, R=tube_radius_mm, n=max(10, samples))
    return _send(code, compact=False)


@mcp.tool()
def respline(points_json: str, sketch_index: int = -1, curve_index: int = 0,
             plane: str = "xy", component_name: str = "") -> str:
    """Rebuild a fitted spline with a NEW set of fit points (Fusion can move
    existing fit points but not add one). points_json is a JSON list of [a,b]
    pairs in WORLD mm, mapped onto `plane` (xy/xz/yz) using measured mappings.

    LIMITATION: recreating a curve breaks downstream references - Fusion has no
    true in-place respline. Reports which features became unhealthy."""
    import json as _json
    try:
        pts = _json.loads(points_json)
        assert isinstance(pts, list) and len(pts) >= 2
        pts = [(float(a), float(b)) for a, b in pts]
    except Exception as e:
        return "ERROR: points_json must be a JSON list of >=2 [a,b] pairs: {}".format(e)
    code = (
        "design = _require_design()\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = find_component(cn)   # raises on a miss; never falls back to root\n"
        "si = {si}\n"
        "sk = host.sketches.item(host.sketches.count - 1 if si < 0 else si)\n"
        "PTS = {pts!r}\n"
        "PLANE = {pl!r}.lower()\n"
        "def to_sketch(a, b):\n"
        "    # measured mappings: xz -> (a, -b); yz -> (-b, a); xy -> (a, b)\n"
        "    if PLANE == 'xz':\n"
        "        return adsk.core.Point3D.create(a/10.0, -b/10.0, 0)\n"
        "    if PLANE == 'yz':\n"
        "        return adsk.core.Point3D.create(-b/10.0, a/10.0, 0)\n"
        "    return adsk.core.Point3D.create(a/10.0, b/10.0, 0)\n"
        "ci = {ci}\n"
        "old_n = sk.sketchCurves.sketchFittedSplines.count\n"
        "print('sketch %r has %d fitted spline(s)' % (sk.name, old_n))\n"
        "if ci < old_n:\n"
        "    try:\n"
        "        sk.sketchCurves.sketchFittedSplines.item(ci).deleteMe()\n"
        "        print('deleted old spline index %d' % ci)\n"
        "    except Exception as _e:\n"
        "        print('WARN: could not delete old spline: %s' % str(_e)[:80])\n"
        "else:\n"
        "    print('NOTE: curve_index %d out of range - adding a new spline' % ci)\n"
        "col = adsk.core.ObjectCollection.create()\n"
        "for (a, b) in PTS:\n"
        "    col.add(to_sketch(a, b))\n"
        "spl = sk.sketchCurves.sketchFittedSplines.add(col)\n"
        "print('created spline with %d fit points' % len(PTS))\n"
        "try:\n"
        "    print('  length %.3f mm' % (spl.length * 10.0))\n"
        "except Exception:\n"
        "    pass\n"
        "broken = []\n"
        "try:\n"
        "    tl = design.timeline\n"
        "    for i in range(tl.count):\n"
        "        try:\n"
        "            it = tl.item(i)\n"
        "            if it.healthState != 0:\n"
        "                broken.append(it.name)\n"
        "        except Exception:\n"
        "            pass\n"
        "except Exception:\n"
        "    pass\n"
        "print('--')\n"
        "if broken:\n"
        "    print('%d downstream feature(s) now unhealthy: %s'\n"
        "          % (len(broken), ', '.join(broken[:12])))\n"
        "    print('Recreating a curve breaks references to it. Repair these, or '\n"
        "          'use force_delete_feature() on anything wedged.')\n"
        "else:\n"
        "    print('no downstream features reported unhealthy')\n"
    ).format(cn=component_name, si=sketch_index, ci=curve_index, pts=pts, pl=plane)
    return _send(code, compact=False)


@mcp.tool()
def run_batch(operations_json: str, stop_on_error: bool = True) -> str:
    """Execute SEVERAL tool operations in ONE round trip and ONE Fusion job.

    Every normal tool call costs a full round trip (model -> server -> HTTP ->
    Fusion main thread -> back). Ten holes = ten round trips. This fuses them
    into a single main-thread job, which is dramatically faster and cheaper: one
    request, one response, one timeline transaction boundary.

    operations_json: JSON list of {"tool": name, "args": {...}}, e.g.
      '[{"tool":"create_box","args":{"length_mm":40,"width_mm":30,"height_mm":20}},
        {"tool":"drill_hole","args":{"diameter_mm":5,"x_mm":10,"y_mm":10,"depth_mm":25}},
        {"tool":"fillet_edges","args":{"radius_mm":2,"which":"vertical"}}]'

    Each operation prints its own output, prefixed with its index, so you still
    see per-step volume deltas and warnings. stop_on_error=True (recommended -
    later steps usually depend on earlier ones) stops at the first op that
    raises OR whose output reports a failure (ERROR, WARNING, ***, FAILED,
    NOT CREATED, ...) - most tools report failure by printing, not raising.
    False runs them all and lists which failed or were flagged.

    Use for any sequence of 3+ known operations. Not for steps whose arguments
    depend on the result of an earlier step - those genuinely need round trips."""
    import json as _json
    try:
        ops = _json.loads(operations_json)
        if not isinstance(ops, list) or not ops:
            return "ERROR: operations_json must be a non-empty JSON list"
    except Exception as e:
        return "ERROR: could not parse operations_json: {}".format(e)

    registry = {}
    for _n, _f in list(globals().items()):
        if callable(_f) and getattr(_f, "__module__", None) == __name__ \
                and not _n.startswith("_"):
            registry[_n] = _f

    # Validate EVERYTHING before entering collection mode, so an invalid batch
    # cannot cause any side effect at all.
    for i, op in enumerate(ops):
        nm = op.get("tool")
        if nm not in registry:
            return "ERROR: op {}: unknown tool {!r}".format(i, nm)
        if nm not in BATCHABLE:
            return ("ERROR: op {}: {!r} cannot be batched. Batchable tools are "
                    "geometry/parameter operations only - reporting, async and "
                    "document-level tools (clear_document, save_document, "
                    "view_model, ...) must be called directly so their results "
                    "and safety checks are not swallowed.".format(i, nm))

    fragments = []
    _batch_lock.acquire()
    _batch_buf.clear()
    _batch_mode[0] = True
    try:
        for i, op in enumerate(ops):
            name = op.get("tool")
            args = op.get("args") or {}
            fn = registry.get(name)
            before = len(_batch_buf)
            try:
                res = fn(**args)
            except TypeError as e:
                return "ERROR: op {} ({}): bad arguments - {}".format(i, name, e)
            except Exception as e:
                return "ERROR: op {} ({}): {}".format(i, name, e)
            if len(_batch_buf) == before:
                # the tool validated its arguments and refused before
                # generating any code - pass its own message through
                return ("ERROR: op {} ({}) was rejected before anything ran: {}"
                        .format(i, name, res))
            fragments.append((i, name, _batch_buf[before]))
    finally:
        _batch_mode[0] = False
        _batch_lock.release()

    # Each op runs as its own compiled string. It used to be re-indented into
    # a try: block, which rewrote multi-line string literals inside
    # run_fusion_code ops and shifted traceback line numbers. Each op's printed
    # output is captured so stop_on_error can act on REPORTED failures.
    code = (
        "import sys as _mcp_sys, contextlib as _mcp_cl, traceback as _mcp_tb\n"
        "_MCP_OPS = " + repr(fragments) + "\n"
        "_MCP_STOP = " + repr(bool(stop_on_error)) + "\n"
        "_MCP_MARKERS = " + repr(_BATCH_STOP_MARKERS) + "\n"
        "class _McpOpTee(object):\n"
        "    def __init__(self, inner):\n"
        "        self.inner = inner\n"
        "        self.parts = []\n"
        "    def write(self, s):\n"
        "        self.parts.append(s)\n"
        "        return self.inner.write(s)\n"
        "    def flush(self):\n"
        "        pass\n"
        "_mcp_flagged = []\n"
        "print('=== batch: %d operation(s) ===' % len(_MCP_OPS))\n"
        "for _mcp_i, _mcp_name, _mcp_src in _MCP_OPS:\n"
        "    print('--- [%d] %s ---' % (_mcp_i, _mcp_name))\n"
        "    _mcp_t = _McpOpTee(_mcp_sys.stdout)\n"
        "    try:\n"
        "        with _mcp_cl.redirect_stdout(_mcp_t):\n"
        "            exec(compile(_mcp_src, '<batch op %d: %s>' % (_mcp_i, _mcp_name),\n"
        "                         'exec'), globals())\n"
        "    except Exception as _mcp_be:\n"
        "        print('*** op %d (%s) FAILED: %s' % (_mcp_i, _mcp_name, str(_mcp_be)[:150]))\n"
        "        print(_mcp_tb.format_exc()[-400:])\n"
        "        if _MCP_STOP:\n"
        "            raise\n"
        "        _mcp_flagged.append(_mcp_i)\n"
        "        continue\n"
        "    _mcp_hit = [m for m in _MCP_MARKERS if m in ''.join(_mcp_t.parts)]\n"
        "    if _mcp_hit:\n"
        "        _mcp_flagged.append(_mcp_i)\n"
        "        if _MCP_STOP:\n"
        "            raise RuntimeError('batch STOPPED at op %d (%s): its output reports '\n"
        "                               '%r. Ops after it were NOT run.'\n"
        "                               % (_mcp_i, _mcp_name, _mcp_hit[0]))\n"
        "        continue\n"
        "    progress('op %d (%s) ok' % (_mcp_i, _mcp_name))\n"
        "if _mcp_flagged:\n"
        "    print('=== batch complete: op(s) %s FAILED or flagged ===' % _mcp_flagged)\n"
        "else:\n"
        "    print('=== batch complete: all %d op(s) ok ===' % len(_MCP_OPS))\n"
    )
    return _send(code)


@mcp.tool()
def fusion_help(topic: str = "topics") -> str:
    """On-demand reference, so the detail does not sit in every tool schema on
    every request. Topics: 'helpers', 'planes', 'traps', 'workflow', 'topics'."""
    T = {
"topics": "Topics: helpers | planes | traps | workflow",
"helpers": """Injected into run_fusion_code / run_fusion_code_safe (no import):
UNITS      mm(x) cm(x) to_mm(x) to_mm2(a) to_mm3(v)
POINTS     P_xy(x,y) P_xz(x,z) P_yz(y,z)   <- world mm in, sketch point out
CHECKS     assert_scale(max_mm=2000)  verify_plane_mapping(sketch)
VOLUME     total_volume()  body_count()  capture_state(target_body)
           report_volume_delta(before, label, target_body)
           fillet_volume_sanity(r_mm, edge_len_mm, delta_cm3)
FACES      smallest_faces(body, n=5)  sliver_face_count(body, min_area_mm2=0.05)
EDGES      edge_ok(e)  safe_edges(body)  edge_face_pair(e)
SKETCH     log_sketch_areas(sketch)  path_report(path)
EDIT       delete_face(face)  retire_body(body)  refresh_geometry()
TEMP BREP  tbm()  commit_tool_body(tool_temp, target_body)
MISC       progress(msg)   _require_design()""",
"planes": """Measured sketch-plane mappings on this build:
  XY   sketch(u,v) -> world(u,  v,  0)      helper P_xy(x,y)
  XZ   sketch(u,v) -> world(u,  0, -v)      helper P_xz(x,z)
  YZ   sketch(u,v) -> world(0,  v, -u)      helper P_yz(y,z)
The YZ row is sketch X -> world -Z, NOT world Y. A wrong mapping produces a body
with a correct bounding box and correct profile area, so it stays invisible for
several operations. On an offset/tangent plane call verify_plane_mapping(sketch)
or sketch_axis_map() - local axes rarely match world.""",
"traps": """API traps on this build:
- TimelineObject has no deleteMe; use tl.item(i).entity.deleteMe().
- Bodies from BaseFeature go STALE after finishEdit -> re-fetch by name, or you
  get ALL_TOOL_BODY_REFERENCE_LOST.
- Sketch entity properties return FRESH wrappers: `is` comparison is ALWAYS
  False. Compare .geometry coords. Print path.count after Path.create.
- addByCenterStartSweep normalizes start/end with a negative sweep angle.
- setMidRadii takes Python lists, not ObjectCollection.
- Degenerate edges (0 length, <2 faces) raise 'Bad index parameter' - guard with
  edge_ok(e).
- app.data can be dead all session: open_document/list_recent_designs cannot be
  worked around; save_document falls back to doc.save().
- computeAll() does not guarantee fresh body.volume - use refresh_geometry().""",
"workflow": """Efficient session shape:
1. document_report() once - confirm which document you are in.
2. Batch known sequences with run_batch (N round trips -> 1).
3. view_model at the default 800x600 (~640 tokens); raise only for fine detail.
4. Verify with cross_section / measure_distance rather than extra screenshots.
5. run_async + job_status for anything over ~90s.
6. Replace a body with retire_body(old), not clear_document.""",
    }
    return T.get(topic.strip().lower(), T["topics"])


@mcp.tool()
def set_verbosity(mode: str = "terse") -> str:
    """Control tool output detail. 'terse' (default) hides indented explanatory
    lines on a CLEAN success - any warning, mismatch or failure always prints in
    full. 'full' shows everything. Terse saves tokens on every single call;
    switch to full when diagnosing something subtle."""
    m = (mode or "").strip().lower()
    if m not in ("terse", "full"):
        return "ERROR: mode must be 'terse' or 'full'"
    _terse[0] = (m == "terse")
    return "output verbosity: {} (failures always print in full)".format(m)


if __name__ == "__main__":
    mcp.run(transport="stdio")
