"""Plan safe native annotation moves from confirmed PDF text collisions.

This module is deliberately side-effect free.  It consumes a final PDF
collision report and proposes one position for an existing native dimension;
the caller must apply the move with ``IAnnotation.SetPosition2`` and perform
native readback before saving the drawing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Mapping


def _box(value: Mapping[str, Any] | None):
    if not isinstance(value, Mapping):
        return None
    try:
        result = {key: float(value[key]) for key in ("left", "bottom", "right", "top")}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result.values()):
        return None
    if result["right"] <= result["left"] or result["top"] <= result["bottom"]:
        return None
    return result


def _overlap(first, second, gap=0.0):
    return (first["left"] < second["right"] + gap and first["right"] + gap > second["left"]
            and first["bottom"] < second["top"] + gap and first["top"] + gap > second["bottom"])


def _translate(box, dx, dy):
    return {key: box[key] + (dx if key in ("left", "right") else dy)
            for key in ("left", "bottom", "right", "top")}


def _position(dimension):
    evidence = dimension.get("box_evidence") or {}
    raw = evidence.get("position_m") or dimension.get("position_m") or []
    try:
        if len(raw) < 2 or not all(math.isfinite(float(item)) for item in raw[:2]):
            return None
        return [float(raw[0]), float(raw[1])]
    except (TypeError, ValueError):
        return None


def _name(dimension):
    return str(dimension.get("name") or dimension.get("identity", {}).get("canonical_key") or "")


def _view_for(dimension, views):
    wanted = str(dimension.get("semantic_view") or dimension.get("view") or "")
    matches = [view for view in views if str(view.get("semantic_view") or view.get("name") or view.get("view") or "") == wanted]
    return matches[0] if len(matches) == 1 else None


def _identity_ok(dimension):
    identity = dimension.get("identity") or {}
    value = dimension.get("system_value") or {}
    return (bool(identity.get("canonical_key")) and value.get("unit") == "m"
            and isinstance(value.get("value"), (int, float))
            and math.isfinite(float(value["value"])))


def _type2(dimension):
    raw = dimension.get("type2", dimension.get("type"))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def plan_pdf_dimension_spacing(
    structure: Mapping[str, Any],
    rendering: Mapping[str, Any],
    *,
    sheet_width_m: float | None = None,
    sheet_height_m: float | None = None,
    title_block_box: Mapping[str, Any] | None = None,
    min_gap_m: float = 0.002,
):
    """Return one verified-candidate plan for a confirmed PDF text collision.

    ``rendering`` is the output of ``inspect_pdf_dimension_rendering``.  A
    missing sheet bound, missing identity/value evidence, ambiguous matched
    name, hole callout, or estimated-only collision is returned as unresolved.
    No dimension is hidden, recreated, or changed by this function.
    """
    structure = structure if isinstance(structure, Mapping) else {}
    rendering = rendering if isinstance(rendering, Mapping) else {}
    dimensions = list(structure.get("dimensions") or [])
    sheet = structure.get("sheet_size") or {}
    try:
        width = float(sheet_width_m if sheet_width_m is not None else sheet["width_m"])
        height = float(sheet_height_m if sheet_height_m is not None else sheet["height_m"])
        gap = float(min_gap_m)
    except (KeyError, TypeError, ValueError):
        return {"status": "review_required", "moves": [], "unresolved": [{"reason": "sheet_bounds_missing"}]}
    if width <= 0 or height <= 0 or gap < 0 or not all(math.isfinite(item) for item in (width, height, gap)):
        return {"status": "review_required", "moves": [], "unresolved": [{"reason": "sheet_bounds_invalid"}]}
    title_data = title_block_box or structure.get("title_block_box") or (structure.get("title_block") or {}).get("box")
    title = _box(title_data)
    views = [item for item in structure.get("views") or [] if isinstance(item, Mapping)]
    view_boxes = [(view, _box(view.get("box"))) for view in views]
    matched = {}
    by_name = defaultdict(list)
    for item in rendering.get("matched") or []:
        try:
            index = int(item["dimension_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if index < 0 or index >= len(dimensions):
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "matched_index_out_of_range", "index": index}]}
        box = _box(item.get("box_m") or item.get("box"))
        name = str(item.get("dimension_name") or "")
        if box is None or not name:
            continue
        dimension = dimensions[index]
        if index in matched:
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "matched_index_duplicated", "index": index}]}
        if name != _name(dimension):
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "matched_identity_mismatch", "index": index, "reported": name, "actual": _name(dimension)}]}
        if item.get("identity") is not None and item.get("identity") != dimension.get("identity"):
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "matched_identity_mismatch", "index": index}]}
        matched[index] = {"box": box, "name": name, "page": item.get("page")}
        by_name[name].append(index)
    if not matched:
        return {"status": "review_required", "moves": [], "unresolved": [{"reason": "no_confirmed_pdf_matches"}]}

    boxes = {}
    for index, dimension in enumerate(dimensions):
        if dimension.get("visibility") in ("hidden", 3):
            boxes[index] = None
            continue
        if index in matched:
            boxes[index] = matched[index]["box"]
        else:
            boxes[index] = _box(dimension.get("box")) or _box((dimension.get("box_evidence") or {}).get("box"))

    collisions = []
    for collision in rendering.get("collisions") or []:
        first, second = str(collision.get("first") or ""), str(collision.get("second") or "")
        first_indexes, second_indexes = by_name.get(first, []), by_name.get(second, [])
        if len(first_indexes) != 1 or len(second_indexes) != 1 or first_indexes == second_indexes:
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "collision_identity_ambiguous", "first": first, "second": second}]}
        first_index, second_index = first_indexes[0], second_indexes[0]
        first_match, second_match = matched[first_index], matched[second_index]
        if first_match.get("page") != second_match.get("page"):
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "collision_cross_page", "first": first, "second": second}]}
        if not _overlap(first_match["box"], second_match["box"]):
            return {"status": "review_required", "moves": [], "unresolved": [{"reason": "collision_boxes_do_not_intersect", "first": first, "second": second}]}
        collisions.append((first_index, second_index))
    if not collisions:
        return {"status": "review_required", "moves": [], "unresolved": []}

    priority = {5: 0, 2: 1, 6: 2}
    unresolved = []
    for pair in collisions:
        candidates = sorted(pair, key=lambda index: priority.get(_type2(dimensions[index]), 99))
        for index in candidates:
            dimension = dimensions[index]
            type2 = _type2(dimension)
            if type2 not in priority:
                continue
            if dimension.get("is_hole_callout") is not False or dimension.get("visibility") not in ("visible", 1) or not _identity_ok(dimension):
                continue
            position = _position(dimension)
            old_box = boxes.get(index)
            view = _view_for(dimension, views)
            view_box = _box(view.get("box")) if view else None
            if position is None or old_box is None or view_box is None:
                unresolved.append({"index": index, "reason": "move_evidence_missing"})
                continue
            center_x = (view_box["left"] + view_box["right"]) / 2
            center_y = (view_box["bottom"] + view_box["top"]) / 2
            sx = 1 if position[0] >= center_x else -1
            sy = -1 if position[1] <= center_y else 1
            # Prefer lateral motion, then move down/up if the view still blocks it.
            offsets = [(0.012 * sx, 0.0), (0.018 * sx, 0.0), (0.024 * sx, 0.0),
                       (0.012 * sx, 0.012 * sy), (0.018 * sx, 0.018 * sy), (0.024 * sx, 0.024 * sy)]
            for dx, dy in offsets:
                candidate_box = _translate(old_box, dx, dy)
                if _overlap(candidate_box, view_box, gap):
                    continue
                if title is not None and _overlap(candidate_box, title, gap):
                    continue
                if any(value < gap for value in (candidate_box["left"], candidate_box["bottom"])) or candidate_box["right"] > width - gap or candidate_box["top"] > height - gap:
                    continue
                if any(other != index and box is not None and _overlap(candidate_box, box, gap) for other, box in boxes.items()):
                    continue
                if any(other_box is not None and _overlap(candidate_box, other_box, gap) for other_view, other_box in view_boxes):
                    continue
                return {"status": "pass", "moves": [{"index": index, "name": _name(dimension), "type2": type2,
                    "identity": dimension["identity"], "from": position, "to": [position[0] + dx, position[1] + dy],
                    "delta_m": [dx, dy], "reason": "confirmed_pdf_text_collision", "expected_value": dimension["system_value"]["value"],
                    "expected_unit": "m", "candidate_box": candidate_box}], "unresolved": unresolved}
        unresolved.append({"pair": pair, "reason": "no_safe_native_move_candidate"})
    return {"status": "blocked", "moves": [], "unresolved": unresolved}


__all__ = ["plan_pdf_dimension_spacing"]
