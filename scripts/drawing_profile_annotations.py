"""Conservative native controller for dimensions on imported profiles.

The collector is evidence-only.  The inserter refuses to claim completion
unless SolidWorks returns an attached, visible, non-dangling dimension with
the expected value; a failed insertion is deleted when possible.
"""

from __future__ import annotations

import math
from typing import Any

from .drawing_profile_candidates import plan_profile_dimensions
from .drawing_geometry_annotations import _project
from .sw_connect import create_empty_dispatch_variant, get_com_member
from .sw_export import _resolve_sw_constant


class ProfileAnnotationError(RuntimeError):
    pass


def _get(workflow, obj, name, *args, default=None):
    return workflow._safe_member(obj, name, *args, default=default)


def _valid_point(p):
    try:
        p = [float(x) for x in p]
        return p if len(p) >= 3 and all(math.isfinite(x) for x in p[:3]) else None
    except (TypeError, ValueError):
        return None


def _visible_views(drawing, workflow):
    sheet = _get(workflow, drawing, "GetCurrentSheet")
    return sheet, list(_get(workflow, sheet, "GetViews", default=[]) or [])


def collect_profile_geometry(drawing, sw_app, workflow, *, views=None):
    """Collect visible vertices, partial face-on arcs, and loop vertices.

    Returns ``(evidence, handles)``.  Handles are intentionally kept separate
    from JSON evidence because they are session-bound COM objects.
    """
    sheet, native_views = _visible_views(drawing, workflow)
    native_views = native_views if views is None else list(views)
    edge_enum = _resolve_sw_constant("swViewEntityType_Edge", sw_app)
    vertex_enum = _resolve_sw_constant("swViewEntityType_Vertex", sw_app)
    if edge_enum is None or vertex_enum is None:
        return {"status": "blocked", "views": [], "errors": ["VIEW_ENTITY_ENUM_UNAVAILABLE"]}, []
    sheet_name = str(_get(workflow, sheet, "GetName", default=""))
    records, handles, errors = [], [], []
    mapped, _, _ = workflow._map_standard_views_from_sheet_views(native_views)
    semantics = {str(_get(workflow,v,"Name")):key for key,v in mapped.items()}
    for view in native_views:
        name = str(_get(workflow, view, "Name", default=""))
        transform = _get(workflow, view, "ModelToViewTransform")
        matrix = list(_get(workflow, transform, "ArrayData", default=[]) or [])
        scale = float(matrix[12]) if len(matrix) >= 13 and math.isfinite(float(matrix[12])) else None
        record = {"name": name, "semantic_view": semantics.get(name, ""), "scale": scale, "matrix": matrix,
                  "box": workflow._normalise_box(_get(workflow, view, "GetOutline")) or {},
                  "vertices": [], "arcs": [], "inner_loops": []}
        if transform is None or scale is None or scale <= 0:
            errors.append({"view": name, "reason": "VIEW_TRANSFORM_UNAVAILABLE"}); continue
        try:
            component = (_get(workflow, view, "GetVisibleComponents", default=[]) or [None])[0]
            edge_entities = _get(workflow, view, "GetVisibleEntities2", component if component is not None else create_empty_dispatch_variant(), edge_enum, default=[]) or []
            vertices_seen = set()
            for edge in edge_entities:
                limits = list(_get(workflow,edge,"GetCurveParams2",default=[]) or [])
                if len(limits)<8:
                    continue
                for endpoint,method in ((0,"GetStartVertex"),(3,"GetEndVertex")):
                    vertex = _get(workflow,edge,method)
                    model = _valid_point(limits[endpoint:endpoint+3])
                    if vertex is None or model is None:
                        continue
                    key=tuple(round(x,10) for x in model)
                    if key in vertices_seen:
                        continue
                    vertices_seen.add(key)
                    ident=f"{name}:vertex:{len(record['vertices'])}"
                    record['vertices'].append({'id':ident,'point_model_m':model,'point_sheet_m':_project(None,transform,model)})
                    handles.append({'id':ident,'view':view,'object':vertex,'kind':'vertex'})
            loop_seen = set()
            for index, edge in enumerate(edge_entities):
                curve = _get(workflow, edge, "GetCurve")
                if not _get(workflow, curve, "IsCircle", default=False): continue
                params = _get(workflow, curve, "CircleParams", default=[])
                limits = _get(workflow, edge, "GetCurveParams2", default=[])
                if not isinstance(params, (list, tuple)) or len(params) < 7 or not isinstance(limits, (list, tuple)) or len(limits) < 8: continue
                span = abs(float(limits[7]) - float(limits[6]))
                if not 1e-6 < span < 2 * math.pi - 1e-6: continue
                axis = _project(None, transform, params[3:6], vector=True)
                norm = math.sqrt(sum(x * x for x in axis))
                face_on = norm > 0 and abs(axis[2]) / norm >= 1 - 1e-8
                if not face_on: continue
                center = _project(None, transform, params[:3])
                ident = f"{name}:arc:{index}"
                start = _project(None,transform,limits[:3])
                end = _project(None,transform,limits[3:6])
                record["arcs"].append({"id": ident, "center_model_m": list(params[:3]), "center_sheet_m": center, "radius_m": float(params[6]), "span_rad": span, "face_on": True,
                                       "start_sheet_m":start,"end_sheet_m":end})
                handles.append({"id": ident, "view": view, "object": edge, "kind": "arc"})
            # Only true inner loops of a face parallel to the sheet qualify.
            # Deduplicate on projected vertices before adding handles: opposite
            # faces of a plate must not overwrite one another's native handles.
            faces_seen = set()
            for edge in edge_entities:
                for face in _get(workflow, edge, "GetTwoAdjacentFaces2", default=[]) or []:
                    surface = _get(workflow, face, "GetSurface")
                    if not _get(workflow, surface, "IsPlane", default=False):
                        continue
                    plane = list(_get(workflow, surface, "PlaneParams", default=[]) or [])
                    if len(plane) < 6:
                        continue
                    normal = _project(None, transform, plane[:3], vector=True)
                    norm = math.sqrt(sum(x*x for x in normal))
                    if norm <= 0 or abs(normal[2])/norm < 1 - 1e-8:
                        continue
                    face_box = list(_get(workflow,face,"GetBox",default=[]) or [])
                    # The approximate face box is only an enumeration cache
                    # key, never a dimension measurement.
                    face_key = tuple(plane+face_box)
                    if len(face_box)==6 and face_key in faces_seen:
                        continue
                    if len(face_box)==6:
                        faces_seen.add(face_key)
                    for loop in _get(workflow, face, "GetLoops", default=[]) or []:
                        if _get(workflow, loop, "IsOuter") is not False:
                            continue
                        loop_vertices, pending, points_seen = [], [], set()
                        for loop_edge in _get(workflow, loop, "GetEdges", default=[]) or []:
                            limits = list(_get(workflow,loop_edge,"GetCurveParams2",default=[]) or [])
                            if len(limits)<8:
                                continue
                            for endpoint,method in ((0,"GetStartVertex"),(3,"GetEndVertex")):
                                vertex = _get(workflow,loop_edge,method)
                                model = _valid_point(limits[endpoint:endpoint+3])
                                if model is None:
                                    continue
                                sheet_point = _project(None, transform, model)
                                key = tuple(round(x,10) for x in sheet_point[:2])
                                if key in points_seen:
                                    continue
                                points_seen.add(key)
                                ident = f"{name}:loop:{len(record['inner_loops'])}:vertex:{len(loop_vertices)}"
                                loop_vertices.append({"id":ident,"point_model_m":model,"point_sheet_m":sheet_point})
                                pending.append({"id":ident,"view":view,"object":vertex,"kind":"vertex"})
                        key = tuple(sorted(points_seen))
                        if len(loop_vertices) >= 3 and key not in loop_seen:
                            loop_seen.add(key)
                            record["inner_loops"].append({"id":f"{name}:inner-loop:{len(record['inner_loops'])}","vertices":loop_vertices})
                            handles.extend(pending)
        except Exception as exc:
            errors.append({"view": name, "reason": str(exc)})
        records.append(record)
    return {"status": "pass" if not errors else "review_required", "sheet": sheet_name, "views": records, "errors": errors}, handles


