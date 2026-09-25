"""tapped_hole sizing. Every case marked 'was' used to get a 2.5 mm drill
silently (or the coarse drill for a fine pitch)."""
import pytest


@pytest.mark.parametrize("size,drill,canonical", [
    ("M3", 2.5, "M3x0.5"),
    ("m4", 3.3, "M4x0.7"),            # was 2.5 (lowercase)
    ("M4X0.7", 3.3, "M4x0.7"),        # was 2.5 (uppercase X)
    ("M4 x 0.7", 3.3, "M4x0.7"),
    ("M4×0.7", 3.3, "M4x0.7"),        # multiplication sign
    ("M8", 6.8, "M8x1.25"),
    ("M8x1", 7.0, "M8x1"),            # was 6.8 (fine pitch)
    ("M10x1.25", 8.75, "M10x1.25"),
    ("M20", 17.5, "M20x2.5"),         # was 2.5 (not in the old table)
    ("M1.6", 1.25, "M1.6x0.35"),
    ("#2-56", 1.78, "#2-56"),         # was 2.5
    ("10-32", 4.1, "#10-32"),
    ("#10-24", 3.9, "#10-24"),
    ("1/4-20", 5.1, "1/4-20"),
    ("1/4-20 UNC", 5.1, "1/4-20"),
    ("3/8-16", 7.94, "3/8-16"),       # was 2.5
])
def test_drill_sizes(srv, size, drill, canonical):
    got = srv._tap_drill(size)
    assert got[0] == pytest.approx(drill)
    assert got[1] == canonical


@pytest.mark.parametrize("size", ["M17", "M3x2", "#2-99", "bogus", "M"])
def test_unknown_sizes_are_refused_not_guessed(srv, size):
    with pytest.raises(ValueError):
        srv._tap_drill(size)


def test_tool_cuts_nothing_for_an_unknown_size(srv, captured):
    res = srv.tapped_hole("M17", 0, 0, 5)
    assert res.startswith("ERROR") and "Nothing was cut" in res
    assert captured == []


def test_explicit_drill_overrides(srv, captured):
    srv.tapped_hole("M17", 0, 0, 5, tap_drill_mm=15.5)
    assert "dia 15.5mm" in captured[0]


def test_generated_code_uses_the_parsed_size(srv, captured):
    srv.tapped_hole("m4", 0, 0, 5)
    assert "dia 3.3mm" in captured[0] and "'M4x0.7'" in captured[0]
    assert "participantBodies = [body]" in captured[0]
