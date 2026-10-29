from scripts.annotation_acceptance import evaluate_annotation_acceptance


def base():
    return {
        "insertModelDimensions": True,
        "documentType": "part",
    }, {
        "dimensions": [{"visibility": "visible", "visible": True, "type2": 2, "system_value": {"value": 0.017, "unit": "m"}} for _ in range(2)]
    }, {"geometry": {"errors": [], "circles": [{"face_on": True, "full_circle": True, "diameter_mm": d} for d in (11.2, 9.0)]}}, {"findings": []}


def test_ring_with_duplicate_and_uncovered_od_is_blocked():
    spec, structure, repair, review = base()
    result = evaluate_annotation_acceptance(spec, structure, repair, review)
    assert result["status"] == "blocked"
    assert any(issue["code"] == "DRAWING_CIRCULAR_DIAMETERS_MISSING" for issue in result["issues"])


def test_covered_diameter_remains_review_required():
    spec, structure, repair, review = base()
    structure["dimensions"].extend({"visibility": "visible", "visible": True, "type2": 6, "system_value": {"value": d, "unit": "m"}} for d in (0.0112, 0.009))
    result = evaluate_annotation_acceptance(spec, structure, repair, review)
    assert result["status"] == "review_required"
    assert result["manual_review_required"] is True


def test_unresolved_duplicates_zero_dimensions_and_pdf_overlap_block():
    spec, structure, repair, review = base()
    repair["unresolved_duplicates"] = [{"index": 2}]
    assert evaluate_annotation_acceptance(spec, structure, repair, review)["status"] == "blocked"
    structure["dimensions"] = []
    assert evaluate_annotation_acceptance(spec, structure, {"geometry": {"circles": [], "errors": []}}, review)["status"] == "blocked"
    structure["dimensions"] = [{"visibility": "visible", "visible": True}]
    review["pdf_dimension_rendering"] = {"collisions": [{"a": 1}]}
    assert evaluate_annotation_acceptance(spec, structure, {"geometry": {"circles": [], "errors": []}}, review)["status"] == "blocked"


def test_part_geometry_errors_block_but_assembly_unsupported_is_review_warning():
    spec, structure, repair, review = base()
    repair["geometry"]["errors"] = [{"reason": "VIEW_TRANSFORM_UNAVAILABLE"}]
    assert evaluate_annotation_acceptance(spec, structure, repair, review)["status"] == "blocked"
    spec["documentType"] = "assembly"
    repair["geometry"]["circles"] = []
    assert evaluate_annotation_acceptance(spec, structure, repair, review)["status"] == "review_required"


def test_partial_imported_profile_or_missing_shell_measurements_block_even_with_diameters():
    spec,structure,repair,review=base()
    repair['geometry']['circles']=[]
    for key in ('imported_profile','shell_thickness'):
        partial={**repair,key:{'status':'blocked','reason':'missing_native_measurement'}}
        result=evaluate_annotation_acceptance(spec,structure,partial,review)
        assert result['status']=='blocked'
        assert any(i['code'] in ('DRAWING_IMPORTED_PROFILE_INCOMPLETE','DRAWING_CYLINDRICAL_SHELL_INCOMPLETE') for i in result['issues'])