def _select(drawing, workflow, obj, view):
    data = _get(workflow, _get(workflow, drawing, "SelectionManager"), "CreateSelectData")
    if data is None: return False
    data.View = view
    return bool(_get(workflow, obj, "Select4", False, data, default=False))


def _delete_new(drawing, workflow, dim):
    """Delete only the just-created annotation, through ModelDocExtension."""
    annotation = _get(workflow, dim, "GetAnnotation")
    selected = bool(_get(workflow, annotation, "Select3", False, None, default=False))
    if not selected:
        _get(workflow, drawing, "ClearSelection2", True)
        return False
    if selected:
        extension = _get(workflow, drawing, "Extension")
        deleted = bool(_get(workflow, extension, "DeleteSelection2", True, default=False))
    else:
        deleted = False
    _get(workflow, drawing, "ClearSelection2", True)
    return deleted


def insert_profile_candidates(drawing, sw_app, workflow, candidates, handles):
    """Insert candidates and return blocked coverage if any native check fails."""
    by_id = {h["id"]: h for h in handles}
    results = []
    for candidate in candidates:
        source = candidate.get("source_geometry", {})
        ids = source.get("entity_ids", []) or source.get("vertex_ids", []) or ([source.get("arc_id")] if source.get("arc_id") else [])
        hs = [by_id.get(str(i)) for i in ids]
        hs = [h for h in hs if h]
        if len(hs) < (1 if candidate.get("kind") == "radius" else 2):
            results.append({**candidate, "status": "blocked", "reason": "SOURCE_HANDLE_UNAVAILABLE"}); continue
        view = hs[0]["view"]
        _get(workflow, drawing, "ClearSelection2", True)
        if not _get(workflow,drawing,"ActivateView",candidate["view"]):
            raise ProfileAnnotationError("Profile view activation failed")
        selected = []
        for index, h in enumerate(hs):
            # COM selection append is False for the first entity and True for
            # every following entity; otherwise only the last vertex remains.
            data = _get(workflow, _get(workflow, drawing, "SelectionManager"), "CreateSelectData")
            data.View = view
            selected.append(bool(_get(workflow, h["object"], "Select4", index > 0, data, default=False)))
        if not all(selected):
            _get(workflow, drawing, "ClearSelection2", True)
            results.append({**candidate, "status": "blocked", "reason": "SOURCE_SELECTION_FAILED"}); continue
        pos = candidate.get("text_position_m") or [0, 0, 0]
        try:
            if candidate.get("kind") == "radius":
                try:
                    from win32com.client import VARIANT
                    import pythoncom
                    error = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
                except ImportError:
                    error = 0
                dim = get_com_member(_get(workflow, drawing, "Extension"), "AddSpecificDimension", float(pos[0]), float(pos[1]), 0.0, 5, error)
            elif candidate.get("orientation") == "vertical":
                dim = get_com_member(drawing, "AddVerticalDimension2", float(pos[0]), float(pos[1]), 0.0)
            else:
                dim = get_com_member(drawing, "AddHorizontalDimension2", float(pos[0]), float(pos[1]), 0.0)
        except Exception as exc:
            results.append({**candidate, "status": "blocked", "reason": "NATIVE_INSERT_FAILED", "detail": str(exc)}); continue
        if dim is None:
            results.append({**candidate, "status": "blocked", "reason": "NATIVE_INSERT_RETURNED_NONE"}); continue
        model_dim = workflow._display_dimension_model_dimension(dim)
        for end_index, condition in enumerate(source.get('arc_end_conditions') or [],1):
            if condition and (_get(workflow,model_dim,'SetArcEndCondition',end_index,condition) != 0
                              or _get(workflow,model_dim,'GetArcEndCondition',end_index) != condition):
                if not _delete_new(drawing,workflow,dim):
                    raise ProfileAnnotationError('Failed to remove invalid arc-end dimension')
                raise ProfileAnnotationError('Native arc-end condition was rejected')
        annotation = _get(workflow, dim, "GetAnnotation")
        dangling = _get(workflow, annotation, "IsDangling")
        visible = _get(workflow, annotation, "Visible", default=0)
        attached = _get(workflow, annotation, "GetAttachedEntityCount3", default=0)
        value_record = {}
        try:
            model_dim = workflow._display_dimension_model_dimension(dim)
            value_record = workflow._dimension_system_value(dim, model_dim) or {}
        except Exception:
            value_record = {}
        expected_type = 5 if candidate.get("kind") == "radius" else 2
        actual_type = value_record.get("type")
        actual_value = value_record.get("value")
        valid_value = value_record.get('unit') == 'm' and actual_value is not None and math.isclose(float(actual_value), float(candidate["expected_m"]), rel_tol=1e-7, abs_tol=1e-10)
        valid_type = actual_type in ({5} if expected_type == 5 else {2})
        if dangling is not False or visible not in (1, True) or int(attached or 0) < len(hs) or not valid_type or not valid_value:
            if not _delete_new(drawing, workflow, dim):
                raise ProfileAnnotationError("Failed to delete invalid native profile dimension")
            results.append({**candidate, "status": "blocked", "reason": "NATIVE_VALIDATION_FAILED", "dangling": dangling, "visible": visible, "attached_count": attached, "system_value": value_record}); continue
        identity = workflow._dimension_identity(view, dim, model_dim)
        results.append({**candidate, "status": "added_and_verified", "attached_count": attached, "identity": identity, "system_value": value_record})
    _get(workflow, drawing, "ClearSelection2", True)
    return {"status": "pass" if all(x["status"] == "added_and_verified" for x in results) else "blocked", "results": results}


