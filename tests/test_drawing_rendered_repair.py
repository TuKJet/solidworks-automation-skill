import copy

import pytest

from scripts import drawing_rendered_repair as repair


class Annotation:
    def __init__(self):
        self.position = [.2785, .208, 0.0]
        self.Visible = 1


class Workflow:
    def __init__(self, annotation, *, effective=True):
        self.annotation = annotation
        self.effective = effective

    @staticmethod
    def _safe_member(obj, name, *args, default=None):
        if name == "GetAnnotation":
            return obj
        if name == "Visible":
            return obj.Visible
        return getattr(obj, name, default)

    def _set_annotation_position(self, annotation, x, y, z=0.0):
        if not self.effective:
            return True
        annotation.position = [x, y, z]
        return True

    @staticmethod
    def _annotation_position(annotation):
        return list(annotation.position)


def _fixture(annotation):
    d8 = {
        "name": "D8", "semantic_view": "front", "type2": 2, "visibility": "visible",
        "is_hole_callout": False,
        "identity": {"canonical_key": "D8", "source": "part.SLDPRT", "configuration": "Default"},
        "system_value": {"type": 2, "unit": "m", "value": .0006},
        "box": {"left": .265, "bottom": .206, "right": .286, "top": .227},
        "box_evidence": {"position_m": [.284, .222]},
    }
    d9 = {
        "name": "D9", "semantic_view": "front", "type2": 5, "visibility": "visible",
        "is_hole_callout": False,
        "identity": {"canonical_key": "D9", "source": "part.SLDPRT", "configuration": "Default"},
        "system_value": {"type": 5, "unit": "m", "value": .0002},
        "box": {"left": .267, "bottom": .197, "right": .290, "top": .219},
        "box_evidence": {"position_m": annotation.position[:2]},
    }
    return {
        "sheet_size": {"width_m": .420, "height_m": .297},
        "title_block": {"box": {"left": .218, "bottom": .022, "right": .398, "top": .077}},
        "views": [{"semantic_view": "front", "box": {"left": .222, "bottom": .218, "right": .260, "top": .252}}],
        "dimensions": [d8, d9],
    }


def _rendering(count):
    matched = [
        {"dimension_index": 0, "dimension_name": "D8", "page": 1,
         "box_m": {"left": .265, "bottom": .206, "right": .286, "top": .227}},
        {"dimension_index": 1, "dimension_name": "D9", "page": 1,
         "box_m": {"left": .267, "bottom": .197, "right": .290, "top": .219}},
    ]
    return {"matched": matched, "collisions": ([{"first": "D8", "second": "D9"}] if count else [])}


def _install(monkeypatch, annotation, renderings, *, snapshots=None, raw=None):
    snapshot = _fixture(annotation)
    snapshots = snapshots or [snapshot] * 8
    state = {"snapshot": 0, "render": 0, "raw": 0}

    def get_snapshot(*_args):
        value = snapshots[min(state["snapshot"], len(snapshots) - 1)]
        state["snapshot"] += 1
        return copy.deepcopy(value), [("view", annotation), ("view", annotation)]

    monkeypatch.setattr(repair.geometry, "_dimension_snapshot", get_snapshot)
    monkeypatch.setattr(repair.geometry, "_model_dimension_inventory",
                        lambda structure: tuple((d["identity"]["canonical_key"], d["system_value"]["value"]) for d in structure["dimensions"]))
    import scripts.sw_drawing_review as review
    def inspect_render(*_args):
        index = min(state["render"], len(renderings) - 1)
        state["render"] += 1
        return copy.deepcopy(renderings[index])

    def inspect_layout(*_args):
        if not raw:
            return {"overlaps": []}
        index = min(state["raw"], len(raw) - 1)
        state["raw"] += 1
        return {"overlaps": list(raw[index])}

    monkeypatch.setattr(review, "inspect_pdf_dimension_rendering", inspect_render)
    monkeypatch.setattr(review, "inspect_pdf_text_layout", inspect_layout)
    return snapshot


