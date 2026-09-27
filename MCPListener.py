# MCPListener.py
# Fusion 360 Add-In: runs a local HTTP server inside Fusion and executes
# incoming Python on Fusion's main thread (required by the API).
#
# v1.2 - ASYNC JOB MODEL
# ----------------------
# The HTTP response is decoupled from operation completion. Endpoints:
#
#   POST /exec       {code, budget_s?}  -> {job_id} immediately (non-blocking)
#   GET  /job/<id>                      -> envelope (status/stdout/traceback/elapsed_ms)
#   GET  /ping                          -> liveness, served WITHOUT the main thread
#   GET  /state                         -> cheap cached doc/body/sketch counts
#   POST /exec_sync  {code, wait_s?}    -> blocks up to wait_s, returns partial
#                                          stdout + envelope (back-compat path)
#
# ping and state are answered purely from cached data on the HTTP thread, so
# they STAY RESPONSIVE even while a geometry call is churning on the main thread.
#
# Install: see docs/INSTALL.md ("Part A: Install the Fusion add-in").
#
# Security: this executes arbitrary Python, so two checks guard every request.
#   1. Host header must be localhost. Binding to 127.0.0.1 keeps the network
#      out, but not a web page in your browser: a DNS-rebinding site reaches
#      127.0.0.1 under its own hostname, and this check refuses it.
#   2. X-MCP-Token header must match a random token written at startup to
#      ~/.fusion_mcp/listener_token (readable only by you). A browser cannot
#      read that file, and a custom header forces a CORS preflight this server
#      never approves - so a page cannot even send the request. /ping is the
#      one exception (liveness + version only, no document data).
# Do not change the bind address to 0.0.0.0 or forward the port.

import adsk.core
import adsk.fusion
import traceback
import threading
import json
import io
import time
import re
import os
import hmac
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
# Kept in lockstep with SERVER_VERSION in fusion_mcp_server.py - ping_fusion
# compares them, and tests/test_versions.py fails the build on any drift.
LISTENER_VERSION = '1.9.1'
PROTOCOL = 3   # bump when the wire schema changes (3: token auth, budget flag)
CUSTOM_EVENT_ID = 'MCPListenerExec'
DEFAULT_BUDGET_S = 600.0   # soft budget; job is FLAGGED budget_exceeded past this

TOKEN_DIR = os.path.join(os.path.expanduser('~'), '.fusion_mcp')
TOKEN_PATH = os.path.join(TOKEN_DIR, 'listener_token')
TOKEN_HEADER = 'X-MCP-Token'
_ALLOWED_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
_token = [None]

app = adsk.core.Application.get()
ui = app.userInterface

# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------
# Each job: {id, status, code, stdout(list), traceback, started, finished,
#            budget_s, elapsed_ms}
# status in: queued | running | ok | error; budget_exceeded is a separate flag
_jobs = {}
_job_lock = threading.Lock()
_counter = [0]
_handlers = []
_httpd = None
_custom_event = None
_stop_event = [None]   # set by stop() so this run's watchdog thread exits

# Liveness + cached cheap state, updated by the main thread, read by HTTP thread.
_main_thread_alive = [time.time()]
_busy = [False]
_current_job = [None]
_cached_state = ['{}']   # JSON string of last cheap state snapshot
_refresh_pending = [False]
_refresh_last = [0.0]

# A live stdout buffer for the currently running job, so GET /job can return
# partial output mid-flight (incremental progress).
_live_stdout = []
_live_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Dangerous-call guard (refuse known host-crashing calls before the main thread)
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERNS = [
    (r'\.defineAsFreeMove\s*\(', 'defineAsFreeMove (move-face free-move) has '
     'hard-crashed the Fusion host on this build; refused. Use a parametric '
     'feature edit or rebuild the body instead.'),
    (r'\.defineAsOffset\s*\(', 'defineAsOffset is not reliably available on '
     'this listener build; refused. Use offsetFeatures or a shell/extrude '
     'instead.'),
]


def _screen_for_danger(code):
    for pattern, msg in _DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            return msg
    return None


