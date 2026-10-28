import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--input-root", required=True)
    args = parser.parse_args()
    root = Path(args.input_root)
    skill_scripts = Path(__file__).resolve().parent.parent / "subskills" / "solidworks-engineering-drawing" / "scripts"
    sys.path.insert(0, str(skill_scripts))
    import drawing_tables

    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    config = config.get('dense', config)
    if not config.get("configuration"):
        raise ValueError("configuration must be explicit in --config")
    drawings = json.loads((root / "drawing_results_drawings_ai.json").read_text(encoding="utf-8"))
    drawings = drawings.get("parts", drawings)
    inventory = json.loads((root / "native_dimension_inventory_ai.json").read_text(encoding="utf-8"))
    part = next(item for item in drawings if item["id"] == config["part"])
    native = next(item for item in inventory if item["id"] == config["part"])
    view = next(item for item in native["views"] if item["name"] == config["view"])
    structure_view = next(item for item in part["structure"]["views"] if item["name"] == config["view"])
    verified = json.loads((root / 'verified_inventory_ai.json').read_text(encoding='utf-8-sig'))
    source_record = next(item for item in verified['parts'] if item['id'] == config['part'])
    if config['configuration'] != source_record['configuration']['active_configuration']:
        raise ValueError('Requested configuration does not match native inventory evidence')
    contract = drawing_tables.contract_from_native_projection(
        part["dxf"], config["view"], view["outline"], structure_view["scale"],
        view["dimensions"], part["source"], config["configuration"],
        config.get('layout', {"paper": "A3", "orientation": "landscape"}),
    )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from apps.desktop.cad_workbench.artifact_ledger import RunLedger
    ledger = RunLedger(output, output.name, "solidworks_dense_regression", policy={"source": str(root), "part": config["part"], "view": config["view"]})
    (output / "native_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    result = drawing_tables.generate_coordinate_appendices(contract, output) if contract.get("status") == "ready" else contract
    if result.get('status') == 'pass':
        import pymupdf
        import ezdxf
        from ezdxf import bbox
        page_checks = []
        for page in result['manifest']['pages']:
            document = ezdxf.readfile(page['dxf'])
            outside = []
            layout = result['manifest']['layout']
            for entity in document.modelspace().query('TEXT'):
                extent = bbox.extents([entity])
                if extent.has_data and (extent.extmin.x < 0 or extent.extmin.y < 0 or extent.extmax.x > layout['width_mm'] or extent.extmax.y > layout['height_mm']):
                    outside.append(entity.dxf.text)
            with pymupdf.open(page['pdf']) as pdf:
                png = output / f"page_{page['page']}.png"
                pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).save(png)
                page_checks.append({'page': page['page'], 'rows': page['row_count'], 'pdf_pages': len(pdf), 'outside_text': outside, 'extractable_text_chars': len(pdf[0].get_text()), 'preview': str(png)})
        result['render_validation'] = page_checks
        result['visual_review_status'] = 'review_required'
        if any(item['outside_text'] or item['pdf_pages'] != 1 for item in page_checks):
            result['status'] = 'blocked'
    (output / "dense_regression.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if ledger is not None:
        for artifact in output.iterdir():
            if artifact.is_file() and artifact.name not in {ledger.marker_path.name, ledger.path.name}:
                ledger.register_artifact(artifact, role="evidence", dependencies=[Path(args.config), root / "drawing_results_drawings_ai.json", root / "native_dimension_inventory_ai.json", root / 'verified_inventory_ai.json', Path(part['dxf']), Path(part['source'])], created_by_run=True)
        ledger.save()
    print(json.dumps({"status": result.get("status"), "pages": len(result.get("manifest", {}).get("pages", [])), "safe_to_hide": result.get("coverage", {}).get("safe_to_hide_dimensions")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