def _run(monkeypatch, tmp_path, annotation, renderings, *, effective=True, snapshots=None, raw=None):
    _install(monkeypatch, annotation, renderings, snapshots=snapshots, raw=raw)
    workflow = Workflow(annotation, effective=effective)
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"before")
    saves, exports = [], []
    def save(_drawing):
        saves.append(1)
        return True
    def export(_drawing, path):
        exports.append(1)
        open(path, "wb").write(b"after")
        return True
    result = repair.repair_rendered_dimension_collisions(object(), None, workflow, pdf,
        save_callback=save, export_callback=export, evidence_dir=tmp_path / "evidence")
    return result, annotation, saves, exports


def test_successful_native_move_requires_strict_pdf_improvement(monkeypatch, tmp_path):
    annotation = Annotation()
    result, annotation, saves, exports = _run(monkeypatch, tmp_path, annotation, [_rendering(1), _rendering(0)], raw=[[], []])
    assert result["status"] == "review_required"
    assert result["before_count"] == 1 and result["after_count"] == 0
    assert len(result["moves"]) == 1
    assert annotation.position[:2] != [.2785, .208]
    assert saves and exports
    assert (tmp_path / "evidence" / "after_01.pdf").is_file()


def test_position_readback_failure_rolls_back_and_returns_unresolved(monkeypatch, tmp_path):
    annotation = Annotation()
    result, annotation, _saves, _exports = _run(monkeypatch, tmp_path, annotation, [_rendering(1), _rendering(0)], effective=False)
    assert result["status"] == "blocked"
    assert result["moves"] == []
    assert result["unresolved"][0]["reason"] == "candidate_rejected_and_rolled_back"
    assert annotation.position[:2] == [.2785, .208]


def test_native_value_change_is_rejected_and_rolled_back(monkeypatch, tmp_path):
    annotation = Annotation()
    initial = _fixture(annotation)
    changed = copy.deepcopy(initial)
    changed["dimensions"][1]["system_value"]["value"] = .0003
    with pytest.raises(repair.geometry.AnnotationRepairError, match="baseline"):
        _run(monkeypatch, tmp_path, annotation, [_rendering(1), _rendering(0)], snapshots=[initial, changed])
    assert annotation.position[:2] == [.2785, .208]


def test_rollback_failure_raises_blocking_error(monkeypatch, tmp_path):
    annotation = Annotation()
    _install(monkeypatch, annotation, [_rendering(1), _rendering(1)])
    workflow = Workflow(annotation)
    calls = {"count": 0}
    def set_position(_annotation, x, y, z=0.0):
        calls["count"] += 1
        if calls["count"] >= 2:
            return False
        _annotation.position = [x, y, z]
        return True
    workflow._set_annotation_position = set_position
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"before")
    with pytest.raises(repair.geometry.AnnotationRepairError, match="rollback"):
        repair.repair_rendered_dimension_collisions(object(), None, workflow, pdf,
            save_callback=lambda _drawing: True,
            export_callback=lambda _drawing, path: (open(path, "wb").write(b"after") and True),
            evidence_dir=tmp_path / "evidence")


def test_no_collision_does_not_write_evidence_or_call_callbacks(monkeypatch, tmp_path):
    annotation = Annotation()
    result, _annotation, saves, exports = _run(monkeypatch, tmp_path, annotation, [_rendering(0)])
    assert result["before_count"] == result["after_count"] == 0
    assert not saves and not exports
    assert not (tmp_path / "evidence").exists()


def test_none_callback_is_not_treated_as_success(monkeypatch, tmp_path):
    annotation = Annotation()
    _install(monkeypatch, annotation, [_rendering(1), _rendering(0)])
    pdf = tmp_path / "drawing.pdf"
    pdf.write_bytes(b"before")
    with pytest.raises(repair.geometry.AnnotationRepairError):
        repair.repair_rendered_dimension_collisions(object(), None, Workflow(annotation), pdf,
            save_callback=lambda _drawing: None,
            export_callback=lambda _drawing, _path: True)


def test_rollback_that_cannot_restore_value_raises(monkeypatch, tmp_path):
    annotation = Annotation()
    initial = _fixture(annotation)
    changed = copy.deepcopy(initial)
    changed["dimensions"][1]["system_value"]["value"] = .0003
    with pytest.raises(repair.geometry.AnnotationRepairError, match="rollback"):
        _run(monkeypatch, tmp_path, annotation, [_rendering(1), _rendering(1)],
             snapshots=[initial, initial, changed, changed])
