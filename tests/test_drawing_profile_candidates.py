from scripts.drawing_profile_candidates import plan_profile_dimensions
import pytest
import math


def view(vertices=None, arcs=None, loops=None, matrix=None):
    return {
        "name": "front", "semantic_view": "front", "scale": 10.0,
        "matrix": matrix or [1, 0, 0, 0, 1, 0, 0, 0, 1],
        "box": {"left": 0, "bottom": 0, "right": 0.1, "top": 0.1},
        "vertices": vertices or [], "arcs": arcs or [], "inner_loops": loops or [],
    }


def vertex(i, x, y, z=0):
    return {"id": i, "point_model_m": [x, y, z], "point_sheet_m": [x * 10, y * 10, z * 10]}


def test_overall_spans_are_real_and_deduped():
    result = plan_profile_dimensions([view([vertex("a", 0, 0), vertex("b", .02, .01), vertex("c", .02, .01)])])
    values = {(x["kind"], round(x["expected_m"], 6)) for x in result["candidates"] if x["source_geometry"]["role"] == "profile_vertex_span"}
    assert values == {("horizontal", .02), ("vertical", .01)}
    assert all(x["source_geometry"]["vertex_ids"] for x in result["candidates"])
    assert all(x["type"] == 2 for x in result["candidates"] if x["kind"] in ("horizontal", "vertical"))


def test_inner_loop_size_and_location_keep_evidence():
    loop = {"id": "L1", "vertices": [vertex("p1", .005, .004), vertex("p2", .012, .004), vertex("p3", .012, .009), vertex("p4", .005, .009)]}
    result = plan_profile_dimensions([view([vertex("o1", 0, 0), vertex("o2", .02, .02)] + loop.get("vertices"), loops=[loop])])
    inner = [x for x in result["candidates"] if x.get("loop_id") == "L1"]
    assert {x["source_geometry"]["role"] for x in inner} == {"inner_loop_size", "inner_loop_location"}
    assert any(x["source_geometry"]["role"] == "inner_loop_location" and x["expected_m"] > 0 for x in inner)


def test_partial_face_on_arc_dedupes_and_rejects_invalid():
    arcs = [
        {"id": "a", "center_sheet_m": [0, 0, 0], "radius_m": .002, "span_rad": 1.0, "face_on": True},
        {"id": "b", "center_sheet_m": [0, 0, 0], "radius_m": .002, "span_rad": 2.0, "face_on": True},
        {"id": "full", "radius_m": .003, "span_rad": 6.283185307, "face_on": True},
        {"id": "oblique", "radius_m": .004, "span_rad": 1.0, "face_on": False},
    ]
    result = plan_profile_dimensions([view(arcs=arcs)])
    radii = [x["radius_m"] for x in result["candidates"]]
    assert radii == [.002]
    assert result["candidates"][0]["representative_count"] == 2
    assert {x["reason"] for x in result["unresolved"]} >= {"invalid_or_complete_arc", "arc_not_face_on"}


def test_budget_overflow_is_explicit_and_not_silent_truncation():
    arcs = [{"id": str(i), "radius_m": .001 + i * .001, "span_rad": 1.0, "face_on": True} for i in range(4)]
    result = plan_profile_dimensions([view(arcs=arcs)], max_dimensions=2)
    assert result["status"] == "blocked"
    assert result["complete"] is False
    assert len(result["candidates"]) == 4
    assert any(x["reason"] == "dimension_budget_exceeded" for x in result["unresolved"])


def test_oblique_matrix_and_missing_coordinates_never_guess():
    result = plan_profile_dimensions([view([{"id": "bad"}], matrix=[.707, -.707, 0, .707, .707, 0, 0, 0, 1])])
    assert result["candidates"] == []
    assert {x["reason"] for x in result["unresolved"]} >= {"missing_or_oblique_matrix"}


def test_same_linear_orientation_and_model_axis_dedup_across_views():
    front = view([vertex("a", 0, 0), vertex("b", .02, 0)])
    top = view([vertex("a", 0, 0), vertex("b", .02, 0)])
    top["name"] = "top"
    result = plan_profile_dimensions([front, top])
    spans = [item for item in result["candidates"] if item["source_geometry"]["role"] == "profile_vertex_span"]
    assert len(spans) == 1


