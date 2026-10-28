"""Run ledger and resumable project batch lifecycle tests."""

import json
from pathlib import Path

import pytest

from apps.desktop.cad_workbench.artifact_ledger import RunLedger, sha256_file
from scripts.sw_project_batch import run_batch


def test_run_ledger_cleanup_is_dry_run_and_protects_dependencies(tmp_path: Path):
    root = tmp_path / "run"
    root.mkdir()
    ledger = RunLedger(root, "r1", "tester")
    scratch = root / "scratch.bin"
    final = root / "final.step"
    evidence = root / "review.json"
    scratch.write_bytes(b"scratch")
    final.write_bytes(b"final")
    evidence.write_text("evidence", encoding="utf-8")
    ledger.register_artifact(scratch, role="scratch", created_by_run=True)
    ledger.register_artifact(final, role="final", dependencies=[scratch], created_by_run=True)
    ledger.register_artifact(evidence, role="evidence", dependencies=[final], created_by_run=True)
    result = ledger.cleanup(dry_run=True, reopened_dependency_audit=True)
    assert result["dry_run"] is True
    assert result["deleted"] == []
    assert result["preserved"][0]["reason"] == "dependency_closure"
    assert scratch.is_file()


def test_fingerprint_reads_explicit_external_source_and_dependency_paths(tmp_path: Path):
    root = tmp_path / "run"
    root.mkdir()
    source = tmp_path / "source.step"
    dependency = tmp_path / "dependency.json"
    source.write_bytes(b"source")
    dependency.write_bytes(b"dependency")
    ledger = RunLedger(root, "r1", "tester")
    first = ledger.fingerprint(source, [dependency], {"policy": 1})
    source.write_bytes(b"changed")
    assert first != ledger.fingerprint(source, [dependency], {"policy": 1})


def test_run_ledger_rejects_owner_takeover(tmp_path: Path):
    root = tmp_path / "run"
    RunLedger(root, "r1", "owner-a")
    with pytest.raises(ValueError, match="owned by another run"):
        RunLedger(root, "r1", "owner-b")


def test_run_ledger_preserves_tampered_and_outside_files(tmp_path: Path):
    root = tmp_path / "run"
    root.mkdir()
    ledger = RunLedger(root, "r1", "tester")
    scratch = root / "scratch.bin"
    scratch.write_bytes(b"old")
    ledger.register_artifact(scratch, role="scratch", created_by_run=True)
    scratch.write_bytes(b"tampered")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError):
        ledger.register_artifact(outside, role="scratch")
    result = ledger.cleanup(dry_run=False, reopened_dependency_audit=True)
    assert result["deleted"] == []
    assert scratch.is_file()


def test_cleanup_protects_transitive_dependency_closure(tmp_path: Path):
    root = tmp_path / "run"
    root.mkdir()
    ledger = RunLedger(root, "r1", "tester")
    scratch2 = root / "scratch2.bin"
    scratch1 = root / "scratch1.bin"
    final = root / "final.step"
    for path in (scratch2, scratch1, final):
        path.write_bytes(path.name.encode())
    ledger.register_artifact(scratch2, role="scratch", created_by_run=True)
    ledger.register_artifact(scratch1, role="scratch", dependencies=[scratch2], created_by_run=True)
    ledger.register_artifact(final, role="final", dependencies=[scratch1], created_by_run=True)
    result = ledger.cleanup(dry_run=False, reopened_dependency_audit=True)
    assert result["deleted"] == []
    assert scratch1.is_file() and scratch2.is_file()


def test_batch_resume_does_not_regenerate_and_input_change_invalidates(tmp_path: Path):
    calls = []
    run_root = tmp_path / "run"
    item = {"id": "p1", "source": str(tmp_path / "source.step"), "dependencies": []}
    Path(item["source"]).write_bytes(b"source")

    def generate(item, context):
        calls.append("generate")
        output = context["root"] / "p1.step"
        output.write_bytes(b"generated")
        return {"outputs": [output]}

    def verify(item, context, generated):
        calls.append("verify")
        return {"status": "pass", "outputs": generated["outputs"]}

    def publish(item, context, verified):
        calls.append("publish")
        return {"status": "published", "outputs": verified["outputs"]}

    first = run_batch([item], run_root, "r1", {}, generate=generate, verify=verify, publish=publish)
    assert first["status"] == "published"
    second = run_batch([item], run_root, "r1", {}, generate=generate, verify=verify, publish=publish)
    assert second["status"] == "published"
    assert calls.count("generate") == 1
    assert calls.count("verify") == 1
    assert calls.count("publish") == 1
    Path(item["source"]).write_bytes(b"changed")
    third = run_batch([item], run_root, "r1", {}, generate=generate, verify=verify, publish=publish)
    assert third["status"] == "published"
    assert calls.count("generate") == 2


