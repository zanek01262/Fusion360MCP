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
SERVER_VERSION = "1.4.1"
EXPECTED_PROTOCOL = 2

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
        parts.append(out.rstrip())
    tb = data.get("traceback", "")
    if tb:
        parts.append("TRACEBACK:\n" + tb.rstrip())
    if status == "timeout":
        parts.append("NOTE: the listener flagged this as TIMEOUT - the operation "
                     "exceeded its budget. It may still be running in Fusion. "
                     "Call job_status('{}') to check, or heartbeat() for "
                     "liveness.".format(jid))
    if status == "running":
        parts.append("NOTE: still running. Poll job_status('{}') for the final "
                     "result; partial stdout shown above.".format(jid))
    return "\n".join(parts)


def _send(code: str) -> str:
    """Run code via the listener's sync path (bounded wait). Returns a structured
    envelope string. If the job outlives SYNC_WAIT_S it stays pollable via
    job_status(); we still return immediately with partial output + a job id."""
    try:
        r = requests.post(LISTENER_URL + "/exec_sync",
                          json={"code": code, "wait_s": SYNC_WAIT_S},
                          timeout=HTTP_TIMEOUT_S)
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
    try:
        r = requests.post(LISTENER_URL + "/exec", json={"code": code}, timeout=10)
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


def _indent(text: str, spaces: int) -> str:
    """Indent a code block so it can be nested inside a try: in the safe wrapper."""
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line
                     for line in text.splitlines())


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
        r = requests.get(LISTENER_URL + "/ping", timeout=5)
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
    """CHEAP, GUARANTEED-FAST document state from the listener cache - active
    document name, lineage id, and body/sketch/component/timeline counts. Does
    NO geometry work and is served WITHOUT the main thread, so it's safe to call
    even when a heavy op is churning or after a suspected hang. Use this first
    for crash recovery. For full per-body geometry detail use get_model_state."""
    try:
        r = requests.get(LISTENER_URL + "/state", timeout=5)
        d = r.json()
        pm = _check_protocol(d)
        if pm:
            return pm
        return ("SUCCESS:\nactive_document: {}\nlineage_id: {}\nbodies: {}  "
                "sketches: {}  components: {}  timeline: {}\nbusy: {}  current_job: {}"
                .format(d.get("active_document"), d.get("lineage_id"),
                        d.get("bodies"), d.get("sketches"), d.get("components"),
                        d.get("timeline_features"), d.get("busy"),
                        d.get("current_job")))
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)


@mcp.tool()
def run_async(code: str) -> str:
    """Start a long-running Fusion operation WITHOUT waiting for it. Returns a
    job_id immediately. Poll job_status(job_id) to get progress (partial stdout)
    and the final result. Use this for operations you expect to exceed ~90s
    (large patterns, heavy booleans, full-assembly rebuilds) so the call never
    blocks. Same injected helpers and units rules as run_fusion_code. Emit
    progress(msg) in your code to mark stages visible while it runs."""
    try:
        r = requests.post(LISTENER_URL + "/exec", json={"code": code}, timeout=10)
        d = r.json()
        jid = d.get("job_id")
        return ("SUCCESS:\nstarted job {} (status: {}). Poll job_status('{}') "
                "for progress and the final result.".format(jid, d.get("status"), jid))
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)


@mcp.tool()
def job_status(job_id: str) -> str:
    """Get the structured result of an async job started with run_async (or of
    any job by id). Returns status (queued/running/ok/error/timeout), elapsed
    time, stdout collected SO FAR (incremental - works mid-run), and traceback
    if it failed. Poll this until status is ok/error/timeout."""
    try:
        r = requests.get(LISTENER_URL + "/job/" + str(job_id), timeout=5)
        if r.status_code == 404:
            return "ERROR: no such job_id {}".format(job_id)
        return _fmt_envelope(r.json())
    except Exception as e:
        return "CONNECTION ERROR: {}".format(e)




@mcp.tool()
def run_fusion_code(code: str) -> str:
    """Execute arbitrary Python inside the running Fusion 360 session.

    `adsk`, `app`, `ui` are in scope. Fusion's internal length unit is
    CENTIMETERS. DO NOT hand-divide by 10 - instead use the injected helper
    mm(x) for every length you pass to the API (e.g. mm(40) -> 4.0 cm). This is
    the single biggest footgun; mm() removes it.

    Injected helpers available in your code (no import needed):
      mm(x), cm(x)               - mm->cm for inputs
      to_mm(x), to_mm2(a), to_mm3(v) - cm->mm for reporting
      assert_scale(max_mm=2000)  - raise if any body bbox is 10x-too-big
      total_volume()             - summed cm^3 of all bodies (capture BEFORE a boolean)
      report_volume_delta(before, label) - print delta and SCREAM if ~zero (no-op)
      sliver_face_count(body)    - count sub-threshold faces (bad-boolean slivers)
      log_sketch_areas(sketch)   - print each profile area in mm^2 (catches scale slips)
      delete_face(face)          - delete one face the way Fusion accepts (DeleteFaceFeatures)

    Recommended pattern for any cut/join:
        _b = total_volume()
        ... do the boolean ...
        report_volume_delta(_b, 'mycut'); assert_scale()

    Anything you print() is returned; tracebacks are returned on error.
    For multi-step or risky work, prefer run_fusion_code_safe.

    SKETCH PLANE / AXIS MAPPING (learn this here, not by crashing):
      - XY plane: sketch X -> world X, sketch Y -> world Y. Extrude +dist goes +Z.
      - XZ plane: sketch X -> world X, sketch Y -> world -Z (INVERTED). A positive
        extrude distance off XZ goes -Y. This sign flip is the usual cause of
        "my extrude went the wrong way" - negate the distance or the sketch Y.
      - YZ plane (MEASURED on this build): sketch X -> world -Z, sketch Y ->
        world +Y, i.e. sketch(u,v) -> world(0, v, -u). Positive extrude -> -X.
        (Older docs claimed 'sketch X -> world Y, sketch Y -> world Z' - that
        is WRONG and produces a plausible body with a correct bounding box and
        correct profile area that only reveals itself several ops later.)
      - On an OFFSET construction plane, the sketch origin is on that plane, not
        at world zero - account for the offset in your coordinates.
      DON'T reason about this by hand - use the injected helpers P_xy(x,y),
      P_xz(x,z), P_yz(y,z), which take WORLD mm and return the correct sketch
      point, and call verify_plane_mapping(sketch) once on any new/offset/
      tangent plane to print its measured basis.

    KNOWN-UNSTABLE (refused by the listener - do not attempt): move-face via
    defineAsFreeMove (hard-crashes the host), defineAsOffset (missing on this
    build). Use parametric edits / offsetFeatures / shell instead.
    """
    return _send(code)


