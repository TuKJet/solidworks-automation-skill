"""Pure spacing plan for cross-view diameter annotation anchors."""
from __future__ import annotations

import math
from collections.abc import Mapping


def _box(value):
    if not isinstance(value, Mapping):
        return None
    try:
        result = {key: float(value[key]) for key in ("left", "bottom", "right", "top")}
    except (KeyError, TypeError, ValueError):
        return None
    return result if all(math.isfinite(v) for v in result.values()) and result["left"] < result["right"] and result["bottom"] < result["top"] else None


def _anchor(dimension):
    evidence = dimension.get("box_evidence") if isinstance(dimension.get("box_evidence"), Mapping) else {}
    value = evidence.get("position_m")
    if value is None:
        value = dimension.get("position_m")
    try:
        point = [float(value[0]), float(value[1])]
    except (IndexError, TypeError, ValueError):
        return None
    return point if all(math.isfinite(v) for v in point) else None


def plan_profile_diameter_spacing(structure, circular_view_names, *, sheet_width_m=0.420, sheet_height_m=0.297):
    """Plan only safe anchor moves for non-circular-view diameter dimensions."""
    structure = structure if isinstance(structure, Mapping) else {}
    views = {}
    for view in structure.get("views") or []:
        if isinstance(view, Mapping):
            name = str(view.get("semantic_view") or view.get("name") or "").casefold()
            box = _box(view.get("box"))
            if name and box:
                views[name] = box
    circular = {str(name).casefold() for name in circular_view_names}
    anchors = []
    candidates = []
    unresolved = []
    for index, dimension in enumerate(structure.get("dimensions") or []):
        if not isinstance(dimension, Mapping) or dimension.get("visibility") != "visible" or dimension.get("visible") is False:
            continue
        view_name = str(dimension.get("semantic_view") or dimension.get("view") or "").casefold()
        point = _anchor(dimension)
        box = views.get(view_name)
        if point is None or box is None:
            continue
        anchors.append((index, point, view_name))
        if view_name in circular or dimension.get("type2", dimension.get("type")) != 6:
            continue
        side = "left" if point[0] < box["left"] else "right" if point[0] > box["right"] else None
        if side is None or not (box["bottom"] <= point[1] <= box["top"]):
            continue
        candidates.append({"index": index, "point": point, "view": view_name, "box": box, "side": side})

    moves = []
    planned = list(anchors)
    try:
        width, height = float(sheet_width_m), float(sheet_height_m)
    except (TypeError, ValueError):
        width, height = .420, .297
    for candidate in candidates:
        index, point, view_name, box, side = candidate["index"], candidate["point"], candidate["view"], candidate["box"], candidate["side"]
        near = any(other_view != view_name and abs(other[0] - point[0]) < .006 and abs(other[1] - point[1]) < .010 for _, other, other_view in planned)
        if not near:
            continue
        opposite = "right" if side == "left" else "left"
        same_view_x = [other[0] for other_index, other, other_view in planned if other_index != index and other_view == view_name]
        if opposite == "right":
            x = max(box["right"], *(same_view_x or [box["right"]])) + .012
        else:
            x = min(box["left"], *(same_view_x or [box["left"]])) - .012
        proposed = [x, point[1]]
        blocked = not (.012 <= proposed[0] <= width - .012 and .012 <= proposed[1] <= height - .012)
        title_box = _box((structure.get("title_block") or {}).get("box"))
        if title_box and title_box["left"] - .005 <= x <= title_box["right"] + .005 and title_box["bottom"] - .005 <= point[1] <= title_box["top"] + .005:
            blocked = True
        for other_view, other_box in views.items():
            if other_view == view_name:
                continue
            padded = {"left": other_box["left"] - .010, "right": other_box["right"] + .010,
                      "bottom": other_box["bottom"] - .010, "top": other_box["top"] + .010}
            if padded["left"] <= proposed[0] <= padded["right"] and padded["bottom"] <= proposed[1] <= padded["top"]:
                blocked = True
        if any(other_index != index and abs(other[0] - proposed[0]) < .006 and abs(other[1] - proposed[1]) < .010 for other_index, other, _ in planned):
            blocked = True
        if blocked:
            unresolved.append({"index": index, "reason": "proposed_anchor_blocked_or_offsheet", "proposed": proposed})
            continue
        moves.append({"index": index, "from": list(point), "to": proposed, "reason": "cross_view_anchor_proximity"})
        planned = [(other_index, proposed if other_index == index else other, other_view) for other_index, other, other_view in planned]
    return {"moves": moves, "unresolved": unresolved}