# ---------------------------------------------------------------------------
# Live stdout: a file-like that appends to _live_stdout AND a per-job list.
# ---------------------------------------------------------------------------
class _TeeStdout(io.TextIOBase):
    def __init__(self, job_lines):
        self._job_lines = job_lines

    def write(self, s):
        if s:
            with _live_lock:
                _live_stdout.append(s)
            self._job_lines.append(s)
        return len(s)

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# Helpers injected into every exec namespace
# ---------------------------------------------------------------------------
_HELPER_SOURCE = r'''
def mm(x):
    """millimeters -> Fusion cm. Use for every length you pass to the API."""
    return x / 10.0

def cm(x):
    return x

def to_mm(x_cm):
    return x_cm * 10.0

def to_mm2(area_cm2):
    return area_cm2 * 100.0

def to_mm3(vol_cm3):
    return vol_cm3 * 1000.0

def _active_design():
    return _require_design()

def _occ_label(o):
    try:
        return o.name
    except Exception:
        return '?'

def find_occurrence(name):
    """Resolve ONE occurrence: by its own name first ('Screw:3', what the
    browser tree shows), then its full path ('Sub:1+Screw:3'), then its
    component name - accepted only when exactly one occurrence uses that
    component. Copies made by copy_component share one component, so once
    'Screw' is copied the bare name is ambiguous: that RAISES, listing the
    occurrence names to use, instead of silently acting on the first copy."""
    d = _require_design()
    occs = list(d.rootComponent.allOccurrences)
    for attr in ('name', 'fullPathName'):
        for o in occs:
            try:
                if getattr(o, attr) == name:
                    return o
            except Exception:
                pass
    by_comp = []
    for o in occs:
        try:
            if o.component.name == name:
                by_comp.append(o)
        except Exception:
            pass
    if len(by_comp) == 1:
        return by_comp[0]
    if by_comp:
        raise RuntimeError(
            'component %r has %d occurrences (%s) - pass one occurrence name '
            'to choose which' % (name, len(by_comp),
                                 ', '.join(_occ_label(o) for o in by_comp)))
    raise RuntimeError(
        'no occurrence or component named %r. Available: %s'
        % (name, ', '.join(_occ_label(o) for o in occs)
           or '(none - all geometry is in the root component)'))

def find_component(name):
    """Resolve a COMPONENT (the shared definition) by component name or by any
    of its occurrence names. Empty name = the root component. RAISES on a miss:
    silently falling back to root put work in the wrong place."""
    d = _require_design()
    root = d.rootComponent
    if not name:
        return root
    occs = list(root.allOccurrences)
    for o in occs:
        try:
            if name in (o.component.name, o.name, o.fullPathName):
                return o.component
        except Exception:
            pass
    raise RuntimeError(
        'no component named %r. Available: %s'
        % (name, ', '.join(sorted(set(o.component.name for o in occs)))
           or '(none - all geometry is in the root component)'))

def _require_design():
    """Return the active Design, or raise a CLEAR error saying why not.

    app.activeProduct returns whatever product is active - which is NOT a Design
    when Fusion is in the Drawing, CAM, Render or Simulation workspace, or when
    the active document is not a design. Accessing .rootComponent on those fails
    with an obscure error, or targets something unexpected. Every geometry tool
    goes through this so the failure is legible instead of mysterious."""
    prod = app.activeProduct
    d = adsk.fusion.Design.cast(prod)
    if d is None:
        try:
            pt = prod.productType
        except Exception:
            pt = type(prod).__name__
        try:
            dn = app.activeDocument.name
        except Exception:
            dn = '(unknown)'
        raise RuntimeError(
            "Active product is %r, not a Design (document %r). Fusion is in a "
            "non-modelling workspace (Drawing/CAM/Render/Simulation) or this "
            "document is not a design. Switch to the DESIGN workspace, or use "
            "activate_document() to select the right document, then retry."
            % (pt, dn))
    return d

def assert_scale(max_mm=2000.0):
    d = _active_design()
    root = d.rootComponent
    for b in root.bRepBodies:
        bb = b.boundingBox
        dx = to_mm(bb.maxPoint.x - bb.minPoint.x)
        dy = to_mm(bb.maxPoint.y - bb.minPoint.y)
        dz = to_mm(bb.maxPoint.z - bb.minPoint.z)
        big = max(dx, dy, dz)
        if big > max_mm:
            raise AssertionError(
                'assert_scale: body "%s" is %.1f mm across (> %.0f mm). '
                'Likely a cm/mm units slip - did you forget mm()?'
                % (b.name, big, max_mm))
    return True

def volume_of(body):
    try:
        return body.volume
    except Exception:
        return 0.0

def smallest_faces(body, n=5):
    """Print the n smallest face areas in mm^2. Far more informative than a
    sliver COUNT - you see whether the smallest face is 0.01 mm^2 (an artifact)
    or 4 mm^2 (fine)."""
    areas = []
    for f in body.faces:
        try:
            areas.append(f.area * 100.0)
        except Exception:
            pass
    areas.sort()
    shown = areas[:n]
    print('[faces] %d total; %d smallest (mm^2): %s'
          % (len(areas), len(shown), ', '.join('%.4f' % a for a in shown)))
    return shown

def sliver_face_count(body, min_area_mm2=0.05):
    # threshold raised from 0.01: faces of 0.010-0.014 mm^2 are ~0.11 mm across
    # and DO show as visible artifacts, but slipped under the old cutoff.
    n = 0
    thresh_cm2 = min_area_mm2 / 100.0
    for f in body.faces:
        try:
            if f.area < thresh_cm2:
                n += 1
        except Exception:
            pass
    return n

def total_volume():
    d = _active_design()
    root = d.rootComponent
    return sum(volume_of(b) for b in root.bRepBodies)

def body_count():
    d = _active_design()
    return d.rootComponent.bRepBodies.count

def capture_state(target_body=None):
    """Snapshot before a boolean. Pass the TARGET body when you have one: a
    Join that merges two existing bodies is volume-neutral overall, so total
    volume alone produces a FALSE no-op alarm on the most common boolean."""
    tv = None
    try:
        if target_body is not None and target_body.isValid:
            tv = target_body.volume
    except Exception:
        tv = None
    return {'total': total_volume(), 'count': body_count(), 'target': tv,
            'name': getattr(target_body, 'name', None)}

def report_volume_delta(before, label='op', target_body=None):
    """Report what an operation actually changed. `before` may be a
    capture_state() dict (preferred) or a bare float total. Warns ONLY when
    body count AND volume are both unchanged - so a volume-neutral Join is
    correctly reported as a real effect, not a no-op."""
    after_total = total_volume()
    after_count = body_count()
    if isinstance(before, dict):
        b_total = before.get('total', 0.0)
        b_count = before.get('count', after_count)
        b_target = before.get('target')
        tname = before.get('name')
    else:
        b_total, b_count, b_target, tname = float(before), after_count, None, None

    d_total = after_total - b_total
    d_count = after_count - b_count
    print('[%s] total volume delta: %+.4f cm^3 (= %+.1f mm^3) | bodies: %d -> %d'
          % (label, d_total, d_total * 1000.0, b_count, after_count))

    d_target = None
    if b_target is not None and target_body is not None:
        try:
            if target_body.isValid:
                d_target = target_body.volume - b_target
                print('[%s] target body %r delta: %+.4f cm^3'
                      % (label, tname, d_target))
        except Exception:
            pass

    tgt_changed = (d_target is not None and abs(d_target) >= 1e-7)
    changed = (abs(d_total) >= 1e-7) or (d_count != 0) or tgt_changed
    if not changed:
        print('[%s] WARNING: nothing changed - volume AND body count are both '
              'unchanged. The operation had NO effect.' % label)
    return d_total

def fillet_volume_sanity(radius_mm, edge_len_mm, delta_cm3, label='fillet'):
    """A fillet of radius r along total edge length L changes volume by at most
    ~0.1*r^2*L. An order-of-magnitude overshoot means the blend propagated past
    the intended edges - typically tangent-chaining running down a
    tangent-continuous surface and filling in a relief far away. Fusion reports
    SUCCESS, the body stays valid, no sliver appears: this is the only cheap
    detector."""
    expected_mm3 = 0.1 * (radius_mm ** 2) * max(edge_len_mm, 0.0)
    actual_mm3 = abs(delta_cm3) * 1000.0
    print('[%s] volume delta %.2f mm^3 (expected <= ~%.2f mm^3 for r=%.2f, L=%.1f)'
          % (label, actual_mm3, expected_mm3, radius_mm, edge_len_mm))
    if expected_mm3 > 0 and actual_mm3 > 10.0 * expected_mm3:
        print('[%s] *** WARNING: volume change is %.1fx the expected maximum. '
              'The fillet almost certainly propagated beyond the intended edges. '
              'VERIFY with cross_section - Fusion will NOT report this.'
              % (label, actual_mm3 / expected_mm3))
        return False
    return True

def log_sketch_areas(sketch):
    try:
        for i in range(sketch.profiles.count):
            a = sketch.profiles.item(i).areaProperties().area
            print('[sketch] profile %d area = %.3f mm^2' % (i, to_mm2(a)))
    except Exception as _e:
        print('[sketch] could not measure areas: ' + str(_e))

def delete_face(face):
    d = _active_design()
    root = d.rootComponent
    try:
        dff = root.features.deleteFaceFeatures
        dff.add(face)
        return True
    except Exception as _e:
        print('[delete_face] failed: ' + str(_e))
        return False

def P_xy(x_mm, y_mm):
    """Sketch point on the XY plane that lands at world (x, y, 0) mm.
    XY mapping: sketch(u,v) -> world(u, v, 0)."""
    return adsk.core.Point3D.create(mm(x_mm), mm(y_mm), 0)

def P_xz(x_mm, z_mm):
    """Sketch point on the XZ plane that lands at world (x, 0, z) mm.
    MEASURED XZ mapping: sketch(u,v) -> world(u, 0, -v), so sketch Y is the
    NEGATED world Z."""
    return adsk.core.Point3D.create(mm(x_mm), mm(-z_mm), 0)

def P_yz(y_mm, z_mm):
    """Sketch point on the YZ plane that lands at world (0, y, z) mm.
    MEASURED YZ mapping on this build: sketch(u,v) -> world(0, v, -u).
    i.e. sketch X -> world -Z and sketch Y -> world +Y. This is NOT the
    'sketch X -> world Y, sketch Y -> world Z' that older docs claimed - that
    error produces a plausible-looking body with a correct bounding box and a
    correct profile area, and only shows up several operations later."""
    return adsk.core.Point3D.create(mm(-z_mm), mm(y_mm), 0)

def verify_plane_mapping(sketch, label='sketch'):
    """Measure and print a sketch's ACTUAL local->world basis via
    sketchToModelSpace. Run this once on any new/offset/tangent plane before
    placing geometry - a wrong mapping never fails loudly."""
    o = sketch.sketchToModelSpace(adsk.core.Point3D.create(0, 0, 0))
    ux = sketch.sketchToModelSpace(adsk.core.Point3D.create(1, 0, 0))
    uy = sketch.sketchToModelSpace(adsk.core.Point3D.create(0, 1, 0))
    def axis_of(a, b):
        d = (b.x - a.x, b.y - a.y, b.z - a.z)
        names = ('X', 'Y', 'Z')
        i = max(range(3), key=lambda k: abs(d[k]))
        return ('+' if d[i] >= 0 else '-') + names[i]
    print('[%s] origin -> world (%.3f, %.3f, %.3f) mm'
          % (label, o.x * 10, o.y * 10, o.z * 10))
    print('[%s] sketch +X -> world %s ; sketch +Y -> world %s'
          % (label, axis_of(o, ux), axis_of(o, uy)))
    return (axis_of(o, ux), axis_of(o, uy))

def retire_body(body):
    """Non-destructively retire a body: root.features.removeFeatures.add(body).
    This is the RECOMMENDED replace-a-body idiom - it keeps timeline history and
    is strictly safer than clearing the document. Build the new body first, then
    retire the old one."""
    d = _active_design()
    root = d.rootComponent
    try:
        name = body.name
        root.features.removeFeatures.add(body)
        print('[retire_body] retired %r (history preserved)' % name)
        return True
    except Exception as _e:
        print('[retire_body] failed: ' + str(_e))
        return False

def edge_ok(e, min_len_mm=1e-4):
    """True if an edge is safe to inspect. Two lofts meeting tangentially can
    leave a ZERO-LENGTH edge with fewer than two faces, and e.faces.item(1) then
    raises 'Bad index parameter'. Every helper that walks body.edges uses this;
    use it in your own loops too."""
    try:
        if e.faces.count < 2:
            return False
        if to_mm(e.length) < min_len_mm:
            return False
    except Exception:
        return False
    return True

def edge_face_pair(e):
    """('Cylinder','Cone') style surface-type pair for an edge, or None if the
    edge is degenerate. Classifying edges by adjacent surface type is how you
    target a fillet - this is the classifier so callers stop re-implementing it."""
    if not edge_ok(e):
        return None
    try:
        a = type(e.faces.item(0).geometry).__name__.replace('adsk::core::', '')
        b = type(e.faces.item(1).geometry).__name__.replace('adsk::core::', '')
        return tuple(sorted((a, b)))
    except Exception:
        return None

def safe_edges(body, min_len_mm=1e-4):
    """Iterate a body's edges with degenerate ones filtered out."""
    out = []
    for e in body.edges:
        if edge_ok(e, min_len_mm):
            out.append(e)
    return out

def path_report(path, label='path'):
    """Print a Path's segment count and total length. A mis-chained path
    silently produces a short sweep with no error - segment count is the only
    cheap way to catch it."""
    try:
        n = path.count
        L = 0.0
        for i in range(n):
            try:
                L += path.item(i).curve.length * 10.0
            except Exception:
                pass
        print('[%s] segments=%d total_length=%.2f mm' % (label, n, L))
        if n <= 1:
            print('[%s] NOTE: only %d segment - if you expected a chain, the '
                  'connection test may have matched the same point twice '
                  '(sketch entity properties return FRESH wrappers, so `is` '
                  'comparison is always False - compare .geometry coords).'
                  % (label, n))
        return n
    except Exception as _e:
        print('[%s] could not report: %s' % (label, _e))
        return -1

def refresh_geometry():
    """Force a recompute and return whether volumes actually moved.
    body.volume / boundingBox can read byte-identical after sketch edits even
    following computeAll(), which makes a correct edit look like a failure."""
    d = _active_design()
    root = d.rootComponent
    before = [volume_of(b) for b in root.bRepBodies]
    try:
        d.computeAll()
    except Exception as _e:
        print('[refresh] computeAll failed: %s' % _e)
    try:
        d.timeline.moveToEnd()
    except Exception:
        pass
    after = [volume_of(b) for b in root.bRepBodies]
    moved = any(abs(a - b) > 1e-12 for a, b in zip(before, after)) or \
            len(before) != len(after)
    print('[refresh] volumes %s after recompute'
          % ('CHANGED' if moved else 'identical'))
    if not moved:
        print('[refresh] identical volume does NOT prove the geometry is '
              'unchanged - cached values can survive a sketch edit. Confirm '
              'with cross_section or pointContainment sampling.')
    return moved

def progress(msg):
    """Emit an incremental progress line. Visible via GET /job before the call
    finishes - use it to mark stages of a long operation."""
    print('[progress] ' + str(msg))

def tbm():
    """TemporaryBRepManager - build throwaway solids in memory, boolean them,
    then commit ONE result. This is the most robust path on this build: it needs
    NO construction planes (setByPlane / ConstructionPoints.add raise
    'Environment is not supported' here) and NO face tempIds (which die when an
    operation splits faces). Hundreds of cuts land as a single timeline feature.

    Typical pattern:
        t = tbm()
        obb = adsk.core.OrientedBoundingBox3D.create(
            adsk.core.Point3D.create(mm(0), mm(0), mm(5)),
            adsk.core.Vector3D.create(1,0,0), adsk.core.Vector3D.create(0,1,0),
            mm(4), mm(4), mm(20))
        tool = t.createBox(obb)
        for extra in others:
            t.booleanOperation(tool, extra,
                adsk.fusion.BooleanTypes.UnionBooleanType)
        bf = root.features.baseFeatures.add()
        bf.startEdit(); tb = root.bRepBodies.add(tool, bf); bf.finishEdit()
        # then a CombineFeature cut against the target body
    See the cut_prisms tool for a complete worked implementation."""
    return adsk.fusion.TemporaryBRepManager.get()

def commit_tool_body(tool_body_temp, target_body, keep_tool=False):
    """Commit a TemporaryBRepManager solid into the design and CUT it from
    target_body in one combine feature. Returns the volume delta (and warns if
    it is ~zero). This is the safe way to land temp-BRep work in the timeline."""
    d = _active_design()
    root = d.rootComponent
    before = target_body.volume if target_body.isValid else 0.0
    bf = root.features.baseFeatures.add()
    bf.startEdit()
    tb = root.bRepBodies.add(tool_body_temp, bf)
    tb_name = tb.name
    bf.finishEdit()
    # The handle returned by bRepBodies.add() is STALE after finishEdit():
    # using it as a Combine tool raises ALL_TOOL_BODY_REFERENCE_LOST, and
    # renaming through it silently does not stick. Re-fetch by name.
    tb = None
    for b in root.bRepBodies:
        if b.name == tb_name:
            tb = b
            break
    if tb is None:
        print('[commit_tool_body] ERROR: could not re-fetch tool body %r'
              % tb_name)
        return 0.0
    combines = root.features.combineFeatures
    col = adsk.core.ObjectCollection.create()
    col.add(tb)
    ci = combines.createInput(target_body, col)
    ci.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
    ci.isKeepToolBodies = keep_tool
    combines.add(ci)
    after = target_body.volume if target_body.isValid else 0.0
    delta = after - before
    print('[commit_tool_body] volume delta: %+.4f cm^3' % delta)
    if abs(delta) < 1e-9:
        print('[commit_tool_body] WARNING: ~ZERO delta - the tool body may not '
              'intersect the target (check coordinates/units).')
    return delta
'''