@mcp.tool()
def run_fusion_code_safe(code: str) -> str:
    """Transactional version of run_fusion_code.

    Records the timeline marker first; if the code raises, rolls the timeline
    back and deletes features created after the marker, leaving the document as
    it was. Use for multi-step / risky operations so partial failures don't
    leave orphaned geometry. Fully effective only on parametric documents
    (design history enabled, the default).

    Same injected helpers as run_fusion_code (mm(), assert_scale(),
    total_volume(), report_volume_delta(), sliver_face_count(),
    log_sketch_areas(), delete_face()). Use mm() for inputs; never hand-divide.
    """
    wrapped = (
        "import traceback as _tb\n"
        "design = app.activeProduct\n"
        "_tl = None\n"
        "_marker = 0\n"
        "try:\n"
        "    _tl = design.timeline\n"
        "    _marker = _tl.markerPosition\n"
        "except Exception:\n"
        "    _tl = None\n"
        "try:\n"
        + _indent(code, 4) + "\n"
        "    print('[safe] committed')\n"
        "except Exception:\n"
        "    _msg = _tb.format_exc()\n"
        "    if _tl is not None:\n"
        "        try:\n"
        "            _cur = _tl.markerPosition\n"
        "            for _i in range(_cur - 1, _marker - 1, -1):\n"
        "                if 0 <= _i < _tl.count:\n"
        "                    try:\n"
        "                        _tl.item(_i).deleteMe()\n"
        "                    except Exception:\n"
        "                        pass\n"
        "            print('[safe] ROLLED BACK after error')\n"
        "        except Exception as _re:\n"
        "            print('[safe] rollback partial: ' + str(_re))\n"
        "    raise Exception(_msg)\n"
    )
    return _send(wrapped)


