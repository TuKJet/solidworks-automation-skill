"""Evidence gate for generated drawing annotation acceptance."""
from __future__ import annotations

import math
from collections.abc import Mapping

from .drawing_quality import quality_metrics


def _visible_dimensions(structure):
    return [d for d in (structure.get("dimensions") or [])
            if isinstance(d, Mapping) and d.get("visibility") == "visible" and d.get("visible") is not False]


def _issue(code, message, **details):
    return {"code": code, "message": message, **details}


def evaluate_annotation_acceptance(spec, structure, repair, review):
    """Return a conservative review gate; never claims manufacturing release."""
    spec = spec if isinstance(spec, Mapping) else {}
    structure = structure if isinstance(structure, Mapping) else {}
    repair = repair if isinstance(repair, Mapping) else {}
    review = review if isinstance(review, Mapping) else {}
    issues = []
    blockers = []
    visible = _visible_dimensions(structure)

    if spec.get("insertModelDimensions", True) and not visible:
        blockers.append(_issue("DRAWING_MODEL_DIMENSIONS_MISSING", "请求模型尺寸但没有可见尺寸实体。"))
    if repair.get("missing_diameters"):
        blockers.append(_issue("DRAWING_CIRCULAR_DIAMETERS_MISSING", "存在未覆盖的真实圆边直径。", missing=repair["missing_diameters"]))
    if repair.get("unresolved_duplicates"):
        blockers.append(_issue("DRAWING_DUPLICATES_UNRESOLVED", "存在未解决的重复尺寸。", duplicates=repair["unresolved_duplicates"]))
    if (repair.get("cross_view_spacing") or {}).get("unresolved"):
        blockers.append(_issue("DRAWING_CROSS_VIEW_SPACING_UNRESOLVED", "跨视图标注间距风险尚未解决。", unresolved=repair["cross_view_spacing"]["unresolved"]))

    geometry = repair.get("geometry") if isinstance(repair.get("geometry"), Mapping) else {}
    if spec.get("insertModelDimensions", True) and not geometry:
        blockers.append(_issue("DRAWING_GEOMETRY_AUDIT_MISSING", "缺少圆边审计证据，不能判定标注完整性。"))
    geometry_errors = list(geometry.get("errors") or [])
    if geometry_errors:
        assembly = str(spec.get("documentType", "part")).casefold() == "assembly"
        issue = _issue("DRAWING_GEOMETRY_AUDIT_INCOMPLETE", "圆边几何审计存在错误。", errors=geometry_errors)
        (issues if assembly else blockers).append(issue)

    covered_values = []
    for dimension in visible:
        if dimension.get("type2", dimension.get("type")) != 6:
            continue
        system = dimension.get("system_value") if isinstance(dimension.get("system_value"), Mapping) else {}
        value = system.get("value")
        if system.get("unit") == "m" and isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0:
            covered_values.append(float(value))
    required_diameters = set()
    for circle in geometry.get("circles") or []:
        if not isinstance(circle, Mapping) or circle.get("face_on") is not True or circle.get("full_circle") is not True:
            continue
        diameter = circle.get("diameter_m")
        if diameter is None and isinstance(circle.get("diameter_mm"), (int, float)):
            diameter = float(circle["diameter_mm"]) / 1000.0
        if isinstance(diameter, (int, float)) and math.isfinite(float(diameter)):
            required_diameters.add(round(float(diameter), 12))
    missing = [diameter for diameter in sorted(required_diameters)
               if not any(math.isclose(diameter, value, rel_tol=0.0, abs_tol=1e-10) for value in covered_values)]
    if missing:
        blockers.append(_issue("DRAWING_CIRCULAR_DIAMETERS_MISSING", "完整、正视圆边没有对应可见直径值。", missing_diameter_m=missing))

    metrics = quality_metrics(structure, review)
    if metrics["confirmed_overlap_count"] or metrics["pdf_text_overlap_count"] or metrics["outside_or_title_intrusion_count"]:
        blockers.append(_issue("DRAWING_PDF_GEOMETRY_OVERLAP", "PDF/边界证据存在确认重叠或侵入。", metrics=metrics))
    for finding in review.get("findings") or []:
        code = str(finding.get("code", ""))
        if finding.get("severity", finding.get("status")) == "fail" and any(term in code for term in ("OVERLAP", "COLLISION", "OUTSIDE", "INTRUSION")):
            blockers.append(_issue(code, "审查发现报告失败。", finding=finding))

    issues = blockers + issues
    return {"status": "blocked" if blockers else "review_required", "issues": issues,
            "manual_review_required": True, "metrics": metrics,
            "visible_dimension_count": len(visible),
            "required_diameter_count": len(required_diameters),
            "covered_diameter_count": len(required_diameters) - len(missing)}