def _doc_identity(doc):
    """Every identity signal for one document, read INDEPENDENTLY.

    Critical: doc.dataFile routes through app.data, which throws
    InternalValidationError for a whole session on some builds. When that
    happens a SAVED document looks unsaved - and any logic that branches on it
    will saveAs (creating a NEW document) instead of versioning the open one.
    So dataFile is captured separately and its failure is reported as
    'unknown', never as 'unsaved'."""
    info = {'name': None, 'is_saved_flag': None, 'is_modified': None,
            'lineage_id': None, 'version': None, 'data_ok': None,
            'path': None}
    try:
        info['name'] = doc.name
    except Exception:
        info['name'] = '(unreadable)'
    for attr, key in (('isSaved', 'is_saved_flag'), ('isModified', 'is_modified')):
        try:
            info[key] = bool(getattr(doc, attr))
        except Exception:
            info[key] = None          # attribute absent on this build
    try:
        df = doc.dataFile
        if df is None:
            info['data_ok'] = True
            info['lineage_id'] = None      # genuinely never saved
        else:
            info['data_ok'] = True
            info['lineage_id'] = df.id
            try:
                info['version'] = df.versionNumber
            except Exception:
                pass
            try:
                info['path'] = df.parentFolder.name
            except Exception:
                pass
    except Exception:
        # app.data is broken - we CANNOT know the lineage. Say so explicitly.
        info['data_ok'] = False
        info['lineage_id'] = 'UNKNOWN (app.data unavailable)'
    return info


