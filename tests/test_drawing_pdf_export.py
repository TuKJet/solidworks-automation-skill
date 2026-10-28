"""PDF export must select valid sheets and verify the API and new output."""
from types import SimpleNamespace

import fitz
import pytest

from scripts import sw_drawing


def pdf(path):
    with fitz.open() as document:
        document.new_page()
        document.save(path)


def setup(monkeypatch, *, sheets_ok=True, save_ok=True, error=0, warning=0, write=True, source_path=""):
    module = sw_drawing._MODULE
    calls = []
    class Variant:
        def __init__(self, kind, value):
            self.value = value
    class Export:
        ViewPdfAfterSaving = True

        def SetSheets(self, which, names):
            calls.append(('sheets', which, tuple(names.value)))
            assert which == 3
            return sheets_ok
    class Extension:
        def SaveAs(self, path, version, options, export, errors, warnings):
            calls.append(('save', path))
            errors.value = error
            warnings.value = warning
            if write:
                pdf(path)
            return save_ok
    monkeypatch.setattr(module, 'VARIANT', Variant)
    model = SimpleNamespace(
        GetSheetNames=lambda: ('图纸1',),
        GetPathName=lambda: source_path,
        ClearSelection2=lambda all_marks: calls.append(('clear_selection', all_marks)),
        Extension=Extension(),
    )
    app = SimpleNamespace(GetExportFileData=lambda value: Export())
    return model, app, calls


def test_pdf_export_uses_specified_sheets_enum_and_typed_names(monkeypatch, tmp_path, capsys):
    model, app, calls = setup(monkeypatch)
    assert sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'sheet.pdf'), sw_app=app)
    assert calls[0] == ('sheets', 3, ('图纸1',))
    log = capsys.readouterr().out
    assert "'event': 'pdf_export_saveas'" in log
    assert "'return_bool': True" in log
    assert "'error_mask': 0" in log
    assert "'warning_mask': 0" in log


def test_pdf_export_disables_viewer_and_clears_selection(monkeypatch, tmp_path):
    model, app, calls = setup(monkeypatch)
    export_data = app.GetExportFileData(1)
    # Reuse one export-data object so the test can inspect the property that
    # is passed to SaveAs, as COM does in production.
    monkeypatch.setattr(app, 'GetExportFileData', lambda _kind: export_data)

    assert sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'sheet.pdf'), sw_app=app)

    assert export_data.ViewPdfAfterSaving is False
    assert ('clear_selection', True) in calls


def test_pdf_export_verifies_saved_drawing_before_export(monkeypatch, tmp_path):
    module = sw_drawing._MODULE
    source = tmp_path / 'drawing.slddrw'
    model, app, calls = setup(monkeypatch, source_path=str(source))
    verified = SimpleNamespace(
        GetSheetNames=model.GetSheetNames,
        GetPathName=model.GetPathName,
        ClearSelection2=model.ClearSelection2,
        Extension=model.Extension,
    )
    activation_calls = []

    def activate(sw, received_model, received_path, *, expected_type):
        activation_calls.append((sw, received_model, received_path, expected_type))
        return verified

    monkeypatch.setattr(module, 'activate_document_verified', activate)
    assert sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'sheet.pdf'), sw_app=app)
    assert activation_calls == [(app, model, str(source), 3)]
    assert ('clear_selection', True) in calls


def test_pdf_export_blocks_when_saved_drawing_activation_is_unverified(monkeypatch, tmp_path):
    module = sw_drawing._MODULE
    model, app, calls = setup(monkeypatch, source_path=str(tmp_path / 'drawing.slddrw'))

    def fail_activation(*_args, **_kwargs):
        raise RuntimeError('active drawing mismatch')

    monkeypatch.setattr(module, 'activate_document_verified', fail_activation)
    assert not sw_drawing.export_sheet_to_pdf(model, str(tmp_path/'sheet.pdf'), sw_app=app)
    assert all(call[0] != 'save' for call in calls)


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