def test_rotated_top_view_uses_model_z_for_thickness():
    # Top view sheet-Y is model-Z; the model Y coordinate is constant.
    top_matrix = [1, 0, 0, 0, 0, 1, 0, -1, 0]
    top_vertices = [
        {"id": "a", "point_model_m": [0, 0, 0], "point_sheet_m": [0, 0, 0]},
        {"id": "b", "point_model_m": [.02, 0, .0012], "point_sheet_m": [.2, .012, 0]},
    ]
    result = plan_profile_dimensions([view(top_vertices, matrix=top_matrix)])
    vertical = [x for x in result["candidates"] if x["kind"] == "vertical"]
    assert len(vertical) == 1
    assert vertical[0]["expected_m"] == pytest.approx(.0012)
    assert vertical[0]["model_axis"] == 2


def test_main_profile_spans_once_and_side_view_only_supplies_thickness():
    front = view([
        {"id": "f0", "point_model_m": [0, 0, 0], "point_sheet_m": [0, 0, 0]},
        {"id": "f1", "point_model_m": [.010, .005, 0], "point_sheet_m": [.10, .05, 0]},
    ], loops=[{"id": "main", "front_view_main_loop": True,
               "vertices": [vertex("p1", .002, .001), vertex("p2", .004, .001), vertex("p3", .004, .003)]}])
    top = view([
        {"id": "t0", "point_model_m": [0, 0, 0], "point_sheet_m": [0, 0, 0]},
        {"id": "t1", "point_model_m": [.002, 0, .0012], "point_sheet_m": [.02, .012, 0]},
    ], matrix=[1, 0, 0, 0, 0, 1, 0, -1, 0])
    top["name"] = "top"
    result = plan_profile_dimensions([front, top])
    spans = [x for x in result["candidates"] if x["source_geometry"]["role"] == "profile_vertex_span"]
    assert {x["model_axis"] for x in spans} == {0, 1, 2}
    assert sum(x["model_axis"] == 0 for x in spans) == 1
    assert any(x["model_axis"] == 2 and x["expected_m"] == pytest.approx(.0012) for x in spans)


def test_bounded_partial_arc_uses_only_in_span_cardinal_extrema():
    arc = {
        "id": "arc-90", "center_sheet_m": [0, 0, 0],
        "start_sheet_m": [.10, 0, 0], "end_sheet_m": [0, .10, 0],
        "radius_m": .01, "span_rad": 3.141592653589793 / 2, "face_on": True,
    }
    # The arc covers 0..90 degrees, so its bounded extrema are x=.10 and
    # y=.10; it must not invent the opposite half of a full diameter.
    result = plan_profile_dimensions([view([
        {"id": "v", "point_model_m": [-.002, 0, 0], "point_sheet_m": [-.02, 0, 0]},
    ], arcs=[arc])])
    spans = [x for x in result["candidates"] if x["source_geometry"]["role"] == "profile_vertex_span"]
    horizontal = next(x for x in spans if x["kind"] == "horizontal")
    assert horizontal["expected_m"] == pytest.approx(.012)
    assert horizontal["expected_m"] < .02
    assert horizontal["source_geometry"]["entity_ids"] == ["v", "arc-90"]
    assert horizontal["source_geometry"]["arc_end_conditions"] == [0, 3]


def test_bottom_r6_arc_includes_270_not_180_cardinal():
    start_angle = math.radians(-138.59)
    end_angle = math.radians(-41.41)
    radius_m = .01
    scale = 10.0
    arc = {
        "id": "r6-bottom", "center_sheet_m": [0, 0, 0],
        "start_sheet_m": [radius_m * scale * math.cos(start_angle), radius_m * scale * math.sin(start_angle), 0],
        "end_sheet_m": [radius_m * scale * math.cos(end_angle), radius_m * scale * math.sin(end_angle), 0],
        "radius_m": radius_m, "span_rad": math.radians(97.18), "face_on": True,
    }
    result = plan_profile_dimensions([view([{
        "id": "endpoint", "point_model_m": [radius_m * math.cos(start_angle), radius_m * math.sin(start_angle), 0],
        "point_sheet_m": arc["start_sheet_m"],
    }], arcs=[arc])])
    spans = [x for x in result["candidates"] if x["source_geometry"]["role"] == "profile_vertex_span"]
    vertical = next(x for x in spans if x["kind"] == "vertical")
    horizontal = next(x for x in spans if x["kind"] == "horizontal")
    assert vertical["expected_m"] > 0
    assert horizontal["expected_m"] < 2 * radius_m

