"""Joint and grounding tools, executed against fakes that behave the way
Fusion did in a live run (1.9.1): joints move component_a onto component_b;
an added joint can exist yet be unsolved (only its health says so); a part
can be 'grounded to parent' while isGrounded is False; and a rejected joint
leaves behind whatever helper geometry was made for it."""
import contextlib
import io
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------- geometry fakes

class Point:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z

    def transformBy(self, m):
        self.x, self.y, self.z = self.x + m.t[0], self.y + m.t[1], self.z + m.t[2]

    def distanceTo(self, o):
        return ((self.x - o.x) ** 2 + (self.y - o.y) ** 2 + (self.z - o.z) ** 2) ** 0.5


class Matrix:                      # translation-only is enough here
    def __init__(self, t=(0.0, 0.0, 0.0)):
        self.t = t


class Proxy:
    def __init__(self, occ, local):
        self.occ, self.local = occ, local


class SketchPoint:
    def __init__(self, p):
        self.p = p

    def createForAssemblyContext(self, occ):
        return Proxy(occ, (self.p.x, self.p.y, self.p.z))


class Sketch:
    def __init__(self, sketches):
        self._owner, self.name, self.isVisible = sketches, "Sketch", True
        self.sketchPoints = SimpleNamespace(add=SketchPoint)

    def deleteMe(self):
        self._owner.items.remove(self)


class Sketches:
    def __init__(self):
        self.items = []

    def add(self, plane):
        sk = Sketch(self)
        self.items.append(sk)
        return sk


class Occurrence:
    def __init__(self, name, grounded=False, to_parent=False):
        self.name, self.isGrounded, self.isGroundToParent = name, grounded, to_parent
        self.transform2 = Matrix()
        origin = SimpleNamespace(createForAssemblyContext=lambda occ: Proxy(occ, (0.0, 0.0, 0.0)))
        self.component = SimpleNamespace(sketches=Sketches(), xYConstructionPlane="XY",
                                         originConstructionPoint=origin)


def world(proxy):
    p = Point(*proxy.local)
    p.transformBy(proxy.occ.transform2)
    return p


class Joints:
    """mode: 'solve' moves component_a onto component_b (as Fusion does);
    'unsolved' adds a joint with bad health; 'apart' reports healthy but moves
    nothing; 'raise' rejects the input."""

    def __init__(self, mode):
        self.mode, self.items = mode, []

    @property
    def count(self):
        return len(self.items)

    def createInput(self, ga, gb):
        return SimpleNamespace(ga=ga, gb=gb, motion=None,
                               setAsRigidJointMotion=lambda: None,
                               setAsRevoluteJointMotion=lambda d: None)

    def add(self, ji):
        if self.mode == "raise":
            raise RuntimeError("3 : Provided input paths for joint are not valid.")
        health, msg = 0, ""
        if self.mode == "solve":
            a, b = ji.ga, ji.gb
            wb = world(b)
            a.occ.transform2 = Matrix((wb.x - a.local[0], wb.y - a.local[1], wb.z - a.local[2]))
        elif self.mode == "unsolved":
            health, msg = 1, ("Can't resolve some component positions because there "
                              "are conflicts.\n\nInspect existing assembly relationships.")
        j = SimpleNamespace(name="Rigid %d" % (self.count + 1), healthState=health,
                            errorOrWarningMessage=msg)
        self.items.append(j)
        return j


def env(mode="solve", a_grounded=False, a_to_parent=False):
    a = Occurrence("A:1", a_grounded, a_to_parent)
    b = Occurrence("B:1", grounded=True)
    b.transform2 = Matrix((4.0, 0.0, 0.0))          # b sits elsewhere
    root = SimpleNamespace(joints=Joints(mode))
    adsk = SimpleNamespace(
        core=SimpleNamespace(Point3D=SimpleNamespace(create=lambda x, y, z: Point(x, y, z))),
        fusion=SimpleNamespace(JointGeometry=SimpleNamespace(createByPoint=lambda p: p),
                               JointDirections=SimpleNamespace(ZAxisJointDirection="Z")))
    ns = {"adsk": adsk, "_require_design": lambda: SimpleNamespace(rootComponent=root),
          "find_occurrence": {"A": a, "B": b}.get}
    return ns, a, b, root