@mcp.tool()
def clear_document(background: bool = True) -> str:
    """Delete all bodies, sketches, and timeline features in the active design.

    Implemented as REVERSE-ORDER timeline deletion via `item(i).entity.deleteMe()`
    (note: TimelineObject itself has NO deleteMe - only .entity does). This runs
    in milliseconds per feature; the old bulk approach could hang for 10+ minutes
    and take the Fusion session down with it.

    background=True (default) runs it through the async job system and returns a
    job_id immediately, so the channel is never blocked - poll job_status().
    Emits progress() per feature.

    SAFER ALTERNATIVE: to replace one body, don't clear at all - build the new
    body, then retire the old one with retire_body(old) (backed by
    removeFeatures). That keeps history and cannot destroy unrelated work.
    Check get_state() for the active document before clearing."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
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
    return _send(code)


@mcp.tool()
def set_parameter(name: str, value_mm: float) -> str:
    """Set (or create) a user parameter by name, in millimeters. Driving a
    model with named parameters lets the dimensions be tweaked later without
    rebuilding. Build parametric-first whenever a value might change."""
    code = (
        "design = app.activeProduct\n"
        "ups = design.userParameters\n"
        "expr = '{v} mm'\n"
        "p = ups.itemByName({n!r})\n"
        "if p:\n"
        "    p.expression = expr\n"
        "    print('updated ' + str({n!r}) + ' = ' + expr)\n"
        "else:\n"
        "    val = adsk.core.ValueInput.createByString(expr)\n"
        "    ups.add({n!r}, val, 'mm', '')\n"
        "    print('created ' + str({n!r}) + ' = ' + expr)\n"
    ).format(v=value_mm, n=name)
    return _send(code)


@mcp.tool()
def list_parameters() -> str:
    """List all user parameters and their current expressions."""
    code = (
        "design = app.activeProduct\n"
        "ups = design.userParameters\n"
        "lines = []\n"
        "for i in range(ups.count):\n"
        "    p = ups.item(i)\n"
        "    lines.append(p.name + ' = ' + p.expression)\n"
        "print('\\n'.join(lines) if lines else 'no user parameters')\n"
    )
    return _send(code)


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
        "design = app.activeProduct\n"
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
        "design = app.activeProduct\n"
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


@mcp.tool()
def drill_hole(diameter_mm: float, x_mm: float, y_mm: float,
               depth_mm: float, body_index: int = 0) -> str:
    """Cut a circular hole straight down (-Z) from the XY plane at (x, y) to the
    given depth. For holes on other faces use sketch_on_face + hole_on_face."""
    R, D = diameter_mm / 20.0, depth_mm / 10.0
    x, y = x_mm / 10.0, y_mm / 10.0
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "_before = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "sk = root.sketches.add(root.xYConstructionPlane)\n"
        "ctr = adsk.core.Point3D.create({x}, {y}, 0)\n"
        "sk.sketchCurves.sketchCircles.addByCenterRadius(ctr, {R})\n"
        "prof = sk.profiles.item(0)\n"
        "ext = root.features.extrudeFeatures\n"
        "inp = ext.createInput(prof, adsk.fusion.FeatureOperations.CutFeatureOperation)\n"
        "inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-{D}))\n"
        "ext.add(inp)\n"
        "_after = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "_delta = _after - _before\n"
        "print('hole cut at ({xm}, {ym}) mm | volume delta: {{:+.4f}} cm^3'.format(_delta))\n"
        "if abs(_delta) < 1e-7:\n"
        "    print('WARNING: volume delta ~ZERO - the cut had NO effect (missed the body or wrong depth/location).')\n"
    ).format(x=x, y=y, R=R, D=D, xm=x_mm, ym=y_mm)
    return _send(code)


@mcp.tool()
def fillet_edges(radius_mm: float, body_index: int = 0, which: str = "all") -> str:
    """Round edges of a body. which='all' fillets every edge; 'vertical' only
    edges with vertical run; 'top' only edges lying on the top face plane."""
    R = radius_mm / 10.0
    code = (
        "design = app.activeProduct\n"
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
        "    print('WARN: no edges matched which={w}')\n"
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
        "design = app.activeProduct\n"
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
        "        if f.area < 0.0001:\n"
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


@mcp.tool()
def tapped_hole(thread_size: str, x_mm: float, y_mm: float, depth_mm: float,
                body_index: int = 0, tap_drill_only: bool = True) -> str:
    """Create a tapped hole on the top (XY) plane, cutting downward.

    thread_size: standard designation, e.g. 'M3', 'M3x0.5', 'M4', 'M5', 'M6',
    '1/4-20', '10-32'. tap_drill_only=True (default) cuts only the correctly
    sized tap-drill clearance hole for you to thread with a real tap (best for
    3D-printed and hand-tapped parts). Set False to also apply a MODELED thread
    (visualization / CNC); modeled threads are best-effort and may be skipped
    with a warning if Fusion can't match the designation.
    """
    tap_drills_mm = {
        "M1.6": 1.25, "M2": 1.6, "M2.5": 2.05, "M3": 2.5, "M3.5": 2.9,
        "M4": 3.3, "M5": 4.2, "M6": 5.0, "M8": 6.8, "M10": 8.5, "M12": 10.2,
        "#4-40": 2.26, "#6-32": 2.7, "#8-32": 3.4, "#10-24": 3.9, "#10-32": 4.1,
        "10-24": 3.9, "10-32": 4.1, "1/4-20": 5.1, "1/4-28": 5.4,
    }
    key = thread_size.split("x")[0].strip()
    drill_mm = tap_drills_mm.get(key, tap_drills_mm.get(thread_size, 2.5))
    drill = drill_mm / 20.0  # radius in cm
    D = depth_mm / 10.0
    x, y = x_mm / 10.0, y_mm / 10.0
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "_ts = {ts!r}\n"
        "_before = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "sk = root.sketches.add(root.xYConstructionPlane)\n"
        "ctr = adsk.core.Point3D.create({x}, {y}, 0)\n"
        "sk.sketchCurves.sketchCircles.addByCenterRadius(ctr, {R})\n"
        "prof = sk.profiles.item(0)\n"
        "ext = root.features.extrudeFeatures\n"
        "inp = ext.createInput(prof, adsk.fusion.FeatureOperations.CutFeatureOperation)\n"
        "inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-{D}))\n"
        "hole = ext.add(inp)\n"
        "_after = sum((b.volume if b.isValid else 0) for b in root.bRepBodies)\n"
        "_delta = _after - _before\n"
        "print('tap-drill hole (' + str(_ts) + ', dia {dmm}mm) cut at ({xm},{ym})mm | volume delta: ' + '{{:+.4f}}'.format(_delta) + ' cm^3')\n"
        "if abs(_delta) < 1e-7:\n"
        "    print('WARNING: volume delta ~ZERO - tap-drill cut had NO effect (missed body / wrong location / depth).')\n"
        "if not {tdo}:\n"
        "    try:\n"
        "        threads = root.features.threadFeatures\n"
        "        q = threads.threadDataQuery\n"
        "        tclass = q.defaultMetricThreadType\n"
        "        ok, sizes = q.getAllSizes(tclass)\n"
        "        size = None\n"
        "        for s in (sizes or []):\n"
        "            if s.replace(' ', '') == '{szkey}'.replace('M',''):\n"
        "                size = s\n"
        "                break\n"
        "        if size is None and sizes:\n"
        "            size = sizes[0]\n"
        "        ok2, desigs = q.getAllDesignations(tclass, size)\n"
        "        desig = desigs[0] if (ok2 and desigs) else None\n"
        "        side = None\n"
        "        for f in hole.sideFaces:\n"
        "            side = f\n"
        "            break\n"
        "        if side and desig:\n"
        "            tinfo = threads.createThreadInfo(True, tclass, size, desig)\n"
        "            fc = adsk.core.ObjectCollection.create()\n"
        "            fc.add(side)\n"
        "            tin = threads.createInput(fc, tinfo)\n"
        "            tin.isModeled = True\n"
        "            threads.add(tin)\n"
        "            print('modeled thread applied: ' + str(desig))\n"
        "        else:\n"
        "            print('WARN: modeled thread skipped (no face/designation match)')\n"
        "    except Exception as _te:\n"
        "        print('WARN: modeled thread skipped: ' + str(_te))\n"
    ).format(bi=body_index, x=x, y=y, R=drill, D=D, ts=thread_size,
             dmm=drill_mm, xm=x_mm, ym=y_mm, tdo=tap_drill_only,
             szkey=key.replace('M', ''))
    return _send(code)


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
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "host = root\n"
        "cn = {cn!r}\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
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
        "design = app.activeProduct\n"
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
        "design = app.activeProduct\n"
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
        "        lineage = '(none)'\n"
        "        saved = 'UNSAVED'\n"
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
        "    for f in b.faces:\n"
        "        try:\n"
        "            if f.area < 0.0001:\n"
        "                slivers += 1\n"
        "        except Exception:\n"
        "            pass\n"
        "    lines.append('{}: valid={} vol={:.3f}cm^3 bbox={:.1f}x{:.1f}x{:.1f}mm faces={} edges={} slivers={}'.format(\n"
        "        b.name, valid, b.volume, dx, dy, dz, b.faces.count, b.edges.count, slivers))\n"
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
    return _send(code)


@mcp.tool()
def measure_distance(which_a: str, which_b: str, body_index: int = 0) -> str:
    """Measure true distance (mm) between two reference faces of a body, each
    selected by orientation: top/bottom/front/back/left/right. Centroid to
    centroid. More trustworthy than the bounding box for verifying features."""
    code = (
        "import math\n"
        "design = app.activeProduct\n"
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
    return _send(code)


@mcp.tool()
def cross_section(body_index: int = 0, axis: str = "z", at_mm: float = None,
                  span_axis: str = "x", samples: int = 41,
                  at_a_mm: float = None, at_b_mm: float = None) -> str:
    """Scan a line of points through a body and report solid/void as an ASCII
    row - the single most useful geometry diagnostic for spotting trapped voids,
    missed cuts, or wrong wall thickness without a screenshot.

    Casts a ray of `samples` points across the body's bbox along `span_axis`,
    held at the mid-value of the other two axes (or at `at_mm` along `axis` if
    given). '#'=solid, '.'=void. Also prints the mm coordinates where solid/void
    boundaries occur, so you can read off wall positions and gaps numerically.

    By default the two non-span axes are held at their bbox MID-value. Pin them
    explicitly with at_a_mm / at_b_mm (in the axis order X,Y,Z excluding
    span_axis) to scan through a specific feature - e.g. a vent tube at x=3.8:
    cross_section(span_axis='y', at_a_mm=3.8) scans along Y at x=3.8.

    Example: cross_section(axis='z', at_mm=5, span_axis='x') samples across X at
    Z=5mm and shows where material starts/stops along X."""
    at_expr = "None" if at_mm is None else str(at_mm / 10.0)
    code = (
        "design = app.activeProduct\n"
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
        "row = []\n"
        "states = []\n"
        "for i in range(N):\n"
        "    t = lo + (hi-lo)*i/(N-1)\n"
        "    coord = {{span:t}}\n"
        "    coord.update(fixed)\n"
        "    pt = adsk.core.Point3D.create(coord['x'],coord['y'],coord['z'])\n"
        "    cont = body.pointContainment(pt)\n"
        "    solid = (cont == adsk.fusion.PointContainment.PointInsidePointContainment)\n"
        "    row.append('#' if solid else '.')\n"
        "    states.append((t, solid))\n"
        "print('cross-section of ' + body.name + ' along ' + span + ' (#=solid .=void):')\n"
        "print(''.join(row))\n"
        "print('span ' + span + ': {{:.1f}}mm .. {{:.1f}}mm at '.format(lo*10,hi*10) + \n"
        "      ', '.join(a+'={{:.1f}}mm'.format(fixed[a]*10) for a in others))\n"
        "# boundaries\n"
        "bounds = []\n"
        "for i in range(1,len(states)):\n"
        "    if states[i][1] != states[i-1][1]:\n"
        "        edge_mm = (states[i][0]+states[i-1][0])/2.0*10\n"
        "        kind = 'void->solid' if states[i][1] else 'solid->void'\n"
        "        bounds.append('{{:.2f}}mm {{}}'.format(edge_mm, kind))\n"
        "print('boundaries: ' + ('; '.join(bounds) if bounds else 'none (uniform)'))\n"
    ).format(bi=body_index, ax=axis, sp=span_axis, at=at_expr, n=max(5, samples),
             pins=[at_a_mm, at_b_mm])
    return _send(code)


@mcp.tool()
def list_faces(body_index: int = 0, max_faces: int = 40) -> str:
    """List a body's faces with a stable tempId, centroid (mm), area (mm^2), and
    surface type. The tempId lets you re-select the same face on a later call
    (pass it to face-targeted raw code via body.findByTempId). Essential for
    multi-step face work; sorted largest-area first."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item(%d)\n"
        "rows = []\n"
        "for f in body.faces:\n"
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
        "print('faces of ' + body.name + ' (' + str(body.faces.count) + ' total):')\n"
        "print('\\n'.join(lines))\n"
    ) % (body_index, max_faces)
    return _send(code)


