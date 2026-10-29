"""Native annotation repair for newly generated, job-owned drawings only.

Keeps model dimension identities, hides only proven duplicate display instances,
and adds associative reference diameters from face-on, complete circular edges.
No model dimension value is written by this module.
"""
from __future__ import annotations

import math
from collections import Counter

from .drawing_annotation_policy import plan_duplicate_dimensions
from .drawing_annotation_spacing import plan_profile_diameter_spacing
from .sw_connect import create_empty_dispatch_variant, get_com_member
from .sw_export import _resolve_sw_constant


class AnnotationRepairError(RuntimeError):
    code = "DRAWING_ANNOTATION_REPAIR_UNVERIFIED"


def complete_circle(params, limits):
    """Only a closed, one-period edge with a finite positive circle radius."""
    try:
        p, u = list(map(float, params)), list(map(float, limits))
        return (len(p) == 7 and len(u) >= 8 and all(math.isfinite(x) for x in p + u[:8])
                and p[6] > 0 and math.dist(u[:3], u[3:6]) <= 1e-8
                and abs(abs(u[7] - u[6]) - 2 * math.pi) <= 1e-6)
    except (TypeError, ValueError):
        return False


def _project(math_util, transform, values, *, vector=False):
    matrix = list(get_com_member(transform, "ArrayData") or [])
    if len(matrix) != 16 or not all(math.isfinite(float(v)) for v in matrix) or matrix[12] <= 0:
        raise AnnotationRepairError("Drawing-view transform could not be read")
    # IMathTransform.ArrayData: 0..8 rotation, 9..11 translation, 12 scale.
    # SOLIDWORKS uses row vectors; directions exclude translation.
    # https://help.solidworks.com/2023/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IMathTransform.html
    return [sum(float(values[j]) * matrix[j * 3 + i] for j in range(3)) * matrix[12]
            + (0 if vector else matrix[9 + i]) for i in range(3)]


