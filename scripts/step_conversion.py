"""Verified SolidWorks part/assembly to STEP Part 21 conversion.

This module deliberately keeps the native CAD operation small: SolidWorks is
opened read-only, the existing STEP exporter is called, and the resulting file
is published only after a structural ISO-10303-21 check.  It does not claim
that FreeCAD was installed or used for import verification.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_ledger import RunLedger, sha256_file


class StepConversionBlocked(RuntimeError):
    """A conversion cannot be safely published."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def validate_step_file(path: Path) -> dict[str, Any]:
    """Validate Part 21 structure without rejecting non-ASCII metadata."""
    target = Path(path)
    if not target.is_file() or target.stat().st_size == 0:
        return {"status": "blocked", "code": "STEP_MISSING_OR_EMPTY", "path": str(target)}
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return {"status": "blocked", "code": "STEP_READ_FAILED", "error": str(exc), "path": str(target)}
    try:
        raw.decode("utf-8-sig")
        metadata_encoding = "utf-8"
    except UnicodeDecodeError:
        metadata_encoding = "not_verified"
    upper = raw.upper()
    data_start = upper.find(b"DATA;")
    data_end = upper.find(b"ENDSEC;", data_start + 5) if data_start >= 0 else -1
    entity_text = raw[data_start:data_end if data_end >= 0 else len(raw)]
    entities = len(re.findall(rb"(?m)^\s*#\d+\s*=", entity_text))
    schema_match = re.search(rb"(?im)^\s*FILE_SCHEMA\s*\(", raw) is not None
    shape_entities = len(re.findall(rb"(?im)^\s*#\d+\s*=.*(?:MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS|FACETED_BREP|SHELL_BASED_WIREFRAME_MODEL|ADVANCED_BREP_SHAPE_REPRESENTATION)", entity_text))
    required = {
        "header": b"HEADER;" in upper,
        "data": data_start >= 0,
        "data_entities": entities > 0,
        "file_schema": schema_match,
        "shape_entities": shape_entities > 0,
        "endsec": data_end >= 0,
        "end_file": b"END-ISO-10303-21;" in upper,
    }
    if not (upper.lstrip(b"\xef\xbb\xbf\t\r\n ").startswith(b"ISO-10303-21") and all(required.values())):
        return {"status": "blocked", "code": "STEP_STRUCTURE_INVALID", "path": str(target), "checks": required, "entity_count": entities, "metadata_encoding": metadata_encoding}
    schema = "STEP ISO-10303-21"
    schema_line = re.search(rb"(?im)^\s*FILE_SCHEMA\s*\([^\r\n]*", raw)
    if schema_line:
        try:
            schema = schema_line.group(0).decode("ascii").strip()
        except UnicodeDecodeError:
            schema = "FILE_SCHEMA (metadata non-ASCII)"
    status = "pass" if metadata_encoding == "utf-8" else "review_required"
    return {"status": status, "path": str(target), "size_bytes": target.stat().st_size, "schema": schema, "entity_count": entities, "shape_entity_count": shape_entities, "units": "not_inferred", "metadata_encoding": metadata_encoding, "checks": required}


def _option(options: Any, name: str, default: Any = None) -> Any:
    return options.get(name, default) if isinstance(options, dict) else getattr(options, name, default)


def _runtime_default() -> dict[str, Any]:
    from .sw_connect import get_com_member
    from .sw_document_data import activate_configuration
    from .sw_export import export_to_step_detailed
    from .sw_process import exclusive_cad_job
    from .sw_session import SolidWorksSession
    return {"get_com_member": get_com_member, "activate_configuration": activate_configuration,
            "export_step": export_to_step_detailed, "exclusive_cad_job": exclusive_cad_job,
            "SolidWorksSession": SolidWorksSession}