def repair_imported_profile_annotations(drawing, sw_app, workflow, *, max_dimensions=14):
    """Entry point: only act when the drawing currently has zero dimensions."""
    structure = workflow.inspect_drawing_structure(drawing)
    if structure.get("dimensions"):
        return {"status": "skipped", "reason": "EXISTING_DIMENSIONS_PRESENT"}
    evidence, handles = collect_profile_geometry(drawing, sw_app, workflow)
    plan = plan_profile_dimensions(evidence.get("views", []), max_dimensions=max_dimensions)
    if not plan["complete"] or evidence["errors"]:
        return {"status": "blocked", "geometry": evidence, "plan": plan}
    inserted = insert_profile_candidates(drawing, sw_app, workflow, plan["candidates"], handles)
    if inserted["status"] == "pass":
        arranger = getattr(workflow, "auto_arrange_drawing_dimensions", None)
        if not callable(arranger):
            raise ProfileAnnotationError("Native profile dimension arrangement API unavailable")
        arranger(drawing, spacing_m=.012)
        final = workflow.inspect_drawing_structure(drawing)
        visible = [item for item in final.get("dimensions", []) if item.get("visibility") == "visible"]
        for item in inserted["results"]:
            matches = [dim for dim in visible if dim.get("identity") == item.get("identity")
                       and dim.get("system_value") == item.get("system_value")]
            if len(matches) != 1:
                raise ProfileAnnotationError("Arranged profile dimension identity/value verification failed")
    return {"status": inserted["status"], "geometry": evidence, "plan": plan, "insertion": inserted}