def _active_index(docs, idents, active_doc, active_name):
    """Index of the active document, -1 if ambiguous, None if not found.

    Name comparison alone marks BOTH of two 'Untitled' documents active. Names
    decide when unique; object equality breaks a tie; if that cannot either,
    the answer is reported as ambiguous rather than guessed."""
    named = [i for i, di in enumerate(idents) if di.get('name') == active_name]
    if len(named) <= 1:
        return named[0] if named else None
    same = []
    for i in named:
        try:
            if docs[i] == active_doc:
                same.append(i)
        except Exception:
            pass
    return same[0] if len(same) == 1 else -1


def _cheap_state_snapshot():
    """CHEAP state - counts and identity only, NO geometry work. Runs on the
    main thread at the end of each job. Includes EVERY open document so a
    caller can see what the user sees, not just the active one."""
    try:
        doc = app.activeDocument
        design = adsk.fusion.Design.cast(app.activeProduct)
        root = design.rootComponent if design else None
        ident = _doc_identity(doc) if doc else {}
        others = []
        try:
            docs = [app.documents.item(i) for i in range(app.documents.count)]
            idents = [_doc_identity(d) for d in docs]
            active_i = _active_index(docs, idents, doc, ident.get('name'))
            for i, di in enumerate(idents):
                # True / False, or None when two open documents share the
                # active name and the API cannot say which one is active
                di['active'] = (None if active_i == -1 else i == active_i)
                others.append(di)
        except Exception:
            pass
        tl_count = 0
        try:
            tl_count = design.timeline.count if design else 0
        except Exception:
            tl_count = -1
        return {
            'active_document': ident.get('name'),
            'lineage_id': ident.get('lineage_id'),
            'is_saved_flag': ident.get('is_saved_flag'),
            'is_modified': ident.get('is_modified'),
            'version': ident.get('version'),
            'data_ok': ident.get('data_ok'),
            'open_documents': others,
            'bodies': root.bRepBodies.count if root else 0,
            'sketches': root.sketches.count if root else 0,
            'components': root.allOccurrences.count if root else 0,
            'timeline_features': tl_count,
            'snapshot_time': time.time(),
        }
    except Exception:
        return {'error': 'state snapshot failed', 'trace': traceback.format_exc()}