def run(code, ns):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, "<joint>", "exec"), ns)
    return out.getvalue()


def at_point(srv, captured):
    captured.clear()
    srv.joint_at_point("A", 5, 5, 10, "B", 25, 5, 0)
    return captured[0]


# ---------------------------------------------------------------- joints

def test_confirmed_only_when_solved_and_coincident(srv, captured):
    ns, a, b, _ = env("solve")
    out = run(at_point(srv, captured), ns)
    assert "CONFIRMED: rigid joint" in out and "coincide (0.0000 mm)" in out


def test_unsolved_joint_is_not_confirmed(srv, captured):
    """Fusion adds the joint and only flags its health. The joint count went
    up, which is all the pre-1.9.1 check looked at."""
    ns, *_ = env("unsolved")
    out = run(at_point(srv, captured), ns)
    assert "JOINT CREATED BUT NOT SOLVED (health 1)" in out
    assert "CONFIRMED" not in out.replace("NOT CONFIRMED", "")


def test_healthy_but_apart_is_unconfirmed(srv, captured):
    ns, *_ = env("apart")
    out = run(at_point(srv, captured), ns)
    assert "JOINT UNCONFIRMED" in out


def test_rejected_joint_leaves_no_helper_sketches(srv, captured):
    ns, a, b, _ = env("raise")
    with pytest.raises(RuntimeError, match="not valid"):
        run(at_point(srv, captured), ns)
    assert a.component.sketches.items == [] and b.component.sketches.items == []


def test_grounded_component_a_is_refused_before_anything_is_built(srv, captured):
    ns, a, b, root = env("solve", a_grounded=True)
    with pytest.raises(RuntimeError, match="A:1 is grounded"):
        run(at_point(srv, captured), ns)
    assert root.joints.count == 0 and a.component.sketches.items == []


def test_ground_to_parent_is_cleared_and_reported(srv, captured):
    ns, a, *_ = env("solve", a_to_parent=True)
    out = run(at_point(srv, captured), ns)
    assert a.isGroundToParent is False
    assert "grounded to its parent; freed it" in out and "CONFIRMED" in out


def test_origin_joints_use_the_same_checks(srv, captured):
    ns, *_ = env("unsolved")
    captured.clear()
    srv.rigid_joint("A", "B")
    assert "NOT SOLVED" in run(captured[0], ns)
    ns, *_ = env("solve")
    captured.clear()
    srv.revolute_joint("A", "B", axis="z")
    assert "CONFIRMED: revolute joint" in run(captured[0], ns)


# ---------------------------------------------------------------- ground_component

def ground_ns(occ):
    return {"_require_design": lambda: SimpleNamespace(rootComponent=None),
            "find_occurrence": lambda name: occ}


def test_ungrounding_clears_both_flags(srv, captured):
    occ = Occurrence("P:1", grounded=False, to_parent=True)
    srv.ground_component("P", grounded=False)
    out = run(captured[0], ground_ns(occ))
    assert occ.isGroundToParent is False and occ.isGrounded is False
    assert "CONFIRMED: P:1 is free to move" in out


def test_state_that_does_not_stick_is_reported(srv, captured):
    class Stuck(Occurrence):
        def __setattr__(self, k, v):
            if k in ("isGrounded", "isGroundToParent") and hasattr(self, k):
                return                    # Fusion ignoring the write
            super().__setattr__(k, v)
    occ = Stuck("P:1", grounded=True)
    srv.ground_component("P", grounded=False)
    out = run(captured[0], ground_ns(occ))
    assert "*** NOT APPLIED" in out and "CONFIRMED" not in out
