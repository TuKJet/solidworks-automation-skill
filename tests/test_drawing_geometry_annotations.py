import math
from types import SimpleNamespace

import pytest

from scripts import drawing_geometry_annotations as geometry
from scripts.drawing_geometry_annotations import complete_circle


class MatrixTransform:
    def __init__(self, values=None):
        self.ArrayData = values or (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


class MathUtility:
    pass


class CircleCurve:
    IsCircle = True
    CircleParams = (0.1, 0.2, 0.0, 0.0, 0.0, 1.0, 0.005)


class CircleEdge:
    def __init__(self, dimension=None):
        self.dimension = dimension

    def GetCurve(self):
        return CircleCurve()

    def GetCurveParams2(self):
        return (0.005, 0.0, 0.0, 0.005, 0.0, 0.0, 0.0, 2 * math.pi, 0.0, 0.0, 0.0)

    def Select4(self, *_args):
        return True


class CircleView:
    Name = "Front"
    def __init__(self, dimensions=None, transform=None):
        self.dimensions = list(dimensions or [])
        self.edge = CircleEdge()
        self.ModelToViewTransform = transform or MatrixTransform()

    def GetVisibleComponents(self):
        return []

    def GetVisibleEntities2(self, *_args):
        return [self.edge]

    def GetOutline(self):
        return (0.05, 0.05, 0.15, 0.15)

    def GetDisplayDimensions(self):
        return list(self.dimensions)


class FakeSheet:
    def __init__(self, view):
        self.view = view

    def GetViews(self):
        return [self.view]


class FakeDrawing:
    def __init__(self, view):
        self.view = view
        self.sheet = FakeSheet(view)
        self.added = []
        self.source_writes = 0
        self.SelectionManager = SimpleNamespace(CreateSelectData=lambda: SimpleNamespace())

    def GetCurrentSheet(self):
        return self.sheet

    def GetSheetNames(self):
        return ["Sheet1"]

    def GetSheet(self, _name):
        return self.sheet

    def GraphicsRedraw2(self):
        return True

    def ClearSelection2(self, _all):
        return True

    def ActivateView(self, _name):
        return True

    def AddDiameterDimension2(self, *_args):
        dimension = self.added.pop(0) if self.added else None
        if dimension is not None:
            self.view.dimensions.append(dimension)
        return dimension


class FakeApp:
    GetMathUtility = lambda self: MathUtility()


class FakeWorkflow:
    @staticmethod
    def _safe_member(obj, name, *args, default=None):
        value = getattr(obj, name, default)
        return value(*args) if callable(value) else value

    @staticmethod
    def _map_standard_views_from_sheet_views(views):
        return {"front": views[0]}, [], "fake"

    @staticmethod
    def _view_display_dimensions(view):
        return view.GetDisplayDimensions()

    @staticmethod
    def _display_dimension_model_dimension(dimension):
        return dimension.model

    @staticmethod
    def _dimension_identity(_view, dimension, model):
        return {"source": "part.sldprt", "configuration": "Default", "canonical_key": model.FullName}

    @staticmethod
    def _dimension_system_value(dimension, _model):
        return {"type": dimension.type2, "unit": "mm", "value": dimension.value}

    @staticmethod
    def inspect_drawing_structure(drawing):
        dimensions = []
        for index, dimension in enumerate(drawing.view.dimensions):
            dimensions.append({
                "identity": {"source": "part.sldprt", "configuration": "Default", "canonical_key": dimension.model.FullName},
                "type2": dimension.type2, "system_value": {"type": dimension.type2, "unit": "mm", "value": dimension.value},
                "semantic_view": "front", "view": "Front", "visibility": "visible" if dimension.annotation.Visible == 1 else "hidden",
                "visible": dimension.annotation.Visible == 1,
                "native_text": {"prefix": "", "suffix": "", "tolerance": "±0.1"}, "name": f"D{index}",
                "is_hole_callout": False, "hole_callout_variables": [],
            })
        return {"dimensions": dimensions}

    @staticmethod
    def _normalise_box(box):
        return {"left": box[0], "bottom": box[1], "right": box[2], "top": box[3]}

    @staticmethod
    def auto_arrange_drawing_dimensions(*_args, **_kwargs):
        return {"status": "pass"}

    @staticmethod
    def _set_annotation_position(annotation, x, y, z):
        annotation.Position = (x, y, z)
        return True

    @staticmethod
    def _annotation_position(annotation):
        return list(annotation.Position) if getattr(annotation, "Position", None) is not None else None


class FakeDimension:
    def __init__(self, key, *, type2=6, value=10.0, visible=1):
        self.model = SimpleNamespace(FullName=key, Tolerance=SimpleNamespace(Type=1))
        self.type2 = type2
        self.value = value
        self.annotation = SimpleNamespace(Visible=visible, IsDangling=False, GetAttachedEntityCount3=lambda: 1)

    def GetAnnotation(self):
        return self.annotation

    def GetText(self, index):
        return "" if index in (1, 2) else "10"


def test_complete_circle_requires_full_period_closed_endpoints_and_positive_radius():
    params=[0,0,0,0,0,1,.005]
    full=[.005,0,0,.005,0,0,0,2*math.pi,0,0,0]
    assert complete_circle(params,full)
    arc=full.copy();arc[7]=math.pi
    assert not complete_circle(params,arc)
    open_edge=full.copy();open_edge[3]=-.005
    assert not complete_circle(params,open_edge)
    assert not complete_circle(params[:-1]+[0],full)
    assert not complete_circle(params[:-1]+[float('nan')],full)


def test_collect_visible_circle_reports_diameter_from_native_radius(monkeypatch):
    monkeypatch.setattr(geometry, "_resolve_sw_constant", lambda *_args: 1)
    result, handles = geometry.collect_visible_circles(FakeDrawing(CircleView()), FakeApp(), FakeWorkflow())
    assert result["status"] == "pass"
    assert result["circles"][0]["radius_m"] == 0.005
    assert result["circles"][0]["diameter_mm"] == 10.0
    assert handles[0][0]["face_on"] is True


def test_collect_circle_applies_native_rotation_scale_and_translation(monkeypatch):
    monkeypatch.setattr(geometry, "_resolve_sw_constant", lambda *_args: 1)
    transform = MatrixTransform((0, 1, 0, -1, 0, 0, 0, 0, 1, 0.3, -0.1, 0.05, 2, 0, 0, 0))
    result, _ = geometry.collect_visible_circles(FakeDrawing(CircleView(transform=transform)), FakeApp(), FakeWorkflow())
    assert result["status"] == "pass"
    assert result["circles"][0]["center_sheet_m"] == pytest.approx([-0.1, 0.1, 0.05])


def test_repair_blocks_native_diameter_with_wrong_return_value(monkeypatch):
    monkeypatch.setattr(geometry, "_resolve_sw_constant", lambda name, *_args: 1)
    view = CircleView()
    drawing = FakeDrawing(view)
    drawing.added = [FakeDimension("new", value=9.0)]
    with pytest.raises(geometry.AnnotationRepairError, match="does not equal"):
        geometry.repair_generated_annotations(drawing, FakeApp(), FakeWorkflow())
    assert drawing.source_writes == 0


def test_repair_hides_only_duplicate_and_keeps_model_inventory(monkeypatch):
    monkeypatch.setattr(geometry, "_resolve_sw_constant", lambda name, *_args: 2 if name == "swAnnotationHidden" else 1)
    first = FakeDimension("D1", value=0.01, visible=1)
    second = FakeDimension("D1", value=0.01, visible=1)
    view = CircleView([first, second])
    drawing = FakeDrawing(view)
    original_snapshot = geometry._dimension_snapshot

    def snapshot_with_native_text(*args, **kwargs):
        structure, objects = original_snapshot(*args, **kwargs)
        for item in structure["dimensions"]:
            item["native_text"].update({"full": "10", "above": "", "below": ""})
        return structure, objects

    monkeypatch.setattr(geometry, "_dimension_snapshot", snapshot_with_native_text)
    result = geometry.repair_generated_annotations(drawing, FakeApp(), FakeWorkflow())
    assert result["status"] in {"pass", "review_required"}
    assert result["stored_before"] == result["stored_after"] == 2
    assert result["duplicates"]["hidden"] == [{"index": 1, "keep_index": 0, "canonical_key": "D1"}]
    assert first.annotation.Visible == 1
    assert second.annotation.Visible == 2


def test_repair_blocks_when_spacing_position_readback_does_not_match(monkeypatch):
    monkeypatch.setattr(geometry, "_resolve_sw_constant", lambda name, *_args: 2 if name == "swAnnotationHidden" else 1)
    view = CircleView([FakeDimension("D1", value=.01)])
    drawing = FakeDrawing(view)
    monkeypatch.setattr(geometry, "plan_profile_diameter_spacing", lambda *_args, **_kwargs: {
        "moves": [{"index": 0, "from": [.22, .22], "to": [.28, .22], "reason": "test"}], "unresolved": []})
    monkeypatch.setattr(FakeWorkflow, "_set_annotation_position", staticmethod(lambda *_args: True))
    with pytest.raises(geometry.AnnotationRepairError, match="spacing did not match"):
        geometry.repair_generated_annotations(drawing, FakeApp(), FakeWorkflow())
    assert drawing.source_writes == 0


def test_repair_accepts_spacing_position_readback_and_preserves_inventory(monkeypatch):
    monkeypatch.setattr(geometry, "_resolve_sw_constant", lambda name, *_args: 2 if name == "swAnnotationHidden" else 1)
    view = CircleView([FakeDimension("D1", value=.01)])
    drawing = FakeDrawing(view)
    monkeypatch.setattr(geometry, "plan_profile_diameter_spacing", lambda *_args, **_kwargs: {
        "moves": [{"index": 0, "from": [.22, .22], "to": [.28, .22], "reason": "test"}], "unresolved": []})
    result = geometry.repair_generated_annotations(drawing, FakeApp(), FakeWorkflow())
    assert result["cross_view_spacing"]["moves"][0]["native_position_m"] == [.28, .22, 0.0]
    assert result["stored_before"] == result["stored_after"] == 1
    assert drawing.source_writes == 0