def _execute_job(job):
    """Runs ON Fusion's main thread. Executes one job, streaming stdout."""
    job_lines = []
    with _live_lock:
        _live_stdout[:] = []  # reset live buffer for this job
    with _job_lock:
        job['status'] = 'running'
        job['started'] = time.time()
        job['stdout'] = job_lines
    _current_job[0] = job['id']
    _busy[0] = True

    local_app = adsk.core.Application.get()
    tee = _TeeStdout(job_lines)
    import contextlib
    try:
        ns = {
            'adsk': adsk,
            'app': local_app,
            'ui': local_app.userInterface,
            '__name__': '__main__',
        }
        with contextlib.redirect_stdout(tee):
            exec(compile(_HELPER_SOURCE, '<helpers>', 'exec'), ns)
            exec(compile(job['code'], '<mcp>', 'exec'), ns)
        # A job that overran its budget still reaches ok/error here: the
        # watchdog only sets budget_exceeded. (It used to set a terminal
        # 'timeout' status that hid the real outcome, so callers retried
        # operations that had in fact completed.)
        with _job_lock:
            job['status'] = 'ok'
            job['traceback'] = ''
    except Exception:
        with _job_lock:
            job['status'] = 'error'
            job['traceback'] = traceback.format_exc()
    finally:
        with _job_lock:
            job['finished'] = time.time()
            job['elapsed_ms'] = int((job['finished'] - job['started']) * 1000)
        _busy[0] = False
        _current_job[0] = None
        _main_thread_alive[0] = time.time()
        with _job_lock:
            _refresh_pending[0] = False
        if job.get('internal'):
            with _job_lock:
                _jobs.pop(job['id'], None)
        # refresh cheap cached state after every job
        try:
            _cached_state[0] = json.dumps(_cheap_state_snapshot())
        except Exception:
            pass


