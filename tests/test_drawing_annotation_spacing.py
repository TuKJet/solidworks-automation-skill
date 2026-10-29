from scripts.drawing_annotation_spacing import plan_profile_diameter_spacing


def structure(*, profile_x=.2205, circle_x=.22262):
    return {
        "views": [
            {"semantic_view": "front", "box": {"left": .22456, "bottom": .18806, "right": .27344, "top": .25994}},
            {"semantic_view": "circular", "box": {"left": .13468, "bottom": .18806, "right": .20656, "top": .25994}},
        ],
        "dimensions": [
            {"semantic_view": "front", "type2": 6, "visibility": "visible", "box_evidence": {"position_m": [profile_x, .224]}},
            {"semantic_view": "circular", "type2": 6, "visibility": "visible", "box_evidence": {"position_m": [circle_x, .224]}},
        ],
    }


def test_profile_anchor_moves_right_of_own_view_when_cross_view_anchor_is_near():
    result = plan_profile_diameter_spacing(structure(), {"circular"})
    assert result["moves"] == [{"index": 0, "from": [.2205, .224], "to": [.28544, .224], "reason": "cross_view_anchor_proximity"}]
    assert result["unresolved"] == []


def test_circular_view_dimension_is_never_moved():
    result = plan_profile_diameter_spacing(structure(profile_x=.2205, circle_x=.2205), {"circular"})
    assert all(move["index"] != 1 for move in result["moves"])


def test_proposed_anchor_offsheet_is_unresolved_without_value_changes():
    result = plan_profile_diameter_spacing(structure(profile_x=.010, circle_x=.011), {"circular"}, sheet_width_m=.28)
    assert result["moves"] == []
    assert result["unresolved"]


def test_same_view_near_anchor_does_not_trigger_cross_view_move():
    value = structure()
    value["dimensions"].pop(1)  # Remove the cross-view anchor; this case is same-view only.
    value["dimensions"].append({"semantic_view": "front", "type2": 6, "visibility": "visible",
                                "box_evidence": {"position_m": [.221, .224]}})
    result = plan_profile_diameter_spacing(value, {"circular"})
    assert result["moves"] == []
