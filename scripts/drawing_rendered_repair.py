"""Controller for safe, native repositioning of confirmed PDF collisions."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from . import drawing_geometry_annotations as geometry
from .drawing_pdf_spacing import plan_pdf_dimension_spacing, _box, _overlap
from .drawing_quality import dimension_signature


def _callback_ok(value):
    if value is True:
        return True
    if isinstance(value, Mapping):
        return value.get("success") is True or value.get("native_success") is True
    return False


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf8")


def _copy_pdf(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _raw_overlap_count(review_module, pdf_path):
    report = review_module.inspect_pdf_text_layout(pdf_path)
    return len(report.get("overlaps") or [])


def _native_value(record):
    value = record.get("system_value") or {}
    return record.get("identity"), value.get("type"), value.get("unit"), value.get("value"), record.get("visibility")


def _visible(value):
    return value in (1, True, "visible")


def _call_export(export_callback, drawing, pdf_path):
    result = export_callback(drawing, str(pdf_path))
    if not _callback_ok(result):
        raise geometry.AnnotationRepairError("Rendered collision repair PDF export failed")
    if not Path(pdf_path).is_file():
        raise geometry.AnnotationRepairError("Rendered collision repair PDF was not created")
    return result


def _call_save(save_callback, drawing):
    result = save_callback(drawing)
    if not _callback_ok(result):
        raise geometry.AnnotationRepairError("Rendered collision repair native save failed")
    return result


def _validate_bounds(record, snapshot, title_block_box):
    box = _box(record.get("box_m") or record.get("box"))
    sheet = snapshot.get("sheet_size") or {}
    try:
        width = float(sheet["width_m"])
        height = float(sheet["height_m"])
    except (KeyError, TypeError, ValueError):
        raise geometry.AnnotationRepairError("Rendered collision repair sheet bounds unavailable")
    values = [] if box is None else list(box.values())
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        raise geometry.AnnotationRepairError("Rendered collision repair sheet bounds unavailable")
    if box is None or not all(math.isfinite(float(value)) for value in values):
        raise geometry.AnnotationRepairError("Moved dimension box unavailable")
    gap = 0.002
    if any((box["left"] < gap, box["bottom"] < gap, box["right"] > width - gap, box["top"] > height - gap)):
        raise geometry.AnnotationRepairError("Moved dimension remains outside sheet")
    def expanded(candidate):
        parsed = _box(candidate)
        if parsed is None or not all(math.isfinite(float(value)) for value in parsed.values()):
            raise geometry.AnnotationRepairError("Rendered collision repair obstacle box unavailable")
        return {"left": parsed["left"] - gap, "bottom": parsed["bottom"] - gap,
                "right": parsed["right"] + gap, "top": parsed["top"] + gap}

    title_source = title_block_box or snapshot.get("title_block_box") or (snapshot.get("title_block") or {}).get("box")
    title = expanded(title_source) if title_source is not None else None
    if title is not None and _overlap(box, title):
        raise geometry.AnnotationRepairError("Moved dimension enters title block")
    for view in snapshot.get("views") or []:
        if not isinstance(view, Mapping) or view.get("box") is None:
            raise geometry.AnnotationRepairError("Rendered collision repair view box unavailable")
        view_box = expanded(view["box"])
        if _overlap(box, view_box):
            raise geometry.AnnotationRepairError("Moved dimension enters a drawing view")


def _rollback(drawing, workflow, objects, old_positions, save_callback, export_callback, pdf_path,
              baseline_signature, baseline_inventory, baseline_visibility):
    for index, position in old_positions.items():
        annotation = workflow._safe_member(objects[index][1], "GetAnnotation")
        if annotation is None or not workflow._set_annotation_position(annotation, *position):
            raise geometry.AnnotationRepairError("Rendered collision repair rollback position failed")
        actual = workflow._annotation_position(annotation)
        if actual is None or math.dist(actual[:2], position[:2]) > 1e-6:
            raise geometry.AnnotationRepairError("Rendered collision repair rollback readback failed")
    _call_save(save_callback, drawing)
    _call_export(export_callback, drawing, pdf_path)
    restored, _ = geometry._dimension_snapshot(drawing, workflow)
    if (dimension_signature(restored) != baseline_signature
            or geometry._model_dimension_inventory(restored) != baseline_inventory
            or [item.get("visibility") for item in restored.get("dimensions", [])] != baseline_visibility):
        raise geometry.AnnotationRepairError("Rendered collision repair rollback changed dimension baseline")


def repair_rendered_dimension_collisions(
    drawing,
    sw_app,
    workflow,
    pdf_path,
    *,
    save_callback: Callable,
    export_callback: Callable,
    title_block_box=None,
    evidence_dir=None,
    max_moves=4,
):
    """Apply at most ``max_moves`` native moves while strictly reducing PDF collisions."""
    del sw_app  # Reserved for parity with the native repair controller.
    try:
        limit = max(0, min(int(max_moves), 4))
    except (TypeError, ValueError):
        raise geometry.AnnotationRepairError("Invalid rendered collision repair move limit")
    pdf_path = Path(pdf_path)
    evidence = Path(evidence_dir) if evidence_dir is not None else None
    review_module = __import__("scripts.sw_drawing_review", fromlist=["inspect_pdf_dimension_rendering"])
    snapshot, objects = geometry._dimension_snapshot(drawing, workflow)
    before_signature = dimension_signature(snapshot)
    if before_signature is None:
        raise geometry.AnnotationRepairError("Rendered collision repair dimension signature unavailable")
    initial_render = review_module.inspect_pdf_dimension_rendering(pdf_path, snapshot)
    initial_count = len(initial_render.get("collisions") or [])
    initial_raw = _raw_overlap_count(review_module, pdf_path)
    if initial_count == 0:
        return {"status": "review_required", "moves": [], "before_count": 0, "after_count": 0, "unresolved": [], "raw_before_count": initial_raw, "raw_after_count": initial_raw}
    if evidence is not None:
        _copy_pdf(pdf_path, evidence / "before_00.pdf")
        _write_json(evidence / "before_00.json", initial_render)

    moves = []
    unresolved = []
    current_render, current_count, current_raw = initial_render, initial_count, initial_raw
    baseline_inventory = geometry._model_dimension_inventory(snapshot)
    baseline_visibility = [item.get("visibility") for item in snapshot.get("dimensions", [])]
    baseline_indexes = {int(item.get("dimension_index", -1)) for item in initial_render.get("matched") or []}
    for attempt in range(1, limit + 1):
        snapshot, objects = geometry._dimension_snapshot(drawing, workflow)
        if (dimension_signature(snapshot) != before_signature
                or geometry._model_dimension_inventory(snapshot) != baseline_inventory):
            raise geometry.AnnotationRepairError("Rendered collision repair baseline changed before native move")
        plan = plan_pdf_dimension_spacing(snapshot, current_render, title_block_box=title_block_box)
        if plan.get("status") != "pass" or not plan.get("moves"):
            unresolved.extend(plan.get("unresolved") or [{"reason": "no_safe_native_move_candidate"}])
            break
        move = plan["moves"][0]
        index = int(move["index"])
        if index < 0 or index >= len(objects):
            unresolved.append({"reason": "native_dimension_index_out_of_range", "index": index})
            break
        before_record = snapshot["dimensions"][index]
        if before_record.get("identity") != move.get("identity") or before_record.get("system_value", {}).get("value") != move.get("expected_value"):
            unresolved.append({"reason": "native_dimension_identity_or_value_changed", "index": index})
            break
        annotation = workflow._safe_member(objects[index][1], "GetAnnotation")
        old_position = workflow._annotation_position(annotation)
        if annotation is None or old_position is None or not _visible(workflow._safe_member(annotation, "Visible")):
            unresolved.append({"reason": "native_annotation_not_visible_or_position_missing", "index": index})
            break
        old_positions = {index: old_position}
        candidate_render = None
        try:
            target = list(move["to"]) + [0.0]
            if not workflow._set_annotation_position(annotation, *target):
                raise geometry.AnnotationRepairError("Rendered collision repair SetPosition2 failed")
            actual = workflow._annotation_position(annotation)
            if actual is None or math.dist(actual[:2], target[:2]) > 1e-6:
                raise geometry.AnnotationRepairError("Rendered collision repair position readback failed")
            after_native, after_objects = geometry._dimension_snapshot(drawing, workflow)
            if dimension_signature(after_native) != before_signature or geometry._model_dimension_inventory(after_native) != geometry._model_dimension_inventory(snapshot):
                raise geometry.AnnotationRepairError("Rendered collision repair changed dimension identity/value signature")
            after_record = after_native["dimensions"][index]
            if _native_value(after_record) != _native_value(before_record) or not _visible(after_record.get("visibility")):
                raise geometry.AnnotationRepairError("Rendered collision repair changed native dimension evidence")
            _call_save(save_callback, drawing)
            _call_export(export_callback, drawing, pdf_path)
            after_render = review_module.inspect_pdf_dimension_rendering(pdf_path, after_native)
            candidate_render = after_render
            after_count = len(after_render.get("collisions") or [])
            after_raw = _raw_overlap_count(review_module, pdf_path)
            after_indexes = {int(item.get("dimension_index", -1)) for item in after_render.get("matched") or []}
            if not baseline_indexes.issubset(after_indexes):
                raise geometry.AnnotationRepairError("Final PDF evidence lost a matched dimension")
            matched = next((item for item in after_render.get("matched") or [] if int(item.get("dimension_index", -1)) == index), None)
            if matched is None:
                raise geometry.AnnotationRepairError("Moved dimension disappeared from final PDF evidence")
            _validate_bounds(matched, after_native, title_block_box)
            if after_count >= current_count or after_raw > current_raw:
                raise geometry.AnnotationRepairError("Rendered collision repair did not strictly improve PDF evidence")
            if evidence is not None:
                _copy_pdf(pdf_path, evidence / f"after_{attempt:02d}.pdf")
                _write_json(evidence / f"after_{attempt:02d}.json", after_render)
            moves.append({**move, "attempt": attempt, "native_position_m": actual, "before_collision_count": current_count, "after_collision_count": after_count})
            current_render, current_count, current_raw = after_render, after_count, after_raw
            if current_count == 0:
                break
        except Exception as error:
            if candidate_render is not None and evidence is not None:
                _copy_pdf(pdf_path, evidence / f"rejected_{attempt:02d}.pdf")
                _write_json(evidence / f"rejected_{attempt:02d}.json", candidate_render)
            try:
                _rollback(drawing, workflow, objects, old_positions, save_callback, export_callback, pdf_path,
                          before_signature, baseline_inventory, baseline_visibility)
            except Exception:
                raise geometry.AnnotationRepairError(f"Rendered collision repair rollback failed: {error}") from error
            unresolved.append({"index": index, "reason": "candidate_rejected_and_rolled_back", "error": str(error)})
            break
    status = "review_required" if current_count == 0 else "blocked"
    return {"status": status, "moves": moves, "before_count": initial_count, "after_count": current_count,
            "raw_before_count": initial_raw, "raw_after_count": current_raw, "unresolved": unresolved}


__all__ = ["repair_rendered_dimension_collisions"]
