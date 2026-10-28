"""Real-project SolidWorks drawing regression runner.

This entry point is intentionally separate from delivery generation.  It copies
the selected native drawings into a new run directory, performs a bounded
open/setup/note/save/export/reopen cycle, and records functional results and
drawing-review evidence independently.  It is not collected by normal pytest.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TEST_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(__file__).resolve().parent / "fixtures" / "project_2d3d_20260817.json"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("delivery_manifest", "source_manifest"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            errors.append(f"missing {key}")
    if config.get("template_candidates") is not None and not isinstance(config["template_candidates"], list):
        errors.append("template_candidates must be a list")
    note = config.get("note")
    if note is not None and (not isinstance(note, dict) or not isinstance(note.get("text"), str)):
        errors.append("note.text must be a string")
    return errors


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_hashes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for item in manifest.get("files", []):
        path = Path(str(item.get("path", "")))
        expected = str(item.get("sha256", "")).lower()
        actual = _sha256(path).lower() if path.is_file() else None
        records.append({"path": str(path), "expected_sha256": expected, "actual_sha256": actual, "unchanged": actual == expected})
    return records


def _require_file(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"missing or empty artifact: {path}")
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _register_evidence_tree(ledger, folder: Path, dependencies: list[Path]) -> None:
    for artifact in folder.rglob('*'):
        if artifact.is_file() and artifact != ledger.path and artifact != ledger.marker_path and not artifact.name.endswith('.run-ledger.json'):
            ledger.register_artifact(artifact, role='evidence', dependencies=dependencies, created_by_run=True)


def _paper_and_projection(row: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    paper = str(row.get("paper_size") or config.get("paper_size_fallback") or "A3").upper()
    projection = str(row.get("projection") or config.get("projection_fallback") or "first_angle").lower()
    return paper, projection


def _view_snapshot(structure: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "sheet": item.get("sheet"),
        "name": item.get("name"),
        "semantic_view": item.get("semantic_view"),
        "orientation": item.get("orientation"),
        "scale": item.get("scale"),
        "type": item.get("type"),
    } for item in structure.get("views") or []]


def _reference_snapshot(structure: dict[str, Any]) -> list[Any]:
    """Keep explicit reference evidence; an absent field is not treated as pass."""
    values = []
    for item in structure.get("views") or []:
        value = item.get("referenced_model") or item.get("model_path") or item.get("source_model")
        values.append({"view": item.get("name"), "reference": value})
    return values


def _com_snapshot(model, get_com_member) -> dict[str, Any]:
    """Capture native sheet/view scale, projection, references and flags."""
    sheet = get_com_member(model, "GetCurrentSheet")
    properties = get_com_member(sheet, "GetProperties2") or []
    views: list[dict[str, Any]] = []
    for view in get_com_member(sheet, 'GetViews') or []:
        reference = str(get_com_member(view, "GetReferencedModelName", default="") or "")
        if not reference:
            continue
        views.append({
            "name": str(get_com_member(view, "Name") or ""),
            "type": get_com_member(view, "Type"),
            "orientation": str(get_com_member(view, "GetOrientationName") or ""),
            "scale_ratio": get_com_member(view, "ScaleRatio"),
            "use_sheet_scale": get_com_member(view, "UseSheetScale"),
            "use_parent_scale": get_com_member(view, "UseParentScale"),
            "referenced_model": reference,
            "referenced_configuration": str(get_com_member(view, "ReferencedConfiguration") or ""),
        })
    return {
        "sheet_properties2": list(properties) if isinstance(properties, (list, tuple)) else str(properties),
        "sheet_scale": list(properties[2:4]) if properties and len(properties) >= 5 else None,
        "first_angle_projection": bool(properties[4]) if properties and len(properties) >= 5 else None,
        "views": views,
    }


def _normalise_note(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _com_notes(model, get_com_member) -> list[Any]:
    """Traverse sheet first view and all model views, de-duplicating COM objects."""
    sheet = get_com_member(model, "GetCurrentSheet")
    notes: list[Any] = []
    seen_views: set[str] = set()
    seen_notes: set[int] = set()
    views = [get_com_member(model, 'GetFirstView'), *(get_com_member(sheet, 'GetViews') or [])]
    for view in views:
        name = str(get_com_member(view, 'Name', default='')) if view is not None else ''
        if view is None or name in seen_views:
            continue
        seen_views.add(name)
        for note in get_com_member(view, "GetNotes") or []:
            if id(note) not in seen_notes:
                seen_notes.add(id(note))
                notes.append(note)
    return notes


def _drawing_snapshot(structure: dict[str, Any]) -> dict[str, Any]:
    return {
        "template_path": structure.get("template_path"),
        "paper_size": structure.get("paper_size"),
        "sheet_size": structure.get("sheet_size"),
        "views": _view_snapshot(structure),
        "view_count": structure.get("view_count"),
        "dimension_count": structure.get("dimension_count"),
        "stored_dimension_count": structure.get("stored_dimension_count"),
        "visibility_counts": {state: sum(d.get('visibility') == state for d in structure.get('dimensions', [])) for state in ('visible', 'hidden', 'unknown')},
        "references": _reference_snapshot(structure),
        "notes": [str(item.get("text") or "") for item in structure.get("notes") or []],
    }


def _functional_verification(before: dict[str, Any], after: dict[str, Any], before_com: dict[str, Any], after_com: dict[str, Any], note_text: str | None, note_update: dict[str, Any] | None, note_pdf: dict[str, Any] | None) -> dict[str, Any]:
    checks = {
        "template_unchanged": before.get("template_path") == after.get("template_path"),
        "paper_unchanged": before.get("sheet_size") == after.get("sheet_size") and before_com.get('sheet_properties2', [])[:1] == after_com.get('sheet_properties2', [])[:1],
        "view_flags_unchanged": before.get("views") == after.get("views"),
        "view_count_unchanged": before.get("view_count") == after.get("view_count"),
        "dimension_count_unchanged": before.get("dimension_count") == after.get("dimension_count"),
        "stored_dimension_count_unchanged": before.get("stored_dimension_count") == after.get("stored_dimension_count"),
        "visibility_counts_unchanged": before.get('visibility_counts') is not None and before.get('visibility_counts') == after.get('visibility_counts'),
        "references_available_and_unchanged": bool(before_com.get("views")) and all(item.get("referenced_model") for item in before_com.get("views", [])) and before_com.get("views") == after_com.get("views"),
        "native_scale_projection_flags_unchanged": all(before_com.get(key) is not None and before_com.get(key) == after_com.get(key) for key in ("sheet_scale", "first_angle_projection")),
        "note_updated": not note_text or bool(note_update and note_update.get("status") == "pass"),
        "note_pdf_verified": not note_text or bool(note_pdf and note_pdf.get("status") == "pass"),
    }
    return {"checks": checks, "pass": all(checks.values())}


def _run_one(session, row: dict[str, Any], config: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    from sw_connect import get_com_member
    from drawing_workflow import add_note, inspect_drawing_structure, setup_current_sheet, update_notes
    from sw_drawing import export_sheet_to_pdf
    from drawing_review import _note_report
    from sw_drawing_review import inspect_pdf_dimension_rendering
    from sw_review import review_drawing_layout
    from drawing_workflow import repair_native_drawing_layout

    part_id = str(row.get("id") or "unknown")
    source = Path(str(row.get("slddrw") or ""))
    if not source.is_file():
        raise FileNotFoundError(f"delivery drawing missing: {source}")
    target_dir = run_dir / part_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}_ai{source.suffix}"
    shutil.copy2(source, target)
    pdf = target.with_suffix(".pdf")
    before = {}
    after = {}
    model = session.open(str(target), silent=True, raise_on_error=True)
    title = str(get_com_member(model, "GetTitle") or "")
    try:
        before = inspect_drawing_structure(model)
        before_com = _com_snapshot(model, get_com_member)
        properties = before_com['sheet_properties2']
        paper = {6: 'A4', 7: 'A4', 8: 'A3'}.get(int(properties[0]), row.get('paper_size') or 'A3')
        projection = 'first_angle' if before_com['first_angle_projection'] else 'third_angle'
        current_template = str(get_com_member(get_com_member(model, 'GetCurrentSheet'), 'GetTemplateName') or '')
        setup = setup_current_sheet(
            model,
            config.get("template_candidates") or ([current_template] if current_template else []),
            paper_size=paper,
            projection=projection,
            require_gbt=bool(config.get("template_candidates")),
            preserve_format_notes=True,
            embedded_format_path=target_dir / (paper.lower() + ' - embedded_ai.slddrt'),
        )
        embedded_format = setup.get('embedded_format_fallback')
        note_result = None
        note = config.get("note") or {}
        if note.get("text"):
            position = note.get("position_m") or [0.02, 0.02]
            note_text = str(note["text"])
            original_note_text = 'AI regression note pending update'
            note_result = add_note(model, float(position[0]), float(position[1]), original_note_text)
            note_objects = []
            for candidate in _com_notes(model, get_com_member):
                actual = get_com_member(candidate, "GetText")
                if _normalise_note(actual) == _normalise_note(original_note_text):
                    note_objects.append(candidate)
            note_result["update"] = update_notes([(candidate, note_text) for candidate in note_objects])
        if not session.save(model, str(target)):
            raise RuntimeError("drawing save failed")
        layout_repair = repair_native_drawing_layout(model, str(pdf.with_name(pdf.stem + '_layout.pdf')), sw_app=session.sw, max_attempts=2) if config.get('exercise_layout_repair') else None
        if layout_repair and layout_repair.get('history') and not session.save(model, str(target)):
            raise RuntimeError('drawing save after layout repair failed')
        native_pdf = bool(export_sheet_to_pdf(model, str(pdf), sw_app=session.sw))
        pdf_info = _require_file(pdf) if native_pdf else {"path": str(pdf), "exists": pdf.is_file(), "size_bytes": pdf.stat().st_size if pdf.is_file() else 0}
        # Reopen the saved copy and retain the actual post-write structure.
        session.close(model=model)
        model = None
        model = session.open(str(target), silent=True, raise_on_error=True)
        after = inspect_drawing_structure(model)
        after_com = _com_snapshot(model, get_com_member)
        # Sheet-level free notes belong to the first (sheet) view, which is not
        # consistently returned by ISheet.GetViews in SW2025.
        after['notes'] = [{'text': str(get_com_member(n, 'GetText') or ''),
                           'position_m': list(get_com_member(get_com_member(n, 'GetAnnotation'), 'GetPosition') or [])}
                          for n in _com_notes(model, get_com_member)]
        pdf_render = inspect_pdf_dimension_rendering(pdf, after) if pdf.is_file() else {"status": "blocked", "error_code": "PDF_MISSING"}
        layout = review_drawing_layout(after)
        spec = {"schemaVersion": "1.0", "sourceModel": str(row.get("source") or ""), "documentType": "part", "standard": "GB_T", "projection": projection, "paperSize": paper, "views": {"front": {}, "top": {}, "right": {}}, "outputs": {"slddrw": True, "pdf": True}, "notes": [str(note.get("text"))] if note.get("text") else []}
        note_pdf = _note_report(spec, after, pdf if pdf.is_file() else None)
        before_snapshot = _drawing_snapshot(before)
        after_snapshot = _drawing_snapshot(after)
        if embedded_format:
            before_snapshot['template_path'] = after_snapshot['template_path']
        functional = _functional_verification(before_snapshot, after_snapshot, before_com, after_com, str(note.get("text")) if note.get("text") else None, note_result.get("update") if note_result else None, note_pdf)
        return {
            "id": part_id,
            "functional_status": "pass" if setup.get("status") == "pass" and native_pdf and functional["pass"] else "review_required",
            "drawing_review_status": layout.get("status", "blocked"),
            "manufacturing_status": "review_required",
            "source": str(source),
            "copy": _require_file(target),
            "pdf": pdf_info,
            "sheet_setup": setup,
            "embedded_format": embedded_format,
            "note": note_result,
            "before": before_snapshot,
            "after": after_snapshot,
            "functional_verification": functional,
            "pdf_dimension_rendering": pdf_render,
            "drawing_layout_review": layout,
            "layout_repair": layout_repair,
            "native_com_snapshot_before": before_com,
            "native_com_snapshot_after": after_com,
            "note_pdf_evidence": note_pdf,
            "review_required_by_provenance": str(row.get("provenance") or "").startswith("adapted_existing_human"),
        }
    finally:
        if model is not None:
            try:
                session.close(model=model)
            except Exception:
                pass


def run(config_path: Path, output_dir: Path, limit: int | None = None, version: int | None = None, part_id: str | None = None) -> dict[str, Any]:
    config = _load(config_path)
    errors = validate_config(config)
    if errors:
        raise ValueError("invalid config: " + ", ".join(errors))
    delivery_path = Path(config["delivery_manifest"])
    source_path = Path(config["source_manifest"])
    delivery = _load(delivery_path)
    source_manifest = _load(source_path)
    rows = list(delivery.get("rows") or [])
    if part_id:
        rows = [row for row in rows if row.get('id') == part_id]
        if not rows:
            raise ValueError(f'Unknown part id: {part_id}')
    rows = rows[:limit] if limit else rows
    run_dir = output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    before = _source_hashes(source_manifest)
    baseline_changes = [item for item in before if not item['unchanged']]
    if not before or any(item['actual_sha256'] is None for item in before) or (baseline_changes and config.get('source_baseline_policy') != 'record_current'):
        raise RuntimeError("source hash mismatch before regression; refusing to continue")
    # Imports are deliberately delayed so --help and --validate-config work on non-CAD hosts.
    sys.path.insert(0, str(TEST_ROOT))
    sys.path.insert(0, str(TEST_ROOT / "scripts"))
    sys.path.insert(0, str(TEST_ROOT / "subskills" / "solidworks-engineering-drawing" / "scripts"))
    from sw_connect import get_sw_version
    from sw_session import SolidWorksSession
    from apps.desktop.cad_workbench.artifact_ledger import RunLedger

    ledger = RunLedger(run_dir, run_dir.name, 'solidworks_project_regression', policy={'stage': 'drawings', 'retention': 'evidence'})

    session = SolidWorksSession(version=version, visible=True, wait_seconds=20)
    # Existing user documents are not our cleanup budget. Permit one drawing
    # and its references above the observed baseline without closing them.
    session.max_documents = max(session.max_documents, len(session._documents()) + 4)
    solidworks_version = get_sw_version(session.sw)
    results: list[dict[str, Any]] = []
    batch_path = run_dir / "batch_report.json"
    try:
        for row in rows:
            try:
                results.append(_run_one(session, row, config, run_dir))
            except Exception as exc:
                results.append({"id": row.get("id"), "functional_status": "failed", "drawing_review_status": "not_run", "error": str(exc), "traceback": traceback.format_exc()})
            _register_evidence_tree(ledger, run_dir / str(row['id']), [Path(row['slddrw']), Path(row['drawing_source'])])
            batch_path.write_text(json.dumps({"status": "running", "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        session.quit_owned_instance()
    after = _source_hashes(source_manifest)
    source_unchanged = len(before) == len(after) and bool(before) and all(
        a.get("actual_sha256") == b.get("actual_sha256")
        for a, b in zip(before, after)
    )
    report = {
        "schema_version": "1.0",
        "status": "pass" if source_unchanged and results and all(item.get("functional_status") == "pass" for item in results) else "review_required",
        "run_dir": str(run_dir),
        "solidworks": solidworks_version,
        "requested_count": len(rows),
        "results": results,
        "source_hash_before": before,
        "source_hash_after": after,
        "source_hash_unchanged": source_unchanged,
        "changes_before_this_run": baseline_changes,
        "drawing_review_is_separate_gate": True,
    }
    report_path = run_dir / "project_regression.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    batch_path.write_text(json.dumps({'status': report['status'], 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / "project_regression.done").write_text("complete\n", encoding="utf-8")
    _register_evidence_tree(ledger, run_dir, [delivery_path, source_path])
    report["report"] = _require_file(report_path)
    return report


def run_auxiliary(config_path: Path, output_dir: Path) -> dict[str, Any]:
    """Exercise project inventory, BOM, checkpoint and dependency gates together."""
    sys.path.insert(0, str(TEST_ROOT))
    from scripts.sw_project_inventory import inventory_project
    from scripts.sw_project_batch import run_batch
    from scripts.sw_session import SolidWorksSession
    from scripts.sw_delivery import collect_assembly_bom, export_bom_bundle, validate_packaged_references, relocate_copied_references
    from scripts.sw_connect import get_com_member
    from apps.desktop.cad_workbench.artifact_ledger import RunLedger
    import csv
    import zipfile
    import xml.etree.ElementTree as ET

    config = _load(config_path)
    sources = _load(Path(config['source_manifest']))
    delivery = _load(Path(config['delivery_manifest']))
    source_root = Path(config.get('project_root') or Path(delivery['destination']).parent)
    run_dir = output_dir / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_dir.mkdir(parents=True, exist_ok=False)
    ledger = RunLedger(run_dir, run_dir.name, 'solidworks_project_regression', policy={'stage': 'auxiliary', 'retention': 'evidence'})
    before = _source_hashes(sources)
    inventory = inventory_project(source_root, run_dir / 'inventory.json', run_dir / 'extracted')
    parts = {f['sha256'] for f in inventory['files'] if Path(f['basename']).suffix.lower() == '.sldprt'}
    items = [{'id': 'assembly_%02d' % i, 'source': f['path'], 'dependencies': []}
             for i, f in enumerate([f for f in sources['files'] if Path(f['path']).suffix.lower() == '.sldasm'], 1)]
    session = SolidWorksSession(visible=True, wait_seconds=1)
    session.max_documents = max(session.max_documents, len(session._documents()) + 8)
    calls = []

    def generate(item, context):
        calls.append(item['id'])
        model = session.open(item['source'], read_only=True, silent=True)
        was_owned = any(doc is model for doc in session._owned_documents)
        try:
            rows = collect_assembly_bom(model, hierarchy=True, include_excluded=True)
            flat = collect_assembly_bom(model)
            for row in rows:
                row['root_assembly'] = item['source']
            bundle = export_bom_bundle(rows, context['root'] / item['id'])
            paths = [str(context['root'] / item['id'] / ('bom.' + ext)) for ext in ('csv', 'xlsx', 'json', 'html')]
            return {'outputs': paths, 'row_count': len(rows), 'flat_row_count': len(flat),
                    'issues': {k: sum(row.get(k) is True for row in rows) for k in ('missing', 'temporary', 'suppressed', 'excluded')}}
        finally:
            if was_owned:
                session.close(model=model)

    def verify(item, context, generated):
        folder = context['root'] / item['id']
        rows = _load(folder / 'bom.json')
        with (folder / 'bom.csv').open(encoding='utf-8-sig', newline='') as stream:
            csv_rows = list(csv.DictReader(stream))
        with zipfile.ZipFile(folder / 'bom.xlsx') as z:
            assert z.testzip() is None
            sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            xml_rows = sheet.findall('.//{*}sheetData/{*}row')
            assert not sheet.findall('.//{*}f')
        passed = len(rows) == len(csv_rows) == len(xml_rows) - 1 == generated['row_count']
        evidence = folder / 'verification.json'
        evidence.write_text(json.dumps({'status': 'pass' if passed else 'blocked',
                                        'rows': len(rows), 'quantity_scope': 'hierarchy_instance',
                                        'flat_row_count': generated['flat_row_count'], 'issues': generated['issues']}, indent=2), encoding='utf-8')
        return {'status': 'pass' if passed else 'blocked', 'outputs': [*generated['outputs'], str(evidence)],
                'delivery_status': 'draft_requires_engineering_review'}

    first = run_batch(items, run_dir / 'bom', 'project-bom', {'schema': 1, 'quantity_scope': 'hierarchy_instance'},
                      generate=generate, verify=verify,
                      publish=lambda item, context, verified: {**verified, 'status': 'published'})
    calls_before_resume = len(calls)
    resumed = run_batch(items, run_dir / 'bom', 'project-bom', {'schema': 1, 'quantity_scope': 'hierarchy_instance'},
                        generate=generate, verify=verify,
                        publish=lambda item, context, verified: {**verified, 'status': 'published'}) if first['status'] == 'published' else {'status': 'not_run', 'reason': 'first_pass_failed'}
    dependency_audits = []
    for row in delivery['rows']:
        if first['status'] != 'published':
            break
        if Path(row['drawing_source']).resolve().is_relative_to(source_root.resolve()):
            continue
        drawing = None
        was_owned = False
        audit_record = {'id': row['id'], 'status': 'blocked'}
        try:
            drawing = session.open(row['slddrw'], read_only=True, silent=True)
            was_owned = any(doc is drawing for doc in session._owned_documents)
            refs = sorted({str(get_com_member(v, 'GetReferencedModelName') or '') for v in get_com_member(get_com_member(drawing, 'GetCurrentSheet'), 'GetViews') or []} - {''})
            audit_record['original_audit'] = validate_packaged_references(Path(delivery['destination']), refs, reopened_references=refs)
            if was_owned:
                session.close(model=drawing)
            drawing = None
            package = run_dir / 'packages' / row['id']
            package.mkdir(parents=True)
            copy_path = package / (Path(row['slddrw']).stem + '_package_ai.SLDDRW')
            shutil.copy2(row['slddrw'], copy_path)
            replacements = {}
            for original in refs:
                original_path = Path(original)
                dependency_copy = package / (original_path.stem + '_package_ai' + original_path.suffix)
                shutil.copy2(original_path, dependency_copy)
                replacements[original] = str(dependency_copy)
            audit_record['relocation'] = relocate_copied_references(session.sw, copy_path, replacements, package)
            relocation = audit_record['relocation']
            if relocation.get('error_code') != 'DRAWING_REFERENCE_RELOCATION_REOPEN_REQUIRED' or not all(r.get('replaced') for r in relocation.get('replacements', [])):
                raise RuntimeError('Reference migration did not reach reopen gate')
            drawing = session.open(copy_path, read_only=True, silent=True)
            was_owned = True
            actual = [v['referenced_model'] for v in _com_snapshot(drawing, get_com_member)['views']]
            audit_record['package_audit'] = validate_packaged_references(package, list(replacements.values()), reopened_references=actual)
            audit_record['status'] = audit_record['package_audit']['status']
        except Exception as exc:
            audit_record['error'] = str(exc)
        finally:
            if drawing is not None and was_owned:
                session.close(model=drawing)
            dependency_audits.append(audit_record)
            (run_dir / 'dependency_audits.json').write_text(json.dumps(dependency_audits, ensure_ascii=False, indent=2), encoding='utf-8')
    after = _source_hashes(sources)
    report = {'status': 'pass' if len(parts) == config.get('expected_distinct_parts', 24) and first['status'] == resumed['status'] == 'published' and len(calls) == calls_before_resume and all(a['status'] == 'pass' for a in dependency_audits) else 'review_required',
              'distinct_parts': len(parts), 'assemblies': len(items), 'inventory': str(run_dir / 'inventory.json'),
              'bom': first, 'resume': resumed, 'resume_generate_calls': len(calls) - calls_before_resume,
              'dependency_audits': dependency_audits,
              'source_hash_unchanged': [r['actual_sha256'] for r in before] == [r['actual_sha256'] for r in after],
              'run_dir': str(run_dir), 'delivery_status': 'draft_requires_engineering_review'}
    if not report['source_hash_unchanged']:
        report['status'] = 'blocked'
    (run_dir / 'auxiliary_regression.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / 'auxiliary_regression.done').write_text('complete', encoding='utf-8')
    for folder in (run_dir / 'packages', run_dir / 'extracted'):
        _register_evidence_tree(ledger, folder, [Path(config['source_manifest'])])
    for artifact in run_dir.iterdir():
        if artifact.is_file() and artifact not in (ledger.path, ledger.marker_path):
            ledger.register_artifact(artifact, role='evidence', dependencies=[Path(config['source_manifest'])], created_by_run=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real 2D/3D project SolidWorks drawing regression")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument('--part-id', help='Select one manifest part for a focused native rerun')
    parser.add_argument("--version", type=int)
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument('--stage', choices=['drawings', 'auxiliary', 'all'], default='drawings')
    args = parser.parse_args()
    config = _load(args.config)
    errors = validate_config(config)
    if args.validate_config:
        print(json.dumps({"valid": not errors, "errors": errors, "config": str(args.config)}, ensure_ascii=False, indent=2))
        return 0 if not errors else 2
    if errors:
        parser.error("invalid config: " + ", ".join(errors))
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(TEST_ROOT))
        from scripts.sw_process import exclusive_cad_job
        with exclusive_cad_job(), (args.output_dir / 'native_run.log').open('a', encoding='utf-8', buffering=1) as log, contextlib.redirect_stdout(log):
            if args.stage == 'all':
                drawing_result = run(args.config, args.output_dir / 'drawings', args.limit, args.version, args.part_id)
                auxiliary_result = run_auxiliary(args.config, args.output_dir / 'auxiliary')
                result = {'status': 'pass' if drawing_result['status'] == auxiliary_result['status'] == 'pass' else 'review_required', 'drawings': drawing_result['run_dir'], 'auxiliary': auxiliary_result['run_dir'], 'run_dir': str(args.output_dir)}
                (args.output_dir / 'all_stages.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            else:
                result = run_auxiliary(args.config, args.output_dir) if args.stage == 'auxiliary' else run(args.config, args.output_dir, args.limit, args.version, args.part_id)
    except Exception as exc:
        failure = {"status": "failed", "error": str(exc), "traceback": traceback.format_exc()}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / 'startup_failure.json').write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({key: result.get(key) for key in ('status', 'run_dir', 'requested_count', 'source_hash_unchanged')}, ensure_ascii=False))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
