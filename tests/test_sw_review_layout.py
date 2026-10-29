"""Regression tests for bounded, evidence-aware drawing layout repair plans."""

from scripts.sw_review import build_layout_repair_plan
import pytest


def _structure(*, estimated=False):
    dimension = {
        "name": "D1",
        "view": "Front",
        "box": {"left": 0.12, "bottom": 0.15, "right": 0.16, "top": 0.17},
    }
    if estimated:
        dimension["box_source"] = "estimated"
    return {
        "sheet_size": {"width_m": 0.42, "height_m": 0.297},
        "views": [
            {"name": "Front", "projection_axis": "horizontal", "box": {"left": 0.10, "bottom": 0.12, "right": 0.18, "top": 0.22}},
            {"name": "Top", "projection_axis": "horizontal", "box": {"left": 0.17, "bottom": 0.18, "right": 0.30, "top": 0.27}},
        ],
        "dimensions": [dimension],
        "notes": [],
        "title_block": {"box": {"left": 0.30, "bottom": 0.01, "right": 0.41, "top": 0.07}},
    }


def test_layout_repair_plan_is_bounded_and_preserves_projection_axis():
    result = build_layout_repair_plan(_structure(), max_attempts=2)

    assert result["status"] == "review_required"
    assert result["attempts"] == 1
    assert result["stopped_reason"] == "bounded_plan_requires_com_verification"
    action = result["actions"][0]
    assert action["kind"] == "move_view"
    assert action["target"] == "Top"
    assert action["preserve_projection_axis"] == "horizontal"
    assert action["verify_after"] == ["DRAWING_VIEW_OVERLAP", "DRAWING_VIEW_TITLE_BLOCK_INTRUSION"]
    assert "delta_m" in action


def test_layout_repair_plan_never_treats_estimated_dimension_box_as_pass():
    result = build_layout_repair_plan(_structure(estimated=True))

    assert result["status"] == "review_required"
    assert result["actions"] == []
    assert result["stopped_reason"] == "estimated_text_box_requires_native_or_visual_review"
    assert result["manual_review_required"] is True


def test_layout_repair_plan_rejects_unbounded_attempts():
    try:
        build_layout_repair_plan(_structure(), max_attempts=0)
    except ValueError as error:
        assert "max_attempts" in str(error)
    else:
        raise AssertionError("expected max_attempts validation")


def test_layout_repair_plan_does_not_move_view_without_projection_axis():
    structure = _structure()
    structure["views"][1].pop("projection_axis")

    result = build_layout_repair_plan(structure)

    assert result["actions"] == []
    assert result["stopped_reason"] == "no_safe_view_move_derived"


def test_repair_stops_when_action_does_not_change_evidence():
    from scripts.sw_review import repair_drawing_layout
    calls = []
    result = repair_drawing_layout(lambda: _structure(), lambda action: calls.append(action) or True)
    assert result['stopped_reason'] == 'no_progress'
    assert len(calls) == 1


def test_sheet_boundary_includes_notes_and_dimensions():
    from scripts.sw_review import review_drawing_layout
    structure = _structure()
    structure['notes'] = [{'text': 'lost', 'box': {'left': .4, 'right': .45, 'bottom': .1, 'top': .12}}]
    assert any(f['code'] == 'DRAWING_OUTSIDE_SHEET' for f in review_drawing_layout(structure)['findings'])


@pytest.mark.parametrize('evidence_source', ['estimated', 'unavailable'])
def test_estimated_sheet_boundary_is_warning_not_blocking_failure(evidence_source):
    from scripts.sw_review import review_drawing_layout

    structure = _structure()
    structure['dimensions'][0].update({
        'box': {'left': 0.12, 'bottom': 0.15, 'right': 0.45, 'top': 0.17},
        'box_source': evidence_source,
        'box_confidence': 'low',
    })
    finding = next(f for f in review_drawing_layout(structure)['findings']
                   if f['code'] == 'DRAWING_OUTSIDE_SHEET')
    assert finding['severity'] == 'warning'
    assert finding['evidence_source'] == evidence_source
    assert finding['confidence'] == 'low'
    assert finding['confirmed_collision'] is False


def test_pdf_vector_sheet_boundary_remains_fail_with_its_confidence():
    from scripts.sw_review import review_drawing_layout

    structure = _structure()
    structure['dimensions'][0].update({
        'box': {'left': 0.12, 'bottom': 0.15, 'right': 0.45, 'top': 0.17},
        'box_source': 'pdf_vector_text',
        'box_confidence': 'medium',
    })
    finding = next(f for f in review_drawing_layout(structure)['findings']
                   if f['code'] == 'DRAWING_OUTSIDE_SHEET')
    assert finding['severity'] == 'fail'
    assert finding['evidence_source'] == 'pdf_vector_text'
    assert finding['confidence'] == 'medium'
    # PDF evidence is a rendered risk/failure but not a native COM collision.
    assert finding['confirmed_collision'] is False