@mcp.tool()
def list_edges(body_index: int = 0, max_edges: int = 40) -> str:
    """List a body's edges with a stable tempId, length (mm), and midpoint (mm).
    The tempId lets you re-select an edge later via body.findByTempId. Sorted
    longest first. Use to target specific edges for fillets/chamfers in raw code."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item(%d)\n"
        "rows = []\n"
        "for e in body.edges:\n"
        "    try:\n"
        "        ev = e.evaluator\n"
        "        ok, sp, ep = ev.getEndPoints()\n"
        "        mid = ((sp.x+ep.x)/2*10, (sp.y+ep.y)/2*10, (sp.z+ep.z)/2*10)\n"
        "        L = e.length*10\n"
        "        rows.append((L, e.tempId, mid))\n"
        "    except Exception:\n"
        "        pass\n"
        "rows.sort(key=lambda r: -r[0])\n"
        "lines = []\n"
        "for r in rows[:%d]:\n"
        "    lines.append('tempId={} len={:.2f}mm mid=({:.1f},{:.1f},{:.1f})mm'.format(\n"
        "        r[1], r[0], r[2][0], r[2][1], r[2][2]))\n"
        "print('edges of ' + body.name + ' (' + str(body.edges.count) + ' total):')\n"
        "print('\\n'.join(lines))\n"
    ) % (body_index, max_edges)
    return _send(code)
    return _send(code)


@mcp.tool()
def heartbeat() -> str:
    """Crash-vs-busy check with explicit recovery guidance. Reads the listener's
    /ping channel (separate from geometry execution, so it answers even during a
    hung op). If it responds: Fusion is alive - check busy/current_job to see if
    an op is still running. If it errors/times out: Fusion likely crashed -
    restart the MCPListener add-in. (ping_fusion returns the same liveness data;
    this adds the recovery instructions.)"""
    try:
        r = requests.get(LISTENER_URL + "/ping", timeout=5)
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
               zoom_z_mm: float = None, zoom_radius_mm: float = None) -> Image:
    """Orient the camera, fit, screenshot the viewport, and RETURN the image.

    orientation: iso / top / bottom / front / back / left / right. An
    unrecognised value is an ERROR (it used to silently return the previous
    view, which reads as "nothing changed" and wastes a round trip).

    ZOOM: pass zoom_x_mm/zoom_y_mm/zoom_z_mm plus zoom_radius_mm to frame a
    small detail instead of fitting the whole model. Inspecting a 4 mm feature
    on a 76 mm part is impossible with a whole-model fit.
    """
    tmp = os.path.join(os.path.expanduser("~"), "fusion_view.png")
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
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
        "    res = vp.saveAsImageFile({path!r}, 1200, 900)\n"
        "    print('screenshot ok' if res else 'screenshot FAILED')\n"
    ).format(o=orientation, z=zoom, path=tmp)
    result = _send(code)
    if "unknown orientation" in result:
        raise RuntimeError(result)
    if "screenshot ok" not in result or not os.path.exists(tmp):
        raise RuntimeError("view_model failed: " + result)
    with open(tmp, "rb") as f:
        data = f.read()
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
    origin. Returns its fullPathName. One component per distinct part."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "m = adsk.core.Matrix3D.create()\n"
        "occ = root.occurrences.addNewComponent(m)\n"
        "occ.component.name = {n!r}\n"
        "print('created component occurrence: ' + occ.fullPathName)\n"
    ).format(n=name)
    return _send(code)


@mcp.tool()
def list_components() -> str:
    """List all occurrences with component name and origin position (mm)."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "lines = []\n"
        "for occ in root.allOccurrences:\n"
        "    t = occ.transform.translation\n"
        "    lines.append(occ.fullPathName + '  [' + occ.component.name + ']  pos=(' +\n"
        "        '{:.1f},{:.1f},{:.1f}'.format(t.x*10,t.y*10,t.z*10) + ')mm')\n"
        "print('\\n'.join(lines) if lines else 'no components (geometry is in root)')\n"
    )
    return _send(code)


