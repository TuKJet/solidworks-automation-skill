"""Recognise cylindrical BRep boundaries whose drawing projection is a circle.

An imported intersection edge can be a spline in 3D and still project to an
exact circle. Surface provenance, closed winding and native dimension readback
are all required; a fitted circle or the approximate part box is insufficient.
"""
from __future__ import annotations

import math

from .sw_connect import create_empty_dispatch_variant, get_com_member
from .sw_export import _resolve_sw_constant


def projected_cylinder_circle(params, limits, samples, project):
    try:
        p, u = list(map(float, params)), list(map(float, limits))
        if len(p) != 7 or len(u) < 8 or not all(math.isfinite(x) for x in p + u[:8]) or p[6] <= 0:
            return None
        if math.dist(u[:3], u[3:6]) > 1e-8 or len(samples) < 17:
            return None
        axis = project(p[3:6], vector=True)
        if len(axis)!=3 or not all(math.isfinite(x) for x in axis):
            return None
        scale = math.sqrt(sum(v*v for v in axis))
        if scale <= 0 or abs(axis[2])/scale < 1 - 1e-8:
            return None
        center = project(p[:3])
        points = [project(s[:3]) for s in samples]
        if any(len(q)!=3 or not all(math.isfinite(x) for x in q) for q in [center]+points):
            return None
        radius = p[6] * scale
        if any(abs(math.hypot(q[0]-center[0], q[1]-center[1])-radius) > max(1e-9, radius*1e-6) for q in points):
            return None
        angles = [math.atan2(q[1]-center[1], q[0]-center[0]) for q in points]
        turns = sum(math.atan2(math.sin(b-a), math.cos(b-a)) for a,b in zip(angles,angles[1:]))
        if abs(abs(turns)-2*math.pi) > 1e-5:
            return None
        return {"center_model_m": p[:3], "axis_model": p[3:6], "center_sheet_m": center,
                "radius_m": p[6], "diameter_mm": p[6]*2000, "face_on": True, "full_circle": True,
                "evidence": "closed BRep boundary / adjacent CylinderParams / projected radius and full winding",
                "projected_cylinder_boundary": True}
    except (TypeError, ValueError, IndexError):
        return None


def circle_from_cylindrical_boundary(edge, transform, workflow):
    from .drawing_geometry_annotations import _project
    get = workflow._safe_member
    curve = get(edge, "GetCurve")
    limits = list(get(edge, "GetCurveParams2", default=[]) or [])
    if len(limits) < 8 or math.dist(limits[:3],limits[3:6])>1e-8:
        return None
    faces = list(get(edge, "GetTwoAdjacentFaces2", default=[]) or [])
    samples = None
    for face in faces:
        surface = get(face, "GetSurface")
        if not get(surface, "IsCylinder", default=False):
            continue
        params = list(get(surface,"CylinderParams",default=[]) or [])
        if len(params)!=7:
            continue
        axis = _project(None,transform,params[3:6],vector=True)
        norm = math.sqrt(sum(x*x for x in axis))
        if norm<=0 or abs(axis[2])/norm < 1-1e-8:
            continue
        if samples is None:
            samples = [get(curve, "Evaluate2", limits[6]+(limits[7]-limits[6])*i/32, 0, default=[]) or [] for i in range(33)]
        result = projected_cylinder_circle(params, limits, samples,
                    lambda point, vector=False: _project(None, transform, point, vector=vector))
        if result:
            return result
    return None


