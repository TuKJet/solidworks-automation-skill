from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from scripts import solidworks_3d_2d as cli
from scripts.drawing_spec import validate_drawing_spec


class Model:
    def __init__(self, path):
        self.path = path
        self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default"))

    def GetType(self):
        return 2 if self.path.suffix.lower() == ".sldasm" else 1

    def GetPathName(self):
        return str(self.path)

    def GetSaveFlag(self):
        return False

    def GetPartBox(self, approximate):
        assert approximate is True
        return (0., 0., 0., .08, .06, .02)

    def GetBox(self, options):
        assert options == 0
        return (0., 0., 0., .08, .06, .02)


class Drawing:
    def __init__(self, model):
        self.model = model
        self.saved = False
        self.view_configuration = model.ConfigurationManager.ActiveConfiguration.Name

    def GetCurrentSheet(self):
        return SimpleNamespace(GetViews=lambda: [SimpleNamespace(Name=name, Type=4,
            GetReferencedModelName=lambda: str(self.model.path), ReferencedConfiguration=self.view_configuration)
            for name in ("Front", "Top", "Right")])


def member(obj, name, *args):
    value = getattr(obj, name)
    return value(*args) if callable(value) else value


@pytest.fixture
def runtime():
    # These real gates import COM bindings but never connect to SolidWorks.
    pytest.importorskip("win32com.client")
    from scripts.sw_drawing import validate_generic_drawing_generation
    from scripts.sw_drawing_review import delivery_verdict
    state = SimpleNamespace(models=[], drawings=[], generation_specs=[], pdf_ok=True, view_mismatch=False, open_calls=0)

    class Session:
        def __init__(self, visible):
            assert visible is True
            self.sw = object()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def open(self, path, read_only, silent, raise_on_error):
            assert (read_only, silent, raise_on_error) == (True, True, True)
            state.open_calls += 1
            self.model = Model(Path(path))
            state.models.append(self.model)
            return self.model

        def new_drawing(self, template_path):
            assert Path(template_path).is_file()
            drawing = Drawing(self.model)
            state.drawings.append(drawing)
            return drawing

        def save(self, drawing, path):
            assert isinstance(drawing, Drawing), "Source model must never be saved"
            Path(path).write_bytes(b"native drawing")
            drawing.saved = True
            return True

    def activate(model, name, *, rebuild, save):
        assert rebuild and not save
        assert name in {"Default", "Alt"}
        model.ConfigurationManager.ActiveConfiguration.Name = name
        return {"success": True, "configuration": name}

    def generate(drawing, spec, source, *, template_candidates):
        assert drawing.saved, "Center marks require the first save"
        assert validate_drawing_spec(spec)["status"] == "pass"
        assert spec["modelSizeMm"] == [80., 60., 20.]
        assert source == str(drawing.model.path)
        state.generation_specs.append(spec)
        if state.view_mismatch:
            drawing.view_configuration = "WRONG"
        return {"status": "review_required", "layout": {}}

    def export(drawing, path, *, sw_app):
        # A failed export can leave a partial file: it must remain evidence only.
        Path(path).write_bytes(b"pdf" if state.pdf_ok else b"partial")
        return state.pdf_ok

    workflow = SimpleNamespace(validate_generic_drawing_generation=validate_generic_drawing_generation,
                               generate_drawing_from_spec=generate, export_sheet_to_pdf=export,
                               inspect_drawing_structure=lambda drawing, **kwargs: {"views": [], "dimensions": []})
    return {"SolidWorksSession": Session, "exclusive_cad_job": nullcontext, "get_com_member": member,
            "activate_configuration": activate, "workflow": workflow, "delivery_verdict": delivery_verdict,
            "review_drawing_artifacts": lambda spec, **kwargs: {"status": "review_required"}, "state": state}