@mcp.tool()
def box_in_component(component_name: str, length_mm: float, width_mm: float,
                     height_mm: float, ox_mm: float = 0, oy_mm: float = 0,
                     oz_mm: float = 0) -> str:
    """Create a box INSIDE a named component (on its own XY plane at the given
    local origin). This is how an assembly part gets its geometry."""
    L, W, H = length_mm / 10.0, width_mm / 10.0, height_mm / 10.0
    ox, oy, oz = ox_mm / 10.0, oy_mm / 10.0, oz_mm / 10.0
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "comp = None\n"
        "for occ in root.allOccurrences:\n"
        "    if occ.component.name == {cn!r}:\n"
        "        comp = occ.component\n"
        "        break\n"
        "if comp is None:\n"
        "    print('ERROR: no component named ' + str({cn!r}))\n"
        "else:\n"
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
    """Set an occurrence's position (origin translated to x,y,z in mm). Position
    parts BEFORE jointing; joints override transforms afterward."""
    x, y, z = x_mm / 10.0, y_mm / 10.0, z_mm / 10.0
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "target = None\n"
        "for occ in root.allOccurrences:\n"
        "    if occ.component.name == {cn!r}:\n"
        "        target = occ\n"
        "        break\n"
        "if target is None:\n"
        "    print('ERROR: no component named ' + str({cn!r}))\n"
        "else:\n"
        "    m = adsk.core.Matrix3D.create()\n"
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
    pattern, each offset (dx,dy,dz) mm from the previous. Reuses one definition
    (edit once, all update) - ideal for repeated hardware."""
    dx, dy, dz = dx_mm / 10.0, dy_mm / 10.0, dz_mm / 10.0
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "src = None\n"
        "for occ in root.allOccurrences:\n"
        "    if occ.component.name == {cn!r}:\n"
        "        src = occ\n"
        "        break\n"
        "if src is None:\n"
        "    print('ERROR: no component named ' + str({cn!r}))\n"
        "else:\n"
        "    base = src.transform.translation\n"
        "    made = 0\n"
        "    for i in range(1, {cnt}+1):\n"
        "        m = adsk.core.Matrix3D.create()\n"
        "        m.translation = adsk.core.Vector3D.create(base.x+{dx}*i, base.y+{dy}*i, base.z+{dz}*i)\n"
        "        root.occurrences.addExistingComponent(src.component, m)\n"
        "        made += 1\n"
        "    print('placed ' + str(made) + ' more occurrences of ' + str({cn!r}))\n"
    ).format(cn=component_name, cnt=count, dx=dx, dy=dy, dz=dz)
    return _send(code)


@mcp.tool()
def ground_component(component_name: str, grounded: bool = True) -> str:
    """Ground (lock in space) or unground a component. An assembly needs exactly
    one grounded base so other parts joint relative to it. Ground base first."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "target = None\n"
        "for occ in root.allOccurrences:\n"
        "    if occ.component.name == {cn!r}:\n"
        "        target = occ\n"
        "        break\n"
        "if target is None:\n"
        "    print('ERROR: no component named ' + str({cn!r}))\n"
        "else:\n"
        "    target.isGrounded = {g}\n"
        "    print(str({cn!r}) + ' grounded={g}')\n"
    ).format(cn=component_name, g=grounded)
    return _send(code)


