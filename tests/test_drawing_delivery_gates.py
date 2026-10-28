"""Regressions from the 24-part project: hidden dimensions, lost notes, false success."""
from scripts import sw_drawing_review


def test_hidden_dimension_not_required_in_final_pdf(tmp_path):
    import fitz
    pdf = tmp_path / 'sheet.pdf'
    with fitz.open() as doc:
        doc.new_page(width=300, height=200)
        doc.save(pdf)
    structure = {'sheet_size': {'width_m': .3, 'height_m': .2},
                 'dimensions': [{'name': 'D1', 'visibility': 'hidden'}]}
    result = sw_drawing_review.inspect_pdf_dimension_rendering(pdf, structure)
    assert result['status'] == 'pass'
    assert result['excluded_hidden_count'] == 1


def test_nearby_wrong_number_is_not_dimension_evidence(tmp_path):
    import fitz
    pdf = tmp_path / 'sheet.pdf'
    with fitz.open() as doc:
        page = doc.new_page(width=300, height=200)
        page.insert_text((100, 100), '999')
        doc.save(pdf)
    result = sw_drawing_review.inspect_pdf_dimension_rendering(pdf, {
        'sheet_size': {'width_m': .3, 'height_m': .2},
        'dimensions': [{'name': 'D1', 'value_mm': 12,
                        'box_evidence': {'position_m': [.105, .105]}}]})
    assert result['status'] != 'pass'


def test_multiline_note_matches_pdf_lines_and_crlf(tmp_path):
    import fitz
    pdf = tmp_path / 'sheet.pdf'
    with fitz.open() as doc:
        page = doc.new_page(width=300, height=200)
        page.insert_text((30, 60), 'Reference diameter 12\nMaterial pending')
        doc.save(pdf)
    result = sw_drawing_review._MODULE._note_report(
        {'notes': ['Reference diameter 12\nMaterial pending']},
        {'notes': [{'text': 'Reference diameter 12\r\nMaterial pending',
                    'position_m': [.03, .14]}]}, pdf)
    assert result['status'] == 'pass'


def test_export_success_cannot_override_failed_review():
    result = sw_drawing_review.delivery_verdict(
        save_ok=True, export_ok=True,
        review={'status': 'blocked'}, manufacturing={'status': 'pass'})
    assert result['status'] == 'blocked'
    assert result['export'] == 'pass'
    assert result['readability'] == 'blocked'
    assert result['release_ready'] is False


def test_readable_drawing_without_manufacturing_evidence_is_not_release_ready():
    result = sw_drawing_review.delivery_verdict(
        save_ok=True, export_ok=True, review={'status': 'pass'})
    assert result['status'] == 'review_required'
    assert result['manufacturing'] == 'unknown'
    assert result['release_ready'] is False


def test_blank_pdf_cannot_pass_drawing_review(tmp_path):
    """@brief 有结构对象不等于最终 PDF 有可读交付内容。"""
    import fitz

    pdf = tmp_path / "blank.pdf"
    with fitz.open() as document:
        document.new_page(width=300, height=200)
        document.save(pdf)
    spec = {
        "schemaVersion": "1.0", "sourceModel": "C:/cad/plate.sldprt", "documentType": "part",
        "standard": "GB_T", "projection": "first_angle", "paperSize": "A3",
        "modelSizeMm": [120, 80, 12],
        "views": {"front": {}, "top": {}, "right": {}},
        "outputs": {"slddrw": True, "pdf": True, "report": True},
        "insertModelDimensions": False,
    }
    structure = {
        "sheet_size": {"width_m": 0.3, "height_m": 0.2},
        "views": [
            {"name": "Front", "box": {"left": .10, "bottom": .10, "right": .15, "top": .15}},
            {"name": "Top", "box": {"left": .10, "bottom": .16, "right": .15, "top": .19}},
            {"name": "Right", "box": {"left": .17, "bottom": .10, "right": .22, "top": .15}},
        ],
        "dimensions": [],
        "title_block": {"box": {"left": .01, "bottom": .01, "right": .08, "top": .04}},
    }

    result = sw_drawing_review.review_drawing_artifacts(spec, structure=structure, pdf_path=pdf)

    assert result["status"] == "review_required"
    assert result["error_code"] == "DRAWING_PDF_VECTOR_TEXT_MISSING"