def options(tmp_path, source=None, **overrides):
    template, sheet = tmp_path / "a3.drwdot", tmp_path / "a3.slddrt"
    template.write_bytes(b"template")
    sheet.write_bytes(b"format")
    values = dict(source=source, drawing_template=template, sheet_format=sheet, bom_template=None,
                  paper="A3", standard="GB_T", configuration=None, output=tmp_path / "result",
                  spec=None, include_assemblies=False, extract_zips=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def part(tmp_path, name="part.SLDPRT", data=b"model"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_source_inventory_content_dedup_and_archive_visibility(tmp_path):
    src = tmp_path / "project"
    part(src, "p.SLDPRT", b"same")
    part(src, "sub/duplicate.SLDPRT", b"same")
    part(src, "agent/ignored.SLDPRT", b"ignored")
    part(src, "~$locked.SLDPRT", b"lock")
    with zipfile.ZipFile(src / "parts.zip", "w") as archive:
        archive.writestr("nested/unique.SLDPRT", b"zip")
    scan = cli.scan_source(src)
    assert len(scan["models"]) == 1
    assert len(scan["not_converted"]) == 1
    assert scan["not_converted"][0]["reason"] == "ARCHIVE_MODEL_NOT_EXTRACTED"
    extracted = cli.scan_source(src, extract_root=tmp_path / "extract")
    assert len(extracted["models"]) == 2
    assert not extracted["not_converted"]
    assert Path(extracted["models"][1]["source"]).read_bytes() == b"zip"


def test_missing_template_fails_before_com_and_writes_report(tmp_path):
    src = part(tmp_path)
    opts = options(tmp_path, src, drawing_template=tmp_path / "absent.drwdot")
    result = cli.convert_one(src, opts, runtime={})
    assert result["error_code"] == "DRAWING_TEMPLATE_REQUIRED"
    assert Path(result["report"]).is_file()
    assert result["source_unchanged"]


def test_configuration_roundtrip_and_native_context(tmp_path, runtime):
    src = part(tmp_path)
    result = cli.convert_one(src, options(tmp_path, src, configuration="Alt"), runtime=runtime)
    assert result["status"] == "review_required", result
    assert result["configuration"] == "Alt"
    assert result["source_configuration_restored"]
    assert runtime["state"].models[0].ConfigurationManager.ActiveConfiguration.Name == "Default"
    assert all(v["configuration"] == "Alt" for v in result["view_configurations"])
    assert result["delivery"]["manufacturing"] == "unknown"
    assert result["source_unchanged"]
    assert result["outputs"]["pdf"]
    ledger = json.loads((opts_root := Path(result["report"]).parent / "drawing.run-ledger.json").read_text())
    assert len(ledger["artifacts"]) >= 5


def test_failed_pdf_is_evidence_never_a_published_output(tmp_path, runtime):
    runtime["state"].pdf_ok = False
    src = part(tmp_path)
    result = cli.convert_one(src, options(tmp_path, src), runtime=runtime)
    assert result["status"] == "blocked"
    assert result["outputs"]["pdf"] is None
    ledger = json.loads((Path(result["report"]).parent / "drawing.run-ledger.json").read_text())
    pdf = next(item for item in ledger["artifacts"] if item["path"].endswith(".pdf"))
    assert pdf["role"] == "evidence"


def test_existing_output_refused_even_when_source_matches(tmp_path, runtime):
    src = part(tmp_path)
    opts = options(tmp_path, src)
    first = cli.convert_one(src, opts, runtime=runtime)
    before = Path(first["report"]).read_bytes()
    opts.configuration = "Alt"
    with pytest.raises(cli.ConversionBlocked, match="result"):
        cli.convert_one(src, opts, runtime=runtime)
    assert Path(first["report"]).read_bytes() == before
    assert runtime["state"].open_calls == 1


def test_unknown_spec_field_blocked_before_drawing(tmp_path, runtime):
    src = part(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"configuration": "not a valid spec field"}))
    result = cli.convert_one(src, options(tmp_path, src, spec=spec), runtime=runtime)
    assert result["error_code"] == "DRAWING_SPEC_BLOCKED"
    assert not runtime["state"].drawings


def test_wrong_view_configuration_blocks_and_restores_source(tmp_path, runtime):
    src = part(tmp_path)
    runtime["state"].view_mismatch = True
    result = cli.convert_one(src, options(tmp_path, src, configuration="Alt"), runtime=runtime)
    assert result["error_code"] == "VIEW_CONFIGURATION_UNVERIFIED"
    assert result["source_configuration_restored"]
    assert result["outputs"] == {}


def test_assembly_template_enters_actual_spec(tmp_path, runtime):
    src = part(tmp_path, "assembly.SLDASM")
    bom = tmp_path / "bom.sldbomtbt"
    bom.write_bytes(b"bom")
    result = cli.convert_one(src, options(tmp_path, src, bom_template=bom), runtime=runtime)
    assert result["status"] == "review_required", result
    spec = runtime["state"].generation_specs[0]
    assert spec["documentType"] == "assembly"
    assert spec["bom"]["templatePath"] == str(bom)
    assert spec["bom"]["configuration"] == "Default"


