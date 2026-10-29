"""Pure planning of dimensions from imported drawing profile evidence.

This module deliberately never estimates from a part bounding box.  Every
candidate carries the source vertices/arc which justify its value.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _point(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    try:
        p = tuple(float(x) for x in value[:3])
    except (TypeError, ValueError):
        return None
    return p if all(math.isfinite(x) for x in p) else None


def _matrix_is_axis_aligned(view: dict[str, Any]) -> bool:
    matrix = view.get("matrix")
    if not isinstance(matrix, (list, tuple)) or len(matrix) < 9:
        return False
    try:
        r = [float(x) for x in matrix[:9]]
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in r):
        return False
    # A drawing transform may permute/flip axes, but must not be oblique.
    for row in range(3):
        vals = [abs(r[row * 3 + col]) for col in range(3)]
        if sum(v > 1e-5 for v in vals) != 1 or max(vals) < 0.99:
            return False
    for col in range(3):
        if sum(abs(r[row * 3 + col]) > 1e-5 for row in range(3)) != 1:
            return False
    return True


def _view_scale(view: dict[str, Any], unresolved: list[dict[str, Any]]) -> float | None:
    scale = view.get("scale")
    if not _finite(scale) or float(scale) <= 0:
        unresolved.append({"view": view.get("name"), "reason": "invalid_scale"})
        return None
    if not _matrix_is_axis_aligned(view):
        unresolved.append({"view": view.get("name"), "reason": "missing_or_oblique_matrix"})
        return None
    return float(scale)


def _vertex_map(view: dict[str, Any], unresolved: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in view.get("vertices") or []:
        if not isinstance(item, dict) or item.get("id") is None:
            unresolved.append({"view": view.get("name"), "reason": "missing_vertex_id"})
            continue
        sheet = _point(item.get("point_sheet_m"))
        model = _point(item.get("point_model_m"))
        if sheet is None and model is None:
            unresolved.append({"view": view.get("name"), "geometry_id": item.get("id"), "reason": "missing_vertex_coordinates"})
            continue
        result[str(item["id"])] = {**item, "_sheet": sheet, "_model": model}
    return result


def _coordinates(item: dict[str, Any], scale: float) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    sheet = item.get("_sheet")
    model = item.get("_model")
    if sheet is not None and model is not None:
        return sheet, model
    return None


def _candidate(kind: str, view: dict[str, Any], expected: float, source: dict[str, Any], position: tuple[float, float], **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        # SolidWorks uses the same linear dimension type for horizontal and
        # vertical dimensions; orientation is carried separately.
        "type": {"horizontal": 2, "vertical": 2, "radius": 5}.get(kind, kind),
        "view": view.get("name"),
        "semantic_view": view.get("semantic_view"),
        "expected_m": expected,
        "unit": "m",
        "text_position_m": [position[0], position[1], 0.0],
        "source_geometry": source,
        **extra,
    }


def _overall(view: dict[str, Any], scale: float, vertices: dict[str, dict[str, Any]], unresolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for item in vertices.values():
        coords = _coordinates(item, scale)
        if coords is not None:
            points.append((item, *coords))
    # Include true cardinal extrema only when they lie on the bounded arc.
    # Native SetArcEndCondition(Max) is then read back against this value.
    for arc in view.get('arcs') or []:
        center=_point(arc.get('center_sheet_m'));start=_point(arc.get('start_sheet_m'));end=_point(arc.get('end_sheet_m'))
        if not center or not start or not end:
            continue
        a0=math.atan2(start[1]-center[1],start[0]-center[0])
        a1=math.atan2(end[1]-center[1],end[0]-center[0])
        ccw=(a1-a0)%(2*math.pi)
        if math.isclose(ccw,float(arc['span_rad']),abs_tol=1e-6):
            sign=1
        elif math.isclose(2*math.pi-ccw,float(arc['span_rad']),abs_tol=1e-6):
            sign=-1
        else:
            unresolved.append({'arc_id':arc.get('id'),'reason':'arc_endpoint_span_mismatch'})
            continue
        for angle in (0,math.pi/2,math.pi,3*math.pi/2):
            travel=(sign*(angle-a0))%(2*math.pi)
            if travel>float(arc['span_rad'])+1e-8:
                continue
            q=(center[0]+float(arc['radius_m'])*scale*math.cos(angle),center[1]+float(arc['radius_m'])*scale*math.sin(angle),center[2])
            points.append(({'id':arc['id'],'arc_end_condition':3},q,None))
    if len(points) < 2:
        if view.get("vertices"):
            unresolved.append({"view": view.get("name"), "reason": "insufficient_valid_vertices"})
        return []
    xs = sorted(points, key=lambda x: x[1][0])
    ys = sorted(points, key=lambda x: x[1][1])
    box = view.get("box") or {}
    left = float(box.get("left", 0.0)) if _finite(box.get("left", 0.0)) else 0.0
    bottom = float(box.get("bottom", 0.0)) if _finite(box.get("bottom", 0.0)) else 0.0
    right = float(box.get("right", left)) if _finite(box.get("right", left)) else left
    top = float(box.get("top", bottom)) if _finite(box.get("top", bottom)) else bottom
    out = []
    for kind, low, high in (("horizontal", xs[0], xs[-1]), ("vertical", ys[0], ys[-1])):
        axis = 0 if kind == "horizontal" else 1
        delta = abs(high[1][axis] - low[1][axis])/scale
        model_axis = max(range(3), key=lambda j: abs(view["matrix"][j*3+axis]))
        if delta <= 1e-12:
            continue
        # Put the dimension outside the view envelope, not over the profile.
        p = ((left + right) / 2, bottom - 0.010) if kind == "horizontal" else (right + 0.010, (bottom + top) / 2)
        conditions=[low[0].get('arc_end_condition',0),high[0].get('arc_end_condition',0)]
        out.append(_candidate(kind, view, delta, {"entity_ids": [low[0].get("id"), high[0].get("id")], "vertex_ids": [low[0].get("id"), high[0].get("id")] if not any(conditions) else [],
                    "arc_end_conditions":conditions,"points_model_m": [low[2], high[2]], "points_sheet_m": [low[1], high[1]], "role": "profile_vertex_span",
                    "extent_evidence":"bounded_arc_extrema_and_endpoints" if any(conditions) else "visible_edge_endpoints"}, p, model_axis=model_axis, orientation=kind))
    return out


def _loop(view: dict[str, Any], scale: float, vertices: dict[str, dict[str, Any]], unresolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    loops = []
    for loop in view.get("inner_loops") or []:
        if not isinstance(loop, dict):
            continue
        refs = loop.get("vertices") or []
        resolved = []
        for ref in refs:
            if isinstance(ref, dict) and (_point(ref.get("point_sheet_m")) or _point(ref.get("point_model_m"))):
                item = {**ref, "_sheet": _point(ref.get("point_sheet_m")), "_model": _point(ref.get("point_model_m"))}
            else:
                item = vertices.get(str(ref.get("id"))) if isinstance(ref, dict) else vertices.get(str(ref))
            if item is not None:
                coords = _coordinates(item, scale)
                if coords is not None:
                    resolved.append((item, *coords))
        if len(resolved) >= 3:
            area = (max(x[1][0] for x in resolved) - min(x[1][0] for x in resolved)) * (max(x[1][1] for x in resolved) - min(x[1][1] for x in resolved))
            loops.append((area, loop, resolved))
        else:
            unresolved.append({"view": view.get("name"), "loop_id": loop.get("id"), "reason": "inner_loop_insufficient_vertices"})
    if not loops:
        return []
    main = [entry for entry in loops if entry[1].get("front_view_main_loop") is True
            or entry[1].get("is_front_view_main_loop") is True]
    _, loop, pts = max(main or loops, key=lambda x: x[0])
    xmin = min(pts, key=lambda x: x[1][0]); xmax = max(pts, key=lambda x: x[1][0])
    ymin = min(pts, key=lambda x: x[1][1]); ymax = max(pts, key=lambda x: x[1][1])
    outer = [(item, _coordinates(item, scale)) for item in vertices.values()]
    outer = [(item, pair) for item, pair in outer if pair is not None]
    bx = min(pair[0][0] for _, pair in outer) if outer else min(x[1][0] for x in pts)
    by = min(pair[0][1] for _, pair in outer) if outer else min(x[1][1] for x in pts)
    bx_model = min(pair[1][0] for _, pair in outer) if outer else min(x[2][0] for x in pts)
    by_model = min(pair[1][1] for _, pair in outer) if outer else min(x[2][1] for x in pts)
    out = []
    for kind, low, high, axis, offset in (("horizontal", xmin, xmax, 0, (0.0, 0.012)), ("vertical", ymin, ymax, 1, (0.012, 0.0))):
        value = abs(high[1][axis] - low[1][axis])/scale
        if value > 1e-12:
            p = ((xmin[1][0] + xmax[1][0]) / 2, view['box']['bottom'] - 0.020) if kind == "horizontal" else (view['box']['left'] - 0.012, (ymin[1][1] + ymax[1][1]) / 2)
            out.append(_candidate(kind, view, value, {"loop_id": loop.get("id"), "vertex_ids": [low[0].get("id"), high[0].get("id")], "points_model_m": [low[2], high[2]], "points_sheet_m": [low[1], high[1]], "role": "inner_loop_size"}, p, loop_id=loop.get("id"), model_axis=kind, orientation=kind))
    refs = (min(outer, key=lambda item_pair: item_pair[1][0][0]) if outer else None, min(outer, key=lambda item_pair: item_pair[1][0][1]) if outer else None)
    for kind, item, axis, ref, p in (("horizontal", xmin, 0, refs[0], (xmin[1][0], view['box']['bottom'] - 0.030)), ("vertical", ymin, 1, refs[1], (view['box']['left'] - 0.022, ymin[1][1]))):
        if ref is None:
            continue
        ref_item, ref_coords = ref
        value = abs(item[1][axis] - ref_coords[0][axis])/scale
        if value > 1e-12:
            out.append(_candidate(kind, view, value, {"loop_id": loop.get("id"), "vertex_ids": [ref_item.get("id"), item[0].get("id")], "reference": "overall_min_vertex", "points_model_m": [ref_coords[1], item[2]], "points_sheet_m": [ref_coords[0], item[1]], "role": "inner_loop_location"}, p, loop_id=loop.get("id"), model_axis=kind, orientation=kind, location=True))
    return out


def _arcs(view: dict[str, Any], unresolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for arc in view.get("arcs") or []:
        if not isinstance(arc, dict) or not _finite(arc.get("radius_m")) or float(arc.get("radius_m")) <= 0 or not _finite(arc.get("span_rad")) or not (0 < float(arc["span_rad"]) < 2 * math.pi - 1e-6):
            unresolved.append({"view": view.get("name"), "arc_id": arc.get("id") if isinstance(arc, dict) else None, "reason": "invalid_or_complete_arc"})
            continue
        if arc.get("face_on") is not True:
            unresolved.append({"view": view.get("name"), "arc_id": arc.get("id"), "reason": "arc_not_face_on"})
            continue
        groups[round(float(arc["radius_m"]), 12)].append(arc)
    out = []
    if len(groups)>6:
        unresolved.append({'view':view.get('name'),'reason':'radius_budget_exceeded','radius_count':len(groups)})
    for group in sorted(groups.values(), key=lambda g: (-max(float(a["span_rad"]) for a in g), float(g[0]["radius_m"]))):
        arc = max(group, key=lambda a: float(a["span_rad"]))
        center = _point(arc.get("center_sheet_m")) or _point(arc.get("center_model_m")) or (0.0, 0.0, 0.0)
        out.append(_candidate(
            "radius", view, float(arc["radius_m"]),
            {"arc_id": arc.get("id"), "center_model_m": arc.get("center_model_m"), "center_sheet_m": arc.get("center_sheet_m"), "span_rad": arc.get("span_rad"), "representative_count": len(group), "role": "face_on_arc"},
            (center[0] + 0.014 * (1 if len(out) % 2 == 0 else -1), center[1] + 0.014 * (1 if len(out) % 3 == 0 else -1)),
            radius_m=float(arc["radius_m"]), representative_count=len(group)))
    return out


def plan_profile_dimensions(views: list[dict[str, Any]], max_dimensions: int = 14) -> dict[str, Any]:
    """Return evidence-backed imported-profile dimension candidates."""
    unresolved: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    if not isinstance(max_dimensions, int) or max_dimensions <= 0:
        return {"status": "blocked", "complete": False, "candidates": [], "unresolved": [{"reason": "invalid_dimension_budget"}]}
    views = sorted(views or [], key=lambda v: -len(v.get('arcs') or []))
    profile_view = next((v for v in views if v.get('arcs') or v.get('inner_loops')), None)
    for view in views:
        if not isinstance(view, dict):
            unresolved.append({"reason": "invalid_view"})
            continue
        scale = _view_scale(view, unresolved)
        if scale is None:
            continue
        vertices = _vertex_map(view, unresolved)
        candidates.extend(_overall(view, scale, vertices, unresolved))
        if view is profile_view:
            candidates.extend(_loop(view, scale, vertices, unresolved))
            candidates.extend(_arcs(view, unresolved))
    # Remove equivalent evidence-backed dimensions while retaining source ids.
    unique: list[dict[str, Any]] = []
    seen = set()
    span_axes = set()
    for item in candidates:
        source = item.get("source_geometry", {})
        role = source.get("role")
        if role == 'profile_vertex_span':
            # Prefer the main profile's references; side views should only
            # supply an uncovered axis (normally plate thickness), not extra
            # spans between different, partially visible outline vertices.
            if item.get('model_axis') in span_axes:
                continue
            span_axes.add(item.get('model_axis'))
        loop_id = source.get("loop_id")
        # Native loop ids are view-qualified; the same linear orientation and
        # model axis must not be emitted twice merely because it appears in
        # front/top projected views.
        loop_key = str(loop_id).split(":")[-1] if loop_id is not None else None
        key = ("linear" if item.get("kind") in ("horizontal","vertical") else item.get("kind"), round(float(item.get("expected_m", 0)), 12),
               item.get("model_axis"), loop_key, role)
        if key not in seen:
            seen.add(key); unique.append(item)
    if len(unique) > max_dimensions:
        unresolved.append({"reason": "dimension_budget_exceeded", "candidate_count": len(unique), "max_dimensions": max_dimensions, "uncovered_count": len(unique) - max_dimensions})
        return {"status": "blocked", "complete": False, "candidates": unique, "unresolved": unresolved}
    if not unique and not unresolved:
        unresolved.append({"reason": "no_profile_geometry_candidates"})
    return {"status": "pass" if not unresolved else "review_required", "complete": not unresolved, "candidates": unique, "unresolved": unresolved}