def test_bom_expected_row_must_match_a_whole_cell_not_a_substring():
    """@brief P1/M8 绝不能由 P10/M80 的单元格冒充。"""
    report = sw_drawing_review._MODULE._bom_report(
        {"documentType": "assembly", "bom": {"required": True, "expectedRows": ["P1", "M8"]}},
        {"tables": [{
            "kind": "bom", "type": 2, "row_count": 2,
            "cells": [["ITEM", "THREAD"], ["P10", "M80"]],
        }]},
    )

    assert report["status"] == "review_required"


def test_legacy_bom_string_rows_are_not_release_evidence_even_when_text_exists():
    report = sw_drawing_review._MODULE._bom_report(
        {"documentType": "assembly", "bom": {"required": True, "expectedRows": ["P1", "M8"]}},
        {"tables": [{
            "kind": "bom", "type": 2, "row_count": 2,
            "cells": [["PART", "THREAD"], ["P1", "M8"]],
        }]},
    )

    assert report["status"] == "review_required"
    assert report["reason"] == "legacy_expected_rows_unstructured"


def test_bom_structured_expected_rows_require_the_same_exact_row():
    structure = {"tables": [{
        "kind": "bom", "type": 2, "row_count": 3,
        "cells": [["ITEM", "PART", "QTY"], ["1", "P1", "2"], ["2", "M8", "4"]],
    }]}
    module = sw_drawing_review._MODULE

    mismatched = module._bom_report(
        {"documentType": "assembly", "bom": {"required": True, "expectedRows": [{"ITEM": "1", "PART": "M8"}]}},
        structure,
    )
    matched = module._bom_report(
        {"documentType": "assembly", "bom": {"required": True, "expectedRows": [{"ITEM": "1", "PART": "P1", "QTY": "2"}]}},
        structure,
    )

    assert mismatched["status"] == "review_required"
    assert matched["status"] == "pass"


def test_canonical_dimension_identity_is_exact_and_legacy_short_id_requires_unique_mapping():
    module = sw_drawing_review._MODULE
    dimensions = [
        {"name": "D1@SketchA@Part", "identity": {"canonical_key": "a|Default|D1@SketchA"}},
        {"name": "D1@SketchB@Part", "identity": {"canonical_key": "a|Default|D1@SketchB"}},
    ]

    canonical = module._required_dimension_report(
        {"requiredDimensions": [{"id": "a|Default|D1@SketchB"}]}, {"dimensions": dimensions},
    )
    ambiguous_legacy = module._required_dimension_report(
        {"requiredDimensions": [{"id": "D1"}]}, {"dimensions": dimensions},
    )
    unique_legacy = module._required_dimension_report(
        {"requiredDimensions": [{"id": "D1"}]}, {"dimensions": [dimensions[0]]},
    )

    assert canonical["status"] == "pass"
    assert canonical["checks"][0]["matched_dimension"] == "a|Default|D1@SketchB"
    assert ambiguous_legacy["status"] == "fail"
    assert unique_legacy["status"] == "pass"


def test_nested_dimension_system_value_is_used_without_guessing_overall_kind(tmp_path):
    """@brief Type2 可确认 angular；线性尺寸不能被臆断为 overall，PDF 必须读嵌套值。"""
    module = sw_drawing_review._MODULE
    assert module._dimension_mismatches(
        {"kind": "angular", "view": "Front"},
        {"type2": 3, "semantic_view": "Front", "system_value": {"value_degrees": 90.0, "unit": "rad"}},
    ) == []
    assert "kind" in module._dimension_mismatches(
        {"kind": "overall", "view": "Front"},
        {"type2": 2, "semantic_view": "Front", "system_value": {"value_mm": 12.0, "unit": "m"}},
    )

    import fitz
    pdf = tmp_path / "wrong-nested-value.pdf"
    with fitz.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((100, 100), "999")
        document.save(pdf)
    result = module.inspect_pdf_dimension_rendering(pdf, {
        "sheet_size": {"width_m": .3, "height_m": .2},
        "dimensions": [{
            "name": "D1", "box_evidence": {"position_m": [.105, .105]},
            "system_value": {"value_mm": 12.0, "value_degrees": None, "unit": "m"},
        }],
    })

    assert result["status"] != "pass"

    angular_pdf = tmp_path / "wrong-nested-angle.pdf"
    with fitz.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((100, 100), "89")
        document.save(angular_pdf)
    angular_result = module.inspect_pdf_dimension_rendering(angular_pdf, {
        "sheet_size": {"width_m": .3, "height_m": .2},
        "dimensions": [{
            "name": "D2", "box_evidence": {"position_m": [.105, .105]},
            "system_value": {"value_mm": None, "value_degrees": 90.0, "unit": "rad"},
        }],
    })

    assert angular_result["status"] != "pass"
