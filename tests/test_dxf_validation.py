from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.dxf_validation import validate_dxf_file
from scripts import sw_export


DXF = """0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n9\n$DIMSCALE\n40\n1.0\n9\n$DIMLFAC\n40\n1.0\n0\nENDSEC\n0\nSECTION\n2\nTABLES\n0\nENDSEC\n0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nB1\n0\nTEXT\n1\nNote\n0\nENDBLK\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n10\n0\n20\n0\n11\n1\n21\n1\n0\nDIMENSION\n8\n0\n0\nENDSEC\n0\nEOF\n"""


def test_dxf_validation_records_geometry_annotations_and_header(tmp_path):
    path = tmp_path / "drawing.dxf"
    path.write_text(DXF, encoding="ascii")
    result = validate_dxf_file(path, expected_visible_dimension_count=1)
    assert result["status"] == "review_required"
    assert result["native_dimension_count"] == 1
    assert result["entity_counts"]["TEXT"] == 1
    assert result["header"]["INSUNITS"] == "4"
    assert result["units"] == "not_inferred"


def test_dxf_validator_preserves_declared_legacy_chinese_encoding(tmp_path):
    path = tmp_path / "cn.dxf"
    path.write_bytes(DXF.replace("Note", "标注").encode("cp936"))
    result = validate_dxf_file(path)
    assert result["encoding"] == "cp936"
    assert result["entity_counts"]["TEXT"] == 1


@pytest.mark.parametrize("content,code", [("", "DXF_MISSING_OR_EMPTY"), ("0\nLINE\n", "DXF_STRUCTURE_INVALID")])
def test_dxf_invalid_or_empty_is_blocked(tmp_path, content, code):
    path = tmp_path / "bad.dxf"
    path.write_text(content, encoding="ascii")
    assert validate_dxf_file(path)["error_code"] == code


def test_geometry_without_annotations_blocks_when_dimensions_expected(tmp_path):
    path = tmp_path / "geometry.dxf"
    path.write_text(DXF.replace("0\nDIMENSION\n8\n0\n", "" ).replace("0\nTEXT\n1\nNote\n", ""), encoding="ascii")
    result = validate_dxf_file(path, expected_visible_dimension_count=2)
    assert result["status"] == "blocked"
    assert result["error_code"] == "DXF_EXPECTED_DIMENSIONS_MISSING"


def test_title_text_does_not_stand_in_for_missing_dimensions(tmp_path):
    path=tmp_path/'title-only.dxf'
    path.write_text(DXF.replace('0\nDIMENSION\n8\n0\n','0\nTEXT\n1\nDrawing title\n'),encoding='ascii')
    assert validate_dxf_file(path,expected_visible_dimension_count=1)['error_code']=='DXF_EXPECTED_DIMENSIONS_MISSING'


def test_missing_header_is_not_accepted_as_complete_exchange(tmp_path):
    path=tmp_path/'missing-header.dxf'
    path.write_text('0\nSECTION\n2\nENTITIES\n0\nLINE\n0\nTEXT\n1\ntitle\n0\nENDSEC\n0\nEOF\n',encoding='ascii')
    assert validate_dxf_file(path)['error_code']=='DXF_STRUCTURE_INVALID'


def test_block_only_annotation_is_not_claimed_as_freecad_safe(tmp_path):
    path = tmp_path / "block-only.dxf"
    path.write_text(DXF.replace("0\nDIMENSION\n8\n0\n", ""), encoding="ascii")
    result = validate_dxf_file(path)
    assert result["status"] == "blocked"
    assert result["error_code"] == "DXF_ANNOTATIONS_ONLY_IN_BLOCKS"
    assert result["top_level_annotation_entity_count"] == 0


class _Variant:
    def __init__(self, _kind, value):
        self.value = value


def test_dxf_detailed_export_requires_app_and_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(sw_export, "VARIANT", _Variant)
    model = SimpleNamespace(GetSldWorksObject=lambda: None, GetPathName=lambda: "x.slddrw", GetType=lambda: 3)
    result = sw_export.export_drawing_to_dxf_detailed(model, tmp_path / "x.dxf")
    assert result["success"] is False


def test_dxf_detailed_export_restores_blocks_preference_and_reports_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(sw_export, "VARIANT", _Variant)
    monkeypatch.setattr(sw_export._win32com, "constants", SimpleNamespace(swDxfExportViewAsBlock=77), raising=False)
    state = {"value": True}
    calls = []
    class Extension:
        def SaveAs(self, path, *_args):
            Path(path).write_text(DXF, encoding="ascii")
            _args[-2].value = 0
            _args[-1].value = 8
            return True
    app = SimpleNamespace(GetUserPreferenceToggle=lambda _p: state["value"], SetUserPreferenceToggle=lambda _p, v: (state.__setitem__("value", bool(v)), calls.append(bool(v))))
    model = SimpleNamespace(GetSldWorksObject=lambda: app, GetPathName=lambda: "x.slddrw", GetType=lambda: 3,
                             ClearSelection2=lambda _all: None, Extension=Extension())
    monkeypatch.setattr(sw_export, "activate_document_verified", lambda *_a, **_k: None)
    result = sw_export.export_drawing_to_dxf_detailed(model, tmp_path / "x.dxf")
    assert result["success"] is True
    assert result["review_required"] is True
    assert state["value"] is True
    assert calls == [False, True]
    model = SimpleNamespace(GetSldWorksObject=lambda: object(), GetPathName=lambda: "x.slddrw", GetType=lambda: 3)
    monkeypatch.setattr(sw_export, "activate_document_verified", lambda *_a, **_k: None)
    result = sw_export.export_drawing_to_dxf_detailed(model, tmp_path / "x.dxf")
    assert result["success"] is False
