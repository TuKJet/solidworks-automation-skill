from scripts.drawing_pdf_spacing import plan_pdf_dimension_spacing


def _dimension(name, type2, value, x, y, box, *, hole=False, visible="visible", key=None):
    return {
        "name": name,
        "semantic_view": "front",
        "type2": type2,
        "visibility": visible,
        "is_hole_callout": hole,
        "identity": {"canonical_key": key or name, "source": "part.SLDPRT", "configuration": "Default"},
        "system_value": {"type": type2, "unit": "m", "value": value},
        "box": box,
        "box_evidence": {"position_m": [x, y]},
    }


def _structure(dimensions):
    return {
        "sheet_size": {"width_m": .420, "height_m": .297},
        "title_block_box": {"left": .218, "bottom": .022, "right": .398, "top": .077},
        "views": [{"semantic_view": "front", "box": {"left": .222, "bottom": .218, "right": .280, "top": .252}}],
        "dimensions": dimensions,
    }


def test_confirmed_collision_moves_radius_and_preserves_value_and_identity():
    first = _dimension("D8", 2, .0006, .284, .222, {"left": .265, "bottom": .206, "right": .286, "top": .227})
    second = _dimension("D9", 5, .0002, .2785, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219})
    rendering = {"matched": [
        {"dimension_index": 0, "dimension_name": "D8", "box_m": {"left": .265, "bottom": .206, "right": .286, "top": .227}},
        {"dimension_index": 1, "dimension_name": "D9", "box_m": {"left": .267, "bottom": .197, "right": .290, "top": .219}},
    ], "collisions": [{"first": "D8", "second": "D9"}]}
    result = plan_pdf_dimension_spacing(_structure([first, second]), rendering)
    assert result["status"] == "pass"
    move = result["moves"][0]
    assert move["index"] == 1
    assert move["identity"] == second["identity"]
    assert move["expected_value"] == .0002
    assert move["to"] != move["from"]


def test_no_confirmed_collision_does_not_move_estimated_overlap():
    first = _dimension("D1", 2, .01, .24, .20, {"left": .22, "bottom": .18, "right": .27, "top": .20})
    second = _dimension("D2", 5, .02, .25, .20, {"left": .24, "bottom": .19, "right": .29, "top": .21})
    result = plan_pdf_dimension_spacing(_structure([first, second]), {"matched": [], "collisions": []})
    assert result["moves"] == []
    assert result["unresolved"] == [{"reason": "no_confirmed_pdf_matches"}]


def test_hole_callout_and_unknown_dimension_are_not_movable():
    hole = _dimension("H1", 5, .008, .278, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219}, hole=True)
    unknown = _dimension("U1", 2, .008, .284, .222, {"left": .265, "bottom": .206, "right": .286, "top": .227}, visible="unknown")
    rendering = {"matched": [
        {"dimension_index": 0, "dimension_name": "H1", "box_m": hole["box"]},
        {"dimension_index": 1, "dimension_name": "U1", "box_m": unknown["box"]},
    ], "collisions": [{"first": "H1", "second": "U1"}]}
    result = plan_pdf_dimension_spacing(_structure([hole, unknown]), rendering)
    assert result["moves"] == []
    assert result["status"] == "blocked"


def test_missing_hole_flag_and_unknown_type_are_not_movable_or_crashing():
    unknown_hole = _dimension("H0", 5, .008, .278, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219})
    unknown_hole["is_hole_callout"] = None
    unknown_type = _dimension("U0", None, .008, .284, .222, {"left": .265, "bottom": .206, "right": .286, "top": .227})
    rendering = {"matched": [
        {"dimension_index": 0, "dimension_name": "H0", "box_m": unknown_hole["box"]},
        {"dimension_index": 1, "dimension_name": "U0", "box_m": unknown_type["box"]},
    ], "collisions": [{"first": "H0", "second": "U0"}]}
    result = plan_pdf_dimension_spacing(_structure([unknown_hole, unknown_type]), rendering)
    assert result["moves"] == []
    assert result["status"] == "blocked"


def test_identity_mismatch_is_rejected_before_move():
    dimension = _dimension("D1", 5, .008, .278, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219})
    result = plan_pdf_dimension_spacing(_structure([dimension]), {"matched": [
        {"dimension_index": 0, "dimension_name": "OTHER", "box_m": dimension["box"]}],
        "collisions": [{"first": "OTHER", "second": "OTHER"}]})
    assert result["moves"] == []
    assert result["unresolved"][0]["reason"] == "matched_identity_mismatch"


def test_missing_sheet_bounds_and_no_safe_candidate_are_explicit():
    dimension = _dimension("D1", 5, .008, .278, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219})
    result = plan_pdf_dimension_spacing({"dimensions": [dimension]}, {"matched": [], "collisions": []})
    assert result["unresolved"] == [{"reason": "sheet_bounds_missing"}]


def test_hidden_dimension_is_not_an_obstacle_and_third_visible_obstacle_blocks():
    first = _dimension("D8", 2, .0006, .284, .222, {"left": .265, "bottom": .206, "right": .286, "top": .227})
    second = _dimension("D9", 5, .0002, .2785, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219})
    hidden = _dimension("HIDDEN", 2, .01, .30, .21, {"left": .278, "bottom": .19, "right": .34, "top": .23}, visible="hidden")
    obstacle = _dimension("D3", 2, .01, .31, .21, {"left": .278, "bottom": .19, "right": .34, "top": .23})
    rendering = {"matched": [
        {"dimension_index": 0, "dimension_name": "D8", "box_m": first["box"]},
        {"dimension_index": 1, "dimension_name": "D9", "box_m": second["box"]},
    ], "collisions": [{"first": "D8", "second": "D9"}]}
    result = plan_pdf_dimension_spacing(_structure([first, second, hidden, obstacle]), rendering)
    assert result["moves"] == []
    assert result["status"] == "blocked"
    assert result["unresolved"][-1]["reason"] == "no_safe_native_move_candidate"


def test_title_block_box_is_used_and_cross_page_or_nonintersecting_collision_is_rejected():
    first = _dimension("D8", 2, .0006, .284, .222, {"left": .265, "bottom": .206, "right": .286, "top": .227})
    second = _dimension("D9", 5, .0002, .2785, .208, {"left": .267, "bottom": .197, "right": .290, "top": .219})
    structure = _structure([first, second])
    structure.pop("title_block_box")
    structure["title_block"] = {"box": {"left": .280, "bottom": .16, "right": .398, "top": .24}}
    rendering = {"matched": [
        {"dimension_index": 0, "dimension_name": "D8", "page": 1, "box_m": first["box"]},
        {"dimension_index": 1, "dimension_name": "D9", "page": 2, "box_m": second["box"]},
    ], "collisions": [{"first": "D8", "second": "D9"}]}
    cross_page = plan_pdf_dimension_spacing(structure, rendering)
    assert cross_page["moves"] == []
    assert cross_page["unresolved"][0]["reason"] == "collision_cross_page"
    rendering["matched"][1]["page"] = 1
    rendering["collisions"][0]["first"] = "D8"
    rendering["collisions"][0]["second"] = "D9"
    # Move boxes apart while retaining the collision record; reject the stale report.
    rendering["matched"][1]["box_m"] = {"left": .10, "bottom": .10, "right": .12, "top": .12}
    apart = plan_pdf_dimension_spacing(structure, rendering)
    assert apart["moves"] == []
    assert apart["unresolved"][0]["reason"] == "collision_boxes_do_not_intersect"