def _discover(source: Path, extract_root: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from .solidworks_3d_2d import scan_source
    scan = scan_source(source, include_assemblies=True, extract_root=extract_root)
    rows = []
    for record in scan.get("models", []):
        actual = Path(record["source"]).resolve()
        suffix = actual.suffix.casefold()
        rows.append({"path": actual, "source_path": Path(record.get("path") or actual), "sha256": record.get("sha256"),
                     "document_type": suffix[1:], "archive": record.get("archive")})
    return rows, scan.get("not_converted", []), scan


def _configuration(model: Any, get: Any) -> str:
    value = get(model, "ConfigurationManager", default=None)
    active = get(value, "ActiveConfiguration", default=None) if value is not None else None
    return str(get(active, "Name", default="") or "")


def _assembly_evidence(model: Any, get: Any, document_type: str) -> dict[str, Any]:
    if document_type != "sldasm":
        return {"status": "not_applicable", "complete": True, "review_required": False}
    try:
        components = get(model, "GetComponents", False, default=None)
        if components is None:
            components = get(model, "GetComponents", default=None)
        components = list(components or [])
    except Exception as exc:
        return {"status": "unknown", "complete": False, "review_required": True, "reason": "ASSEMBLY_COMPONENT_EVIDENCE_UNAVAILABLE", "error": str(exc)}
    missing, suppressed, component_details = [], [], []
    if not components:
        return {"status": "unknown", "complete": False, "review_required": True,
                "reason": "ASSEMBLY_COMPONENT_EVIDENCE_EMPTY", "component_count": 0,
                "missing": [], "suppressed_or_unresolved": [], "components": []}
    for component in components:
        path = str(get(component, "GetPathName", default="") or "")
        loaded = get(component, "GetModelDoc2", default=None)
        file_exists = bool(path and Path(path).is_file())
        loaded_flag = loaded is not None
        suppression = get(component, "GetSuppression", default=None)
        detail = {"path": path, "name": str(get(component, "Name2", default=get(component, "GetTitle", default="")) or ""),
                  "referenced_config": str(get(component, "ReferencedConfiguration", default="") or ""),
                  "suppression": suppression, "loaded": loaded_flag, "file_exists": file_exists}
        component_details.append(detail)
        if not path or not file_exists or not loaded_flag:
            missing.append("<unknown>")
            if path:
                missing[-1] = path
        # SolidWorks swComponentSuppressionState_e: 0=suppressed,
        # 1=lightweight, 2=fully resolved.  Only 2 proves a loaded component.
        if suppression is None or suppression != 2:
            suppressed.append(path or "<unknown>")
    complete = not missing and not suppressed
    return {"status": "pass" if complete else "review_required", "complete": complete, "review_required": not complete,
            "component_count": len(components), "missing": missing, "suppressed_or_unresolved": suppressed, "components": component_details}


def _stem(path: Path) -> str:
    return path.stem if path.stem.casefold().endswith("_ai") else f"{path.stem}_ai"


def _export_one(row: dict[str, Any], root: Path, options: Any, runtime: dict[str, Any], session: Any, ledger: RunLedger) -> dict[str, Any]:
    source = Path(row["path"])
    base_stem = _stem(source)
    output_dir = root / base_stem
    if output_dir.exists():
        output_dir = root / f"{base_stem}_{str(row['sha256'])[:8]}"
    output_dir.mkdir(parents=True, exist_ok=False)
    step_path = output_dir / f"{base_stem}.step"
    before_error = None
    try:
        before_hash = sha256_file(source)
    except Exception as exc:
        before_hash, before_error = None, str(exc)
    result: dict[str, Any] = {"source": str(row["source_path"]), "opened_path": str(source), "document_type": row["document_type"],
                              "source_sha256_before": before_hash, "source_hash_error": before_error, "status": "blocked", "outputs": {},
                              "freecad_compatibility": {"status": "not_tested", "claim": "STEP ISO-10303-21; FreeCAD import not executed in this VM"}}
    model = None
    original_config = ""
    config_changed = False
    requested_config = None
    restored_config = None
    try:
        if before_hash is None:
            raise StepConversionBlocked("SOURCE_HASH_READ_FAILED", before_error or "unknown")
        model = session.open(str(source), read_only=True, silent=True, raise_on_error=True)
        get = runtime["get_com_member"]
        actual_path = str(get(model, "GetPathName", default="") or "")
        if not actual_path:
            raise StepConversionBlocked("SOURCE_PATH_READBACK_MISSING")
        if Path(actual_path).resolve() != source.resolve():
            raise StepConversionBlocked("SOURCE_PATH_READBACK_MISMATCH", actual_path)
        native_type = get(model, "GetType", default=None)
        expected = 1 if row["document_type"] == "sldprt" else 2
        if native_type is None:
            raise StepConversionBlocked("SOURCE_TYPE_READBACK_MISSING")
        if native_type != expected:
            raise StepConversionBlocked("SOURCE_TYPE_READBACK_MISMATCH", str(native_type))
        original_config = _configuration(model, get)
        if not original_config:
            raise StepConversionBlocked("SOURCE_CONFIGURATION_READBACK_MISSING")
        if get(model, "GetSaveFlag", default=None) is None:
            raise StepConversionBlocked("SOURCE_SAVE_FLAG_READBACK_MISSING")
        if get(model, "GetSaveFlag", default=0):
            raise StepConversionBlocked("SOURCE_DIRTY_AFTER_READ_ONLY_OPEN")
        requested = _option(options, "configuration") or original_config
        requested_config = requested
        if requested and original_config and requested.casefold() != original_config.casefold():
            config_changed = True
            restore = runtime["activate_configuration"](model, requested, rebuild=True, save=False)
            if not restore or not restore.get("success"):
                raise StepConversionBlocked("SOURCE_CONFIGURATION_ACTIVATION_FAILED", requested)
            if _configuration(model, get).casefold() != requested.casefold():
                raise StepConversionBlocked("SOURCE_CONFIGURATION_MISMATCH", requested)
        result["configuration"] = _configuration(model, get)
        result["assembly_evidence"] = _assembly_evidence(model, get, row["document_type"])
        native_result = runtime["export_step"](model, str(step_path), sw_app=getattr(session, "sw", None))
        if isinstance(native_result, dict):
            result["native_export"] = native_result
            if not native_result.get("success"):
                raise StepConversionBlocked("NATIVE_STEP_EXPORT_FAILED", str(native_result))
            if native_result.get("errors") not in (None, 0):
                raise StepConversionBlocked("NATIVE_STEP_EXPORT_ERROR", str(native_result))
        elif not native_result:
            raise StepConversionBlocked("NATIVE_STEP_EXPORT_FAILED", str(native_result))
        if not step_path.is_file():
            raise StepConversionBlocked("NATIVE_STEP_OUTPUT_MISSING")
        validation = validate_step_file(step_path)
        result["step_validation"] = validation
        if validation["status"] == "blocked":
            raise StepConversionBlocked(validation["code"])
        result["outputs"]["step"] = str(step_path)
        warning = isinstance(native_result, dict) and native_result.get("warnings") not in (None, 0)
        result["status"] = "review_required" if warning or validation["status"] == "review_required" or result["assembly_evidence"]["review_required"] else "pass"
    except StepConversionBlocked as exc:
        result.update({"status": "blocked", "error_code": exc.code, "error": str(exc)})
    except Exception as exc:
        result.update({"status": "blocked", "error_code": getattr(exc, "code", "STEP_CONVERSION_FAILED"), "error": str(exc)})
    finally:
        if model is not None and config_changed:
            try:
                restore = runtime["activate_configuration"](model, original_config, rebuild=True, save=False)
                restored_config = bool(restore and restore.get("success")) and _configuration(model, runtime["get_com_member"]).casefold() == original_config.casefold()
                if not restored_config:
                    raise RuntimeError("configuration restore readback failed")
            except Exception as exc:
                result.update({"status": "blocked", "error_code": "SOURCE_CONFIGURATION_RESTORE_FAILED", "error": str(exc)})
        elif model is not None:
            restored_config = True
        try:
            final_actual_config = _configuration(model, runtime["get_com_member"]) if model is not None else None
        except Exception:
            final_actual_config = None
        if model is not None and original_config:
            restored_config = bool(final_actual_config) and final_actual_config.casefold() == original_config.casefold()
            if not restored_config:
                result.update({"status": "blocked", "error_code": "SOURCE_CONFIGURATION_RESTORE_FAILED", "error": "Final source configuration differs from the original"})
        result["configuration_evidence"] = {"original": original_config or None, "requested": requested_config,
                                             "restored": restored_config, "final_actual": final_actual_config}
        try:
            session.close_owned_documents()
        except Exception as exc:
            result.update({"status": "blocked", "error_code": getattr(exc, "code", "SW_DOCUMENT_CLEANUP_FAILED"), "error": str(exc)})
        try:
            result["source_sha256_after"] = sha256_file(source)
            result["source_hash_after_error"] = None
        except Exception as exc:
            result["source_sha256_after"] = None
            result["source_hash_after_error"] = str(exc)
            result.update({"status": "blocked", "error_code": "SOURCE_HASH_READ_FAILED_AFTER", "error": str(exc)})
        result["source_unchanged"] = bool(result["source_sha256_before"] and result["source_sha256_before"] == result["source_sha256_after"])
        if result["source_sha256_before"] is not None and result["source_sha256_after"] is not None and not result["source_unchanged"]:
            result.update({"status": "blocked", "error_code": "SOURCE_CHANGED"})
        if result.get("status") == "blocked" and result.get("outputs", {}).get("step"):
            result.setdefault("evidence_outputs", {})["step"] = result["outputs"].pop("step")
        if result.get("status") == "pass":
            ledger.register_artifact(step_path, role="final", dependencies=[source], created_by_run=True)
        elif step_path.is_file():
            ledger.register_artifact(step_path, role="evidence", dependencies=[source], created_by_run=True)
        report = output_dir / f"{base_stem}.report.json"
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        ledger.register_artifact(report, role="evidence", dependencies=[source], created_by_run=True)
    return result


def export_step_project(options: Any, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Export all parts/assemblies from ``options.source`` into a new run root."""
    runtime = {**_runtime_default(), **(runtime or {})}
    source = Path(_option(options, "source")).expanduser().resolve()
    output = Path(_option(options, "output") or (source.parent / f"step-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}" )).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refuse to overwrite existing output directory: {output}")
    output.mkdir(parents=True, exist_ok=False)
    ledger = RunLedger(output, f"step-export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}", "solidworks-3d-2d", policy={"overwrite": False, "read_only": True})
    rows, not_converted, scan = [], [], {}
    results = []
    busy = False
    discovery_error = None
    try:
        rows, not_converted, scan = _discover(source, output / "sources_ai" if _option(options, "extract_zips", False) else None)
        inventory_path = output / "inventory_ai.json"
        inventory_path.write_text(json.dumps(scan, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        ledger.register_artifact(inventory_path, role="evidence", created_by_run=True)
        for extracted in ((output / "sources_ai").rglob("*") if (output / "sources_ai").exists() else ()):
            if extracted.is_file():
                ledger.register_artifact(extracted, role="evidence", created_by_run=True)
    except Exception as exc:
        discovery_error = {"status": "blocked", "error_code": getattr(exc, "code", "STEP_SOURCE_DISCOVERY_FAILED"), "error": str(exc)}
        results.append(discovery_error)
        not_converted.append({"source": str(source), "reason": "SOURCE_DISCOVERY_FAILED"})
    last_error = None
    consecutive_errors = 0
    processed = 0
    try:
        if not discovery_error:
            with runtime["exclusive_cad_job"]():
                with runtime["SolidWorksSession"](visible=True) as session:
                    for index, row in enumerate(rows):
                        processed = index + 1
                        result = _export_one(row, output, options, runtime, session, ledger)
                        results.append(result)
                        error_code = result.get("error_code")
                        if error_code:
                            consecutive_errors = consecutive_errors + 1 if error_code == last_error else 1
                            last_error = error_code
                        else:
                            consecutive_errors, last_error = 0, None
                        if error_code in {"SW_HOST_BUSY", "SW_DOCUMENT_CLEANUP_FAILED", "SW_COM_CONNECTION_LOST"} or (error_code and consecutive_errors >= 2):
                            busy = True
                            reason = "BATCH_STOPPED_AFTER_REPEATED_ERROR" if consecutive_errors >= 2 else "BATCH_STOPPED_AFTER_FATAL_ERROR"
                            not_converted.extend({"source": str(rest["source_path"]), "reason": reason} for rest in rows[index + 1:])
                            break
    except Exception as exc:
        results.append({"status": "blocked", "error_code": getattr(exc, "code", "STEP_BATCH_FAILED"), "error": str(exc)})
        not_converted.extend({"source": str(rest["source_path"]), "reason": "BATCH_EXCEPTION"} for rest in rows[processed:])
    status = "blocked" if any(item.get("status") == "blocked" for item in results) or busy else "review_required"
    report = {"status": status, "source": str(source), "output": str(output), "count": len(rows), "results": results,
              "not_converted": not_converted, "duplicate_sources": scan.get("duplicates", []),
              "freecad_compatibility": {"status": "not_tested", "claim": "STEP ISO-10303-21 structure verified; FreeCAD import not executed in this VM"}}
    report_path = output / "batch_report_ai.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    ledger.register_artifact(report_path, role="evidence", created_by_run=True)
    ledger.data.setdefault("checkpoints", {})["batch"] = {"phase": "export", "status": status, "count": len(results), "not_converted": len(not_converted)}
    ledger.save()
    return report
