"""PDF export must select valid sheets and verify the API and new output."""
from types import SimpleNamespace

import fitz
import pytest

from scripts import sw_drawing


def pdf(path):
    with fitz.open() as document:
        document.new_page()
        document.save(path)


def setup(monkeypatch, *, sheets_ok=True, save_ok=True, error=0, write=True):
    module = sw_drawing._MODULE
    calls = []
    class Variant:
        def __init__(self, kind, value):
            self.value = value
    class Export:
        def SetSheets(self, which, names):
            calls.append(('sheets', which, tuple(names.value)))
            assert which == 3
            return sheets_ok
    class Extension:
        def SaveAs(self, path, version, options, export, errors, warnings):
            calls.append(('save', path))
            errors.value = error
            if write:
                pdf(path)
            return save_ok
    monkeypatch.setattr(module, 'VARIANT', Variant)
    model = SimpleNamespace(GetSheetNames=lambda: ('图纸1',), Extension=Extension())
    app = SimpleNamespace(GetExportFileData=lambda value: Export())
    return model, app, calls


def test_pdf_export_uses_specified_sheets_enum_and_typed_names(monkeypatch, tmp_path):
    model, app, calls = setup(monkeypatch)
    assert sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'sheet.pdf'), sw_app=app)
    assert calls[0] == ('sheets', 3, ('图纸1',))


@pytest.mark.parametrize('save_ok,error', [(False,0), (True,128)])
def test_pdf_file_does_not_override_api_save_failure(monkeypatch, tmp_path, save_ok, error):
    model, app, _ = setup(monkeypatch, save_ok=save_ok, error=error)
    assert not sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'sheet.pdf'), sw_app=app)


def test_rejected_sheet_selection_does_not_accept_old_pdf(monkeypatch, tmp_path):
    path = tmp_path/'sheet.pdf'
    pdf(path)
    model, app, calls = setup(monkeypatch, sheets_ok=False)
    assert not sw_drawing.export_sheet_to_pdf(model, str(path), sw_app=app)
    assert all(call[0] != 'save' for call in calls)


def test_successful_api_without_pdf_output_is_not_export_success(monkeypatch, tmp_path):
    model, app, _ = setup(monkeypatch, write=False)
    assert not sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'missing.pdf'), sw_app=app)


def test_successful_api_cannot_reuse_unchanged_existing_pdf(monkeypatch, tmp_path):
    path = tmp_path/'old.pdf'
    pdf(path)
    model, app, _ = setup(monkeypatch, write=False)
    assert not sw_drawing.export_sheet_to_pdf(model, str(path), sw_app=app)