# ==========================================================================
# JOINTS
# ==========================================================================
@mcp.tool()
def rigid_joint(component_a: str, component_b: str) -> str:
    """Create a RIGID joint locking two components together at their current
    relative position, referencing each component's origin point."""
    code = _joint_code(component_a, component_b, "rigid", "z",
                       use_origin=True, ax=0, ay=0, az=0, bx=0, by=0, bz=0)
    return _send(code)


@mcp.tool()
def revolute_joint(component_a: str, component_b: str, axis: str = "z") -> str:
    """Create a REVOLUTE joint (one rotational DOF) about axis (x/y/z) at the
    components' origins. For hinges, lids, swinging arms."""
    code = _joint_code(component_a, component_b, "revolute", axis,
                       use_origin=True, ax=0, ay=0, az=0, bx=0, by=0, bz=0)
    return _send(code)


@mcp.tool()
def joint_at_point(component_a: str, ax_mm: float, ay_mm: float, az_mm: float,
                   component_b: str, bx_mm: float, by_mm: float, bz_mm: float,
                   motion: str = "rigid", axis: str = "z") -> str:
    """Joint two components at explicit LOCAL points (each component's own
    coordinate space, mm) rather than their origins. Creates a construction
    point on each, then joins. motion: 'rigid' or 'revolute'; axis for revolute."""
    code = _joint_code(component_a, component_b, motion, axis, use_origin=False,
                       ax=ax_mm / 10.0, ay=ay_mm / 10.0, az=az_mm / 10.0,
                       bx=bx_mm / 10.0, by=by_mm / 10.0, bz=bz_mm / 10.0)
    return _send(code)


@mcp.tool()
def list_joints() -> str:
    """List all joints with their motion type."""
    code = (
        "design = app.activeProduct\n"
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
    return _send(code)


def _joint_code(ca, cb, motion, axis, use_origin, ax, ay, az, bx, by, bz):
    """Shared joint builder. Not a tool (no decorator) - internal helper."""
    amap = {"x": "XAxisJointDirection", "y": "YAxisJointDirection",
            "z": "ZAxisJointDirection"}
    axdir = amap.get(axis, "ZAxisJointDirection")
    if use_origin:
        geo_setup = (
            "    ga = adsk.fusion.JointGeometry.createByPoint(\n"
            "        a.component.originConstructionPoint.createForAssemblyContext(a))\n"
            "    gb = adsk.fusion.JointGeometry.createByPoint(\n"
            "        b.component.originConstructionPoint.createForAssemblyContext(b))\n"
        )
    else:
        geo_setup = (
            "    def addpt(comp, x, y, z):\n"
            "        ip = comp.constructionPoints.createInput()\n"
            "        ip.setByPoint(adsk.core.Point3D.create(x, y, z))\n"
            "        return comp.constructionPoints.add(ip)\n"
            "    pa = addpt(a.component, {ax}, {ay}, {az})\n"
            "    pb = addpt(b.component, {bx}, {by}, {bz})\n"
            "    ga = adsk.fusion.JointGeometry.createByPoint(pa.createForAssemblyContext(a))\n"
            "    gb = adsk.fusion.JointGeometry.createByPoint(pb.createForAssemblyContext(b))\n"
        ).format(ax=ax, ay=ay, az=az, bx=bx, by=by, bz=bz)

    motion_setup = (
        "    ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.{ad})\n".format(ad=axdir)
        if motion == "revolute" else
        "    ji.setAsRigidJointMotion()\n"
    )

    return (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "def find(name):\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == name:\n"
        "            return occ\n"
        "    return None\n"
        "a = find({ca!r})\n"
        "b = find({cb!r})\n"
        "if not a or not b:\n"
        "    print('ERROR: component not found (a=' + str({ca!r}) + ' b=' + str({cb!r}) + ')')\n"
        "else:\n"
        + geo_setup +
        "    ji = root.joints.createInput(ga, gb)\n"
        + motion_setup +
        "    root.joints.add(ji)\n"
        "    print('{motion} joint created: ' + str({ca!r}) + ' <-> ' + str({cb!r}))\n"
    ).format(ca=ca, cb=cb, motion=motion)


# ==========================================================================
# EXPORT
# ==========================================================================
@mcp.tool()
def export_step(filepath: str) -> str:
    """Export the active design to a STEP file at an absolute path (.step)."""
    code = (
        "design = app.activeProduct\n"
        "em = design.exportManager\n"
        "opts = em.createSTEPExportOptions({fp!r})\n"
        "em.execute(opts)\n"
        "print('exported STEP to ' + str({fp!r}))\n"
    ).format(fp=filepath)
    return _send(code)


@mcp.tool()
def export_stl(filepath: str, body_index: int = 0, quality: str = "medium") -> str:
    """Export a body to STL for 3D printing. quality: low/medium/high."""
    # Actual members on this build are MeshRefinementLow/Medium/High/Custom.
    # The *QualityTriangleMeshRefinement names do NOT exist and raise
    # AttributeError at every quality setting.
    qmap = {"low": "MeshRefinementLow",
            "medium": "MeshRefinementMedium",
            "high": "MeshRefinementHigh"}
    ref = qmap.get(quality, qmap["medium"])
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "em = design.exportManager\n"
        "opts = em.createSTLExportOptions(body, {fp!r})\n"
        "try:\n"
        "    opts.meshRefinement = getattr(adsk.fusion.MeshRefinementSettings, {ref!r})\n"
        "except AttributeError:\n"
        "    _avail = [a for a in dir(adsk.fusion.MeshRefinementSettings) if 'Refinement' in a]\n"
        "    print('WARN: {ref} missing; available: ' + str(_avail))\n"
        "em.execute(opts)\n"
        "print('exported STL to ' + str({fp!r}))\n"
    ).format(bi=body_index, fp=filepath, ref=ref)
    return _send(code)


