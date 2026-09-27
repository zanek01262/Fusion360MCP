"""Behaviour that depends on the SHAPE of Fusion API return values.

These are the bugs a live Fusion run found in 1.9.0 and mocks had missed,
because the old fakes were written from the code's assumptions instead of
from Fusion. The fakes here reproduce what Fusion actually returned when
probed (2026-09): getCurvature gives three values, ThreadDataQuery's
all* methods return sequences directly, and inch sizes are decimal-inch
strings, some labelled ('0.19(#10)')."""
import contextlib
import io
from types import SimpleNamespace

import pytest


def run(code, ns):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, "<generated>", "exec"), ns)
    return out.getvalue()


# ---------------------------------------------------------------- sweep_tube_check

class Evaluator:
    """CurveEvaluator3D as Fusion returns it: getCurvature -> (ok, dir, k/cm)."""

    def __init__(self, curvature_per_cm, length_cm=1.0, fail=False):
        self.k, self.length, self.fail = curvature_per_cm, length_cm, fail

    def getParameterExtents(self):
        return True, 0.0, 1.0

    def getLengthAtParameter(self, p0, p1):
        return True, self.length

    def getCurvature(self, t):
        if self.fail:
            raise RuntimeError("evaluator unavailable")
        return [True, SimpleNamespace(x=0, y=1, z=0), self.k]


def sweep_ns(*evaluators):
    curves = [SimpleNamespace(geometry=SimpleNamespace(evaluator=e)) for e in evaluators]
    sk = SimpleNamespace(name="Path", sketchCurves=SimpleNamespace(
        count=len(curves), item=lambda i: curves[i]))
    host = SimpleNamespace(sketches=SimpleNamespace(count=1, item=lambda i: sk))
    design = SimpleNamespace(rootComponent=host)
    return {"_require_design": lambda: design, "find_component": lambda cn: host}


def test_sweep_measures_a_known_arc(srv, captured):
    # line + arc of radius 10 mm (curvature 1.0 per cm), as probed live
    srv.sweep_tube_check(tube_radius_mm=1.0)
    out = run(captured[0], sweep_ns(Evaluator(0.0, 2.0), Evaluator(1.0, 1.571)))
    assert "minimum curvature radius 10.000 mm" in out
    assert "comfortable" in out


def test_sweep_flags_a_tube_wider_than_the_bend(srv, captured):
    srv.sweep_tube_check(tube_radius_mm=12.0)
    out = run(captured[0], sweep_ns(Evaluator(1.0)))
    assert "WILL SELF-INTERSECT" in out


def test_sweep_reports_evaluation_errors_instead_of_guessing(srv, captured):
    srv.sweep_tube_check()
    out = run(captured[0], sweep_ns(Evaluator(1.0, fail=True)))
    assert "*** WARNING: curvature could not be evaluated" in out
    assert "evaluator unavailable" in out


def test_sweep_straight_path_warns_about_corners(srv, captured):
    srv.sweep_tube_check()
    out = run(captured[0], sweep_ns(Evaluator(0.0), Evaluator(0.0)))
    assert "no curved segments" in out and "sharp corner" in out


# ---------------------------------------------------------------- modeled threads

METRIC, INCH = "ISO Metric profile", "ANSI Unified Screw Threads"
SIZES = {
    METRIC: ("2.5", "3.0", "8.0", "20.0"),
    INCH: ("0.06(#0)", "0.073(#1)", "0.19(#10)", "0.216(#12)", "0.25",
           "0.3125", "0.375", "0.39"),
}
DESIGNATIONS = {
    "3.0": ["M3x0.5", "M3x0.35"],
    "8.0": ["M8x1.25", "M8x1", "M8x0.8", "M8x0.75", "M8x0.5"],
    "20.0": [],
    "0.073(#1)": ["1-64 UNC", "1-72 UNF"],
    "0.19(#10)": ["10-24 UNC", "10-28 UNS", "10-32 UNF", "10-36 UNS"],
    "0.25": ["1/4-20 UNC", "1/4-24 UNS", "1/4-27 UNS", "1/4-28 UNF"],
    "0.375": ["3/8-16 UNC", "3/8-18 UNS", "3/8-24 UNF"],
}