def collect_visible_circles(drawing, sw_app, workflow):
    """Return JSON evidence and separate in-session edge/view handles."""
    get = workflow._safe_member
    edge_type = _resolve_sw_constant("swViewEntityType_Edge", sw_app)
    if edge_type is None:
        return {"status": "review_required", "circles": [], "errors": ["EDGE_ENUM_UNAVAILABLE"]}, []
    sheet = get(drawing, "GetCurrentSheet")
    sheet_name = str(get(sheet, "GetName", default=""))
    if len(list(get(drawing, "GetSheetNames", default=[]) or [])) != 1:
        return {"status": "review_required", "circles": [], "errors": ["MULTISHEET_CIRCLE_REPAIR_UNSUPPORTED"]}, []
    sheet_views = list(get(sheet, "GetViews", default=[]) or [])
    mapped, _, _ = workflow._map_standard_views_from_sheet_views(sheet_views)
    semantics = {str(get(v, "Name", default="")): key for key, v in mapped.items()}
    math_util = None  # Matrix readback avoids version-dependent MathUtility COM calls.
    circles, handles, errors, seen, diagnostics = [], [], [], set(), []
    for view in sheet_views:
        name = str(get(view, "Name", default=""))
        source = str(get(view, "GetReferencedModelName", default=""))
        if source.lower().endswith(".sldasm"):
            errors.append({"view": name, "reason": "ASSEMBLY_CIRCLE_REPAIR_UNSUPPORTED"})
            continue
        transform = get(view, "ModelToViewTransform")
        if transform is None:
            errors.append({"view": name, "reason": "VIEW_TRANSFORM_UNAVAILABLE"})
            continue
        components = list(get(view, "GetVisibleComponents", default=[]) or [])
        if not components:
            component = get(get(view, "RootDrawingComponent"), "Component")
            if component is not None:
                components = [component]
        if len(components) > 1:
            errors.append({"view": name, "reason": "ASSEMBLY_CIRCLE_REPAIR_UNSUPPORTED"})
            continue
        for component in components or [None]:
            stage = "GetVisibleEntities2"
            try:
                edges = get_com_member(view, "GetVisibleEntities2", component if component is not None else create_empty_dispatch_variant(), edge_type)
                diagnostics.append({"view": name, "components": len(components), "edges": len(edges or [])})
                for edge in edges or []:
                    stage = "GetCurve"
                    curve = get(edge, "GetCurve")
                    if not get(curve, "IsCircle", default=False):
                        from .drawing_projected_cylinders import circle_from_cylindrical_boundary
                        projected = circle_from_cylindrical_boundary(edge, transform, workflow)
                        if projected:
                            center = projected["center_sheet_m"]
                            key = (name, round(center[0], 8), round(center[1], 8), round(projected["radius_m"], 10))
                            if key not in seen:
                                seen.add(key)
                                record = {"sheet": sheet_name, "view": name, "semantic_view": semantics.get(name, ""), **projected}
                                circles.append(record)
                                handles.append((record, edge, view))
                        continue
                    params = get(curve, "CircleParams", default=[])
                    limits = get(edge, "GetCurveParams2", default=[])
                    if not complete_circle(params, limits):
                        continue
                    stage = "ProjectCircleAxis"
                    normal = _project(math_util, transform, params[3:6], vector=True)
                    norm = math.sqrt(sum(v * v for v in normal))
                    if norm <= 0 or abs(normal[2]) / norm < 1 - 1e-8:
                        continue  # A model circle seen obliquely is not a drawing circle.
                    stage = "ProjectCircleCenter"
                    center = _project(math_util, transform, params[:3])
                    radius = float(params[6])
                    key = (name, round(center[0], 8), round(center[1], 8), round(radius, 10))
                    if key in seen:
                        continue
                    seen.add(key)
                    record = {"sheet": sheet_name, "view": name, "semantic_view": semantics.get(name, ""),
                              "center_model_m": list(params[:3]), "axis_model": list(params[3:6]),
                              "center_sheet_m": center, "radius_m": radius, "diameter_mm": radius * 2000,
                              "full_circle": True, "face_on": True,
                              "evidence": "IView.GetVisibleEntities2 / ICurve.CircleParams / ModelToViewTransform"}
                    circles.append(record)
                    handles.append((record, edge, view))
            except Exception as exc:
                errors.append({"view": name, "stage": stage, "reason": str(exc)})
    return {"status": "pass" if not errors else "review_required", "circles": circles, "errors": errors, "diagnostics": diagnostics}, handles


def _dimension_objects(drawing, workflow):
    result = []
    get = workflow._safe_member
    sheets = get(drawing, "GetSheetNames", default=[]) or [""]
    for sheet_name in sheets:
        sheet = get(drawing, "GetSheet", sheet_name) or get(drawing, "GetCurrentSheet")
        for view in get(sheet, "GetViews", default=[]) or []:
            for dimension in workflow._view_display_dimensions(view):
                result.append((view, dimension))
    return result


def _dimension_snapshot(drawing, workflow):
    structure = workflow.inspect_drawing_structure(drawing)
    objects = _dimension_objects(drawing, workflow)
    if len(objects) != len(structure["dimensions"]):
        raise AnnotationRepairError("Dimension enumeration changed during readback")
    for record, (view, dd) in zip(structure["dimensions"], objects):
        model_dimension = workflow._display_dimension_model_dimension(dd)
        identity = workflow._dimension_identity(view, dd, model_dimension)
        if identity != record["identity"]:
            raise AnnotationRepairError("Dimension object does not match recorded identity")
        tolerance = workflow._safe_member(model_dimension, "Tolerance")
        tolerance_type = workflow._safe_member(tolerance, "Type")
        record["native_text"] = {"prefix": workflow._safe_member(dd, "GetText", 1),
                                 "suffix": workflow._safe_member(dd, "GetText", 2),
                                 "full": workflow._safe_member(dd, "GetText", 0),
                                 "above": workflow._safe_member(dd, "GetText", 3),
                                 "below": workflow._safe_member(dd, "GetText", 4),
                                 "tolerance": {"type": tolerance_type, "shared_model_dimension": identity["canonical_key"]} if tolerance_type is not None else None}
    return structure, objects


def _model_dimension_inventory(structure):
    return Counter((d["identity"].get("canonical_key"), d["system_value"].get("type"), d["system_value"].get("value"))
                   for d in structure["dimensions"])


