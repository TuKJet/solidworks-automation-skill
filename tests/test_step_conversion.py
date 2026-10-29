import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.step_conversion import _assembly_evidence, export_step_project, validate_step_file


VALID_STEP = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('test'),'2;1');
FILE_NAME('test.step','2026-01-01',('ai'),('ai'),'','','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN_CC2'));
ENDSEC;
DATA;
#1 = APPLICATION_CONTEXT('a');
#2 = MANIFOLD_SOLID_BREP('shape',());
ENDSEC;
END-ISO-10303-21;
"""


class FakeModel:
    def __init__(self, path, kind):
        self.path, self.kind = str(path), kind
        self.config = "Default"


class FakeSession:
    models = []

    def __init__(self, **_kwargs):
        self.owned = []
        self.sw = object()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def open(self, path, **_kwargs):
        kind = 2 if path.lower().endswith(".sldasm") else 1
        model = FakeModel(path, kind)
        self.owned.append(model)
        self.models.append(model)
        return model

    def close_owned_documents(self):
        self.owned.clear()


class FakeJob:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def fake_get(obj, name, *args, default=None):
    if obj is None:
        return default
    if name == "GetPathName":
        return obj.path
    if name == "GetType":
        return obj.kind
    if name == "GetSaveFlag":
        return 0
    if name == "ConfigurationManager":
        return obj
    if name == "ActiveConfiguration":
        return obj
    if name == "Name":
        return obj.config
    if name == "GetComponents":
        return []
    return default


def fake_export(model, output, **_kwargs):
    Path(output).write_text(VALID_STEP, encoding="ascii")
    return True


def test_native_legacy_metadata_encoding_is_review_not_failed_export(tmp_path):
    source=tmp_path/'part.sldprt'
    source.write_bytes(b'model')
    def legacy(model,output,**kwargs):
        Path(output).write_bytes(VALID_STEP.replace('test.step','中文.step').encode('cp936'))
        return {'success':True,'errors':0,'warnings':0}
    result=export_step_project({'source':source,'output':tmp_path/'out'},runtime=runtime(legacy))
    assert result['results'][0]['status']=='review_required'
    assert result['results'][0]['outputs']['step']
    assert result['results'][0]['step_validation']['metadata_encoding']=='not_verified'


def runtime(export=fake_export):
    return {"get_com_member": fake_get, "activate_configuration": lambda *_a, **_k: {"success": True},
            "export_step": export, "exclusive_cad_job": lambda: FakeJob(), "SolidWorksSession": FakeSession}


def test_validate_step_file_requires_data_entity(tmp_path):
    good = tmp_path / "good.step"
    good.write_text(VALID_STEP, encoding="ascii")
    assert validate_step_file(good)["status"] == "pass"
    bad = tmp_path / "bad.step"
    bad.write_text("ISO-10303-21;\nEND-ISO-10303-21;", encoding="ascii")
    assert validate_step_file(bad)["code"] == "STEP_STRUCTURE_INVALID"
    product_only = tmp_path / "product-only.step"
    product_only.write_text(VALID_STEP.replace("#2 = MANIFOLD_SOLID_BREP('shape',());\n", "#2 = PRODUCT_DEFINITION_SHAPE('shape',());\n"), encoding="ascii")
    assert validate_step_file(product_only)["code"] == "STEP_STRUCTURE_INVALID"


def test_validate_step_accepts_utf8_metadata(tmp_path):
    path = tmp_path / "中文.step"
    path.write_bytes(VALID_STEP.replace("'test.step'", "'中文.step'").encode("utf-8"))
    result = validate_step_file(path)
    assert result["status"] == "pass"
    assert result["metadata_encoding"] == "utf-8"


def test_validate_step_keeps_structural_evidence_for_non_utf8_metadata(tmp_path):
    path = tmp_path / "legacy.step"
    path.write_bytes(VALID_STEP.replace("'test.step'", "'中文.step'").encode("cp936"))
    result = validate_step_file(path)
    assert result["status"] == "review_required"
    assert result["metadata_encoding"] == "not_verified"
    assert result["entity_count"] == 2


def test_assembly_suppression_states_require_review_except_resolved(tmp_path):
    component_file = tmp_path / "component.SLDPRT"
    component_file.write_bytes(b"component")
    model = SimpleNamespace(components=[])

    def get(obj, name, *args, default=None):
        if name == "GetComponents":
            return obj.components
        if name == "GetPathName":
            return str(component_file)
        if name == "GetModelDoc2":
            return object()
        if name == "GetSuppression":
            return obj.suppression
        return default

    for state in (0, 1):
        component = SimpleNamespace(suppression=state)
        model.components = [component]
        evidence = _assembly_evidence(model, get, "sldasm")
        assert evidence["review_required"] is True
        assert evidence["complete"] is False
    component = SimpleNamespace(suppression=2)
    model.components = [component]
    evidence = _assembly_evidence(model, get, "sldasm")
    assert evidence["review_required"] is False
    assert evidence["complete"] is True


def test_export_project_reports_and_preserves_sources(tmp_path):
    source = tmp_path / "models"
    source.mkdir()
    part = source / "Bracket.SLDPRT"
    asm = source / "Assembly.SLDASM"
    part.write_bytes(b"part-source")
    asm.write_bytes(b"assembly-source")
    before = {p: p.read_bytes() for p in (part, asm)}
    out = tmp_path / "out"
    report = export_step_project(SimpleNamespace(source=source, output=out, configuration=None, extract_zips=False), runtime=runtime())
    assert report["status"] == "review_required"
    assert len(report["results"]) == 2
    assert all(item["source_unchanged"] for item in report["results"])
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(list(out.rglob("*_ai.step"))) == 2
    assert json.loads((out / "batch_report_ai.json").read_text(encoding="utf-8"))["status"] == "review_required"


def test_existing_output_is_refused(tmp_path):
    source = tmp_path / "part.SLDPRT"
    source.write_bytes(b"source")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(FileExistsError):
        export_step_project(SimpleNamespace(source=source, output=out, configuration=None, extract_zips=False), runtime=runtime())


def test_invalid_native_step_is_not_published(tmp_path):
    source = tmp_path / "part.SLDPRT"
    source.write_bytes(b"source")
    out = tmp_path / "out"
    report = export_step_project(SimpleNamespace(source=source, output=out, configuration=None, extract_zips=False), runtime=runtime(lambda _m, p, **_k: (Path(p).write_text("bad", encoding="ascii"), True)[1]))
    assert report["status"] == "blocked"
    assert list(out.rglob("*_ai.step"))
    ledger = json.loads(next(out.glob("*.run-ledger.json")).read_text(encoding="utf-8"))
    assert all(item["role"] != "final" for item in ledger["artifacts"] if item["path"].endswith(".step"))


def test_repeated_error_stops_batch_and_reports_remaining_sources(tmp_path):
    source = tmp_path / "models"
    source.mkdir()
    for name in ("a.SLDPRT", "b.SLDPRT", "c.SLDPRT"):
        (source / name).write_bytes(name.encode())
    def fail_export(_model, _path):
        raise RuntimeError("native unavailable")
    report = export_step_project(SimpleNamespace(source=source, output=tmp_path / "out", configuration=None, extract_zips=False), runtime=runtime(fail_export))
    assert report["status"] == "blocked"
    assert len(report["results"]) == 2
    assert report["not_converted"][0]["reason"] == "BATCH_STOPPED_AFTER_REPEATED_ERROR"


def test_same_basename_and_hash_assemblies_get_stable_unique_outputs(tmp_path):
    source = tmp_path / "models"
    (source / "one").mkdir(parents=True)
    (source / "two").mkdir()
    (source / "one" / "same.SLDASM").write_bytes(b"same-assembly")
    (source / "two" / "same.SLDASM").write_bytes(b"same-assembly")
    report = export_step_project(SimpleNamespace(source=source, output=tmp_path / "out", configuration=None, extract_zips=False), runtime=runtime())
    assert len(report["results"]) == 2
    assert len({item["outputs"]["step"] for item in report["results"]}) == 2
    assert report["duplicate_sources"] == []
