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
# Security note: this listens ONLY on 127.0.0.1 (localhost). It executes
# arbitrary Python sent to it. Do not change the bind address to 0.0.0.0
# and do not run it on an untrusted network.

import adsk.core
import adsk.fusion
import traceback
import threading
import json
import io
import time
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
LISTENER_VERSION = '1.2.1'
PROTOCOL = 2   # bump when the wire schema changes
CUSTOM_EVENT_ID = 'MCPListenerExec'
DEFAULT_BUDGET_S = 600.0   # soft budget; job is FLAGGED timeout past this

app = adsk.core.Application.get()
ui = app.userInterface

# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------
# Each job: {id, status, code, stdout(list), traceback, started, finished,
#            budget_s, elapsed_ms}
# status in: queued | running | ok | error | timeout
_jobs = {}
_job_lock = threading.Lock()
_counter = [0]
_handlers = []
_httpd = None
_custom_event = None

# Liveness + cached cheap state, updated by the main thread, read by HTTP thread.
_main_thread_alive = [time.time()]
_busy = [False]
_current_job = [None]
_cached_state = ['{}']   # JSON string of last cheap state snapshot

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
    return adsk.fusion.Design.cast(app.activeProduct)

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

def sliver_face_count(body, min_area_mm2=0.01):
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

def report_volume_delta(before_cm3, label='op'):
    after = total_volume()
    delta = after - before_cm3
    print('[%s] volume delta: %+.4f cm^3 (= %+.1f mm^3)'
          % (label, delta, delta * 1000.0))
    if abs(delta) < 1e-7:
        print('[%s] WARNING: volume delta is ~ZERO - the operation may have had '
              'NO effect (silent no-op boolean / cut missed the body).' % label)
    return delta

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

def progress(msg):
    """Emit an incremental progress line. Visible via GET /job before the call
    finishes - use it to mark stages of a long operation."""
    print('[progress] ' + str(msg))
'''


def _cheap_state_snapshot():
    """Build a CHEAP state dict - counts and identity only, NO geometry work.
    Runs on the main thread (called at the end of each job)."""
    try:
        doc = app.activeDocument
        design = adsk.fusion.Design.cast(app.activeProduct)
        root = design.rootComponent if design else None
        name = doc.name if doc else '(none)'
        lineage = ''
        try:
            if doc and doc.dataFile:
                lineage = doc.dataFile.id
        except Exception:
            lineage = '(unsaved)'
        tl_count = 0
        try:
            tl_count = design.timeline.count if design else 0
        except Exception:
            tl_count = -1
        return {
            'active_document': name,
            'lineage_id': lineage,
            'bodies': root.bRepBodies.count if root else 0,
            'sketches': root.sketches.count if root else 0,
            'components': root.allOccurrences.count if root else 0,
            'timeline_features': tl_count,
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
        with _job_lock:
            # don't override a watchdog-set timeout flag
            if job['status'] == 'running':
                job['status'] = 'ok'
            job['traceback'] = ''
    except Exception:
        with _job_lock:
            if job['status'] == 'running':
                job['status'] = 'error'
            job['traceback'] = traceback.format_exc()
    finally:
        with _job_lock:
            job['finished'] = time.time()
            job['elapsed_ms'] = int((job['finished'] - job['started']) * 1000)
        _busy[0] = False
        _current_job[0] = None
        _main_thread_alive[0] = time.time()
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
        'status': status,                 # queued|running|ok|error|timeout
        'stdout': stdout,
        'traceback': tb,
        'elapsed_ms': elapsed if elapsed is not None else 0,
        # back-compat fields so older server code still works:
        'ok': status == 'ok',
        'output': (stdout + ('\n' + tb if tb else '')) if stdout or tb else 'OK',
    }


def _watchdog():
    """Background thread: flags jobs that blow past their budget as 'timeout'.
    Cannot force-kill a hung main thread, but gives the caller a clear signal
    instead of a dead channel."""
    while True:
        time.sleep(2.0)
        now = time.time()
        with _job_lock:
            for job in _jobs.values():
                if job['status'] == 'running' and job.get('started'):
                    if now - job['started'] > job.get('budget_s', DEFAULT_BUDGET_S):
                        job['status'] = 'timeout'
                        job['timed_out_at'] = now


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ---- GET: ping / state / job status (all served WITHOUT the main thread) ----
    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if path in ('', '/ping', '/'):
            return self._ping()
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
            done = [j for j in _jobs.values() if j['status'] in ('ok','error','timeout')]
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
            if st in ('ok', 'error', 'timeout'):
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

        wd = threading.Thread(target=_watchdog, daemon=True)
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
