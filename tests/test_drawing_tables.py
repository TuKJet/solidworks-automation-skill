"""Dense dimension coordinate-appendix contracts and vector artifact regressions."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "subskills" / "solidworks-engineering-drawing" / "scripts" / "drawing_tables.py"
SPEC = importlib.util.spec_from_file_location("drawing_tables_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
drawing_tables = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drawing_tables)


def _contract(**overrides):
    payload = {
        "layout": {"paper": "A4", "orientation": "portrait", "margins_mm": [10, 10, 10, 10], "row_height_mm": 8, "header_height_mm": 12},
        "geometry": {
            "unit": "mm", "scale": 1.0, "origin_mm": [20, 20],
            "entities": [
                {"kind": "LINE", "start": [0, 0], "end": [50, 0]},
                {"kind": "CIRCLE", "center": [25, 25], "radius": 10},
            ],
        },
        "dimensions": [
            {
                "canonical_id": "D-HOLE-X", "source": "bracket.sldprt", "view": "Front", "configuration": "Default", "datum": "A/B",
                "model_value": 25.0, "unit": "mm", "scale": 1.0, "anchor_points": [[0, 25], [25, 25]],
            },
            {
                "canonical_id": "D-ANGLE", "source": "bracket.sldprt", "view": "Front", "configuration": "Default", "datum": "A/B",
                "model_value": 1.5707963267948966, "unit": "rad", "scale": 1.0, "anchor_points": [[0, 0], [25, 25]],
            },
        ],
        "holes": [
            {"canonical_id": "H-A", "diameter": 6.0, "unit": "mm", "count": 1, "xy": [[25, 25]], "datum": "A/B", "source": "bracket.sldprt"},
            {"canonical_id": "H-B", "diameter": 8.0, "unit": "mm", "count": 1, "xy": [[25, 25]], "datum": "A/B", "source": "bracket.sldprt"},
        ],
    }
    payload.update(overrides)
    return payload


def test_coordinate_appendices_create_paginated_csv_true_vector_dxf_and_pdf(tmp_path: Path):
    ezdxf = pytest.importorskip("ezdxf", reason="vector appendix test requires declared ezdxf dependency")
    result = drawing_tables.generate_coordinate_appendices(_contract(), tmp_path)

    assert result["status"] == "pass"
    assert result["coverage"] == {
        # A generic table with an angle row is a delivery aid, not authority to
        # hide native drawing dimensions.  Only explicitly verified linear rows
        # with completed text-layout review may become hide-safe.
        "expected_count": 4, "mapped_count": 4, "coverage_passed": True, "safe_to_hide_dimensions": False,
    }
    assert result["mappedIDs"] == ["D-HOLE-X", "D-ANGLE", "H-A", "H-B"]
    manifest = result["manifest"]
    assert Path(manifest["coordinate_csv"]).is_file()
    assert Path(manifest["hole_csv"]).is_file()
    assert manifest["pages"]
    first_dxf = ezdxf.readfile(manifest["pages"][0]["dxf"])
    assert len(first_dxf.modelspace().query("LINE CIRCLE")) >= 2
    assert "SOURCE_GEOMETRY" in first_dxf.layers
    assert all(Path(page["pdf"]).stat().st_size > 0 for page in manifest["pages"])
    # Same XY must retain separate feature identities; it must not be coordinate-deduplicated.
    hole_csv = Path(manifest["hole_csv"]).read_text(encoding="utf-8-sig")
    assert "H-A" in hole_csv and "H-B" in hole_csv
    assert "90.0" in Path(manifest["coordinate_csv"]).read_text(encoding="utf-8-sig")


def test_missing_anchor_or_scale_blocks_all_coverage_without_artifacts(tmp_path: Path):
    contract = _contract()
    contract["dimensions"][1].pop("anchor_points")
    contract["geometry"]["scale"] = None

    result = drawing_tables.generate_coordinate_appendices(contract, tmp_path)

    assert result["status"] == "blocked"
    assert result["manifest"]["mapped_ids"] == []
    assert result["manifest"]["unmapped_ids"] == ["D-HOLE-X", "D-ANGLE", "H-A", "H-B"]
    assert result["manifest"]["coverage"]["safe_to_hide_dimensions"] is False
    assert not list(tmp_path.iterdir())


def test_row_height_drives_pagination_instead_of_coordinate_specific_magic_numbers(tmp_path: Path, monkeypatch):
    contract = _contract()
    contract["layout"] = {"paper": "A4", "orientation": "portrait", "margins_mm": [10, 10, 10, 10], "row_height_mm": 120, "header_height_mm": 12}
    contract["dimensions"] = [
        {**contract["dimensions"][0], "canonical_id": f"D-{index}"}
        for index in range(4)
    ]
    contract["holes"] = []
    monkeypatch.setattr(drawing_tables, "_render_table_dxf", lambda path, *_args: path.write_bytes(b"dxf"))
    monkeypatch.setattr(drawing_tables, "_render_pdf", lambda _dxf, path, *_args: path.write_bytes(b"pdf"))

    result = drawing_tables.generate_coordinate_appendices(contract, tmp_path)

    assert result["status"] == "pass"
    pages = result["manifest"]["pages"]
    assert result["manifest"]["layout"]["row_capacity"] == 1
    assert [page["row_count"] for page in pages] == [1, 1, 1, 1]


def test_hole_instances_are_not_silently_dropped_and_metres_convert(tmp_path, monkeypatch):
    contract = _contract(dimensions=[], holes=[{'canonical_id': 'group', 'diameter': .006, 'unit': 'm', 'count': 2, 'xy': [[.010, .020], [.030, .040]], 'datum': 'A', 'source': 'part.sldprt'}])
    rendered = []
    def draw(path, layout, geometry, rows, kind, page):
        rendered.extend(rows)
        path.write_bytes(b'dxf')
    monkeypatch.setattr(drawing_tables, '_render_table_dxf', draw)
    monkeypatch.setattr(drawing_tables, '_render_pdf', lambda _source, target, _layout: target.write_bytes(b'pdf'))
    result = drawing_tables.generate_coordinate_appendices(contract, tmp_path)
    assert result['status'] == 'pass'
    assert [row['xy'] for row in rendered] == [[[10., 20.]], [[30., 40.]]]
    assert [row['_row_id'] for row in rendered] == ['H1.1', 'H1.2']
    assert all(row['diameter'] == 6 for row in rendered)
