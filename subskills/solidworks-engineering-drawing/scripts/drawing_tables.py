"""密集工程图尺寸的坐标/孔表附页。

本模块只消费调用者显式给出的二维几何、尺寸锚点与模型值，不从显示文字、
DXF 尺寸实体或坐标位置猜测尺寸。附页是对原图密集尺寸的可追溯补充，不能
替代完整的制造工程图审查。
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


PAPER_SIZES_MM = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}
_UNIT_TO_MM = {"mm": 1.0, "m": 1000.0}
_VALUE_UNITS = {"mm", "m", "deg", "rad"}


def _blocked(errors: list[dict[str, Any]], expected_ids: list[str]) -> dict[str, Any]:
    """返回不可覆盖任何原尺寸的明确阻断结果。"""
    return {
        "status": "blocked",
        "stage": "preflight",
        "error_code": "DRAWING_COORDINATE_APPENDIX_CONTRACT_INVALID",
        "errors": errors,
        "manifest": {
            "mapped_ids": [],
            "unmapped_ids": expected_ids,
            "coverage": {
                "expected_count": len(expected_ids),
                "mapped_count": 0,
                "coverage_passed": False,
                "safe_to_hide_dimensions": False,
            },
            "limitations": ["未确认的 geometry/scale/value/anchor 不会被坐标表覆盖。"],
        },
        "manual_review_required": True,
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        return None
    x, y = _number(value[0]), _number(value[1])
    return (x, y) if x is not None and y is not None else None


def _identity(item: Mapping[str, Any], collection: str, index: int) -> str:
    value = item.get("canonical_id") or item.get("id")
    return str(value).strip() if value else f"<{collection}-{index + 1}-missing-id>"


def _paper_layout(contract: Mapping[str, Any], errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    layout = dict(contract.get("layout") or {})
    paper = str(layout.get("paper") or "A4").upper()
    orientation = str(layout.get("orientation") or "portrait").lower()
    if paper not in PAPER_SIZES_MM or orientation not in {"portrait", "landscape"}:
        errors.append({"code": "DRAWING_TABLE_LAYOUT_INVALID", "field": "layout.paper/orientation"})
        return None
    width, height = PAPER_SIZES_MM[paper]
    if orientation == "landscape":
        width, height = height, width
    margins = layout.get("margins_mm", [12.0, 12.0, 12.0, 12.0])
    if isinstance(margins, Mapping):
        margins = [margins.get(key) for key in ("left", "bottom", "right", "top")]
    if not isinstance(margins, Sequence) or isinstance(margins, (str, bytes)) or len(margins) != 4:
        errors.append({"code": "DRAWING_TABLE_MARGINS_INVALID", "field": "layout.margins_mm"})
        return None
    margin_values = [_number(value) for value in margins]
    row_height = _number(layout.get("row_height_mm", 6.0))
    header_height = _number(layout.get("header_height_mm", 12.0))
    if (
        any(value is None or value < 0 for value in margin_values)
        or row_height is None or row_height <= 0
        or header_height is None or header_height <= 0
    ):
        errors.append({"code": "DRAWING_TABLE_LAYOUT_INVALID", "field": "layout"})
        return None
    left, bottom, right, top = margin_values
    usable_width, usable_height = width - left - right, height - bottom - top
    if usable_width <= 0 or usable_height <= header_height + row_height:
        errors.append({"code": "DRAWING_TABLE_LAYOUT_NO_USABLE_AREA", "field": "layout"})
        return None
    # Reserve the title band and one complete column-header row.
    capacity = int((usable_height - header_height) // row_height) - 1
    if capacity < 1:
        errors.append({"code": "DRAWING_TABLE_LAYOUT_NO_ROW_CAPACITY", "field": "layout.row_height_mm"})
        return None
    return {
        "paper": paper,
        "orientation": orientation,
        "width_mm": width,
        "height_mm": height,
        "margins_mm": {"left": left, "bottom": bottom, "right": right, "top": top},
        "row_height_mm": row_height,
        "header_height_mm": header_height,
        "row_capacity": capacity,
    }


def _validate_geometry(value: Any, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append({"code": "DRAWING_TABLE_GEOMETRY_MISSING", "field": "geometry"})
        return None
    unit = str(value.get("unit") or "").lower()
    scale = _number(value.get("scale"))
    origin = _point(value.get("origin_mm"))
    entities = list(value.get("entities") or [])
    native_source = value.get("native_source_dxf")
    native_handles = [str(item) for item in value.get("selected_handles") or []]
    native_transform = value.get("native_transform")
    native_mode = bool(native_source or native_handles or native_transform)
    if unit not in _UNIT_TO_MM or scale is None or scale <= 0 or origin is None or (not entities and not native_mode):
        errors.append({"code": "DRAWING_TABLE_GEOMETRY_SCALE_UNCONFIRMED", "field": "geometry.unit/scale/origin_mm/entities"})
        return None
    if native_mode:
        if not isinstance(native_transform, Mapping) or not Path(str(native_source or "")).is_file() or not native_handles:
            errors.append({"code": "DRAWING_TABLE_NATIVE_GEOMETRY_UNCONFIRMED", "field": "geometry.native_source_dxf/selected_handles/native_transform"})
            return None
        source_origin = _point(native_transform.get("source_origin"))
        translate_mm = _point(native_transform.get("translate_mm"))
        transform_scale = _number(native_transform.get("scale"))
        bounds = native_transform.get("bounds_mm")
        if source_origin is None or translate_mm is None or transform_scale is None or transform_scale <= 0 or not isinstance(bounds, Sequence) or len(bounds) != 4:
            errors.append({"code": "DRAWING_TABLE_NATIVE_GEOMETRY_UNCONFIRMED", "field": "geometry.native_transform"})
            return None
        bounds_values = [_number(item) for item in bounds]
        if any(item is None for item in bounds_values) or bounds_values[2] <= bounds_values[0] or bounds_values[3] <= bounds_values[1]:
            errors.append({"code": "DRAWING_TABLE_NATIVE_GEOMETRY_UNCONFIRMED", "field": "geometry.native_transform.bounds_mm"})
            return None
        return {
            "unit": unit, "scale": scale, "origin_mm": origin, "factor_to_page_mm": _UNIT_TO_MM[unit] * scale,
            # Native entities intentionally remain in the source DXF.  In particular,
            # projecting a spline into an invented JSON polyline loses manufacturing
            # geometry, so the renderer copies the selected original entities.
            "entities": [], "native_source_dxf": str(Path(str(native_source)).resolve()),
            "selected_handles": native_handles,
            "native_transform": {"source_origin": source_origin, "translate_mm": translate_mm, "scale": transform_scale, "bounds_mm": bounds_values},
            "native_entity_count": len(native_handles),
        }
    normalized = []
    factor = _UNIT_TO_MM[unit] * scale
    for index, entity in enumerate(entities):
        if not isinstance(entity, Mapping):
            errors.append({"code": "DRAWING_TABLE_GEOMETRY_ENTITY_INVALID", "index": index})
            continue
        kind = str(entity.get("kind") or "").upper()
        record = {"kind": kind}
        if kind == "LINE":
            record["start"] = _point(entity.get("start"))
            record["end"] = _point(entity.get("end"))
            valid = record["start"] is not None and record["end"] is not None
        elif kind in {"CIRCLE", "ARC"}:
            record["center"] = _point(entity.get("center"))
            record["radius"] = _number(entity.get("radius"))
            valid = record["center"] is not None and record["radius"] is not None and record["radius"] > 0
            if kind == "ARC":
                record["start_angle"] = _number(entity.get("start_angle"))
                record["end_angle"] = _number(entity.get("end_angle"))
                valid = valid and record["start_angle"] is not None and record["end_angle"] is not None
        elif kind == "LWPOLYLINE":
            record["points"] = [_point(point) for point in entity.get("points") or []]
            record["closed"] = bool(entity.get("closed", False))
            valid = len(record["points"]) >= 2 and all(point is not None for point in record["points"])
        else:
            valid = False
        if not valid:
            errors.append({"code": "DRAWING_TABLE_GEOMETRY_ENTITY_INVALID", "index": index, "kind": kind})
            continue
        normalized.append(record)
    if errors:
        return None
    return {"unit": unit, "scale": scale, "origin_mm": origin, "factor_to_page_mm": factor, "entities": normalized}


def _display_value(value: float, unit: str) -> tuple[float, str]:
    if unit == "m":
        return value * 1000.0, "mm"
    if unit == "rad":
        return math.degrees(value), "deg"
    return value, unit


def _validate_dimensions(items: Any, errors: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    result, expected_ids = [], []
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        errors.append({"code": "DRAWING_TABLE_DIMENSIONS_INVALID", "field": "dimensions"})
        return result, expected_ids
    for index, item in enumerate(items):
        mapping = item if isinstance(item, Mapping) else {}
        identifier = _identity(mapping, "dimension", index)
        expected_ids.append(identifier)
        anchors = [_point(point) for point in mapping.get("anchor_points") or []]
        value = _number(mapping.get("model_value"))
        unit = str(mapping.get("unit") or "").lower()
        scale = _number(mapping.get("scale"))
        source = str(mapping.get("source") or "").strip()
        view = str(mapping.get("view") or "").strip()
        configuration = str(mapping.get("configuration") or "").strip()
        datum = str(mapping.get("datum") or "").strip()
        kind = str(mapping.get("kind") or "").lower()
        axis = str(mapping.get("axis") or "").lower()
        if (
            not isinstance(item, Mapping) or identifier.startswith("<") or value is None or unit not in _VALUE_UNITS
            or scale is None or scale <= 0 or not anchors or any(point is None for point in anchors)
            or not source or not view or not configuration or not datum
        ):
            errors.append({"code": "DRAWING_TABLE_DIMENSION_MAPPING_UNCONFIRMED", "canonical_id": identifier, "index": index})
            continue
        display_value, display_unit = _display_value(value, unit)
        linear_verified = False
        if kind in {"linear", "distance"}:
            if axis not in {"x", "y", "distance"} or len(anchors) != 2:
                errors.append({"code": "DRAWING_TABLE_LINEAR_ANCHOR_INVALID", "canonical_id": identifier})
                continue
            distance = abs(anchors[1][0]-anchors[0][0]) if axis == "x" else abs(anchors[1][1]-anchors[0][1]) if axis == "y" else math.hypot(anchors[1][0]-anchors[0][0], anchors[1][1]-anchors[0][1])
            expected_mm = value * _UNIT_TO_MM.get(unit, 1.0)
            linear_verified = math.isclose(distance * scale, expected_mm, abs_tol=float(mapping.get("tolerance_mm", 0.01)))
            if not linear_verified:
                errors.append({"code": "DRAWING_TABLE_LINEAR_VALUE_MISMATCH", "canonical_id": identifier, "coordinate_mm": distance * scale, "model_mm": expected_mm})
        result.append({
            "canonical_id": identifier,
            "source": source,
            "view": view,
            "configuration": configuration,
            "datum": datum,
            "model_value": value,
            "unit": unit,
            "display_value": display_value,
            "display_unit": display_unit,
            "scale": scale,
            "anchor_points": [[point[0], point[1]] for point in anchors],
            "kind": kind, "axis": axis, "linear_verified": linear_verified,
        })
    return result, expected_ids


def _validate_holes(items: Any, errors: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    result, expected_ids = [], []
    if items is None:
        return result, expected_ids
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        errors.append({"code": "DRAWING_TABLE_HOLES_INVALID", "field": "holes"})
        return result, expected_ids
    for index, item in enumerate(items):
        mapping = item if isinstance(item, Mapping) else {}
        identifier = _identity(mapping, "hole", index)
        expected_ids.append(identifier)
        diameter = _number(mapping.get("diameter"))
        unit = str(mapping.get("unit") or "mm").lower()
        count = mapping.get("count")
        positions = [_point(point) for point in mapping.get("xy") or []]
        source = str(mapping.get("source") or "").strip()
        datum = str(mapping.get("datum") or "").strip()
        if (
            not isinstance(item, Mapping) or identifier.startswith("<") or diameter is None or diameter <= 0
            or unit not in _UNIT_TO_MM or not isinstance(count, int) or count <= 0 or len(positions) != count
            or any(point is None for point in positions) or not source or not datum
        ):
            errors.append({"code": "DRAWING_TABLE_HOLE_MAPPING_UNCONFIRMED", "canonical_id": identifier, "index": index})
            continue
        result.append({
            "canonical_id": identifier,
            "diameter": diameter * _UNIT_TO_MM[unit],
            "unit": "mm",
            "count": count,
            "xy": [[point[0] * _UNIT_TO_MM[unit], point[1] * _UNIT_TO_MM[unit]] for point in positions],
            "source": source,
            "datum": datum,
        })
    return result, expected_ids


def _chunk(items: list[dict[str, Any]], capacity: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + capacity] for index in range(0, len(items), capacity)]


def _csv_rows(rows: list[dict[str, Any]], kind: str) -> tuple[list[str], list[list[str]]]:
    if kind == "coordinates":
        header = ["canonical_id", "model_value", "unit", "display_value", "display_unit", "anchors", "datum", "source", "view", "configuration", "scale", "row_id", "axis", "point_ids"]
        values = [[
            row["canonical_id"], str(row["model_value"]), row["unit"], str(row["display_value"]), row["display_unit"],
            "; ".join(f"({x:g},{y:g})" for x, y in row["anchor_points"]), row["datum"], row["source"], row["view"], row["configuration"], str(row["scale"]), f"R{row.get('_ordinal', '')}", row.get("axis", ""), ";".join(row.get("_point_ids", [])),
        ] for row in rows]
    else:
        header = ["canonical_id", "diameter_mm", "count", "xy", "datum", "source"]
        values = [[
            row["canonical_id"], str(row["diameter"]), str(row["count"]),
            "; ".join(f"({x:g},{y:g})" for x, y in row["xy"]), row["datum"], row["source"],
        ] for row in rows]
    return header, values


def _write_csv(path: Path, rows: list[dict[str, Any]], kind: str) -> None:
    header, values = _csv_rows(rows, kind)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(values)


def _geometry_bounds(geometry: Mapping[str, Any]) -> tuple[float, float, float, float]:
    if geometry.get("native_transform"):
        return tuple(float(value) for value in geometry["native_transform"]["bounds_mm"])
    factor = float(geometry["factor_to_page_mm"])
    origin_x, origin_y = geometry["origin_mm"]
    points = []
    for entity in geometry["entities"]:
        kind = entity["kind"]
        if kind == "LINE":
            points.extend((entity["start"], entity["end"]))
        elif kind in {"CIRCLE", "ARC"}:
            x, y = entity["center"]
            radius = entity["radius"]
            points.extend(((x - radius, y - radius), (x + radius, y + radius)))
        else:
            points.extend(entity["points"])
    transformed = [(origin_x + x * factor, origin_y + y * factor) for x, y in points]
    return min(x for x, _ in transformed), min(y for _, y in transformed), max(x for x, _ in transformed), max(y for _, y in transformed)


def _add_geometry(msp, geometry: Mapping[str, Any]) -> None:
    if geometry.get("native_source_dxf"):
        import ezdxf
        from ezdxf.math import Matrix44

        source = ezdxf.readfile(geometry["native_source_dxf"])
        selected = set(geometry["selected_handles"])
        transform = geometry["native_transform"]
        sx, sy = transform["source_origin"]
        tx, ty = transform["translate_mm"]
        matrix = Matrix44.translate(-sx, -sy, 0) @ Matrix44.scale(transform["scale"]) @ Matrix44.translate(tx, ty, 0)
        copied = 0
        for entity in source.modelspace():
            if str(entity.dxf.handle) not in selected:
                continue
            clone = entity.copy()
            clone.transform(matrix)
            clone.dxf.layer = "SOURCE_GEOMETRY"
            if clone.dxf.is_supported('color'):
                clone.dxf.color = 1
            msp.add_entity(clone)
            copied += 1
        if copied != len(selected):
            raise RuntimeError("native DXF selected handle was not copied")
        return
    factor = float(geometry["factor_to_page_mm"])
    ox, oy = geometry["origin_mm"]
    point = lambda value: (ox + value[0] * factor, oy + value[1] * factor)
    for entity in geometry["entities"]:
        kind = entity["kind"]
        if kind == "LINE":
            msp.add_line(point(entity["start"]), point(entity["end"]), dxfattribs={"layer": "SOURCE_GEOMETRY"})
        elif kind == "CIRCLE":
            msp.add_circle(point(entity["center"]), entity["radius"] * factor, dxfattribs={"layer": "SOURCE_GEOMETRY"})
        elif kind == "ARC":
            msp.add_arc(point(entity["center"]), entity["radius"] * factor, entity["start_angle"], entity["end_angle"], dxfattribs={"layer": "SOURCE_GEOMETRY"})
        elif kind == "LWPOLYLINE":
            msp.add_lwpolyline([point(item) for item in entity["points"]], close=entity["closed"], dxfattribs={"layer": "SOURCE_GEOMETRY"})


def _native_page_point(geometry: Mapping[str, Any], point: Sequence[float]) -> tuple[float, float]:
    transform = geometry["native_transform"]
    sx, sy = transform["source_origin"]
    tx, ty = transform["translate_mm"]
    factor = float(transform["scale"])
    return tx + (float(point[0]) - sx) * factor, ty + (float(point[1]) - sy) * factor


def _render_table_dxf(path: Path, layout: Mapping[str, Any], geometry: Mapping[str, Any], rows: list[dict[str, Any]], kind: str, page_number: int) -> None:
    import ezdxf

    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.MM
    doc.layers.add("SOURCE_GEOMETRY", color=1)
    doc.layers.add("COORDINATE_TABLE", color=1)
    msp = doc.modelspace()
    width, height = layout["width_mm"], layout["height_mm"]
    margins = layout["margins_mm"]
    left, bottom, right, top = margins["left"], margins["bottom"], margins["right"], margins["top"]
    msp.add_lwpolyline([(0, 0), (width, 0), (width, height), (0, height)], close=True, dxfattribs={"layer": "COORDINATE_TABLE"})
    _add_geometry(msp, geometry)
    region = layout.get("table_region_mm") or {"left": left, "right": width-right}
    table_left, table_right = float(region["left"]), float(region["right"])
    if kind == "coordinates":
        point_map = {}
        for row in rows:
            for index, identifier in enumerate(row.get("_point_ids", [])):
                native = row.get("native_first" if index == 0 else "native_second")
                if native is not None:
                    point_map[identifier] = _native_page_point(geometry, native)
        bounds = _geometry_bounds(geometry)
        middle_x = (bounds[0] + bounds[2]) / 2
        for left_side in (True, False):
            points = sorted([(key, pt) for key, pt in point_map.items()
                             if (pt[0] <= middle_x) == left_side], key=lambda item: -item[1][1])
            if len(points) * 6.0 > height - top - bottom:
                raise ValueError("Point labels require a larger paper size or additional pages")
            label_x = bounds[0] - 22.0 if left_side else bounds[2] + 9.0
            last_y = height - top + 6.0
            for index, (identifier, (px, py)) in enumerate(points):
                label_y = max(bottom + (len(points)-index-1)*6.0,
                              min(py, last_y-6.0, height-top-3.0))
                last_y = label_y
                msp.add_circle((px, py), 0.6, dxfattribs={"layer": "COORDINATE_TABLE"})
                end_x = label_x + 8.0 if left_side else label_x - 1.0
                msp.add_line((px, py), (end_x, label_y+1.0), dxfattribs={"layer": "COORDINATE_TABLE"})
                msp.add_text(identifier, dxfattribs={"height": 3.0, "insert": (label_x, label_y), "layer": "COORDINATE_TABLE"})
        datum = geometry.get("native_datum_origin")
        if datum is not None:
            ox, oy = _native_page_point(geometry, datum)
            msp.add_line((ox - 4.0, oy), (ox + 4.0, oy), dxfattribs={"layer": "COORDINATE_TABLE", "color": 1})
            msp.add_line((ox, oy - 4.0), (ox, oy + 4.0), dxfattribs={"layer": "COORDINATE_TABLE", "color": 1})
            msp.add_text("O (REFERENCE)", dxfattribs={"height": 2.0, "insert": (ox + 4.5, oy + 3.0), "layer": "COORDINATE_TABLE", "color": 1})
    title_y = height - top - 4.0
    title = "COORDINATE DIMENSION APPENDIX" if kind == "coordinates" else "HOLE COORDINATE APPENDIX"
    msp.add_text(f"{title} - PAGE {page_number}", dxfattribs={"height": 4.0, "insert": (table_left, title_y), "layer": "COORDINATE_TABLE"})
    headers, values = _csv_rows(rows, kind)
    if kind == "coordinates":
        column_headers = ["Row", "Value", "Unit", "Axis", "From", "To"]
        values = [[f"R{row['_ordinal']}", f"{row['display_value']:g}", row['display_unit'], row["axis"].upper(), *row["_point_ids"][:2]] for row in rows]
    else:
        column_headers = ["Hole", "Dia mm", "X mm", "Y mm"]
        values = [[row['_row_id'], row["diameter"], *(f"{p:g}" for p in row["xy"][0])] for row in rows]
    column_width = (table_right - table_left) / len(column_headers)
    top_y = height - top - layout['header_height_mm']
    row_height = layout["row_height_mm"]
    for index in range(len(rows) + 2):
        y = top_y - index * row_height
        msp.add_line((table_left, y), (table_right, y), dxfattribs={"layer": "COORDINATE_TABLE"})
    for index in range(len(column_headers) + 1):
        x = table_left + index * column_width
        msp.add_line((x, top_y), (x, top_y - (len(rows) + 1) * row_height), dxfattribs={"layer": "COORDINATE_TABLE"})
    text_height = min(3.0, row_height * 0.55)
    for index, header in enumerate(column_headers):
        msp.add_text(header, dxfattribs={"height": text_height, "insert": (table_left + index * column_width + 1, top_y - row_height * .75), "layer": "COORDINATE_TABLE"})
    for row_index, row in enumerate(values):
        for column_index in range(len(column_headers)):
            value = str(row[column_index])
            msp.add_text(value, dxfattribs={"height": text_height, "insert": (table_left + column_index * column_width + 1, top_y - (row_index + 1.75) * row_height), "layer": "COORDINATE_TABLE"})
    doc.saveas(path)


def _render_pdf(dxf_path: Path, pdf_path: Path, layout: Mapping[str, Any]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    document = ezdxf.readfile(dxf_path)
    figure = plt.figure(figsize=(layout["width_mm"] / 25.4, layout["height_mm"] / 25.4), dpi=144)
    axes = figure.add_axes([0, 0, 1, 1])
    axes.set_aspect("equal")
    axes.axis("off")
    axes.set_xlim(0, layout["width_mm"])
    axes.set_ylim(0, layout["height_mm"])
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE, color_policy=config.ColorPolicy.BLACK)
    Frontend(RenderContext(document), MatplotlibBackend(axes), config=cfg).draw_layout(document.modelspace(), finalize=True)
    # The backend finalizer autoscales; restore the physical sheet limits after it.
    axes.set_xlim(0, layout["width_mm"])
    axes.set_ylim(0, layout["height_mm"])
    figure.savefig(pdf_path, format="pdf", facecolor="white", bbox_inches=None, pad_inches=0)
    plt.close(figure)


def _native_projection_blocked(
    errors: list[dict[str, Any]], expected_ids: list[str], not_covered_ids: list[str],
) -> dict[str, Any]:
    """Return a non-renderable native projection contract without hiding evidence."""
    result = _blocked(errors, expected_ids)
    result["error_code"] = "DRAWING_NATIVE_PROJECTION_CONTRACT_BLOCKED"
    result["not_covered_ids"] = not_covered_ids
    result["expected_dimension_ids"] = expected_ids
    return result


def _native_xy(value: Any) -> tuple[float, float] | None:
    """Read an ezdxf vector-like value without accepting missing Z as a coordinate."""
    x, y = _number(getattr(value, "x", None)), _number(getattr(value, "y", None))
    return (x, y) if x is not None and y is not None else None


def _scale_ratio(value: Any) -> float | None:
    """Normalise SolidWorks ScaleRatio-style [numerator, denominator] input."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        numerator, denominator = _number(value[0]), _number(value[1])
        if numerator is None or denominator is None or denominator == 0:
            return None
        value = numerator / denominator
    scale = _number(value)
    return scale if scale is not None and scale > 0 else None


