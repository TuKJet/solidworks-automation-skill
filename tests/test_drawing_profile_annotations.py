from types import SimpleNamespace
import pytest
import scripts.drawing_profile_annotations as profile

from scripts.drawing_profile_annotations import insert_profile_candidates, repair_imported_profile_annotations


class Workflow:
    def _safe_member(self, obj, name, *args, default=None):
        if obj is None:
            return default
        value = getattr(obj, name, default)
        return value(*args) if callable(value) else value
    def _display_dimension_model_dimension(self, dim):
        return dim
    def _dimension_system_value(self, _dim, _model):
        return {"type": 2, "value": .01, "unit": "m"}
    def _dimension_identity(self, _view, _dim, _model):
        return {"canonical_key": "test-dimension"}
    def auto_arrange_drawing_dimensions(self, *_args, **_kwargs):
        return {"status": "pass"}


class Annotation:
    Visible = 1
    IsDangling = False
    def GetAttachedEntityCount3(self):
        return 2
    def Select3(self, *_):
        return True


class Dim:
    IsDangling = False
    def GetAnnotation(self):
        return Annotation()


class Drawing:
    def __init__(self):
        self.SelectionManager = SimpleNamespace(CreateSelectData=lambda: SimpleNamespace())
        self.Extension = SimpleNamespace(calls=[], AddSpecificDimension=self._specific, DeleteSelection2=lambda *_: True)
        self.created = []
    def _specific(self, *args):
        self.Extension.calls.append(args)
        return Dim()
    def ClearSelection2(self, *_):
        return True
    def ActivateView(self, _name):
        return True
    def AddHorizontalDimension2(self, *_):
        self.created.append("h")
        return Dim()
    def AddVerticalDimension2(self, *_):
        self.created.append("v")
        return Dim()


class Vertex:
    def Select4(self, *_):
        return True


