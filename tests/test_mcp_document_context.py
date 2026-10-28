from pathlib import Path
from types import SimpleNamespace

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
import server


def model(path, doc_type=1, config="Default"):
    active = SimpleNamespace(Name=config)
    manager = SimpleNamespace(ActiveConfiguration=active)
    return SimpleNamespace(
        GetPathName=lambda: str(path), GetType=lambda: doc_type,
        GetTitle=lambda: Path(path).name, ConfigurationManager=manager,
    )


def test_bound_context_rejects_active_path_change(tmp_path):
    source = tmp_path / "a.sldprt"
    other = tmp_path / "b.sldprt"
    server._document_context = {"path": str(source), "type": 1, "configuration": "Default"}
    server.get_com_member = lambda obj, name, default=None, *args: getattr(obj, name, default)() if callable(getattr(obj, name, None)) else getattr(obj, name, default)
    with pytest.raises(RuntimeError, match="活动文档已变化"):
        server._verify_document_context(SimpleNamespace(), model(other))


def test_bound_context_rejects_configuration_change(tmp_path):
    source = tmp_path / "a.sldprt"
    server._document_context = {"path": str(source), "type": 1, "configuration": "Default"}
    server.get_com_member = lambda obj, name, default=None, *args: getattr(obj, name, default)() if callable(getattr(obj, name, None)) else getattr(obj, name, default)
    with pytest.raises(RuntimeError, match="配置已变化"):
        server._verify_document_context(SimpleNamespace(), model(source, config="Alt"))


def test_new_document_clears_old_binding(monkeypatch, tmp_path):
    created = model(tmp_path / "new.sldprt")
    app = SimpleNamespace()
    monkeypatch.setattr(server, "connect_solidworks", lambda **_: (app, None), raising=False)
    monkeypatch.setattr(server, "new_document", lambda *args, **kwargs: created, raising=False)
    monkeypatch.setattr(server, "get_com_member", lambda obj, name, default=None, *args: getattr(obj, name, default)() if callable(getattr(obj, name, None)) else getattr(obj, name, default), raising=False)
    monkeypatch.setattr(server, "_run_locked", lambda operation, *args, **kwargs: operation())
    server._document_context = {"path": "old.sldprt", "type": 1, "configuration": "Default"}
    result = server.solidworks_new_document(SimpleNamespace(doc_type=SimpleNamespace(value="part"), template_path=None, response_format="json"))
    assert result["status"] == "ok"
    assert server._document_context is None


def test_close_rejects_duplicate_title(monkeypatch, tmp_path):
    source = tmp_path / "same.sldprt"
    other = tmp_path / "same2.sldprt"
    first, second = model(source), model(other)
    first.GetTitle = lambda: "same.sldprt"
    second.GetTitle = lambda: "same.sldprt"
    app = SimpleNamespace(ActiveDoc=first, GetDocuments=lambda: [first, second])
    monkeypatch.setattr(server, "connect_solidworks", lambda **_: (app, first), raising=False)
    monkeypatch.setattr(server, "get_com_member", lambda obj, name, default=None, *args: getattr(obj, name, default)() if callable(getattr(obj, name, None)) else getattr(obj, name, default), raising=False)
    monkeypatch.setattr(server, "_run_locked", lambda operation, *args, **kwargs: operation())
    server._document_context = {"path": str(source), "type": 1, "configuration": "Default"}
    params = SimpleNamespace(close_all=False, save_changes=False, response_format="json")
    with pytest.raises(RuntimeError, match="标题不唯一"):
        server.solidworks_close_documents(params)