class ExecHandler(adsk.core.CustomEventHandler):
    """Fires on the main thread; pulls the next queued job and runs it."""
    def notify(self, args):
        try:
            jid = args.additionalInfo
            with _job_lock:
                job = _jobs.get(jid)
            if job is not None and job['status'] == 'queued':
                _execute_job(job)
        except Exception:
            # last-resort: never let an exception escape the event handler
            try:
                with _job_lock:
                    job = _jobs.get(args.additionalInfo)
                    if job:
                        job['status'] = 'error'
                        job['traceback'] = traceback.format_exc()
                        job['finished'] = time.time()
            except Exception:
                pass


def _envelope(job, include_partial=True):
    """Build the structured result envelope for a job."""
    with _job_lock:
        status = job['status']
        lines = list(job.get('stdout', []))
        tb = job.get('traceback', '')
        started = job.get('started')
        elapsed = job.get('elapsed_ms')
        over = bool(job.get('budget_exceeded'))
        budget = job.get('budget_s')
    if elapsed is None and started is not None:
        elapsed = int((time.time() - started) * 1000)
    stdout = ''.join(lines)
    if status == 'running' and include_partial:
        # add a live tail in case the per-job list lags
        with _live_lock:
            live = ''.join(_live_stdout)
        if len(live) > len(stdout):
            stdout = live
    return {
        'listener_version': LISTENER_VERSION,
        'protocol': PROTOCOL,
        'stdout_bytes': len(stdout),
        'job_id': job['id'],
        'status': status,                 # queued|running|ok|error
        'budget_exceeded': over,          # ran past budget_s (may still finish)
        'budget_s': budget,
        'stdout': stdout,
        'traceback': tb,
        'elapsed_ms': elapsed if elapsed is not None else 0,
        # back-compat fields so older server code still works:
        'ok': status == 'ok',
        'output': (stdout + ('\n' + tb if tb else '')) if stdout or tb else 'OK',
    }