def test_linear_uses_same_native_type_and_verifies_visibility():
    candidate = {"kind": "vertical", "orientation": "vertical", "expected_m": .01,
                 "view": "front", "text_position_m": [.2, .3, 0], "source_geometry": {"vertex_ids": ["a", "b"]}}
    result = insert_profile_candidates(Drawing(), None, Workflow(), [candidate],
        [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    assert result["status"] == "pass"


def test_existing_dimensions_are_not_touched():
    workflow = Workflow()
    workflow.inspect_drawing_structure = lambda _drawing: {"dimensions": [{"identity": "existing"}]}
    result = repair_imported_profile_annotations(object(), None, workflow)
    assert result == {"status": "skipped", "reason": "EXISTING_DIMENSIONS_PRESENT"}


def test_radius_uses_extension_specific_dimension_and_byref_slot():
    class RadiusWorkflow(Workflow):
        def _dimension_system_value(self, _dim, _model):
            return {"type": 5, "value": .01, "unit": "m"}
    candidate = {"kind": "radius", "expected_m": .01,
                 "view": "front", "text_position_m": [.2, .3, 0], "source_geometry": {"arc_id": "arc"}}
    drawing = Drawing()
    result = insert_profile_candidates(drawing, None, RadiusWorkflow(), [candidate],
        [{"id": "arc", "view": object(), "object": Vertex()}])
    assert result["status"] == "pass"
    assert drawing.Extension.calls and drawing.Extension.calls[0][3] == 5
    assert len(drawing.Extension.calls[0]) == 5


def test_radius_type_six_is_rejected():
    class DiameterWorkflow(Workflow):
        def _dimension_system_value(self, _dim, _model):
            return {"type": 6, "value": .01}
    candidate = {"kind": "radius", "expected_m": .01, "view": "front",
                 "text_position_m": [.2, .3, 0], "source_geometry": {"arc_id": "arc"}}
    result = insert_profile_candidates(Drawing(), None, DiameterWorkflow(), [candidate],
        [{"id": "arc", "view": object(), "object": Vertex()}])
    assert result["status"] == "blocked"


def test_multiple_arc_max_conditions_must_all_return_zero():
    class ArcDim(Dim):
        def __init__(self, result):
            self.result, self.calls = result, []
        def SetArcEndCondition(self, index, condition):
            self.calls.append((index, condition))
            return self.result
        def GetArcEndCondition(self, index):
            return self.calls[index - 1][1]
    class ArcDrawing(Drawing):
        def __init__(self, result):
            super().__init__(); self.arc_dim = ArcDim(result)
        def AddHorizontalDimension2(self, *_):
            return self.arc_dim
    candidate = {"kind": "horizontal", "orientation": "horizontal", "expected_m": .01,
                 "view": "front", "text_position_m": [.2, .3, 0],
                 "source_geometry": {"entity_ids": ["a", "b"], "arc_end_conditions": [3, 3]}}
    drawing = ArcDrawing(0)
    result = insert_profile_candidates(drawing, None, Workflow(), [candidate],
        [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    assert result["status"] == "pass"
    assert drawing.arc_dim.calls == [(1, 3), (2, 3)]

    with pytest.raises(Exception, match="arc-end condition"):
        insert_profile_candidates(ArcDrawing(1), None, Workflow(), [candidate],
            [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])


def test_native_dimension_unit_must_match_expected_meters():
    class MillimeterWorkflow(Workflow):
        def _dimension_system_value(self, _dim, _model):
            return {"type": 2, "value": .01, "unit": "mm"}
    candidate = {"kind": "horizontal", "orientation": "horizontal", "expected_m": .01,
                 "view": "front", "text_position_m": [.2, .3, 0],
                 "source_geometry": {"vertex_ids": ["a", "b"]}}
    result = insert_profile_candidates(Drawing(), None, MillimeterWorkflow(), [candidate],
        [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    assert result["status"] == "blocked"


def test_collector_filters_outer_and_oblique_planes_and_dedupes_opposite_faces(monkeypatch):
    class Surface:
        IsPlane = True
        PlaneParams = [0, 0, 1, 0, 0, 0]
    class BadSurface(Surface):
        PlaneParams = [1, 0, 0, 0, 0, 0]
    points = [VertexPoint([0, 0, 0]), VertexPoint([.01, 0, 0]), VertexPoint([0, .01, 0])]
    edges = [LoopEdge(points[0], points[1]), LoopEdge(points[1], points[2]), LoopEdge(points[2], points[0])]
    inner = Loop(False, edges)
    outer = Loop(True, edges)
    face = Face(Surface(), [inner, outer])
    opposite = Face(BadSurface(), [inner])
    profile_edge = ProfileEdge([face, opposite])
    native_view = NativeView([profile_edge])
    sheet = SimpleNamespace(GetName=lambda: "Sheet1", GetViews=lambda: [native_view])
    drawing = SimpleNamespace(GetCurrentSheet=lambda: sheet)
    transform = SimpleNamespace(ArrayData=[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0])
    native_view.ModelToViewTransform = transform
    workflow = CollectorWorkflow()
    monkeypatch.setattr(profile, "_resolve_sw_constant", lambda name, _app: "edge" if name.endswith("Edge") else "vertex")
    evidence, handles = profile.collect_profile_geometry(drawing, object(), workflow)
    loops = evidence["views"][0]["inner_loops"]
    assert len(loops) == 1
    assert len(loops[0]["vertices"]) == 3
    assert loops[0]["vertices"][0]["point_model_m"] == [0, 0, 0]
    assert len([h for h in handles if h["kind"] == "vertex"]) == 3


def test_collector_arc_records_curve_endpoints_without_evaluate2(monkeypatch):
    arc = ArcEdge()
    native_view = NativeView([arc])
    sheet = SimpleNamespace(GetName=lambda: "Sheet1", GetViews=lambda: [native_view])
    drawing = SimpleNamespace(GetCurrentSheet=lambda: sheet)
    native_view.ModelToViewTransform = SimpleNamespace(ArrayData=[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0])
    workflow = CollectorWorkflow()
    monkeypatch.setattr(profile, "_resolve_sw_constant", lambda name, _app: "edge" if name.endswith("Edge") else "vertex")
    evidence, _ = profile.collect_profile_geometry(drawing, object(), workflow)
    assert len(evidence["views"][0]["arcs"]) == 1
    item = evidence["views"][0]["arcs"][0]
    assert item["start_sheet_m"] == pytest.approx([.01, 0, 0])
    assert item["end_sheet_m"] == pytest.approx([0, .01, 0])


class VertexPoint:
    def __init__(self, point):
        self.point = point
    def GetPoint(self):
        return [self.point[0] + 100.0, self.point[1] + 200.0, self.point[2] + 300.0]


class LoopEdge:
    def __init__(self, start, end):
        self.start, self.end = start, end
    def GetCurveParams2(self):
        # Native endpoint coordinates are authoritative; GetPoint below is
        # deliberately translated to catch accidental double transforms.
        return self.start.point + self.end.point + [0, 0, 0, 0, 0, 0]
    def GetStartVertex(self): return self.start
    def GetEndVertex(self): return self.end


class Loop:
    def __init__(self, outer, edges): self.IsOuter, self.edges = outer, edges
    def GetEdges(self): return self.edges


class Face:
    def __init__(self, surface, loops): self.surface, self.loops = surface, loops
    def GetSurface(self): return self.surface
    def GetLoops(self): return self.loops


class ProfileEdge:
    def __init__(self, faces): self.faces, self.GetCurve = faces, lambda: SimpleNamespace(IsCircle=False)
    def GetTwoAdjacentFaces2(self): return self.faces


class ArcEdge(ProfileEdge):
    def __init__(self):
        self.GetCurve = lambda: SimpleNamespace(IsCircle=True, CircleParams=[0, 0, 0, 0, 0, 1, .01], Evaluate2=lambda *_: (_ for _ in ()).throw(AssertionError("Evaluate2 must not be used")))
    def GetCurveParams2(self): return [.01, 0, 0, 0, .01, 0, 0, 1.5707963267948966]
    def GetTwoAdjacentFaces2(self): return []


class NativeView:
    Name = "front"
    ModelToViewTransform = None
    def __init__(self, edges): self.edges = edges
    def GetOutline(self): return [0, 0, .1, .1]
    def GetVisibleComponents(self): return []
    def GetVisibleEntities2(self, _component, enum): return self.edges if enum == "edge" else []


class CollectorWorkflow(Workflow):
    def _map_standard_views_from_sheet_views(self, views): return ({"front": views[0]}, None, None)
    def _normalise_box(self, box): return {"left": box[0], "bottom": box[1], "right": box[2], "top": box[3]}


class RecordingVertex(Vertex):
    def __init__(self):
        self.calls = []
    def Select4(self, append, data):
        self.calls.append((append, data.View))
        return True


def linear_candidate(**kwargs):
    return {"kind": "horizontal", "orientation": "horizontal", "expected_m": .01,
            "view": "front", "text_position_m": [.2, .3, 0],
            "source_geometry": {"vertex_ids": ["a", "b"]}, **kwargs}


def test_two_vertices_use_append_false_then_true():
    a, b = RecordingVertex(), RecordingVertex()
    drawing = Drawing()
    result = insert_profile_candidates(drawing, None, Workflow(), [linear_candidate()],
        [{"id": "a", "view": object(), "object": a}, {"id": "b", "view": object(), "object": b}])
    assert result["status"] == "pass"
    assert [a.calls[0][0], b.calls[0][0]] == [False, True]


def test_dangling_unknown_blocks_and_deletes_new_dimension():
    class UnknownDim(Dim):
        def GetAnnotation(self):
            annotation = Annotation()
            annotation.IsDangling = None
            return annotation
    class UnknownDrawing(Drawing):
        def AddHorizontalDimension2(self, *_):
            self.dim = UnknownDim()
            return self.dim
    result = insert_profile_candidates(UnknownDrawing(), None, Workflow(), [linear_candidate()],
        [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    assert result["status"] == "blocked"
    assert result["results"][0]["reason"] == "NATIVE_VALIDATION_FAILED"


def test_linear_rejects_less_than_two_attached_entities():
    class OneAttached(Annotation):
        def GetAttachedEntityCount3(self):
            return 1
    class OneDrawing(Drawing):
        def AddHorizontalDimension2(self, *_):
            return SimpleNamespace(GetAnnotation=lambda: OneAttached())
    result = insert_profile_candidates(OneDrawing(), None, Workflow(), [linear_candidate()],
        [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    assert result["status"] == "blocked"


def test_wrong_value_is_deleted_and_delete_failure_raises():
    class WrongWorkflow(Workflow):
        def _dimension_system_value(self, _dim, _model):
            return {"type": 2, "value": .5}
    drawing = Drawing()
    result = insert_profile_candidates(drawing, None, WrongWorkflow(), [linear_candidate()],
        [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    assert result["status"] == "blocked"

    class FailingDelete(Drawing):
        def AddHorizontalDimension2(self, *_):
            return Dim()
    FailingDelete().Extension.DeleteSelection2 = lambda *_: False
    failing = FailingDelete()
    failing.Extension.DeleteSelection2 = lambda *_: False
    try:
        insert_profile_candidates(failing, None, WrongWorkflow(), [linear_candidate()],
            [{"id": "a", "view": object(), "object": Vertex()}, {"id": "b", "view": object(), "object": Vertex()}])
    except Exception as exc:
        assert "delete" in str(exc).lower()
    else:
        raise AssertionError("delete failure must raise")