@mcp.tool()
def export_dxf(filepath: str, sketch_index: int = 0,
               component_name: str = "") -> str:
    """Export a sketch's profile to DXF (flat-stock / laser / waterjet). Selects
    sketch by index within the root (or a named component). filepath absolute (.dxf)."""
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "host = root\n"
        "cn = {cn!r}\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
        "if host.sketches.count == 0:\n"
        "    print('ERROR: no sketches to export')\n"
        "else:\n"
        "    sk = host.sketches.item({si})\n"
        "    sk.saveAsDXF({fp!r})\n"
        "    print('exported DXF to ' + str({fp!r}))\n"
    ).format(cn=component_name, si=sketch_index, fp=filepath)
    return _send(code)


# ==========================================================================
# DOCUMENT PERSISTENCE
# ==========================================================================
@mcp.tool()
def save_document(name: str = "", create_new: bool = False) -> str:
    """Save the active document - and ALWAYS report WHICH lineage was written, so
    a wrong-document save is immediately visible instead of looking identical to
    a correct one.

    Behavior:
    - create_new=True: ALWAYS save-as a brand-new document named `name` (a fresh
      lineage). Use this for a NEW part so it never lands in an unrelated design's
      version history. `name` required.
    - create_new=False (default): if the doc is unsaved, save-as `name` (new
      lineage); if already saved, save a NEW VERSION of the CURRENT lineage. The
      result prints the document name + lineage id it versioned - check it
      matches the part you intended before trusting the save.

    WARNING: with create_new=False on an already-saved doc, you version whatever
    lineage is currently open. If you opened part A and meant to save new part B,
    that buries B's content in A's history. When in doubt, use create_new=True or
    check get_state()/get_model_state() for the active lineage first. Requires
    being signed in with an active project."""
    code = (
        "doc = app.activeDocument\n"
        "name = {n!r}\n"
        "create_new = {cn}\n"
        "try:\n"
        "    proj = None\n"
        "    folder = None\n"
        "    _data_ok = True\n"
        "    try:\n"
        "        proj = app.data.activeProject\n"
        "        folder = proj.rootFolder\n"
        "    except Exception as _de:\n"
        "        _data_ok = False\n"
        "        print('NOTE: app.data is unavailable (%s).' % str(_de)[:70])\n"
        "        print('      This build can throw InternalValidationError '\n"
        "              'on all app.data access; only saveAs needs it.')\n"
        "    if (not _data_ok) and doc.isSaved:\n"
        "        # Fall back to the path that DOES work: version the open doc.\n"
        "        doc.save('saved via MCP (app.data unavailable - fallback)')\n"
        "        print('FALLBACK: saved a new VERSION of the already-saved '\n"
        "              'document via doc.save().')\n"
        "        print('  document: ' + doc.name)\n"
        "        print('  NOTE: could not create a new lineage - saveAs needs '\n"
        "              'app.data. To start a separate design, create the new '\n"
        "              'document in the Fusion UI first.')\n"
        "    elif not _data_ok:\n"
        "        print('ERROR: document is unsaved AND app.data is unavailable, '\n"
        "              'so no lineage can be created. Save once manually in the '\n"
        "              'Fusion UI, then this tool can version it.')\n"
        "    elif create_new:\n"
        "        if not name:\n"
        "            print('ERROR: create_new=True requires a name')\n"
        "        else:\n"
        "            doc.saveAs(name, folder, 'created via MCP (new lineage)', '')\n"
        "            lin = doc.dataFile.id if doc.dataFile else '(?)'\n"
        "            print('saved as NEW document: ' + name + '  lineage=' + str(lin))\n"
        "    elif doc.isSaved:\n"
        "        prev_lin = doc.dataFile.id if doc.dataFile else '(?)'\n"
        "        prev_name = doc.name\n"
        "        doc.save('saved via MCP')\n"
        "        print('saved NEW VERSION of EXISTING lineage:')\n"
        "        print('  document: ' + prev_name)\n"
        "        print('  lineage:  ' + str(prev_lin))\n"
        "        print('  >> verify this is the part you intended to version <<')\n"
        "    elif not name:\n"
        "        print('ERROR: document is unsaved - provide a name (or create_new=True)')\n"
        "    else:\n"
        "        doc.saveAs(name, folder, 'created via MCP', '')\n"
        "        lin = doc.dataFile.id if doc.dataFile else '(?)'\n"
        "        print('saved as: ' + name + '  lineage=' + str(lin))\n"
        "except Exception as _e:\n"
        "    print('ERROR saving: ' + str(_e))\n"
    ).format(n=name, cn=create_new)
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
    return _send(code)


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
    return _send(code)


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
    the primitive for Voronoi / text / texture patterns on curved & revolved
    bodies. Do NOT hand-roll this: closed splines on curved faces,
    createRuledSurface, loft between non-planar closed splines and
    BRepBodyDefinition are all unreliable on this build.

    sketch_index: -1 = most recent sketch. depth_mm: cut depth (protrude=True
    embosses outward instead). face_tempids: comma-separated ids from
    list_faces; OMIT to auto-target the largest curved face (recommended -
    tempIds die when an operation splits faces).

    Cuts PER-PROFILE with an individual volume check and reports ok/no-op/failed
    counts. A no-op profile usually falls outside the face's angular sector - a
    single flat sketch only wraps onto roughly one 70-100 degree sector, so
    split large patterns into sectors.
    """
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = root\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
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
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = root\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
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
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = root\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
        "base = {b!r}.lower()\n"
        "planes = {{'xy': host.xYConstructionPlane,\n"
        "          'xz': host.xZConstructionPlane,\n"
        "          'yz': host.yZConstructionPlane}}\n"
        "bp = planes.get(base, host.xYConstructionPlane)\n"
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
    Echoes world position so unit/orientation slips surface immediately."""
    code = (
        "import math\n"
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = root\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
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
        "    ci = host.constructionPlanes.createInput()\n"
        "    try:\n"
        "        ci.setByTangentAtPoint(best, pt)\n"
        "        pl = host.constructionPlanes.add(ci)\n"
        "        g = pl.geometry\n"
        "        print('tangent plane at %.1f deg' % {ang})\n"
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
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = root\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
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
    return _send(code)


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
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "cn = {cn!r}\n"
        "host = root\n"
        "if cn:\n"
        "    for occ in root.allOccurrences:\n"
        "        if occ.component.name == cn:\n"
        "            host = occ.component\n"
        "            break\n"
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
                       which: str = "all", min_radius_mm: float = 0.2) -> str:
    """Fillet that SEARCHES for a working configuration instead of failing.

    For each radius (starting at radius_mm, halving down to min_radius_mm) it
    tries tangent-chain ON then OFF, and for each attempt verifies the volume
    delta against the ~0.1*r^2*L expectation. Reports which combination worked
    and whether the result is geometrically sane.

    This exists because a blend that fails at every constant radius, every
    variable radius, and as a chamfer can succeed on the FIRST radius once
    isTangentChain=True gives it a run-out - but tangent chaining is
    double-edged: it also lets a blend propagate down a tangent-continuous
    surface and fill in geometry far away. So this verifies rather than just
    trying."""
    R0 = radius_mm / 10.0
    RMIN = min_radius_mm / 10.0
    code = (
        "design = app.activeProduct\n"
        "root = design.rootComponent\n"
        "body = root.bRepBodies.item({bi})\n"
        "maxz = body.boundingBox.maxPoint.z\n"
        "def pick_edges():\n"
        "    col = adsk.core.ObjectCollection.create()\n"
        "    L = 0.0\n"
        "    b = root.bRepBodies.item({bi})\n"
        "    for e in b.edges:\n"
        "        sp = e.startVertex.geometry\n"
        "        ep = e.endVertex.geometry\n"
        "        vert = abs(sp.z - ep.z) > 1e-6\n"
        "        top = (abs(sp.z - maxz) < 1e-6) and (abs(ep.z - maxz) < 1e-6)\n"
        "        _sel = {w!r}\n"
        "        take = (_sel == 'all') or (_sel == 'vertical' and vert) or (_sel == 'top' and top)\n"
        "        if take:\n"
        "            col.add(e)\n"
        "            try:\n"
        "                L += e.length * 10.0\n"
        "            except Exception:\n"
        "                pass\n"
        "    return col, L\n"
        "r = {R0}\n"
        "rmin = {RMIN}\n"
        "attempt = 0\n"
        "solved = None\n"
        "while r >= rmin and solved is None:\n"
        "    for chain in (True, False):\n"
        "        attempt += 1\n"
        "        edges, Lmm = pick_edges()\n"
        "        if edges.count == 0:\n"
        "            print('no edges matched which={w}')\n"
        "            break\n"
        "        vol_before = sum((b.volume if b.isValid else 0)\n"
        "                         for b in root.bRepBodies)\n"
        "        marker = design.timeline.markerPosition\n"
        "        try:\n"
        "            fil = root.features.filletFeatures\n"
        "            inp = fil.createInput()\n"
        "            inp.isRollingBallCorner = True\n"
        "            inp.addConstantRadiusEdgeSet(\n"
        "                edges, adsk.core.ValueInput.createByReal(r), chain)\n"
        "            fil.add(inp)\n"
        "            vol_after = sum((b.volume if b.isValid else 0)\n"
        "                            for b in root.bRepBodies)\n"
        "            delta_mm3 = abs(vol_after - vol_before) * 1000.0\n"
        "            exp_mm3 = 0.1 * ((r*10.0) ** 2) * Lmm\n"
        "            sane = not (exp_mm3 > 0 and delta_mm3 > 10.0 * exp_mm3)\n"
        "            print('attempt %d: r=%.2fmm chain=%s -> SUCCESS, '\n"
        "                  'delta %.2f mm^3 (expect <= ~%.2f) %s'\n"
        "                  % (attempt, r*10, chain, delta_mm3, exp_mm3,\n"
        "                     'SANE' if sane else '*** OVERSHOOT ***'))\n"
        "            if sane:\n"
        "                solved = (r*10, chain, delta_mm3)\n"
        "                break\n"
        "            else:\n"
        "                print('  rolling back overshooting fillet')\n"
        "                cur = design.timeline.markerPosition\n"
        "                for i in range(cur - 1, marker - 1, -1):\n"
        "                    try:\n"
        "                        design.timeline.item(i).entity.deleteMe()\n"
        "                    except Exception:\n"
        "                        pass\n"
        "        except Exception as _e:\n"
        "            msg = str(_e)\n"
        "            print('attempt %d: r=%.2fmm chain=%s -> FAILED: %s'\n"
        "                  % (attempt, r*10, chain, msg[:90]))\n"
        "    if solved is None:\n"
        "        r = r / 2.0\n"
        "if solved:\n"
        "    print('RESOLVED: radius=%.2fmm tangent_chain=%s (volume delta %.2f mm^3)'\n"
        "          % solved)\n"
        "else:\n"
        "    print('NO WORKING CONFIGURATION down to %.2fmm.' % ({RMIN}*10))\n"
        "    print('Next: include adjacent edges so the blend has a run-out, or '\n"
        "          'fillet a shorter sub-chain. Pass the raw error to '\n"
        "          'explain_fillet_error() for a code-specific hint.')\n"
    ).format(bi=body_index, w=which, R0=R0, RMIN=RMIN)
    return _send(code)


if __name__ == "__main__":
    mcp.run(transport="stdio")
