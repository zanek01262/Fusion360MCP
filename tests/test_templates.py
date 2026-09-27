"""Every tool builds a Python program that runs inside Fusion. None of that
code is seen by a linter, so compile every one of them here - with plausible
arguments - and check for patterns known to be wrong on Fusion's API."""
import inspect

import pytest

# Arguments for tools whose parameters have no defaults (or whose defaults
# skip an interesting branch).
ARGS = {
    "activate_document": {"name_or_index": "Untitled"},
    "run_fusion_code": {"code": "print(1)"},
    "run_fusion_code_safe": {"code": 's = """a\nb"""\nprint(s)'},
    "set_parameter": {"name": "w", "value_mm": 12.5},
    "create_box": {"length_mm": 10, "width_mm": 10, "height_mm": 10},
    "create_cylinder": {"diameter_mm": 10, "height_mm": 10},
    "drill_hole": {"diameter_mm": 3, "x_mm": 1, "y_mm": 1, "depth_mm": 5},
    "tapped_hole": {"thread_size": "M3", "x_mm": 1, "y_mm": 1, "depth_mm": 5,
                    "tap_drill_only": False},
    "hole_on_face": {"diameter_mm": 3, "u_mm": 0, "v_mm": 0, "depth_mm": 5},
    "measure_distance": {"which_a": "top", "which_b": "bottom"},
    "cross_section": {"at_mm": 3.0, "at_a_mm": 1.0},
    "create_component": {"name": "Lid"},
    "box_in_component": {"component_name": "Lid", "length_mm": 1,
                         "width_mm": 1, "height_mm": 1},
    "move_component": {"component_name": "Lid:1", "x_mm": 1, "y_mm": 2, "z_mm": 3},
    "copy_component": {"component_name": "Lid", "count": 2, "dx_mm": 5},
    "ground_component": {"component_name": "Lid"},
    "rigid_joint": {"component_a": "A", "component_b": "B"},
    "revolute_joint": {"component_a": "A", "component_b": "B"},
    "joint_at_point": {"component_a": "A", "ax_mm": 0, "ay_mm": 0, "az_mm": 0,
                       "component_b": "B", "bx_mm": 1, "by_mm": 1, "bz_mm": 1},
    "export_step": {"filepath": r"C:\tmp\a.step"},
    "export_stl": {"filepath": r"C:\tmp\a.stl"},
    "export_dxf": {"filepath": r"C:\tmp\a.dxf", "component_name": "Plate"},
    "save_document": {"name": "x"},
    "open_document": {"design_id": "abc"},
    "wrap_cut": {"face_tempids": "1,2"},
    "construction_plane_offset": {"offset_mm": 5, "base": "xz"},
    "cut_prisms": {"prisms": '[{"x":0,"y":0,"z":5,"w":4,"d":4,"h":20}]'},
    "fillet_edges_smart": {"radius_mm": 2, "edge_tempids": "1,2",
                           "component_name": "Lid"},
    "select_edges": {"face_pair": "Cone", "z_min_mm": 1.0},
    "fillet_preview": {"radius_mm": 1, "edge_tempids": "3,4",
                       "component_name": "Lid"},
    "force_delete_feature": {"feature_name": "Fillet1"},
    "respline": {"points_json": "[[0,0],[5,5],[10,0]]", "plane": "yz"},
    "fillet_edges": {"radius_mm": 1},
    "shell_body": {"thickness_mm": 2},
}
# talk to the listener directly or generate no Fusion code
NOT_CODEGEN = {"ping_fusion", "get_state", "run_async", "job_status", "heartbeat",
               "explain_fillet_error", "fusion_help", "set_verbosity", "run_batch"}


def _tools(srv):
    return sorted(
        name for name, fn in vars(srv).items()
        if callable(fn) and not name.startswith("_") and not inspect.isclass(fn)
        and getattr(fn, "__module__", "") == srv.__name__
        and name not in NOT_CODEGEN)


def _generate(srv, captured, name):
    captured.clear()
    try:
        getattr(srv, name)(**ARGS.get(name, {}))
    except RuntimeError:
        if name != "view_model":   # reads the screenshot back after _send
            raise
    assert captured, "%s generated no code" % name
    return list(captured)


def test_every_tool_is_covered(srv):
    assert len(_tools(srv)) >= 45


@pytest.mark.parametrize("name", _tools(__import__("fusion_mcp_server")))
def test_generated_code_compiles(srv, captured, name):
    for code in _generate(srv, captured, name):
        compile(code, "<%s>" % name, "exec")


@pytest.mark.parametrize("name", _tools(__import__("fusion_mcp_server")))
def test_no_known_bad_patterns(srv, captured, name):
    for code in _generate(srv, captured, name):
        # TimelineObject has no deleteMe - only its .entity does
        assert ".item(_i).deleteMe()" not in code
        assert "tl.item(i).deleteMe()" not in code
        # component lookups must go through find_component/find_occurrence,
        # which raise on a miss instead of falling back to the root
        assert "occ.component.name ==" not in code
        # API shapes found wrong in a live Fusion run (1.9.0): these methods
        # do not exist, and getCurvature returns THREE values
        for bad in ("getAllSizes", "getAllDesignations", "getAllClasses",
                    "okc, curv = ev.getCurvature",
                    # raw-coordinate construction points fail in parametric designs
                    "constructionPoints.createInput"):
            assert bad not in code, bad