def _native_dimension_record(entity: Any) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float], float] | None:
    """Read the text anchor, definition points and principal baseline angle of one DIMENSION."""
    attrs = entity.dxf
    text = _native_xy(attrs.get("text_midpoint"))
    first = _native_xy(attrs.get("defpoint2"))
    second = _native_xy(attrs.get("defpoint3"))
    angle = _number(attrs.get("angle", 0.0))
    if text is None or first is None or second is None or angle is None:
        return None
    return text, first, second, angle % 180.0


def _native_axis(angle: float, tolerance: float = 0.01) -> str | None:
    if min(abs(angle), abs(angle - 180.0)) <= tolerance:
        return "x"
    if abs(angle - 90.0) <= tolerance:
        return "y"
    return None


def _outline_mm(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    numbers = [_number(item) for item in value]
    if any(item is None for item in numbers):
        return None
    left, bottom, right, top = [float(item) * 1000.0 for item in numbers]
    return (left, bottom, right, top) if right > left and top > bottom else None


def _native_projection_geometry(source_dxf: Path, outline_mm: tuple[float, float, float, float], layout: Mapping[str, Any], errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select genuine source entities wholly in the view rectangle and fit them on paper."""
    try:
        import ezdxf
        from ezdxf import bbox
    except ImportError:
        errors.append({"code": "DRAWING_NATIVE_DXF_DEPENDENCY_MISSING", "dependency": "ezdxf"})
        return None
    try:
        document = ezdxf.readfile(source_dxf)
    except Exception as exc:
        errors.append({"code": "DRAWING_NATIVE_DXF_READ_FAILED", "source_dxf": str(source_dxf), "error": str(exc)})
        return None
    left, bottom, right, top = outline_mm
    selected: list[Any] = []
    allowed = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "LWPOLYLINE", "POLYLINE"}
    for entity in document.modelspace():
        if entity.dxftype() not in allowed:
            continue
        try:
            extent = bbox.extents([entity])
        except Exception:
            # An entity whose bounds cannot be proven is not represented or copied.
            continue
        if not extent.has_data:
            continue
        if (
            extent.extmin.x >= left - 0.01 and extent.extmax.x <= right + 0.01
            and extent.extmin.y >= bottom - 0.01 and extent.extmax.y <= top + 0.01
        ):
            selected.append(entity)
    if not selected:
        errors.append({"code": "DRAWING_NATIVE_GEOMETRY_UNCONFIRMED", "source_dxf": str(source_dxf), "view_outline_mm": list(outline_mm)})
        return None
    try:
        body = bbox.extents(selected)
    except Exception as exc:
        errors.append({"code": "DRAWING_NATIVE_GEOMETRY_UNCONFIRMED", "error": str(exc)})
        return None
    body_width, body_height = body.size.x, body.size.y
    if body_width <= 0 or body_height <= 0:
        errors.append({"code": "DRAWING_NATIVE_GEOMETRY_UNCONFIRMED", "reason": "degenerate_bbox"})
        return None
    margins = layout["margins_mm"]
    usable_width = layout["width_mm"] - margins["left"] - margins["right"]
    usable_height = layout["height_mm"] - margins["bottom"] - margins["top"]
    table_min_width, gap = 105.0, 8.0
    geometry_width = usable_width - table_min_width - gap
    if geometry_width <= 0:
        errors.append({"code": "DRAWING_TABLE_GEOMETRY_TABLE_OVERLAP"})
        return None
    fit = min((geometry_width * 0.70) / body_width, (usable_height * 0.90) / body_height)
    if fit <= 0 or not math.isfinite(fit):
        errors.append({"code": "DRAWING_NATIVE_GEOMETRY_UNCONFIRMED", "reason": "fit"})
        return None
    final_width, final_height = body_width * fit, body_height * fit
    region_left = margins["left"]
    target_center_x = region_left + geometry_width / 2.0
    target_center_y = margins["bottom"] + usable_height / 2.0
    source_origin = (body.extmin.x, body.extmin.y)
    translate = (target_center_x - final_width / 2.0, target_center_y - final_height / 2.0)
    return {
        "unit": "mm",
        "scale": 1.0,
        "origin_mm": [0.0, 0.0],
        "entities": [],
        "native_source_dxf": str(source_dxf.resolve()),
        "selected_handles": [str(entity.dxf.handle) for entity in selected],
        "native_transform": {
            "source_origin": list(source_origin),
            "translate_mm": list(translate),
            "scale": fit,
            "bounds_mm": [translate[0], translate[1], translate[0] + final_width, translate[1] + final_height],
        },
    }


def contract_from_native_projection(
    source_dxf: str | Path,
    view_name: str,
    view_outline_m: Sequence[float],
    view_scale: float | Sequence[float],
    dimensions: Sequence[Mapping[str, Any]],
    source_model: str,
    configuration: str,
    layout: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a strict coordinate-appendix contract from a native drawing projection.

    Only Type2 (linear) model dimensions are transcribed.  A model dimension must
    have exactly one unused DXF DIMENSION text anchor within 0.05 mm and must agree
    with its DXF definition-point delta after applying the view scale.  The result
    deliberately retains unsupported (for example radial) identities in
    ``not_covered_ids`` for the native drawing; callers must never globally hide
    drawing dimensions from this contract alone.
    """
    errors: list[dict[str, Any]] = []
    source = Path(source_dxf).expanduser()
    outline = _outline_mm(view_outline_m)
    scale = _scale_ratio(view_scale)
    requested_layout = _paper_layout({"layout": dict(layout or {"paper": "A4", "orientation": "landscape"})}, errors)
    linear: list[Mapping[str, Any]] = []
    not_covered: list[str] = []
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        errors.append({"code": "DRAWING_NATIVE_DIMENSIONS_INVALID"})
        source_dimensions: Sequence[Any] = []
    else:
        source_dimensions = dimensions
    for index, raw in enumerate(source_dimensions):
        if not isinstance(raw, Mapping):
            errors.append({"code": "DRAWING_NATIVE_DIMENSION_IDENTITY_INVALID", "index": index})
            continue
        name = str(raw.get("name") or raw.get("canonical_id") or f"<dimension-{index + 1}-missing-id>").strip()
        kind = _number(raw.get("type"))
        if kind == 2:
            linear.append(raw)
            if name.startswith("<"):
                errors.append({"code": "DRAWING_NATIVE_DIMENSION_IDENTITY_INVALID", "index": index})
        else:
            not_covered.append(name)
    expected_ids = [str(raw.get("name") or raw.get("canonical_id") or f"<dimension-{index + 1}-missing-id>").strip() for index, raw in enumerate(linear)]
    if not source.is_file():
        errors.append({"code": "DRAWING_NATIVE_DXF_MISSING", "source_dxf": str(source)})
    if outline is None:
        errors.append({"code": "DRAWING_NATIVE_VIEW_OUTLINE_INVALID"})
    if scale is None:
        errors.append({"code": "DRAWING_NATIVE_VIEW_SCALE_INVALID"})
    if not str(view_name).strip() or not str(source_model).strip() or not str(configuration).strip():
        errors.append({"code": "DRAWING_NATIVE_DIMENSION_IDENTITY_INVALID"})
    if not linear:
        errors.append({"code": "DRAWING_NATIVE_LINEAR_DIMENSIONS_MISSING"})
    if errors or requested_layout is None or outline is None or scale is None:
        return _native_projection_blocked(errors, expected_ids, not_covered)
    try:
        import ezdxf
    except ImportError:
        return _native_projection_blocked([{"code": "DRAWING_NATIVE_DXF_DEPENDENCY_MISSING", "dependency": "ezdxf"}], expected_ids, not_covered)
    try:
        document = ezdxf.readfile(source)
    except Exception as exc:
        return _native_projection_blocked([{"code": "DRAWING_NATIVE_DXF_READ_FAILED", "error": str(exc)}], expected_ids, not_covered)
    native_dimensions = []
    for entity in document.modelspace():
        if entity.dxftype() != "DIMENSION":
            continue
        record = _native_dimension_record(entity)
        if record is not None:
            native_dimensions.append((str(entity.dxf.handle), entity, record))
    used_handles: set[str] = set()
    references: list[dict[str, Any]] = []
    for index, model_dimension in enumerate(linear):
        identifier = expected_ids[index]
        position = model_dimension.get("position")
        if not isinstance(position, Sequence) or isinstance(position, (str, bytes)) or len(position) < 2:
            errors.append({"code": "DRAWING_NATIVE_DIMENSION_POSITION_INVALID", "canonical_id": identifier})
            continue
        position_mm = (_number(position[0]), _number(position[1]))
        value_system = _number(model_dimension.get("value_system"))
        if position_mm[0] is None or position_mm[1] is None or value_system is None or value_system < 0:
            errors.append({"code": "DRAWING_NATIVE_DIMENSION_VALUE_INVALID", "canonical_id": identifier})
            continue
        target = (position_mm[0] * 1000.0, position_mm[1] * 1000.0)
        candidates = [(handle, entity, record) for handle, entity, record in native_dimensions if math.hypot(record[0][0] - target[0], record[0][1] - target[1]) <= 0.05]
        if len(candidates) != 1:
            errors.append({"code": "DRAWING_NATIVE_PROJECTION_DIMENSION_UNMATCHED" if not candidates else "DRAWING_NATIVE_PROJECTION_DIMENSION_AMBIGUOUS", "canonical_id": identifier, "candidate_count": len(candidates)})
            continue
        handle, _entity, record = candidates[0]
        if handle in used_handles:
            errors.append({"code": "DRAWING_NATIVE_PROJECTION_DIMENSION_REUSED", "canonical_id": identifier, "handle": handle})
            continue
        _text, first, second, angle = record
        axis = _native_axis(angle)
        if axis is None:
            errors.append({"code": "DRAWING_NATIVE_PROJECTION_DIMENSION_NONAXIS", "canonical_id": identifier, "angle": angle})
            continue
        paper_delta = abs(second[0] - first[0]) if axis == "x" else abs(second[1] - first[1])
        model_mm = value_system * 1000.0
        if not math.isclose(paper_delta / scale, model_mm, abs_tol=0.00002):
            errors.append({"code": "DRAWING_NATIVE_PROJECTION_VALUE_MISMATCH", "canonical_id": identifier, "paper_delta_mm": paper_delta, "view_scale": scale, "model_value_mm": model_mm})
            continue
        used_handles.add(handle)
        references.append({"canonical_id": identifier, "axis": axis, "first": first, "second": second, "model_value_m": value_system, "native_handle": handle})
    if errors or len(references) != len(linear):
        return _native_projection_blocked(errors, expected_ids, not_covered)
    x_origins = [record["first"][0] for record in references if record["axis"] == "x"]
    y_origins = [record["first"][1] for record in references if record["axis"] == "y"]
    if not x_origins or not y_origins or any(not math.isclose(item, x_origins[0], abs_tol=0.000001) for item in x_origins) or any(not math.isclose(item, y_origins[0], abs_tol=0.000001) for item in y_origins):
        return _native_projection_blocked([{"code": "DRAWING_NATIVE_PROJECTION_DATUM_UNSTABLE"}], expected_ids, not_covered)
    contract_dimensions = []
    for record in references:
        first, second = record["first"], record["second"]
        anchors = [[(first[0] - x_origins[0]) / scale, (first[1] - y_origins[0]) / scale], [(second[0] - x_origins[0]) / scale, (second[1] - y_origins[0]) / scale]]
        contract_dimensions.append({
            "canonical_id": record["canonical_id"], "source": str(source_model), "view": str(view_name),
            "configuration": str(configuration), "datum": "native_projection_xy_datum",
            "model_value": record["model_value_m"] * 1000.0, "model_value_system": record["model_value_m"],
            "unit": "mm", "scale": 1.0, "anchor_points": anchors, "kind": "linear", "axis": record["axis"],
            "source_view_scale": scale, "native_dimension_handle": record["native_handle"], "tolerance_mm": 0.00002,
            "native_first": list(first), "native_second": list(second),
        })
    geometry = _native_projection_geometry(source, outline, requested_layout, errors)
    if errors or geometry is None:
        return _native_projection_blocked(errors, expected_ids, not_covered)
    return {
        "status": "ready", "geometry": geometry, "dimensions": contract_dimensions, "holes": [],
        "layout": dict(layout or {"paper": "A4", "orientation": "landscape"}),
        "expected_dimension_ids": expected_ids, "not_covered_ids": not_covered,
        "source_view_scale": scale, "source_dxf": str(source.resolve()),
        "native_datum_origin": [x_origins[0], y_origins[0]],
        # A vector appendix exists, but readable-label review has not happened yet.
        "render_text_layout_complete": False,
        "manual_review_required": True,
    }


def generate_coordinate_appendices(contract: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """生成可分页 CSV + 真矢量 DXF/PDF 的尺寸坐标及孔表附页。

    ``contract`` 必须显式提供 geometry(unit/scale/origin_mm/entities)、每个
    dimension 的 model_value/unit/scale/anchor_points/canonical_id/source/view/
    configuration/datum，以及每个 hole 的 canonical_id/diameter/count/xy/source/
    datum。任何局部不确定性都会阻断整批，且 ``safe_to_hide_dimensions`` 保持 false。
    """
    if not isinstance(contract, Mapping):
        return _blocked([{"code": "DRAWING_TABLE_CONTRACT_NOT_MAPPING"}], [])
    errors: list[dict[str, Any]] = []
    layout = _paper_layout(contract, errors)
    geometry = _validate_geometry(contract.get("geometry"), errors)
    dimensions, dimension_ids = _validate_dimensions(contract.get("dimensions"), errors)
    holes, hole_ids = _validate_holes(contract.get("holes", []), errors)
    if geometry is not None and contract.get("native_datum_origin") is not None:
        geometry["native_datum_origin"] = [float(x) for x in contract["native_datum_origin"]]
    for source_row, validated_row in zip(contract.get("dimensions") or [], dimensions):
        if isinstance(source_row, Mapping):
            validated_row["native_first"] = source_row.get("native_first")
            validated_row["native_second"] = source_row.get("native_second")
    explicit_expected = contract.get("expected_dimension_ids")
    expected_ids = [str(x) for x in explicit_expected] if explicit_expected is not None else dimension_ids + hole_ids
    returned_ids = set(dimension_ids + hole_ids)
    missing_expected = [x for x in expected_ids if x not in returned_ids]
    if missing_expected: errors.append({"code": "DRAWING_TABLE_EXPECTED_DIMENSIONS_UNMAPPED", "ids": missing_expected})
    if len(set(expected_ids)) != len(expected_ids):
        errors.append({"code": "DRAWING_TABLE_CANONICAL_ID_DUPLICATE"})
    if not dimensions and not holes:
        errors.append({"code": "DRAWING_TABLE_ROWS_MISSING"})
    if errors or layout is None or geometry is None:
        return _blocked(errors, expected_ids)

    bounds = _geometry_bounds(geometry)
    margins = layout["margins_mm"]
    if bounds[0] < margins["left"] or bounds[1] < margins["bottom"] or bounds[2] > layout["width_mm"] - margins["right"] or bounds[3] > layout["height_mm"] - margins["top"]:
        return _blocked([{"code": "DRAWING_TABLE_GEOMETRY_OUTSIDE_PAGE", "bounds_mm": list(bounds)}], expected_ids)
    gap = 30.0 if geometry.get("native_source_dxf") else 5.0
    available_right = layout["width_mm"] - margins["right"] - bounds[2] - gap
    available_left = bounds[0] - margins["left"] - gap
    table_width = max(0.0, max(available_right, available_left))
    if table_width < (84.0 if dimensions else 60.0):
        return _blocked([{"code": "DRAWING_TABLE_GEOMETRY_TABLE_OVERLAP", "geometry_bounds_mm": list(bounds), "gap_mm": gap}], expected_ids)
    layout["geometry_region_mm"] = {"left": bounds[0], "bottom": bounds[1], "right": bounds[2], "top": bounds[3]}
    layout["table_region_mm"] = {"left": bounds[2] + gap if available_right >= available_left else margins["left"], "bottom": margins["bottom"], "right": layout["width_mm"] - margins["right"] if available_right >= available_left else bounds[0] - gap, "top": layout["height_mm"] - margins["top"]}
    layout["table_region_width_mm"] = table_width

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    coordinate_csv = destination / "coordinate_dimensions.csv"
    holes_csv = destination / "hole_coordinates.csv"
    point_ids = {}
    point_index = []
    for ordinal, row in enumerate(dimensions, 1):
        row["_ordinal"] = ordinal
        row["_point_ids"] = []
        for point in row["anchor_points"]:
            key = tuple(round(coordinate, 6) for coordinate in point)
            if key not in point_ids:
                identifier = "P" + str(len(point_ids) + 1)
                point_ids[key] = identifier
                point_index.append({"id": identifier, "xy_mm": list(point)})
            row["_point_ids"].append(point_ids[key])
    _write_csv(coordinate_csv, dimensions, "coordinates")
    _write_csv(holes_csv, holes, "holes")
    for ordinal, row in enumerate(dimensions, 1): row["_ordinal"] = ordinal
    page_specs = [("coordinates", chunk) for chunk in _chunk(dimensions, layout["row_capacity"])]
    hole_instances = [{**hole, 'xy': [point], '_row_id': f'H{group}.{instance}'}
                      for group, hole in enumerate(holes, 1)
                      for instance, point in enumerate(hole['xy'], 1)]
    page_specs.extend(("holes", chunk) for chunk in _chunk(hole_instances, layout["row_capacity"]))
    pages = []
    try:
        for index, (kind, rows) in enumerate(page_specs, 1):
            dxf_path = destination / f"appendix_{index:03d}_{kind}.dxf"
            pdf_path = destination / f"appendix_{index:03d}_{kind}.pdf"
            _render_table_dxf(dxf_path, layout, geometry, rows, kind, index)
            _render_pdf(dxf_path, pdf_path, layout)
            if not dxf_path.is_file() or not pdf_path.is_file() or dxf_path.stat().st_size <= 0 or pdf_path.stat().st_size <= 0:
                raise RuntimeError("DXF/PDF artifact verification failed")
            pages.append({
                "page": index,
                "kind": kind,
                "row_count": len(rows),
                "canonical_ids": [row["canonical_id"] for row in rows],
                "dxf": str(dxf_path),
                "pdf": str(pdf_path),
                "geometry_layer": "SOURCE_GEOMETRY",
                "table_layer": "COORDINATE_TABLE",
            })
    except Exception as exc:
        return {
            "status": "failed",
            "stage": "render",
            "error_code": "DRAWING_COORDINATE_APPENDIX_RENDER_FAILED",
            "error": str(exc),
            "manifest": {"mapped_ids": [], "unmapped_ids": expected_ids, "coverage": {"expected_count": len(expected_ids), "mapped_count": 0, "coverage_passed": False, "safe_to_hide_dimensions": False}},
            "manual_review_required": True,
        }
    all_linear = bool(dimensions) and all(row.get("kind") in {"linear", "distance"} and row.get("linear_verified") for row in dimensions)
    no_non_linear = not any(row.get("kind") in {"angle", "radius"} for row in dimensions)
    not_covered_ids = [str(item) for item in contract.get("not_covered_ids") or []]
    coverage = {
        "expected_count": len(expected_ids),
        "mapped_count": len(set(expected_ids) & returned_ids),
        "coverage_passed": not missing_expected,
        # Unsupported native dimensions intentionally stay on the main sheet.  A
        # coordinate appendix is never authority to hide them en masse.
        "safe_to_hide_dimensions": bool(all_linear and no_non_linear and not missing_expected and not not_covered_ids and contract.get("render_text_layout_complete") is True),
    }
    return {
        "status": "pass",
        "stage": "render",
        "manifest": {
            "layout": layout,
            "geometry": {
                "unit": geometry["unit"], "scale": geometry["scale"], "origin_mm": list(geometry["origin_mm"]),
                "entity_count": geometry.get("native_entity_count", len(geometry["entities"])), "bounds_mm": list(bounds),
                "native_source_dxf": geometry.get("native_source_dxf"), "selected_handles": geometry.get("selected_handles", []),
            },
            "point_index": point_index,
            "row_index": [{"row_id": f"R{row['_ordinal']}", "canonical_id": row["canonical_id"], "point_ids": row["_point_ids"]} for row in dimensions],
            "coordinate_csv": str(coordinate_csv),
            "hole_csv": str(holes_csv),
            "pages": pages,
            "mapped_ids": [x for x in expected_ids if x in returned_ids],
            "unmapped_ids": missing_expected,
            "not_covered_ids": not_covered_ids,
            "coverage": coverage,
            "limitations": ["坐标/孔表仅覆盖 manifest 中的显式 canonical_id，不等于完整制造尺寸交付。"],
        },
        "mappedIDs": [x for x in expected_ids if x in returned_ids],
        "unmappedIDs": missing_expected,
        "coverage": coverage,
        "manual_review_required": True,
    }
