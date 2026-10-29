from types import SimpleNamespace

import pythoncom
import win32com.client.gencache

from scripts import sw_export


def test_first_typelib_load_registers_constants_on_client(monkeypatch, tmp_path):
    (tmp_path / "swconst.tlb").write_bytes(b"test typelib marker")
    constants = SimpleNamespace()
    monkeypatch.setattr(sw_export._win32com, "constants", constants)
    monkeypatch.setattr(sw_export, "_SW_CONSTANT_CACHE", {})
    monkeypatch.setattr(pythoncom, "LoadTypeLib", lambda _: SimpleNamespace(GetLibAttr=lambda: ("guid", 0, 0, 1, 0)))

    def ensure(*_args):
        constants.swViewEntityType_Edge = 1
        return SimpleNamespace()

    monkeypatch.setattr(win32com.client.gencache, "EnsureModule", ensure)
    app = SimpleNamespace(GetExecutablePath=lambda: str(tmp_path / "SLDWORKS.exe"))
    assert sw_export._resolve_sw_constant("swViewEntityType_Edge", app) == 1
    assert sw_export._SW_CONSTANT_CACHE["swViewEntityType_Edge"] == 1
