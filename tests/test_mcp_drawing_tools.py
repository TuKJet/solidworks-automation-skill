"""工程图 MCP 工具注册和输入契约测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "mcp-server"
if str(MCP_SERVER) not in sys.path:
    sys.path.insert(0, str(MCP_SERVER))

import server  # noqa: E402
from scripts.drawing_spec import validate_drawing_spec  # noqa: E402
from scripts.validate_mcp import REQUIRED_TOOLS  # noqa: E402


def test_drawing_mcp_tools_are_registered():
    tools = server.mcp._tool_manager._tools

    assert {"solidworks_generate_drawing", "solidworks_review_drawing", "solidworks_inspect_drawing"} <= set(tools)


def test_release_mcp_gate_requires_drawing_tools():
    """@brief 发布验证清单必须覆盖三个工程图 MCP 工具。"""
    assert {"solidworks_generate_drawing", "solidworks_review_drawing", "solidworks_inspect_drawing"} <= REQUIRED_TOOLS


def test_generate_drawing_input_rejects_non_drawing_spec(tmp_path: Path):
    source = tmp_path / "spec.txt"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="DrawingSpec"):
        server.SolidWorksGenerateDrawingInput(spec_path=str(source), output_dir=str(tmp_path / "out"))


def test_drawing_spec_validation_can_be_used_without_loading_com(tmp_path: Path):
    source = tmp_path / "drawing.json"
    source.write_text(json.dumps({"schemaVersion": "1.0"}), encoding="utf-8")

    result = validate_drawing_spec(str(source))

    assert result["status"] == "blocked"


@pytest.mark.parametrize("pdf_ok", [False, True])
def test_inspect_drawing_requires_fresh_pdf(monkeypatch, tmp_path: Path, pdf_ok):
    (tmp_path / "model.slddrw").touch()
    monkeypatch.setattr(server, "_run_locked", lambda op, _format: op())
    monkeypatch.setattr(server, "connect_solidworks", lambda **kw: (object(), None), raising=False)
    monkeypatch.setattr(server, "open_document", lambda *a, **kw: object(), raising=False)
    monkeypatch.setattr(server, "drawing_inspect_structure", lambda *a, **kw: {"status": "pass"}, raising=False)
    monkeypatch.setattr(server, "drawing_export_sheet_to_pdf", lambda *a, **kw: pdf_ok, raising=False)
    def previews(*args, **kwargs):
        assert pdf_ok, "PDF failure must not invoke desktop viewport fallback"
        return []
    monkeypatch.setattr(server, "drawing_save_review_previews", previews, raising=False)
    result = server.solidworks_inspect_drawing(server.SolidWorksInspectDrawingInput(
        drawing_path=str(tmp_path / "model.slddrw"), output_dir=str(tmp_path), basename="inspect"))
    assert result["status"] == ("pass" if pdf_ok else "blocked")
    assert result["pdf_export_succeeded"] is pdf_ok
    assert bool(result["outputs"]["pdf"]["path"]) is pdf_ok


def test_failed_overwrite_does_not_publish_stale_pdf(monkeypatch, tmp_path: Path):
    from scripts.sw_drawing_review import delivery_verdict
    monkeypatch.setattr(server, 'drawing_delivery_verdict', delivery_verdict, raising=False)
    monkeypatch.setattr(server, '_bind_document_context', lambda model: None)
    source = tmp_path / "source.sldprt"
    source.touch()
    (tmp_path / "block.pdf").write_bytes(b"old pdf")
    spec = {"sourceModel": str(source)}
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(server, "_run_locked", lambda op, _format: op())
    for name, fn in {
        "drawing_validate_spec": lambda *a: {"spec": spec},
        "connect_solidworks": lambda **kw: (object(), None),
        "open_document": lambda *a, **kw: object(),
        "new_document": lambda *a: object(),
        "drawing_generate_from_spec": lambda *a, **kw: {"status": "pass", "structure": {}},
        "drawing_inspect_structure": lambda *a, **kw: {"status": "pass", "inspection_phase": "after_save_export"},
        "save_document": lambda *a: True,
        "drawing_export_sheet_to_pdf": lambda *a, **kw: False,
        "drawing_save_review_previews": lambda *a, **kw: [],
        "drawing_review_artifacts": lambda *a, **kw: {"status": "blocked"},
    }.items():
        monkeypatch.setattr(server, name, fn, raising=False)
    result = server.solidworks_generate_drawing(server.SolidWorksGenerateDrawingInput(
        spec_path=str(tmp_path / "spec.json"), output_dir=str(tmp_path),
        drawing_filename="block.slddrw", overwrite=True))
    assert result["outputs"]["pdf"] == {
        "path": None, "exists": False, "size_bytes": 0, "export_succeeded": False}
    assert (tmp_path / "block.pdf").read_bytes() == b"old pdf"
    assert result['status'] == 'blocked'
    assert result['delivery']['export'] == 'fail'
    assert result['delivery']['release_ready'] is False
    assert result['final_structure']['inspection_phase'] == 'after_save_export'