def _native_repair_structure(*, overlap):
    top_box = {"left": 0.17, "bottom": 0.18, "right": 0.30, "top": 0.27} if overlap else {"left": 0.289, "bottom": 0.18, "right": 0.419, "top": 0.27}
    return {
        "status": "pass", "sheet_size": {"width_m": 0.42, "height_m": 0.297},
        "views": [
            {"sheet": "Sheet1", "name": "Front", "box": {"left": 0.10, "bottom": 0.12, "right": 0.18, "top": 0.22},
             "projection_axis": None, "parent_relation_verified": False, "child_linkage_verified": True, "child_view_count": 1},
            {"sheet": "Sheet1", "name": "Top", "box": top_box,
             "projection_axis": "horizontal", "parent_relation_verified": True, "child_linkage_verified": True, "child_view_count": 0},
            {"sheet": "Sheet1", "name": "Right", "box": {"left": 0.02, "bottom": 0.12, "right": 0.08, "top": 0.18},
             "projection_axis": None, "parent_relation_verified": False, "child_linkage_verified": True, "child_view_count": 0},
        ],
        "dimensions": [], "notes": [], "title_block": {"box": {"left": 0.30, "bottom": 0.01, "right": 0.41, "top": 0.07}},
    }


def test_native_layout_repair_exports_fresh_evidence_moves_then_reinspects(monkeypatch, tmp_path):
    from scripts import sw_drawing

    workflow = sw_drawing._MODULE
    exports, structures = [], [_native_repair_structure(overlap=True), _native_repair_structure(overlap=False)]

    class View:
        ScaleRatio = (1.0, 1.0)
        UseSheetScale = False
        UseParentScale = False

        def __init__(self, name, position, base=None):
            self.Name, self.Position, self.base = name, position, base

        def GetBaseView(self):
            return self.base

        def SetViewPosition(self, value, _use_sheet_scale):
            self.Position = tuple(getattr(value, "value", value))
            return True

    front = View("Front", (0.14, 0.17))
    top = View("Top", (0.235, 0.17), front)
    right = View("Right", (0.05, 0.15))

    class Sheet:
        def GetViews(self):
            return [front, top, right]

    class Drawing:
        def GetSheetNames(self):
            return ["Sheet1"]

        def GetSheet(self, _name):
            return Sheet()

        def EditRebuild3(self):
            self.rebuilt = getattr(self, "rebuilt", 0) + 1
            return True

    drawing = Drawing()
    monkeypatch.setattr(workflow, "export_sheet_to_pdf", lambda _model, path, **_kwargs: exports.append(path) or True)
    monkeypatch.setattr(workflow, "inspect_drawing_structure", lambda _model: structures.pop(0))

    result = workflow.repair_native_drawing_layout(drawing, tmp_path / "fresh.pdf", max_attempts=2)

    assert result["status"] == "pass"
    assert result["fresh_pdf_exports"] == 2
    assert len(exports) == 2
    assert drawing.rebuilt == 1
    assert top.Position[0] > 0.235
    assert result["manual_review_required"] is True


def test_native_layout_repair_blocks_when_position_readback_does_not_move(monkeypatch, tmp_path):
    from scripts import sw_drawing

    workflow = sw_drawing._MODULE

    class View:
        ScaleRatio = (1.0, 1.0)
        UseSheetScale = False
        UseParentScale = False

        def __init__(self, name, position, base=None):
            self.Name, self.Position, self.base = name, position, base

        def GetBaseView(self):
            return self.base

        def SetViewPosition(self, _value, _use_sheet_scale):
            return True  # COM call reports success but position must be read back.

    front = View("Front", (0.14, 0.17))
    top = View("Top", (0.235, 0.17), front)
    right = View("Right", (0.05, 0.15))

    class Sheet:
        def GetViews(self):
            return [front, top, right]

    class Drawing:
        def GetSheetNames(self):
            return ["Sheet1"]

        def GetSheet(self, _name):
            return Sheet()

        def EditRebuild3(self):
            raise AssertionError("must not rebuild after failed position readback")

    monkeypatch.setattr(workflow, "export_sheet_to_pdf", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workflow, "inspect_drawing_structure", lambda _model: _native_repair_structure(overlap=True))

    result = workflow.repair_native_drawing_layout(Drawing(), tmp_path / "fresh.pdf", max_attempts=2)

    assert result["status"] == "blocked"
    assert result["stopped_reason"] == "action_failed"
    assert result["history"][0]["result"]["error_code"] == "DRAWING_LAYOUT_NATIVE_MOVE_FAILED"
