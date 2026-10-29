"""Standalone, fail-closed SolidWorks project-to-drawing command line entrypoint."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.artifact_ledger import RunLedger, sha256_file
from scripts.sw_project_inventory import inventory_project
from scripts.drawing_spec import validate_drawing_spec
from scripts.annotation_acceptance import evaluate_annotation_acceptance

SKILL_NAME = "solidworks-3d-2d"
MODEL_TYPES = {".sldprt": (1, "part"), ".sldasm": (2, "assembly")}


def implementation_fingerprint():
    """Identify the exact installed implementation used by a native run."""
    paths = sorted(list((REPO_ROOT / "scripts").rglob("*.py")) +
                   list((REPO_ROOT / "subskills").rglob("*.py")))
    return {path.relative_to(REPO_ROOT).as_posix(): sha256_file(path) for path in paths}


class ConversionBlocked(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@contextmanager
def _shared_session_context(session):
    """Yield a batch-owned session and always release that item's documents."""
    try:
        yield session
    finally:
        session.close_owned_documents()


def _run_id():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]


def _json_write(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _new_ledger(root, run_id, policy=None):
    """Claim a new run atomically; never adopt an existing or interrupted run."""
    _reject_links(root)
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ConversionBlocked("OUTPUT_EXISTS_REFUSE_OVERWRITE", str(root)) from error
    return RunLedger(root, run_id, SKILL_NAME, policy=policy)


def _reject_links(path):
    cursor = path.expanduser().absolute()
    while cursor != cursor.parent:
        if RunLedger._is_reparse(cursor):
            raise ConversionBlocked("INPUT_LINK_UNSUPPORTED", str(cursor))
        cursor = cursor.parent


def scan_source(source, *, include_assemblies=False, extract_root=None):
    source = Path(source).expanduser().absolute()
    _reject_links(source)
    source = source.resolve()
    allowed = {".sldprt", ".sldasm"} if include_assemblies else {".sldprt"}
    if source.is_dir():
        inventory = inventory_project(source, extract_root=extract_root)
        records = inventory["files"]
    elif source.is_file() and source.suffix.casefold() in allowed and not source.name.startswith("~$"):
        records = [{"path": str(source), "sha256": sha256_file(source), "provenance": [str(source)]}]
        inventory = {"root": str(source.parent), "files": records, "zip_archives": []}
    else:
        raise ConversionBlocked("INPUT_UNSUPPORTED", "Use a project directory or .SLDPRT; assemblies require --include-assemblies. ZIP input must be inside the project directory.")
    selected, skipped, duplicates, seen = [], [], [], set()
    # Prefer ordinary source files over identical archive copies.
    for rec in sorted(records, key=lambda x: (bool(x.get("archive")), x["path"].casefold())):
        suffix = Path(rec["path"]).suffix.casefold()
        if suffix not in allowed:
            continue
        actual = rec.get("extracted_path") or (None if rec.get("archive") else rec["path"])
        # Assembly bytes can refer to different relative dependency trees.
        key = rec["sha256"] if suffix == ".sldprt" else rec["path"].casefold()
        if key in seen:
            duplicates.append({**rec, "duplicate_of": next((item["path"] for item in selected if (item["sha256"] if suffix == ".sldprt" else item["path"].casefold()) == key), None), "reason": "DEDUPLICATED_SOURCE"})
            continue
        if actual is None:
            skipped.append({"source": rec["path"], "sha256": rec["sha256"], "reason": "ARCHIVE_MODEL_NOT_EXTRACTED", "action": "Run in a new output directory with --extract-zips."})
            continue
        seen.add(key)
        selected.append({**rec, "source": actual})
    skipped = [rec for rec in skipped if rec["sha256"] not in seen]
    return {"schema_version": "1.0", "skill": SKILL_NAME, "source": str(source), "inventory": inventory,
            "models": selected, "not_converted": skipped, "duplicates": duplicates,
            "configuration_inventory": {"status": "not_inspected", "reason": "Requires native COM readback"}}


def discover_models(source, *, include_assemblies=False):
    return [Path(row["source"]) for row in scan_source(source, include_assemblies=include_assemblies)["models"]]


def write_inventory(source, output, *, include_assemblies=False):
    payload = scan_source(source, include_assemblies=include_assemblies)
    output = Path(output).expanduser().absolute()
    _reject_links(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return payload


def doctor():
    checks = {"python": sys.version.split()[0], "platform": sys.platform}
    if sys.version_info < (3, 10) or os.name != "nt":
        return {"status": "blocked", "error_code": "WINDOWS_PYTHON_310_REQUIRED", "checks": checks}
    try:
        from scripts.sw_preflight import run_preflight
        checks["preflight"] = asdict(run_preflight(allow_install=False, check_solidworks=True))
        missing = [module for module in ("jsonschema", "fitz") if importlib.util.find_spec(module) is None]
        checks["missing_drawing_dependencies"] = missing
        ready = checks["preflight"]["dependencies_ready"] and checks["preflight"]["solidworks_ready"] and not missing
        return {"status": "pass" if ready else "blocked", "checks": checks,
                "scope": "installation_and_dependencies_only_no_COM_session_started"}
    except Exception as error:
        return {"status": "blocked", "checks": checks, "error_code": type(error).__name__, "error": str(error)}


def _load_runtime():
    # Avoid an interactive package-install prompt in automation.
    from scripts.sw_preflight import run_preflight
    run_preflight(allow_install=False, check_solidworks=True)
    from scripts import sw_drawing as workflow, sw_drawing_review as review
    from scripts.sw_connect import get_com_member
    from scripts.sw_document_data import activate_configuration
    from scripts.sw_process import exclusive_cad_job
    from scripts.sw_session import SolidWorksSession
    from scripts.sw_export import export_drawing_to_dxf_detailed
    from scripts.dxf_validation import validate_dxf_file
    from scripts.drawing_geometry_annotations import repair_generated_annotations
    from scripts.drawing_rendered_repair import repair_rendered_dimension_collisions
    return {"workflow": workflow, "review_drawing_artifacts": review.review_drawing_artifacts,
            "delivery_verdict": review.delivery_verdict, "get_com_member": get_com_member,
            "activate_configuration": activate_configuration, "exclusive_cad_job": exclusive_cad_job,
            "SolidWorksSession": SolidWorksSession, "export_dxf": export_drawing_to_dxf_detailed,
            "validate_dxf": validate_dxf_file, "implementation_fingerprint": implementation_fingerprint(),
            "repair_annotations": lambda drawing, sw_app: repair_generated_annotations(drawing, sw_app, workflow._MODULE),
            "repair_rendered_collisions": repair_rendered_dimension_collisions}


def _configuration(model, get):
    return str(get(get(get(model, "ConfigurationManager"), "ActiveConfiguration"), "Name") or "")


def _model_box(model, get, document_type):
    values = get(model, "GetPartBox", True) if document_type == 1 else get(model, "GetBox", 0)
    values = list(values or [])
    if len(values) != 6 or not all(math.isfinite(float(v)) for v in values):
        raise ConversionBlocked("MODEL_BOUNDS_UNAVAILABLE", "Native bounding box could not be read")
    dimensions = [(float(values[i + 3]) - float(values[i])) * 1000 for i in range(3)]
    if not all(value > 0 for value in dimensions):
        raise ConversionBlocked("MODEL_BOUNDS_INVALID", str(dimensions))
    return dimensions


def _template(path, suffix, code):
    if path is None or Path(path).suffix.casefold() != suffix or not Path(path).is_file():
        raise ConversionBlocked(code, f"Supply an existing {suffix} template")
    return str(Path(path).expanduser().resolve())


def _prepare_spec(source, options):
    drawing_template = _template(options.drawing_template, ".drwdot", "DRAWING_TEMPLATE_REQUIRED")
    sheet_format = _template(options.sheet_format, ".slddrt", "SHEET_FORMAT_REQUIRED")
    if options.spec:
        spec = json.loads(Path(options.spec).read_text(encoding="utf-8-sig"))
    else:
        spec = {"schemaVersion": "1.0", "standard": options.standard, "projection": "first_angle",
                "paperSize": options.paper, "views": {"front": {}, "top": {}, "right": {}},
                "insertModelDimensions": True, "notes": [],
                "outputs": {"slddrw": True, "pdf": True, "dxf": True, "report": True}}
    kind = MODEL_TYPES[source.suffix.casefold()][1]
    if spec.get("documentType", kind) != kind:
        raise ConversionBlocked("DOCUMENT_TYPE_MISMATCH", "Spec documentType disagrees with source extension")
    spec.update(sourceModel=str(source), documentType=kind, templateCandidates=[sheet_format])
    # Temporary positive dimensions only validate the schema before COM; the
    # actual native bounds replace them before planning or creating any views.
    spec["modelSizeMm"] = [1.0, 1.0, 1.0]
    if kind == "assembly":
        bom = dict(spec.get("bom") or {})
        bom["templatePath"] = _template(options.bom_template or bom.get("templatePath"), ".sldbomtbt", "BOM_TEMPLATE_REQUIRED")
        bom["required"] = True
        spec["bom"] = bom
    validation = validate_drawing_spec(spec)
    if validation["status"] == "blocked":
        raise ConversionBlocked("DRAWING_SPEC_BLOCKED", json.dumps(validation["issues"], ensure_ascii=False))
    return spec, drawing_template


def _view_configuration_evidence(drawing, source, configuration, get):
    sheet = get(drawing, "GetCurrentSheet")
    views = list(get(sheet, "GetViews") or [])
    rows = []
    for view in views:
        referenced = str(get(view, "GetReferencedModelName") or "")
        # A sheet-format view is not a model view.
        if not referenced and int(get(view, "Type") or 0) == 1:
            continue
        config = str(get(view, "ReferencedConfiguration") or "")
        matches = bool(referenced) and Path(referenced).resolve() == source and config == configuration
        rows.append({"name": str(get(view, "Name") or ""), "model": referenced, "configuration": config, "matches": matches})
    if len(rows) < 3 or not all(row["matches"] for row in rows):
        raise ConversionBlocked("VIEW_CONFIGURATION_UNVERIFIED", json.dumps(rows, ensure_ascii=False))
    return rows


def _error_code(error):
    if getattr(error, "hresult", None) in {-2147023170, -2147023174, -2147417848}:
        return "SW_COM_CONNECTION_LOST"
    return str(getattr(error, "code", None) or type(error).__name__)


def _drawing_attempt(session, source, configuration, spec, template, root, stem, runtime):
    """Create and review a fresh drawing; never edit source dimensions for layout."""
    workflow, get = runtime["workflow"], runtime["get_com_member"]
    root.mkdir(parents=True, exist_ok=True)
    _json_write(root / "drawing_spec_ai.json", spec)
    drawing = session.new_drawing(template_path=template)
    try:
        drawing_path, pdf_path = root / f"{stem}.SLDDRW", root / f"{stem}.pdf"
        if not session.save(drawing, str(drawing_path)):
            raise ConversionBlocked("DRAWING_INITIAL_SAVE_FAILED", str(drawing_path))
        generated = workflow.generate_drawing_from_spec(drawing, spec, str(source), template_candidates=spec["templateCandidates"])
        _json_write(root / "generation_ai.json", generated)
        if generated.get("status") not in {"pass", "review_required"}:
            raise ConversionBlocked(generated.get("error_code") or "DRAWING_GENERATION_FAILED", json.dumps(generated, ensure_ascii=False))
        views = _view_configuration_evidence(drawing, source, configuration, get)
        annotation_repair = runtime["repair_annotations"](drawing, session.sw) if spec.get("insertModelDimensions", True) else {"status": "not_requested"}
        generated["annotation_repair"] = annotation_repair
        _json_write(root / "generation_ai.json", generated)
        save_ok = bool(session.save(drawing, str(drawing_path)))
        export_ok = save_ok and bool(workflow.export_sheet_to_pdf(drawing, str(pdf_path), sw_app=session.sw))
        rendered_repair = {"status": "not_requested"}
        repair_fn = runtime.get("repair_rendered_collisions")
        if export_ok and repair_fn is not None:
            try:
                rendered_repair = repair_fn(
                    drawing, session.sw, workflow._MODULE, pdf_path,
                    save_callback=lambda model: session.save(model, str(drawing_path)),
                    export_callback=lambda model, path: workflow.export_sheet_to_pdf(model, str(path), sw_app=session.sw),
                    title_block_box=(generated.get("layout") or {}).get("title_block_box"),
                    evidence_dir=root / "rendered_annotation_repair",
                )
            except Exception as error:
                rendered_repair = {"status": "blocked", "error_code": type(error).__name__, "error": str(error),
                                   "moves": [], "unresolved": [{"reason": "controller_exception", "error": str(error)}]}
                _json_write(root / "rendered_annotation_repair.json", rendered_repair)
                raise ConversionBlocked("DRAWING_RENDERED_REPAIR_UNVERIFIED", str(error)) from error
        _json_write(root / "rendered_annotation_repair.json", rendered_repair)
        structure = workflow.inspect_drawing_structure(drawing, paper_size_hint=spec["paperSize"], title_block_box=(generated.get("layout") or {}).get("title_block_box"))
        _json_write(root / "drawing_structure_ai.json", structure)
        dxf_path = root / f"{stem}.dxf"
        dxf = {"status": "not_requested"}
        if spec.get("outputs", {}).get("dxf", True):
            native_dxf = runtime["export_dxf"](drawing, str(dxf_path), sw_app=session.sw) if save_ok else {"success": False, "error": "native drawing save failed"}
            if native_dxf.get("preference_restore") is False or (native_dxf.get("view_blocks_preference") or {}).get("restored") is False:
                _json_write(root / "dxf_review_ai.json", {"status": "blocked", "native_export": native_dxf})
                raise ConversionBlocked("SW_EXPORT_PREFERENCE_RESTORE_FAILED", "DXF export preference was not restored")
            dxf = {"status": "blocked", "native_export": native_dxf}
            if native_dxf.get("success"):
                visible_count = sum(d.get("visibility") not in ("hidden", 3) and d.get("visible") is not False for d in structure.get("dimensions", []))
                dxf["validation"] = runtime["validate_dxf"](dxf_path, expected_visible_dimension_count=visible_count)
                dxf["status"] = dxf["validation"]["status"]
            _json_write(root / "dxf_review_ai.json", dxf)
        review = runtime["review_drawing_artifacts"](spec, structure=structure, pdf_path=pdf_path if export_ok else None)
        acceptance = evaluate_annotation_acceptance(spec, structure, annotation_repair, review)
        if acceptance["status"] == "blocked":
            review.setdefault("findings", []).extend({**issue, "severity": "fail"} for issue in acceptance["issues"])
            review["status"] = "blocked"
        verdict = runtime["delivery_verdict"](save_ok=save_ok, export_ok=export_ok, review=review, manufacturing={"status": "unknown"})
        status = "blocked" if dxf["status"] == "blocked" or annotation_repair["status"] == "blocked" or rendered_repair.get("status") == "blocked" else verdict["status"]
        return {"generation": generated, "view_configurations": views, "annotation_acceptance": acceptance,
                "status": status, "save": save_ok, "export": export_ok, "review": review, "delivery": verdict, "dxf": dxf,
                "rendered_annotation_repair": rendered_repair,
                "outputs": {"slddrw": str(drawing_path) if save_ok else None, "pdf": str(pdf_path) if export_ok else None,
                            "dxf": str(dxf_path) if dxf["status"] in {"pass", "review_required"} else None},
                "structure": structure, "spec": spec}
    finally:
        if not session.close(model=drawing):
            raise ConversionBlocked("SW_DOCUMENT_CLEANUP_FAILED", "The owned drawing could not be closed; stop before opening another document")


def _drawing_with_layout_retries(session, source, configuration, spec, template, root, stem, runtime, max_attempts):
    from scripts.drawing_quality import accept_candidate, dimension_signature, layout_candidate_specs, quality_metrics
    best = _drawing_attempt(session, source, configuration, spec, template, root, stem, runtime)
    history = [{"attempt": 1, "accepted": True, "outputs": best["outputs"],
                "metrics": quality_metrics(best["structure"], best["review"])}]
    if best["save"] and best["export"] and history[0]["metrics"]["needs_layout_retry"] and dimension_signature(best["structure"]):
        candidates = layout_candidate_specs(spec, (best["generation"].get("layout") or {}).get("scale"), max_attempts=max_attempts)
        for index, candidate_spec in enumerate(candidates, 2):
            attempt_root = root / f"layout_attempt_{index:02d}"
            try:
                candidate = _drawing_attempt(session, source, configuration, candidate_spec, template, attempt_root, stem, runtime)
                accepted = candidate["save"] and candidate["export"] and candidate.get("dxf", {}).get("status") != "blocked" and candidate.get("rendered_annotation_repair", {}).get("status") != "blocked" and accept_candidate(
                    best["structure"], candidate["structure"], best["review"], candidate["review"])
                history.append({"attempt": index, "accepted": accepted, "outputs": candidate["outputs"],
                                "metrics": quality_metrics(candidate["structure"], candidate["review"])})
                if accepted:
                    best = candidate
                if not quality_metrics(best["structure"], best["review"])["needs_layout_retry"]:
                    break
            except Exception as error:
                code = _error_code(error)
                history.append({"attempt": index, "accepted": False, "error_code": code, "error": str(error)})
                if code in {"SW_COM_CONNECTION_LOST", "SW_HOST_BUSY", "SW_DOCUMENT_CLEANUP_FAILED", "SW_EXPORT_PREFERENCE_RESTORE_FAILED"}:
                    raise
                if code in {"DRAWING_SCALE_DOES_NOT_FIT", "DRAWING_LAYOUT_BLOCKED", "DRAWING_GENERATION_FAILED"}:
                    continue
                break
    best["layout_attempts"] = history
    best["selected_layout_attempt"] = next(item["attempt"] for item in reversed(history) if item.get("accepted"))
    _json_write(root / "drawing_structure_ai.json", best.pop("structure"))
    _json_write(root / "drawing_spec_ai.json", best.pop("spec"))
    _json_write(root / "generation_ai.json", best["generation"])
    if "dxf" in best:
        _json_write(root / "dxf_review_ai.json", best["dxf"])
    return best


def convert_one(source, options, *, runtime=None):
    source = Path(source).expanduser().resolve()
    root = Path(options.output).expanduser().absolute()
    # No automatic resume: stale artifacts, changed policy and interrupted runs
    # must never be mistaken for fresh validation.
    ledger = _new_ledger(root, "drawing", policy={"source": str(source)})
    root = ledger.root
    before = sha256_file(source)
    stem = source.stem[:80] + "_ai"
    report_path = root / f"{stem}.report.json"
    result = {"source": str(source), "source_sha256": before, "status": "blocked", "outputs": {},
              "manual_review_required": True, "report": str(report_path)}
    dependencies = [source]
    try:
        spec, template = _prepare_spec(source, options)
        dependencies += [Path(template), Path(options.sheet_format)]
        if options.spec:
            dependencies.append(Path(options.spec))
        if spec.get("bom"):
            dependencies.append(Path(spec["bom"]["templatePath"]))
        runtime = runtime or _load_runtime()
        result["implementation_fingerprint"] = runtime.get("implementation_fingerprint")
        workflow, get = runtime["workflow"], runtime["get_com_member"]
        capability = workflow.validate_generic_drawing_generation(spec)
        if capability.get("status") != "pass":
            raise ConversionBlocked("DRAWING_SPEC_CAPABILITY_UNSUPPORTED", json.dumps(capability, ensure_ascii=False))
        # A batch keeps one COM/SolidWorks connection alive.  Reconnecting for
        # every part can race SolidWorks' ROT registration while the previous
        # process is still shutting down (SW_INSTANCE_NOT_READY).  The
        # project runner supplies ``_session``; standalone calls retain the
        # original owned-session behavior.
        shared_session = runtime.get("_session")
        job_context = nullcontext() if shared_session is not None else runtime["exclusive_cad_job"]()
        session_context = (_shared_session_context(shared_session)
                           if shared_session is not None
                           else runtime["SolidWorksSession"](visible=True))
        with job_context:
            with session_context as session:
                model = session.open(str(source), read_only=True, silent=True, raise_on_error=True)
                doc_type = int(get(model, "GetType"))
                path = str(get(model, "GetPathName") or "")
                if not path or Path(path).resolve() != source or doc_type != MODEL_TYPES[source.suffix.casefold()][0]:
                    raise ConversionBlocked("SOURCE_CONTEXT_MISMATCH", path)
                original_config = _configuration(model, get)
                if not original_config:
                    raise ConversionBlocked("SOURCE_CONFIGURATION_UNKNOWN", "Native configuration unavailable")
                source_save_flag = get(model, "GetSaveFlag")
                if source_save_flag is None:
                    raise ConversionBlocked("SOURCE_SAVE_STATE_UNKNOWN", "Native dirty-state readback unavailable")
                if bool(source_save_flag):
                    raise ConversionBlocked("SOURCE_UNSAVED_CHANGES", "Save or revert the existing model before conversion")
                result["source_context"] = {"path": path, "document_type": doc_type, "original_configuration": original_config}
                try:
                    requested = options.configuration or original_config
                    if requested != original_config:
                        activated = runtime["activate_configuration"](model, requested, rebuild=True, save=False)
                        if not activated.get("success"):
                            raise ConversionBlocked("SOURCE_CONFIGURATION_FAILED", str(activated))
                    configuration = _configuration(model, get)
                    if configuration.casefold() != requested.casefold():
                        raise ConversionBlocked("SOURCE_CONFIGURATION_MISMATCH", configuration)
                    result["configuration"] = configuration
                    spec["modelSizeMm"] = _model_box(model, get, doc_type)
                    if spec.get("bom"):
                        spec["bom"]["configuration"] = configuration
                    validation = validate_drawing_spec(spec)
                    if validation["status"] == "blocked":
                        raise ConversionBlocked("DRAWING_SPEC_BLOCKED", str(validation))
                    result.update(_drawing_with_layout_retries(session, source, configuration, spec, template,
                                  root, stem, runtime, getattr(options, "layout_attempts", 3)))
                finally:
                    result["source_configuration_restored"] = False
                    active = _configuration(model, get)
                    restored = active == original_config
                    if not restored:
                        restore = runtime["activate_configuration"](model, original_config, rebuild=True, save=False)
                        restored = bool(restore.get("success")) and _configuration(model, get) == original_config
                    result["source_configuration_restored"] = restored
                    if not restored:
                        raise ConversionBlocked("SOURCE_CONFIGURATION_RESTORE_FAILED", original_config)
    except Exception as error:
        result.update(status="blocked" if isinstance(error, ConversionBlocked) else "failed", error_code=_error_code(error), error=str(error))
    try:
        result["source_sha256_after"] = sha256_file(source)
        result["source_unchanged"] = result["source_sha256_after"] == before
    except OSError:
        result["source_unchanged"] = False
    if not result["source_unchanged"]:
        result.update(status="blocked", error_code="SOURCE_CHANGED_DURING_CONVERSION")
    _json_write(report_path, result)
    # Preserve every created file as evidence, including a failed native export.
    # A partial file is never listed as a successful output above.
    for artifact in root.rglob("*"):
        if artifact.is_file() and artifact not in {ledger.path, ledger.marker_path}:
            ledger.register_artifact(artifact, role="evidence", dependencies=dependencies, created_by_run=True)
    ledger.save()
    return result


def convert_project(options, *, runtime=None):
    source = options.source.expanduser().absolute()
    root = options.output or ((source if source.is_dir() else source.parent) / "agent" / SKILL_NAME / _run_id())
    root = Path(root).expanduser().absolute()
    ledger = _new_ledger(root, "project")
    root = ledger.root
    report_path = root / "batch_report_ai.json"
    payload = {"status": "blocked", "output_root": str(root), "report": str(report_path), "results": [], "not_converted": []}
    try:
        scan = scan_source(source, include_assemblies=options.include_assemblies,
                           extract_root=root / "sources_ai" if options.extract_zips else None)
        payload["not_converted"] = scan["not_converted"]
        _json_write(root / "inventory_ai.json", scan)
        models = scan["models"]
        if not models:
            raise ConversionBlocked("NO_SUPPORTED_MODELS", "No available native models; check archive-only inputs and --include-assemblies")
        # Validate templates/spec before opening a CAD session.
        for row in models:
            _prepare_spec(Path(row["source"]), options)
        runtime = runtime or _load_runtime()
        previous_error, error_count = None, 0
        with runtime["exclusive_cad_job"]():
            with runtime["SolidWorksSession"](visible=True) as session:
                batch_runtime = dict(runtime)
                batch_runtime["_session"] = session
                for index, row in enumerate(models, 1):
                    item_options = argparse.Namespace(**vars(options))
                    item_options.output = root / f"part{index:04d}_{row['sha256'][:12]}"
                    result = convert_one(Path(row["source"]), item_options, runtime=batch_runtime)
                    payload["results"].append(result)
                    code = result.get("error_code")
                    error_count = error_count + 1 if code and code == previous_error else int(bool(code))
                    previous_error = code
                    _json_write(report_path, payload)
                    if code in {"SW_COM_CONNECTION_LOST", "SW_HOST_BUSY", "SW_DOCUMENT_CLEANUP_FAILED", "SW_EXPORT_PREFERENCE_RESTORE_FAILED"} or error_count >= 2:
                        payload["not_converted"].extend({"source": r["source"], "reason": "BATCH_STOPPED_AFTER_ERROR"} for r in models[index:])
                        break
        statuses = {r["status"] for r in payload["results"]}
        payload["status"] = "blocked" if statuses & {"blocked", "failed"} else "review_required" if "review_required" in statuses or payload["not_converted"] else "pass"
    except Exception as error:
        payload.update(status="blocked", error_code=_error_code(error), error=str(error))
    _json_write(report_path, payload)
    for artifact in root.rglob("*"):
        if artifact.is_file() and artifact not in {ledger.path, ledger.marker_path}:
            ledger.register_artifact(artifact, role="evidence", created_by_run=True)
    ledger.save()
    return payload


def _parser():
    parser = argparse.ArgumentParser(description="SolidWorks 3D-to-2D drawing drafts with native review gates")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check installed dependencies and SolidWorks registration; do not launch CAD")
    inv = sub.add_parser("inventory", help="Read-only native/ZIP inventory")
    inv.add_argument("--source", type=Path, required=True)
    inv.add_argument("--output", type=Path, required=True)
    inv.add_argument("--include-assemblies", action="store_true")
    conv = sub.add_parser("convert", help="Create a fresh drawing run; existing output directories are refused")
    conv.add_argument("--source", type=Path, required=True)
    conv.add_argument("--drawing-template", type=Path, required=True)
    conv.add_argument("--sheet-format", type=Path, required=True)
    conv.add_argument("--bom-template", type=Path)
    conv.add_argument("--paper", choices=["A4", "A3", "A2", "A1", "A0"], default="A3")
    conv.add_argument("--standard", choices=["GB_T", "ISO"], default="GB_T")
    conv.add_argument("--configuration")
    conv.add_argument("--layout-attempts", type=int, choices=[1, 2, 3], default=3,
                      help="Bounded redraws for confirmed crowding; preserve dimension identity/value/visibility and retain the better verified candidate")
    conv.add_argument("--output", type=Path)
    conv.add_argument("--spec", type=Path)
    conv.add_argument("--include-assemblies", action="store_true")
    conv.add_argument("--extract-zips", action="store_true", help="Extract validated project archives under this new run, retaining relative references")
    step = sub.add_parser("export-step", help="Export native parts and assemblies to reviewed STEP files for FreeCAD")
    step.add_argument("--source", type=Path, required=True)
    step.add_argument("--output", type=Path)
    step.add_argument("--configuration")
    step.add_argument("--extract-zips", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            if args.command == "doctor":
                payload = doctor()
            elif args.command == "inventory":
                payload = write_inventory(args.source, args.output, include_assemblies=args.include_assemblies)
            elif args.command == "export-step":
                from scripts.step_conversion import export_step_project
                payload = export_step_project(args)
            else:
                payload = convert_project(args)
    except Exception as error:
        payload = {"status": "blocked", "error_code": _error_code(error), "error": str(error)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if payload.get("status") in {"blocked", "failed"} else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