def test_main_same_name_models_have_unique_outputs(tmp_path, runtime, monkeypatch, capsys):
    src = tmp_path / "project"
    part(src, "a/p.SLDPRT", b"first")
    part(src, "b/p.SLDPRT", b"second")
    opts = options(tmp_path, src)
    monkeypatch.setattr(cli, "_load_runtime", lambda: runtime)
    code = cli.main(["convert", "--source", str(src), "--drawing-template", str(opts.drawing_template),
                     "--sheet-format", str(opts.sheet_format)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "review_required"
    assert len(payload["results"]) == 2
    assert len({item["outputs"]["slddrw"] for item in payload["results"]}) == 2
    assert Path(payload["output_root"]).is_relative_to(src / "agent")
    assert Path(payload["report"]).is_file()


def test_repeated_failures_stop_batch_with_unprocessed_items(tmp_path, runtime):
    src = tmp_path / "project"
    for n in range(4):
        part(src, f"part{n}.SLDPRT", str(n).encode())
    opts = options(tmp_path, src)
    runtime["state"].view_mismatch = True
    result = cli.convert_project(opts, runtime=runtime)
    assert result["status"] == "blocked"
    assert len(result["results"]) == 2
    assert len(result["not_converted"]) == 2


def test_doctor_uses_real_preflight_contract(monkeypatch):
    from scripts import sw_preflight
    calls = []
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(sw_preflight, "run_preflight", lambda **kwargs: calls.append(kwargs) or sw_preflight.PreflightResult([], True, True))
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    result = cli.doctor()
    assert result["status"] == "pass"
    assert calls == [{"allow_install": False, "check_solidworks": True}]


def test_cli_help_works_outside_checkout(tmp_path):
    result = subprocess.run([sys.executable, str(cli.REPO_ROOT / "scripts/solidworks_3d_2d.py"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "inventory" in result.stdout


def test_configuration_restore_failure_is_explicit(tmp_path, runtime):
    src = part(tmp_path)
    original = runtime["activate_configuration"]
    def activate(model, name, **kwargs):
        if name == "Default":
            return {"success": False}
        return original(model, name, **kwargs)
    runtime["activate_configuration"] = activate
    result = cli.convert_one(src, options(tmp_path, src, configuration="Alt"), runtime=runtime)
    assert result["status"] == "blocked"
    assert result["error_code"] == "SOURCE_CONFIGURATION_RESTORE_FAILED"
    assert result["source_configuration_restored"] is False


def test_source_write_is_detected_even_if_native_steps_succeed(tmp_path, runtime):
    src = part(tmp_path)
    export = runtime["workflow"].export_sheet_to_pdf
    def changing_export(drawing, path, **kwargs):
        src.write_bytes(b"unexpected change")
        return export(drawing, path, **kwargs)
    runtime["workflow"].export_sheet_to_pdf = changing_export
    result = cli.convert_one(src, options(tmp_path, src), runtime=runtime)
    assert result["error_code"] == "SOURCE_CHANGED_DURING_CONVERSION"
    assert result["source_unchanged"] is False
    assert result["status"] == "blocked"


def test_main_reports_archive_only_inputs_without_silent_success(tmp_path, runtime, monkeypatch, capsys):
    src = tmp_path / "project"
    src.mkdir()
    with zipfile.ZipFile(src / "parts.zip", "w") as archive:
        archive.writestr("p.SLDPRT", b"archive model")
    opts = options(tmp_path, src)
    monkeypatch.setattr(cli, "_load_runtime", lambda: runtime)
    argv = ["convert", "--source", str(src), "--drawing-template", str(opts.drawing_template),
            "--sheet-format", str(opts.sheet_format)]
    assert cli.main(argv) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["not_converted"][0]["reason"] == "ARCHIVE_MODEL_NOT_EXTRACTED"
    assert runtime["state"].open_calls == 0
    assert cli.main(argv + ["--extract-zips"]) == 0
    extracted = json.loads(capsys.readouterr().out)
    assert extracted["results"][0]["source_unchanged"]
    assert Path(extracted["results"][0]["source"]).is_relative_to(Path(extracted["output_root"]) / "sources_ai")