def add_cylindrical_shell_thickness(drawing, sw_app, workflow):
    """Dimension the separation of concentric-cylinder silhouette tangents.

Only a side-on pair on the same side of the common axis is eligible. The
linear separation must equal the difference of the two true surface radii.
"""
    from .drawing_geometry_annotations import _project, AnnotationRepairError
    get = workflow._safe_member
    kind = _resolve_sw_constant("swViewEntityType_SilhouetteEdge", sw_app)
    if kind is None:
        raise AnnotationRepairError("Silhouette enum unavailable")
    candidates = []
    for view in get(get(drawing, "GetCurrentSheet"), "GetViews", default=[]) or []:
        transform = get(view, "ModelToViewTransform")
        matrix = list(get(transform, "ArrayData", default=[]) or [])
        if len(matrix) != 16 or matrix[12] <= 0:
            continue
        component = (get(view, "GetVisibleComponents", default=[]) or [None])[0]
        silhouettes = get(view, "GetVisibleEntities2", component if component is not None else create_empty_dispatch_variant(), kind, default=[]) or []
        lines = []
        for silhouette in silhouettes:
            curve = get(silhouette, "GetCurve")
            surface = get(get(silhouette, "GetFace"), "GetSurface")
            if not get(curve, "IsLine") or not get(surface, "IsCylinder"):
                continue
            p = list(get(surface, "CylinderParams", default=[]) or [])
            line = list(get(curve, "LineParams", default=[]) or [])
            if len(p) != 7 or len(line) != 6:
                continue
            q = _project(None, transform, line[:3]); axis = _project(None, transform, p[3:6], vector=True)
            direction = _project(None, transform, line[3:], vector=True)
            # Orthographic, axis-aligned tangents only.
            orientation = 0 if abs(direction[0]) < 1e-8 and abs(direction[1]) > 1e-8 else (1 if abs(direction[1]) < 1e-8 and abs(direction[0]) > 1e-8 else None)
            if orientation is not None and abs(axis[2]) < 1e-8:
                lines.append((silhouette,p,q,orientation))
        for i,a in enumerate(lines):
            for b in lines[i+1:]:
                if a[3] != b[3] or abs(abs(sum(x*y for x,y in zip(a[1][3:6],b[1][3:6])))-1) > 1e-8:
                    continue
                delta = [x-y for x,y in zip(a[1][:3],b[1][:3])]; axial = sum(x*y for x,y in zip(delta,a[1][3:6]))
                if math.sqrt(sum((delta[j]-axial*a[1][3+j])**2 for j in range(3))) > 1e-8:
                    continue
                expected = abs(a[1][6]-b[1][6]); span = abs(a[2][a[3]]-b[2][b[3]])/matrix[12]
                if expected > 1e-8 and math.isclose(span,expected,rel_tol=1e-6,abs_tol=1e-9):
                    candidates.append((expected,view,a,b))
    if not candidates:
        return {"status":"blocked", "reason":"NO_CONCENTRIC_TANGENT_PAIR", "added":[]}
    expected, view, a,b = min(candidates,key=lambda x:x[0])
    name = get(view,"Name"); box = workflow._normalise_box(get(view,"GetOutline"))
    if not box or not get(drawing,"ActivateView",name):
        raise AnnotationRepairError("Shell thickness view unavailable")
    drawing.ClearSelection2(True)
    data = get(get(drawing,"SelectionManager"),"CreateSelectData"); data.View=view
    if not get(a[0],"Select2",False,data) or not get(b[0],"Select2",True,data):
        raise AnnotationRepairError("Shell silhouette selection failed")
    horizontal = a[3] == 0
    pos = [(box["left"]+box["right"])/2,box["bottom"]-.015,0.] if horizontal else [box["right"]+.015,(box["bottom"]+box["top"])/2,0.]
    dd = get_com_member(drawing,"AddHorizontalDimension2" if horizontal else "AddVerticalDimension2",*pos)
    if dd is None:
        raise AnnotationRepairError("Native shell thickness creation returned no dimension")
    dim = workflow._display_dimension_model_dimension(dd); value = workflow._dimension_system_value(dd,dim)
    annotation = get(dd,"GetAnnotation")
    if (value.get("type") != 2 or value.get("value") is None or not math.isclose(value["value"],expected,rel_tol=1e-8,abs_tol=1e-10)
            or get(annotation,"Visible") != 1 or get(annotation,"IsDangling") is not False or (get(annotation,"GetAttachedEntityCount3") or 0)<2):
        raise AnnotationRepairError("Native shell thickness did not verify against cylinder radii")
    drawing.ClearSelection2(True)
    added = [{"identity":workflow._dimension_identity(view,dd,dim),"system_value":value,
            "cylinders_model_m":[a[1],b[1]],"view":name,"evidence":"concentric cylinder radii / associated silhouette tangent separation"}]
    outer = max((a[1],b[1]),key=lambda p:p[6])
    added.append(add_projected_bend_radius(drawing,sw_app,workflow,outer))
    return {"status":"review_required", "added":added}


