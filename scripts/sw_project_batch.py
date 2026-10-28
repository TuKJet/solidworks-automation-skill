"""Resumable project batch orchestration over existing CAD callbacks."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Callable

from apps.desktop.cad_workbench.artifact_ledger import RunLedger, sha256_file


def _outputs(value: Any) -> list[Path]:
    if not isinstance(value, dict):
        return []
    raw = value.get("outputs") or []
    if isinstance(raw, (str, Path)):
        raw = [raw]
    result = []
    for item in raw:
        result.append(Path(item.get("path") if isinstance(item, dict) else item))
    return result


def _error_code(error: Exception) -> str:
    if getattr(error, 'hresult', None) in {-2147023170, -2147023174, -2147417848}:
        return 'SW_COM_CONNECTION_LOST'
    return str(getattr(error, "error_code", None) or str(error).split(":", 1)[0] or error.__class__.__name__)


def _input_hashes(source: Path, dependencies: list[Path]) -> dict[str, str]:
    paths = [source, *dependencies]
    return {str(path.expanduser().resolve()): sha256_file(path.expanduser().resolve()) for path in paths}


def _write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def run_batch(items: list[dict[str, Any]], root: Path, run_id: str, policy: dict[str, Any], *, generate: Callable | None = None, verify: Callable | None = None, publish: Callable | None = None) -> dict[str, Any]:
    ledger = RunLedger(root, run_id, "sw_project_batch", policy=policy)
    error_counts = ledger.data.setdefault("errorCounts", {})
    report_path = ledger.root / "batch_report.json"
    results = []
    _write_report(report_path, {"schemaVersion": "1.0", "runId": run_id, "status": "running", "results": [], "errorCounts": error_counts})
    consecutive_code = None
    consecutive_count = 0
    for item in items:
        item_id = str(item["id"])
        source = Path(item["source"])
        dependencies = [Path(value) for value in item.get("dependencies", [])]
        try:
            fingerprint = ledger.fingerprint(source, dependencies, policy)
            input_before = _input_hashes(source, dependencies)
        except Exception as error:
            code = _error_code(error)
            consecutive_count = consecutive_count + 1 if consecutive_code == code else 1
            consecutive_code = code
            results.append({"id": item_id, "status": "failed", "error_code": code})
            _write_report(report_path, {"schemaVersion": "1.0", "runId": run_id, "status": "running", "results": results, "errorCounts": error_counts})
            if consecutive_count >= 2 or code == 'SW_COM_CONNECTION_LOST':
                break
            continue
        previous = ledger.data.get("checkpoints", {}).get(item_id, {})
        context = {"root": ledger.root, "ledger": ledger, "fingerprint": fingerprint}
        generated = verified = published = None
        try:
            previous_generated = previous.get("generated", {})
            output_paths = [Path(value) for value in previous_generated.get("outputs", [])]
            def valid_phase(phase: dict[str, Any]) -> bool:
                paths = [Path(value) for value in phase.get("outputs", [])]
                hashes = phase.get("outputHashes", [])
                return bool(paths) and len(paths) == len(hashes) and all(path.is_file() and path.resolve().is_relative_to(ledger.root) and sha256_file(path) == expected for path, expected in zip(paths, hashes))

            if previous.get("published", {}).get("status") == "published" and previous.get("published", {}).get("fingerprint") == fingerprint and valid_phase(previous["published"]):
                if not all(valid_phase(previous.get(phase, {})) for phase in ("generated", "verified", "published")):
                    pass
                else:
                    results.append({"id": item_id, "status": "published", "fingerprint": fingerprint, "resumed": True})
                    _write_report(report_path, {"schemaVersion": "1.0", "runId": run_id, "status": "running", "results": results, "errorCounts": error_counts})
                    if input_before != _input_hashes(source, dependencies):
                        results.append({"id": item_id, "status": "failed", "error_code": "input_changed_after_phase"})
                    consecutive_code = None
                    consecutive_count = 0
                    continue
            if previous_generated.get("status") == "generated" and previous_generated.get("fingerprint") == fingerprint and valid_phase(previous_generated):
                generated = previous_generated.get("result") or {"outputs": output_paths, "resumed": True}
            else:
                if generate is None:
                    raise RuntimeError("generate callback required")
                ledger.data.setdefault("checkpoints", {})[item_id] = {}
                generated = generate(item, context)
                outputs = _outputs(generated)
                if not outputs:
                    raise RuntimeError("generated_outputs_empty")
                for path in outputs:
                    ledger.register_artifact(path, role="scratch", dependencies=dependencies, created_by_run=True)
                ledger.checkpoint(item_id, phase="generated", status="generated", fingerprint=fingerprint, outputs=outputs, result=_jsonable(generated))
            previous_verified = previous.get("verified", {})
            if previous_verified.get("status") == "verified" and previous_verified.get("fingerprint") == fingerprint and valid_phase(previous_verified):
                verified = previous_verified.get("result") or {"outputs": [Path(value) for value in previous_verified.get("outputs", [])], "status": "verified", "resumed": True}
            else:
                if verify is None:
                    raise RuntimeError("verify callback required")
                verified = verify(item, context, generated)
                if not isinstance(verified, dict) or str(verified.get("status")) not in {"pass", "verified"}:
                    raise RuntimeError("readability_failure")
                verify_outputs = _outputs(verified)
                if not verify_outputs:
                    raise RuntimeError("verified_outputs_empty")
                for path in verify_outputs:
                    ledger.register_artifact(path, role="evidence", dependencies=_outputs(generated), created_by_run=True)
                ledger.checkpoint(item_id, phase="verified", status="verified", fingerprint=fingerprint, outputs=verify_outputs, result=_jsonable(verified))
            if publish is not None:
                published = publish(item, context, verified)
                if not isinstance(published, dict) or str(published.get("status")) not in {"published", "pass"} or not _outputs(published):
                    raise RuntimeError("publish_failed")
                for path in _outputs(published):
                    ledger.register_artifact(path, role="final", dependencies=[*_outputs(generated), *_outputs(verified)], created_by_run=True)
                ledger.checkpoint(item_id, phase="published", status="published", fingerprint=fingerprint, outputs=_outputs(published), result=_jsonable(published))
            results.append({"id": item_id, "status": "published" if published else "verified", "fingerprint": fingerprint})
            consecutive_code = None
            consecutive_count = 0
            _write_report(report_path, {"schemaVersion": "1.0", "runId": run_id, "status": "running", "results": results, "errorCounts": error_counts})
        except Exception as error:
            code = _error_code(error)
            error_key = f"{code}:{fingerprint}"
            error_counts[error_key] = int(error_counts.get(error_key, 0)) + 1
            ledger.data["errorCounts"] = error_counts
            ledger.checkpoint(item_id, phase="failed", status="failed", fingerprint=fingerprint, error_code=code)
            results.append({"id": item_id, "status": "failed", "error_code": code})
            _write_report(report_path, {"schemaVersion": "1.0", "runId": run_id, "status": "running", "results": results, "errorCounts": error_counts})
            consecutive_count = consecutive_count + 1 if consecutive_code == code else 1
            consecutive_code = code
            if consecutive_count >= 2 or error_counts[error_key] >= 2 or code == 'SW_COM_CONNECTION_LOST':
                ledger.data["status"] = "failed"
                break
        input_after = _input_hashes(source, dependencies)
        if input_before != input_after:
            results.append({"id": item_id, "status": "failed", "error_code": "input_changed_after_phase"})
            break
    status = "failed" if any(item["status"] == "failed" for item in results) else ("published" if results and all(item["status"] == "published" for item in results) else "verified" if results else "empty")
    report = {"schemaVersion": "1.0", "runId": run_id, "status": status, "results": results, "errorCounts": error_counts}
    if status == "failed":
        report["error_code"] = next((item.get("error_code") for item in reversed(results) if item.get("status") == "failed"), None)
    _write_report(report_path, report)
    return report


if __name__ == "__main__":
    raise SystemExit("Use run_batch from an existing CAD workflow; no arbitrary callback entrypoint is exposed.")