class ThreadDataQuery:
    """Shaped like Fusion's: no getAll* methods; all* return sequences."""
    defaultMetricThreadType = METRIC
    defaultInchThreadType = INCH

    def allSizes(self, t):
        return SIZES[t]

    def allDesignations(self, t, size):
        return tuple(DESIGNATIONS.get(size, []))

    def allClasses(self, internal, t, desig):
        return ("6H",) if t == METRIC else ("1B", "2B", "3B")


class ThreadFeatures:
    def __init__(self):
        self.threadDataQuery = ThreadDataQuery()
        self.added = []

    def createThreadInfo(self, internal, ttype, desig, cls):
        return (internal, ttype, desig, cls)

    def createInput(self, face, info):
        return SimpleNamespace(face=face, info=info, isModeled=False)

    def add(self, tin):
        self.added.append(tin)


def apply_thread(srv, size_text):
    canon, kind, size, pitch = srv._tap_drill(size_text)[1:]
    tf = ThreadFeatures()
    ns = {"root": SimpleNamespace(features=SimpleNamespace(threadFeatures=tf)),
          "hole": SimpleNamespace(sideFaces=["side-face"])}
    return run(srv._modeled_thread_code(canon, kind, size, pitch), ns), tf


@pytest.mark.parametrize("size,applied", [
    ("M3", "M3x0.5 6H"),
    ("M8x1", "M8x1 6H"),            # not the coarse M8x1.25 listed first
    ("#10-32", "10-32 UNF 2B"),     # Fusion drops the '#'
    ("10-24", "10-24 UNC 2B"),
    ("#1-64", "1-64 UNC 2B"),
    ("1/4-20", "1/4-20 UNC 2B"),
    ("3/8-16", "3/8-16 UNC 2B"),
])
def test_thread_matches_fusions_real_catalogue(srv, size, applied):
    out, tf = apply_thread(srv, size)
    assert "modeled thread applied: " + applied in out
    (tin,) = tf.added
    assert tin.isModeled and tin.face == "side-face"
    assert tin.info[0] is True       # internal thread


def test_thread_missing_designation_is_a_warning(srv):
    out, tf = apply_thread(srv, "M20")          # size listed, no designations
    assert "WARNING: modeled thread NOT applied" in out and tf.added == []


def test_unknown_designation_is_a_warning(srv):
    tf = ThreadFeatures()
    ns = {"root": SimpleNamespace(features=SimpleNamespace(threadFeatures=tf)),
          "hole": SimpleNamespace(sideFaces=["f"])}
    out = run(srv._modeled_thread_code("M17", None, None, None), ns)
    assert "cannot be matched" in out and tf.added == []


def test_tap_drill_only_adds_no_thread_code(srv, captured):
    srv.tapped_hole("M3", 0, 0, 5)
    assert "threadFeatures" not in captured[0]
    srv.tapped_hole("M3", 0, 0, 5, tap_drill_only=False)
    assert "allSizes" in captured[1]


# ---------------------------------------------------------------- holes and joints

@pytest.mark.parametrize("tool,args", [
    ("drill_hole", dict(diameter_mm=3, x_mm=10, y_mm=10, depth_mm=5)),
    ("tapped_hole", dict(thread_size="M3", x_mm=5, y_mm=5, depth_mm=5)),
])
def test_holes_start_at_the_body_top(srv, captured, tool, args):
    getattr(srv, tool)(**args)
    code = captured[0]
    assert "_top = body.boundingBox.maxPoint.z" in code
    assert "setByOffset(root.xYConstructionPlane" in code
    # sketching at z=0 and cutting toward -Z missed bodies built upward
    assert "sketches.add(root.xYConstructionPlane)" not in code


def test_joint_at_point_uses_sketch_points(srv, captured):
    srv.joint_at_point("A:1", 0, 0, 0, "B:1", 1, 1, 1)
    assert "sketchPoints.add" in captured[0]
    assert "constructionPoints" not in captured[0]
