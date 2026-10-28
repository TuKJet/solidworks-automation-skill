"""Cross-process safety checks for the SolidWorks host job mutex."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _run_lock_client(code: str, *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
    )


def test_exclusive_cad_job_rejects_other_process_then_releases():
    """Use actual child processes, not mocks, for the named-mutex contract."""
    holder_code = """
from scripts.sw_process import exclusive_cad_job
import time
with exclusive_cad_job():
    print('acquired', flush=True)
    time.sleep(0.5)
"""
    probe_code = """
from scripts.sw_process import SolidWorksHostBusyError, exclusive_cad_job
try:
    with exclusive_cad_job():
        print('acquired')
except SolidWorksHostBusyError as exc:
    print('busy:' + exc.code)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "acquired"

        busy = _run_lock_client(probe_code)
        assert busy.returncode == 0, busy.stderr
        assert busy.stdout.strip() == "busy:SW_HOST_BUSY"

        assert holder.wait(timeout=5) == 0
        released = _run_lock_client(probe_code)
        assert released.returncode == 0, released.stderr
        assert released.stdout.strip() == "acquired"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_exclusive_cad_job_is_reentrant_on_one_thread():
    from scripts.sw_process import exclusive_cad_job

    with exclusive_cad_job():
        with exclusive_cad_job():
            pass


def test_run_locked_uses_host_guard_only_for_com_operations(monkeypatch):
    """Headless load_automation=False calls must not claim the CAD mutex."""
    import os

    if os.name != "nt":
        return
    server_dir = ROOT / "mcp-server"
    if str(server_dir) not in sys.path:
        sys.path.insert(0, str(server_dir))
    import server

    entries = []

    @contextmanager
    def guard():
        entries.append("enter")
        yield
        entries.append("exit")

    monkeypatch.setattr(server, "exclusive_cad_job", guard)
    monkeypatch.setattr(server, "_load_automation_modules", lambda: None)
    monkeypatch.setattr(server, "pythoncom", None)
    monkeypatch.setattr(server, "_artifact_access", lambda: type("Artifacts", (), {"publish": lambda self, payload: []})())

    com_result = json.loads(server._run_locked(lambda: {"status": "ok"}, server.ResponseFormat.JSON))
    headless_result = json.loads(server._run_locked(
        lambda: {"status": "ok"}, server.ResponseFormat.JSON, load_automation=False,
    ))

    assert com_result["status"] == headless_result["status"] == "ok"
    assert entries == ["enter", "exit"]


def test_run_locked_returns_structured_host_busy_without_running_operation(monkeypatch):
    import os

    if os.name != "nt":
        return
    server_dir = ROOT / "mcp-server"
    if str(server_dir) not in sys.path:
        sys.path.insert(0, str(server_dir))
    import server

    @contextmanager
    def busy_guard():
        raise server.SolidWorksHostBusyError("occupied")
        yield

    monkeypatch.setattr(server, "exclusive_cad_job", busy_guard)
    called = []
    result = json.loads(server._run_locked(
        lambda: called.append(True) or {"status": "ok"}, server.ResponseFormat.JSON,
    ))

    assert called == []
    assert result["status"] == "blocked"
    assert result["error_code"] == "SW_HOST_BUSY"
    assert result["retryable"] is True
