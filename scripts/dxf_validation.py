"""Dependency-free structural validation for ASCII DXF drawing exports."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any


def _pairs(path: Path) -> tuple[list[tuple[int, str]], str]:
    raw = path.read_bytes()
    if raw.startswith(b"AutoCAD Binary DXF"):
        raise ValueError("binary AutoCAD DXF is not supported")
    declared = None
    marker = raw.find(b"$DWGCODEPAGE")
    if marker >= 0:
        tail = raw[marker:marker + 160].splitlines()
        if len(tail) >= 3:
            declared = {b"ANSI_936": "cp936", b"ANSI_1252": "cp1252", b"ANSI_932": "cp932", b"UTF-8": "utf-8"}.get(tail[2].strip())
    candidates = [item for item in (declared, "utf-8", "cp936", "cp1252") if item]
    for encoding in dict.fromkeys(candidates):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        raise ValueError("DXF encoding is not recognized")
    lines = text.splitlines()
    if len(lines) % 2:
        raise ValueError("DXF group-code stream has an odd number of lines")
    pairs = []
    for index in range(0, len(lines), 2):
        try:
            code = int(lines[index].strip())
        except ValueError as exc:
            raise ValueError(f"invalid DXF group code at line {index + 1}") from exc
        pairs.append((code, lines[index + 1].strip()))
    return pairs, encoding


def validate_dxf_file(path: Path, *, expected_visible_dimension_count: int | None = None) -> dict[str, Any]:
    target = Path(path)
    base = {"path": str(target), "freecad_compatibility": {"status": "not_tested", "claim": "FreeCAD DXF import not executed"}}
    if not target.is_file() or target.stat().st_size == 0:
        return {**base, "status": "blocked", "error_code": "DXF_MISSING_OR_EMPTY"}
    try:
        pairs, encoding = _pairs(target)
    except ValueError as exc:
        return {**base, "status": "blocked", "error_code": "DXF_PARSE_FAILED", "error": str(exc)}
    sections: list[str] = []
    boundaries = {"section_start": 0, "section_end": 0, "eof": 0}
    entities = Counter()
    section_entities = {"ENTITIES": Counter(), "BLOCKS": Counter()}
    header: dict[str, str] = {}
    section = None
    header_var = None
    for code, value in pairs:
        if code == 0 and value == "SECTION":
            boundaries["section_start"] += 1
            section = None
        elif code == 2 and section is None and value in {"HEADER", "TABLES", "ENTITIES", "BLOCKS"}:
            section = value
            sections.append(value)
        elif code == 0 and value == "ENDSEC":
            boundaries["section_end"] += 1
            section = None
            header_var = None
        elif code == 0 and value == "EOF":
            boundaries["eof"] += 1
        elif section == "HEADER" and code == 9:
            header_var = value
        elif section == "HEADER" and header_var and code in {1, 2, 70, 40, 41}:
            header[header_var] = value
            header_var = None
        elif section in {"ENTITIES", "BLOCKS"} and code == 0 and value not in {"ENDSEC", "SECTION", "EOF"}:
            entities[value] += 1
            section_entities[section][value] += 1
    required_sections = {name: name in sections for name in ("HEADER", "TABLES", "ENTITIES", "BLOCKS")}
    geometry_names = {"LINE", "CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE", "ELLIPSE", "SPLINE", "3DFACE", "POINT", "HATCH", "SOLID", "TRACE"}
    geometry_count = sum(entities[name] for name in geometry_names)
    dimension_count = entities["DIMENSION"]
    direct_dimension_count = section_entities["ENTITIES"]["DIMENSION"]
    text_count = sum(entities[name] for name in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"))
    direct_text_count = sum(section_entities["ENTITIES"][name] for name in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"))
    block_annotation_count = dimension_count - direct_dimension_count + text_count - direct_text_count
    annotation_count = dimension_count + text_count
    checks = {"sections": required_sections, "section_boundaries": boundaries, "eof": boundaries["eof"] == 1,
              "has_geometry": geometry_count > 0, "has_annotations": annotation_count > 0}
    if not checks["eof"] or boundaries["section_start"] != boundaries["section_end"] or not all(required_sections.values()):
        status, error_code = "blocked", "DXF_STRUCTURE_INVALID"
    elif geometry_count == 0:
        status, error_code = "blocked", "DXF_NO_GEOMETRY"
    elif direct_dimension_count + direct_text_count == 0 and block_annotation_count > 0:
        status, error_code = "blocked", "DXF_ANNOTATIONS_ONLY_IN_BLOCKS"
    elif expected_visible_dimension_count and direct_dimension_count < expected_visible_dimension_count:
        status, error_code = "blocked", "DXF_EXPECTED_DIMENSIONS_MISSING"
    else:
        status, error_code = "review_required", None
    result = {**base, "status": status, "encoding": encoding, "checks": checks, "sections": sections, "entity_counts": dict(entities),
              "geometry_entity_count": geometry_count, "native_dimension_count": dimension_count,
              "top_level_native_dimension_count": direct_dimension_count, "top_level_annotation_entity_count": direct_dimension_count + direct_text_count,
              "block_annotation_entity_count": block_annotation_count, "annotation_entity_count": annotation_count,
              "header": {"INSUNITS": header.get("$INSUNITS"), "DIMSCALE": header.get("$DIMSCALE"), "DIMLFAC": header.get("$DIMLFAC")},
              "units": "not_inferred", "exploded_annotation_possible": bool(text_count and not dimension_count)}
    result["dimension_count_check"] = {"expected_visible": expected_visible_dimension_count, "top_level_native": direct_dimension_count,
                                        "matches": direct_dimension_count == expected_visible_dimension_count if expected_visible_dimension_count is not None else None}
    if error_code:
        result["error_code"] = error_code
    return result