def repair_generated_annotations(drawing, sw_app, workflow):
    """Only call on a drawing created by this run, before its final save."""
    geometry, handles = collect_visible_circles(drawing, sw_app, workflow)
    before, objects = _dimension_snapshot(drawing, workflow)
    plan = plan_duplicate_dimensions(before, circular_views={r["semantic_view"] for r in geometry["circles"]})
    hidden_value = _resolve_sw_constant("swAnnotationHidden", sw_app)
    if plan["hidden"] and hidden_value is None:
        raise AnnotationRepairError("Hidden-annotation enum unavailable")
    for item in plan["hidden"]:
        annotation = workflow._safe_member(objects[item["index"]][1], "GetAnnotation")
        annotation.Visible = hidden_value
        if workflow._safe_member(annotation, "Visible") != hidden_value:
            raise AnnotationRepairError("Duplicate annotation visibility did not change")
    workflow._safe_member(drawing, "GraphicsRedraw2")
    after, _ = _dimension_snapshot(drawing, workflow)
    if _model_dimension_inventory(before) != _model_dimension_inventory(after):
        raise AnnotationRepairError("Hiding display duplicates changed the model dimension inventory")
    for item in plan["hidden"]:
        if after["dimensions"][item["index"]]["visibility"] != "hidden" or after["dimensions"][item["keep_index"]]["visibility"] != "visible":
            raise AnnotationRepairError("Duplicate dimension preservation did not verify")

    # Existing diametric model dimensions remain authoritative. The coverage is
    # about diameter values, not hole quantity, location, tolerance or depth.
    covered = [d["system_value"]["value"] for d in after["dimensions"]
               if d["visibility"] == "visible" and d.get("type2") == 6 and d["system_value"].get("value") is not None]
    added, coverage = [], []
    targets = sorted(handles, key=lambda h: -h[0]["radius_m"])
    for circle, edge, view in targets:
        diameter = circle["radius_m"] * 2
        if any(math.isclose(diameter, value, rel_tol=1e-8, abs_tol=1e-10) for value in covered):
            coverage.append({**circle, "status": "same_value_present_attachment_unverified"})
            continue
        if len(added) >= 6:
            coverage.append({**circle, "status": "missing", "reason": "AUTOMATIC_DIAMETER_LIMIT"})
            continue
        get = workflow._safe_member
        drawing.ClearSelection2(True)
        if not get(drawing, "ActivateView", circle["view"], default=False):
            raise AnnotationRepairError("Cannot activate circular-edge drawing view")
        select_data = get(get(drawing, "SelectionManager"), "CreateSelectData")
        if select_data is None:
            raise AnnotationRepairError("Drawing selection context unavailable")
        select_data.View = view
        if not get(edge, "Select4", False, select_data, default=False):
            raise AnnotationRepairError("Circular edge could not be selected in its drawing view")
        box = workflow._normalise_box(get(view, "GetOutline"))
        if box is None:
            raise AnnotationRepairError("Circle view outline unavailable")
        right = len(added) % 2 == 0
        x = box["right"] + .014 if right else box["left"] - .014
        y = box["top"] + .010 + (len(added) // 2) * .008
        dd = get_com_member(drawing, "AddDiameterDimension2", x, y, 0.)
        if dd is None:
            raise AnnotationRepairError("Native diameter creation returned no dimension")
        dim = workflow._display_dimension_model_dimension(dd)
        value = workflow._dimension_system_value(dd, dim)
        if value.get("type") != 6 or value.get("value") is None or not math.isclose(value["value"], diameter, rel_tol=1e-8, abs_tol=1e-10):
            raise AnnotationRepairError("Native diameter does not equal the selected circular edge")
        annotation = get(dd, "GetAnnotation")
        if (get(annotation, "Visible") != 1 or get(annotation, "IsDangling") is not False
                or (get(annotation, "GetAttachedEntityCount3") or 0) < 1):
            raise AnnotationRepairError("Created diameter is not visible and attached")
        identity = workflow._dimension_identity(view, dd, dim)
        entry = {**circle, "status": "added_and_value_verified", "identity": identity,
                 "system_value": value, "text_position_m": [x, y, 0.]}
        added.append(entry)
        coverage.append(entry)
        covered.append(diameter)
    imported_profile = {"status": "skipped", "reason": "EXISTING_MODEL_DIMENSIONS"}
    shell_thickness = {"status": "skipped"}
    if not before["dimensions"]:
        if any(circle.get("projected_cylinder_boundary") for circle in geometry["circles"]):
            from .drawing_projected_cylinders import add_cylindrical_shell_thickness
            shell_thickness = add_cylindrical_shell_thickness(drawing, sw_app, workflow)
        elif not added:
            from .drawing_profile_annotations import repair_imported_profile_annotations
            imported_profile = repair_imported_profile_annotations(drawing, sw_app, workflow)
    drawing.ClearSelection2(True)
    workflow._safe_member(drawing, "ForceRebuild3", False)
    arrangement = workflow.auto_arrange_drawing_dimensions(drawing, spacing_m=.012)
    final, final_objects = _dimension_snapshot(drawing, workflow)
    sheet_size = final.get("sheet_size") or {}
    spacing = plan_profile_diameter_spacing(final, {r["semantic_view"] for r in geometry["circles"]},
        sheet_width_m=sheet_size.get("width_m"), sheet_height_m=sheet_size.get("height_m")) if not geometry["errors"] else {"moves": [], "unresolved": []}
    inventory_before_spacing = _model_dimension_inventory(final)
    for move in spacing["moves"]:
        annotation = workflow._safe_member(final_objects[move["index"]][1], "GetAnnotation")
        if not workflow._set_annotation_position(annotation, *move["to"], 0.):
            raise AnnotationRepairError("Profile diameter spacing was rejected by SolidWorks")
        position = workflow._annotation_position(annotation)
        if position is None or math.dist(position[:2], move["to"]) > 1e-6:
            raise AnnotationRepairError("Profile diameter spacing did not match native readback")
        move["native_position_m"] = position
    if spacing["moves"]:
        workflow._safe_member(drawing, "GraphicsRedraw2")
        final, _ = _dimension_snapshot(drawing, workflow)
        if _model_dimension_inventory(final) != inventory_before_spacing:
            raise AnnotationRepairError("Spacing changed a dimension identity or value")
    visible = [d for d in final["dimensions"] if d["visibility"] == "visible"]
    repeated = Counter(d["identity"].get("canonical_key") for d in visible if d["identity"].get("canonical_key"))
    unresolved = [{"canonical_key": key, "visible_instances": count} for key, count in repeated.items() if count > 1]
    for added_dimension in added:
        matches = [d for d in visible if d["identity"] == added_dimension["identity"]
                   and d["system_value"] == added_dimension["system_value"]]
        if len(matches) != 1:
            raise AnnotationRepairError("Created diameter identity/value/visibility changed during arrangement")
    for item in list(shell_thickness.get("added") or []) + list((imported_profile.get("insertion") or {}).get("results") or []):
        if item.get("status") == "blocked":
            continue
        if len([d for d in visible if d["identity"] == item.get("identity") and d["system_value"] == item.get("system_value")]) != 1:
            raise AnnotationRepairError("Created profile dimension changed during final arrangement")
    missing = [r for r in coverage if r["status"] == "missing"]
    return {"status": "blocked" if missing or unresolved or spacing["unresolved"] or imported_profile.get("status") == "blocked" or shell_thickness.get("status") == "blocked" else "review_required",
            "stored_before": len(before["dimensions"]), "stored_after": len(final["dimensions"]),
            "visible_before": sum(d["visibility"] == "visible" for d in before["dimensions"]),
            "visible_after": len(visible), "duplicates": plan, "geometry": geometry,
            "added_diameters": added, "diameter_coverage": coverage,
            "missing_diameters": missing, "unresolved_duplicates": unresolved, "arrangement": arrangement,
            "cross_view_spacing": spacing,
            "imported_profile": imported_profile, "shell_thickness": shell_thickness,
            "scope": "Diameter-value coverage only; hole quantity, location, depth and tolerances require separate evidence"}
