"""Regression contract for native DXF-to-coordinate-appendix extraction.

The production fixture paths are deliberately test-only: the library must not
contain part-number or customer-output heuristics.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "subskills" / "solidworks-engineering-drawing" / "scripts" / "drawing_tables.py"
SPEC = importlib.util.spec_from_file_location("drawing_tables_native_contract", MODULE_PATH)
drawing_tables = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(drawing_tables)


# This is input metadata for the real-run fixture only.  No source function may
# inspect these values or derive part-specific coordinates from them.
DENSE_PROJECTION_FIXTURE = {
    "id": "part16",
    "drawing_results_path": "C:/cad-output/2D_3D_ai_20261028/drawing_results_drawings_ai.json",
    "native_dimension_inventory_path": "C:/cad-output/2D_3D_ai_20261028/native_dimension_inventory_ai.json",
}


def _dimension(msp, *, first, second, text, angle):
    return msp.new_entity("DIMENSION", dxfattribs={
        "version": 0,
        "dimstyle": "Standard",
        "defpoint": (0.0, 0.0, 0.0),
        "defpoint2": (*first, 0.0),
        "defpoint3": (*second, 0.0),
        "text_midpoint": (*text, 0.0),
        "angle": angle,
    })


def _native_dxf(path: Path) -> None:
    document = ezdxf.new("R2018", setup=True)
    msp = document.modelspace()
    msp.add_line((0, 0), (30, 0))
    msp.add_circle((15, 10), 4)
    # JSON primitives cannot faithfully represent this source curve; its handle
    # must remain selected and copied by the native branch.
    msp.add_ellipse((20, 10), major_axis=(5, 0), ratio=0.5)
    _dimension(msp, first=(0, 0), second=(20, 0), text=(10, 5), angle=0)
    _dimension(msp, first=(0, 0), second=(0, 10), text=(5, 10), angle=90)
    document.saveas(path)


def _dimensions():
    return [
        {"name": "D-X", "type": 2, "position": [0.010, 0.005], "value_system": 0.040},
        {"name": "D-Y", "type": 2, "position": [0.005, 0.010], "value_system": 0.020},
        {"name": "RD-KEEP", "type": 6, "position": [0.015, 0.010], "value_system": 0.004},
    ]


def _contract(path: Path, dimensions=None):
    return drawing_tables.contract_from_native_projection(
        path, "Front", [-0.001, -0.001, 0.031, 0.021], [1, 2],
        _dimensions() if dimensions is None else dimensions,
        "C:/models/bracket.SLDPRT", "Default",
        {"paper": "A4", "orientation": "landscape"},
    )


def test_contract_requires_unique_verified_linear_dimension_and_keeps_nonlinear(tmp_path):
    source = tmp_path / "native.dxf"
    _native_dxf(source)

    contract = _contract(source)

    assert contract["status"] == "ready"
    assert contract["expected_dimension_ids"] == ["D-X", "D-Y"]
    assert contract["not_covered_ids"] == ["RD-KEEP"]
    assert {row["axis"] for row in contract["dimensions"]} == {"x", "y"}
    assert contract["dimensions"][0]["anchor_points"] == [[0.0, 0.0], [40.0, 0.0]]
    assert contract["geometry"]["native_source_dxf"] == str(source.resolve())
    assert contract["geometry"]["selected_handles"]
    assert contract["render_text_layout_complete"] is False


def test_unknown_native_dimension_match_blocks_without_partial_contract(tmp_path):
    source = tmp_path / "native.dxf"
    _native_dxf(source)
    dimensions = _dimensions()
    dimensions[1] = {**dimensions[1], "position": [0.099, 0.099]}

    result = _contract(source, dimensions)

    assert result["status"] == "blocked"
    assert result["manifest"]["mapped_ids"] == []
    assert result["manifest"]["unmapped_ids"] == ["D-X", "D-Y"]
    assert any(error["code"] == "DRAWING_NATIVE_PROJECTION_DIMENSION_UNMATCHED" for error in result["errors"])


def test_native_curve_is_copied_and_unsupported_ids_prevent_safe_hide(tmp_path, monkeypatch):
    source = tmp_path / "native.dxf"
    _native_dxf(source)
    contract = _contract(source)
    target = ezdxf.new("R2018", setup=True)

    drawing_tables._add_geometry(target.modelspace(), contract["geometry"])

    assert len(target.modelspace().query("ELLIPSE")) == 1
    monkeypatch.setattr(drawing_tables, "_render_table_dxf", lambda path, *_args: path.write_bytes(b"dxf"))
    monkeypatch.setattr(drawing_tables, "_render_pdf", lambda _dxf, path, *_args: path.write_bytes(b"pdf"))
    result = drawing_tables.generate_coordinate_appendices(contract, tmp_path / "result")
    assert result["status"] == "pass"
    assert result["coverage"]["safe_to_hide_dimensions"] is False
    assert result["manifest"]["not_covered_ids"] == ["RD-KEEP"]