def test_batch_repeated_same_error_fails_fast(tmp_path: Path):
    run_root = tmp_path / "run"
    item = {"id": "p1", "source": str(tmp_path / "source.step"), "dependencies": []}
    Path(item["source"]).write_bytes(b"source")
    calls = []

    def generate(item, context):
        calls.append(1)
        raise RuntimeError("E_SAME")

    result = run_batch([item, {**item, "id": "p2"}], run_root, "r1", {}, generate=generate)
    assert result["status"] == "failed"
    assert result["error_code"] == "E_SAME"
    assert len(calls) == 2


def test_batch_same_error_code_across_distinct_sources_fails_fast(tmp_path: Path):
    source_a = tmp_path / "a.step"
    source_b = tmp_path / "b.step"
    source_a.write_bytes(b"a")
    source_b.write_bytes(b"b")
    calls = []

    def generate(item, context):
        calls.append(item["id"])
        raise RuntimeError("E_SHARED")

    result = run_batch([
        {"id": "a", "source": str(source_a), "dependencies": []},
        {"id": "b", "source": str(source_b), "dependencies": []},
    ], tmp_path / "run", "r1", {}, generate=generate)
    assert result["status"] == "failed"
    assert result["error_code"] == "E_SHARED"
    assert calls == ["a", "b"]


def test_batch_missing_source_writes_failed_report_and_empty_batch_is_not_verified(tmp_path: Path):
    root = tmp_path / "run"
    missing = tmp_path / "missing.step"
    result = run_batch([{"id": "missing", "source": str(missing), "dependencies": []}], root, "r1", {}, generate=lambda *_: {})
    assert result["status"] == "failed"
    assert (root / "batch_report.json").is_file()
    empty = run_batch([], tmp_path / "empty-run", "r2", {})
    assert empty["status"] not in {"verified", "published"}


def test_published_resume_requires_all_phase_hashes(tmp_path: Path):
    source = tmp_path / "source.step"
    source.write_bytes(b"source")
    root = tmp_path / "run"
    calls = []

    def generate(item, context):
        calls.append("generate")
        output = context["root"] / "generated.step"
        output.write_bytes(b"generated")
        return {"outputs": [output], "meta": "generated"}

    def verify(item, context, generated):
        calls.append("verify")
        evidence = context["root"] / "evidence.json"
        evidence.write_text("evidence", encoding="utf-8")
        return {"status": "pass", "outputs": [evidence], "meta": "verified"}

    def publish(item, context, verified):
        calls.append("publish")
        final = context["root"] / "final.step"
        final.write_bytes(b"final")
        return {"status": "published", "outputs": [final]}

    item = {"id": "p", "source": str(source), "dependencies": []}
    run_batch([item], root, "r1", {}, generate=generate, verify=verify, publish=publish)
    (root / "evidence.json").write_text("tampered", encoding="utf-8")
    resumed = run_batch([item], root, "r1", {}, generate=generate, verify=verify, publish=publish)
    assert resumed["status"] == "published"
    assert calls == ["generate", "verify", "publish", "verify", "publish"]


def test_batch_rejects_empty_phase_outputs_and_writes_incremental_report(tmp_path: Path):
    source = tmp_path / "source.step"
    source.write_bytes(b"source")
    root = tmp_path / "run"
    observed = []

    def generate(item, context):
        return {"outputs": []}

    def verify(item, context, generated):
        observed.append((context["root"] / "batch_report.json").is_file())
        return {"status": "pass", "outputs": []}

    result = run_batch([{"id": "p1", "source": str(source), "dependencies": []}], root, "r1", {}, generate=generate, verify=verify)
    assert result["status"] == "failed"
    assert not observed
    assert (root / "batch_report.json").is_file()