def _request_identity_refresh():
    """Queue ONE identity refresh on the main thread. Called on demand by the
    /state endpoint when its cache is stale - NOT on a timer.

    The previous implementation fired a main-thread job every 4 seconds for as
    long as Fusion was open (~21,600 interrupts/day), each one enumerating every
    open document and touching dataFile - which raises (expensively) on builds
    where app.data is broken. Cost was paid continuously even when nobody was
    using the tool. Now the cost is paid only when someone actually asks for
    state and the answer is stale."""
    now = time.time()
    with _job_lock:
        # coalesce: at most one in-flight refresh, and no more than one per 2s
        if _refresh_pending[0] or (now - _refresh_last[0]) < 2.0:
            return
        _refresh_pending[0] = True
        _refresh_last[0] = now
        _counter[0] += 1
        jid = 'refresh-%d' % _counter[0]
        _jobs[jid] = {'id': jid, 'status': 'queued', 'code': 'pass',
                      'stdout': [], 'traceback': '', 'started': None,
                      'finished': None, 'budget_s': 15.0,
                      'elapsed_ms': None, 'internal': True}
    try:
        adsk.core.Application.get().fireCustomEvent(CUSTOM_EVENT_ID, jid)
    except Exception:
        with _job_lock:
            _refresh_pending[0] = False
            _jobs.pop(jid, None)


def _flag_overruns(now):
    """Flag running jobs that are past their budget. Only FLAGS - status still
    moves to ok/error when the job ends, so the caller learns the outcome."""
    with _job_lock:
        for job in _jobs.values():
            if (job['status'] == 'running' and job.get('started')
                    and not job.get('budget_exceeded')
                    and now - job['started']
                    > job.get('budget_s', DEFAULT_BUDGET_S)):
                job['budget_exceeded'] = True
                job['budget_exceeded_at'] = now


def _watchdog(stop_event):
    """Background thread: flags jobs that run past their budget. Cannot
    force-kill a hung main thread, but gives the caller a clear signal instead
    of a dead channel.

    Exits when stop_event is set. Without that, every Stop -> Run of the add-in
    (which every upgrade asks for) left another watchdog thread running."""
    while not stop_event.wait(2.0):
        _flag_overruns(time.time())


def _write_token():
    """Create this run's random auth token and write it where only the
    current user can read it. fusion_mcp_server.py reads it per request, so a
    listener restart (new token) needs no server restart."""
    tok = secrets.token_urlsafe(32)
    os.makedirs(TOKEN_DIR, exist_ok=True)
    try:
        os.chmod(TOKEN_DIR, 0o700)
    except Exception:
        pass
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(tok)
    _token[0] = tok
    return tok


