"""run_fusion_code_safe: execute the generated wrapper against a fake timeline.

The fake mirrors the Fusion behaviour that caused the original bug: a timeline
item (TimelineObject) has NO deleteMe - only its .entity does. The old wrapper
called item.deleteMe(), swallowed the AttributeError, and printed ROLLED BACK
anyway."""
import contextlib
import io

import pytest


class FakeEntity:
    def __init__(self, tl, fail=False):
        self.tl, self.fail, self.item = tl, fail, None

    def deleteMe(self):
        if self.fail:
            raise RuntimeError("cannot delete")
        self.tl.items.remove(self.item)
        if self.tl.markerPosition > len(self.tl.items):
            self.tl.markerPosition = len(self.tl.items)


class FakeItem:            # like TimelineObject: deliberately no deleteMe
    def __init__(self, entity):
        self.entity = entity


class FakeTimeline:
    def __init__(self, n=0, marker=None):
        self.items = []
        self.markerPosition = 0
        for _ in range(n):
            self.add()
        if marker is not None:
            self.markerPosition = marker

    @property
    def count(self):
        return len(self.items)

    def item(self, i):
        return self.items[i]

    def add(self, fail=False):
        """New features are inserted AT the marker, as in Fusion."""
        ent = FakeEntity(self, fail)
        it = FakeItem(ent)
        ent.item = it
        self.items.insert(self.markerPosition, it)
        self.markerPosition += 1
        return it


class FakeDesign:
    def __init__(self, tl):
        self.timeline = tl


def run_safe(srv, captured, user_code, tl):
    captured.clear()
    srv.run_fusion_code_safe(user_code)
    ns = {"_require_design": lambda: FakeDesign(tl), "tl": tl}
    out = io.StringIO()
    err = None
    with contextlib.redirect_stdout(out):
        try:
            exec(compile(captured[0], "<safe>", "exec"), ns)
        except Exception as e:     # the wrapper re-raises the user's error
            err = e
    return out.getvalue(), err


def test_rolls_back_features_created_by_the_call(srv, captured):
    tl = FakeTimeline(n=2)
    out, err = run_safe(srv, captured, "tl.add()\ntl.add()\nraise ValueError('boom')", tl)
    assert err is not None and "boom" in str(err)
    assert "ROLLED BACK" in out and "removed 2 feature(s)" in out
    assert tl.count == 2


def test_reports_incomplete_rollback_instead_of_claiming_success(srv, captured):
    tl = FakeTimeline(n=1)
    out, err = run_safe(srv, captured,
                        "tl.add()\ntl.add(fail=True)\nraise ValueError('boom')", tl)
    assert "ROLLED BACK" not in out
    assert "ROLLBACK INCOMPLETE: 1 of 2" in out
    assert tl.count == 2      # the undeletable one is still there, and we said so


def test_keeps_features_that_were_already_past_a_rolled_back_marker(srv, captured):
    tl = FakeTimeline(n=3, marker=1)          # items 1 and 2 are "rolled back"
    before = list(tl.items)
    out, _ = run_safe(srv, captured, "tl.add()\nraise ValueError('boom')", tl)
    assert "ROLLED BACK" in out
    assert tl.items == before                 # only the call's own feature went


def test_error_before_any_feature(srv, captured):
    tl = FakeTimeline(n=2)
    out, err = run_safe(srv, captured, "raise ValueError('early')", tl)
    assert "nothing to roll back" in out and err is not None
    assert tl.count == 2


def test_direct_modelling_document_says_there_is_no_rollback(srv, captured):
    captured.clear()
    srv.run_fusion_code_safe("raise ValueError('x')")

    class NoTimeline:
        @property
        def timeline(self):
            raise RuntimeError("direct modelling")

    ns = {"_require_design": lambda: NoTimeline()}
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(Exception):
        exec(compile(captured[0], "<safe>", "exec"), ns)
    assert "NO ROLLBACK" in out.getvalue()


def test_success_commits_and_preserves_multiline_strings(srv, captured):
    tl = FakeTimeline()
    out, err = run_safe(srv, captured, 's = """line1\nline2"""\nprint(repr(s))', tl)
    assert err is None and "[safe] committed" in out
    # the old wrapper re-indented this into 'line1\n    line2'
    assert repr("line1\nline2") in out


def test_traceback_line_numbers_are_the_users(srv, captured):
    tl = FakeTimeline()
    _, err = run_safe(srv, captured, "x = 1\ny = 2\nraise ValueError('line three')", tl)
    assert 'File "<mcp>", line 3' in str(err)