def add_projected_bend_radius(drawing,sw_app,workflow,cylinder):
    """Attach a native radius to a projected arc of the proven shell cylinder."""
    from .drawing_geometry_annotations import _project, AnnotationRepairError
    get = workflow._safe_member
    kind = _resolve_sw_constant("swViewEntityType_Edge",sw_app)
    for view in get(get(drawing,"GetCurrentSheet"),"GetViews",default=[]) or []:
        transform = get(view,"ModelToViewTransform")
        axis = _project(None,transform,cylinder[3:6],vector=True)
        scale = math.sqrt(sum(v*v for v in axis))
        if scale<=0 or abs(axis[2])/scale < 1-1e-8:
            continue
        center = _project(None,transform,cylinder[:3])
        component = (get(view,"GetVisibleComponents",default=[]) or [None])[0]
        for edge in get(view,"GetVisibleEntities2",component if component is not None else create_empty_dispatch_variant(),kind,default=[]) or []:
            surfaces = [get(face,"GetSurface") for face in get(edge,"GetTwoAdjacentFaces2",default=[]) or []]
            if not any(get(s,"IsCylinder") and math.dist(list(get(s,"CylinderParams",default=[]) or []),cylinder)<1e-8 for s in surfaces):
                continue
            curve = get(edge,"GetCurve"); limits = list(get(edge,"GetCurveParams2",default=[]) or [])
            if len(limits)<8:
                continue
            points = [_project(None,transform,list(get(curve,"Evaluate2",limits[6]+(limits[7]-limits[6])*i/16,0))[:3]) for i in range(17)]
            if any(abs(math.hypot(p[0]-center[0],p[1]-center[1])-cylinder[6]*scale)>1e-8 for p in points):
                continue
            if max(math.dist(p[:2],points[0][:2]) for p in points)<1e-6:
                continue
            box = workflow._normalise_box(get(view,"GetOutline")); name=get(view,"Name")
            drawing.ClearSelection2(True)
            if not get(drawing,"ActivateView",name):
                raise AnnotationRepairError("Bend radius view activation failed")
            data=get(get(drawing,"SelectionManager"),"CreateSelectData");data.View=view
            if not get(edge,"Select4",False,data):
                raise AnnotationRepairError("Bend radius edge selection failed")
            dd=get_com_member(drawing,"AddRadialDimension2",box['right']+.02,box['top']+.015,0.)
            if dd is None:
                raise AnnotationRepairError("Native bend radius creation returned no dimension")
            dim=workflow._display_dimension_model_dimension(dd);value=workflow._dimension_system_value(dd,dim);ann=get(dd,"GetAnnotation")
            if (value.get('type')!=5 or value.get('value') is None or not math.isclose(value['value'],cylinder[6],rel_tol=1e-8,abs_tol=1e-10)
                    or get(ann,'Visible')!=1 or get(ann,'IsDangling') is not False or (get(ann,'GetAttachedEntityCount3') or 0)<1):
                raise AnnotationRepairError("Native bend radius does not match cylinder evidence")
            drawing.ClearSelection2(True)
            return {'identity':workflow._dimension_identity(view,dd,dim),'system_value':value,'view':name,
                    'cylinder_model_m':cylinder,'evidence':'adjacent cylinder / projected circular arc / native associated radius'}
    raise AnnotationRepairError("Shell curvature has no verified radius annotation")