def _remove_token():
    _token[0] = None
    try:
        os.remove(TOKEN_PATH)
    except Exception:
        pass


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ---- request guard (see the Security note at the top of this file) ----
    def _host_ok(self):
        host = (self.headers.get('Host') or '').strip().lower()
        if host.startswith('['):
            name = host.split(']')[0] + ']'
        else:
            name = host.split(':')[0]
        return name in _ALLOWED_HOSTS

    def _token_ok(self):
        want = _token[0]
        got = self.headers.get(TOKEN_HEADER) or ''
        return bool(want) and hmac.compare_digest(got.encode('utf-8'),
                                                  want.encode('utf-8'))

    def _guard(self, need_token=True):
        """True if the request may proceed; otherwise responds and returns
        False. Refusals carry version + protocol so the server can explain."""
        if not self._host_ok():
            self._respond(403, {
                'ok': False, 'status': 'error', 'error': 'forbidden_host',
                'listener_version': LISTENER_VERSION, 'protocol': PROTOCOL,
                'output': 'REFUSED: Host %r is not localhost.'
                          % self.headers.get('Host')})
            return False
        if need_token and not self._token_ok():
            self._respond(401, {
                'ok': False, 'status': 'error', 'error': 'bad_token',
                'listener_version': LISTENER_VERSION, 'protocol': PROTOCOL,
                'output': 'REFUSED: missing or wrong %s header. The token is '
                          'in %s and changes each time the add-in starts.'
                          % (TOKEN_HEADER, TOKEN_PATH)})
            return False
        return True

    # ---- GET: ping / state / job status (all served WITHOUT the main thread) ----
    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if path in ('', '/ping', '/'):
            if not self._guard(need_token=False):
                return
            return self._ping()
        if not self._guard():
            return
        if path == '/state':
            return self._state()
        if path.startswith('/job/'):
            jid = path[len('/job/'):]
            return self._job(jid)
        # legacy: bare GET behaves like ping
        return self._ping()

    def _ping(self):
        now = time.time()
        age = now - _main_thread_alive[0]
        self._respond(200, {
            'ok': True,
            'listener_version': LISTENER_VERSION,
            'protocol': PROTOCOL,
            'alive': True,
            'busy': _busy[0],
            'current_job': _current_job[0],
            'idle_seconds': round(age, 2),
            'output': 'MCPListener alive (busy={}, current_job={}, idle_ms={})'.format(
                _busy[0], _current_job[0], int(age * 1000)),
        })

    def _state(self):
        # served from cache; never triggers geometry work
        try:
            cached = json.loads(_cached_state[0])
        except Exception:
            cached = {}
        try:
            _age = time.time() - float(cached.get('snapshot_time') or 0)
            cached['snapshot_age_s'] = round(_age, 1)
            cached['snapshot_fresh'] = _age < 10.0
        except Exception:
            cached['snapshot_age_s'] = None
            cached['snapshot_fresh'] = False
        if not cached.get('snapshot_fresh'):
            _request_identity_refresh()   # lazy: only when actually stale
        cached['listener_version'] = LISTENER_VERSION
        cached['protocol'] = PROTOCOL
        cached['busy'] = _busy[0]
        cached['current_job'] = _current_job[0]
        cached['ok'] = True
        self._respond(200, cached)

    def _job(self, jid):
        with _job_lock:
            job = _jobs.get(jid)
        if job is None:
            self._respond(404, {'ok': False, 'status': 'unknown',
                                'output': 'no such job_id: ' + jid})
            return
        self._respond(200, _envelope(job))

    # ---- POST: exec (async) / exec_sync (blocking, back-compat) ----
    def do_POST(self):
        if not self._guard():
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            data = json.loads(body)
            code = data.get('code', '')
        except Exception:
            self._respond(400, {'ok': False, 'output': 'bad request / json'})
            return

        danger = _screen_for_danger(code)
        if danger:
            self._respond(200, {'ok': False, 'status': 'error',
                                'traceback': '', 'stdout': '',
                                'output': 'REFUSED (stability guard): ' + danger})
            return

        path = self.path.split('?')[0].rstrip('/')
        budget = float(data.get('budget_s', DEFAULT_BUDGET_S))
        job = self._enqueue(code, budget)

        if path == '/exec_sync' or data.get('sync'):
            wait_s = float(data.get('wait_s', 110.0))
            self._wait_and_respond(job, wait_s)
        else:
            # async: return job id immediately
            self._respond(200, {'ok': True, 'status': 'queued',
                                'job_id': job['id'],
                                'output': 'queued job ' + job['id']})

    def _enqueue(self, code, budget):
        with _job_lock:
            # prune: keep only the 50 most recent finished jobs
            done = [j for j in _jobs.values() if j['status'] in ('ok', 'error')]
            if len(done) > 50:
                done.sort(key=lambda j: j.get('finished') or 0)
                for old in done[:-50]:
                    _jobs.pop(old['id'], None)
            _counter[0] += 1
            jid = str(_counter[0])
            job = {'id': jid, 'status': 'queued', 'code': code,
                   'stdout': [], 'traceback': '', 'started': None,
                   'finished': None, 'budget_s': budget, 'elapsed_ms': None}
            _jobs[jid] = job
        try:
            adsk.core.Application.get().fireCustomEvent(CUSTOM_EVENT_ID, jid)
        except Exception:
            with _job_lock:
                job['status'] = 'error'
                job['traceback'] = 'could not fire custom event'
        return job

    def _wait_and_respond(self, job, wait_s):
        deadline = time.time() + wait_s
        while time.time() < deadline:
            with _job_lock:
                st = job['status']
            if st in ('ok', 'error'):
                self._respond(200, _envelope(job))
                return
            time.sleep(0.1)
        # still running at wait deadline: return partial envelope, status running
        env = _envelope(job)
        env['output'] = ('STILL RUNNING after %.0fs (job %s). Partial stdout '
                         'above. Poll GET /job/%s for completion.'
                         % (wait_s, job['id'], job['id'])) + '\n' + env['stdout']
        self._respond(200, env)

    def _respond(self, status, obj):
        payload = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run(context):
    global _httpd, _custom_event
    try:
        _custom_event = app.registerCustomEvent(CUSTOM_EVENT_ID)
        handler = ExecHandler()
        _custom_event.add(handler)
        _handlers.append(handler)

        # seed cached state
        try:
            _cached_state[0] = json.dumps(_cheap_state_snapshot())
        except Exception:
            pass

        _write_token()

        ev = threading.Event()
        _stop_event[0] = ev
        wd = threading.Thread(target=_watchdog, args=(ev,), daemon=True)
        wd.start()

        _httpd = ThreadingHTTPServer(('127.0.0.1', PORT), RequestHandler)
        _httpd.daemon_threads = True
        t = threading.Thread(target=_httpd.serve_forever, daemon=True)
        t.start()

        ui.messageBox(
            'MCPListener (async) running on http://127.0.0.1:{}\n'
            'Leave Fusion open while using the MCP server.'.format(PORT)
        )
    except Exception:
        if ui:
            ui.messageBox('MCPListener failed to start:\n{}'.format(traceback.format_exc()))


def stop(context):
    global _httpd, _custom_event
    if _stop_event[0] is not None:
        _stop_event[0].set()
        _stop_event[0] = None
    _remove_token()
    try:
        if _httpd:
            _httpd.shutdown()
            _httpd.server_close()
            _httpd = None
    except Exception:
        pass
    try:
        if _custom_event:
            for h in _handlers:
                try:
                    _custom_event.remove(h)
                except Exception:
                    pass
        app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        _custom_event = None
    except Exception:
        pass
    _handlers.clear()
    if ui:
        ui.messageBox('MCPListener stopped.')
