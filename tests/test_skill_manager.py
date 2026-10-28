from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "scripts" / "skill_manager.py"


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_text("---\nname: solidworks-3d-2d\n---\n", encoding="utf-8")
    (source / "scripts" / "solidworks_3d_2d.py").write_text("print('v1')\n", encoding="utf-8")
    git("init", "-b", "vm-sw-3d-2d", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    git("config", "user.name", "skill-test", cwd=source)
    git("add", ".", cwd=source)
    git("commit", "-m", "v1", cwd=source)
    return source, source


def run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(MANAGER), *args], text=True, capture_output=True,
                          encoding="utf-8", cwd=ROOT)


def test_install_and_check_local_source(tmp_path: Path):
    source, _ = make_repo(tmp_path)
    target = tmp_path / "installed"
    result = run(tmp_path, "install", "--source", str(source), "--target", str(target))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["branch"] == "vm-sw-3d-2d"
    checked = run(tmp_path, "check", "--target", str(target))
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "ok"
    duplicate = run(tmp_path, "install", "--source", str(source), "--target", str(target))
    assert duplicate.returncode == 2


def test_update_and_dirty_checkout_rejected(tmp_path: Path):
    source, _ = make_repo(tmp_path)
    target = tmp_path / "installed"
    assert run(tmp_path, "install", "--source", str(source), "--target", str(target)).returncode == 0
    (source / "scripts" / "solidworks_3d_2d.py").write_text("print('v2')\n", encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-m", "v2", cwd=source)
    updated = run(tmp_path, "update", "--source", str(source), "--target", str(target))
    assert updated.returncode == 0, updated.stderr
    assert "updated" in updated.stdout
    (target / "user.txt").write_text("keep", encoding="utf-8")
    dirty = run(tmp_path, "update", "--source", str(source), "--target", str(target))
    assert dirty.returncode == 2
    assert (target / "user.txt").read_text(encoding="utf-8") == "keep"


def test_tracked_dirty_and_check_reports_remote_version(tmp_path: Path):
    source, _ = make_repo(tmp_path)
    target = tmp_path / "installed"
    assert run(tmp_path, "install", "--source", str(source), "--target", str(target)).returncode == 0
    (source / "scripts" / "solidworks_3d_2d.py").write_text("print('v2')\n", encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-m", "v2", cwd=source)
    checked = run(tmp_path, "check", "--target", str(target))
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["update_available"] is True
    (target / "scripts" / "solidworks_3d_2d.py").write_text("user edit\n", encoding="utf-8")
    blocked = run(tmp_path, "update", "--source", str(source), "--target", str(target))
    assert blocked.returncode == 2
    assert (target / "scripts" / "solidworks_3d_2d.py").read_text(encoding="utf-8") == "user edit\n"


def test_wrong_branch_and_origin_rejected(tmp_path: Path):
    source, _ = make_repo(tmp_path)
    target = tmp_path / "installed"
    assert run(tmp_path, "install", "--source", str(source), "--target", str(target)).returncode == 0
    wrong = run(tmp_path, "check", "--target", str(target), "--source", str(tmp_path / "other"))
    assert wrong.returncode == 2
    bad_branch = run(tmp_path, "check", "--target", str(target), "--branch", "main")
    assert bad_branch.returncode != 0


def test_failed_validation_restores_commit(tmp_path: Path):
    source, _ = make_repo(tmp_path)
    target = tmp_path / "installed"
    assert run(tmp_path, "install", "--source", str(source), "--target", str(target)).returncode == 0
    before = git("rev-parse", "HEAD", cwd=target)
    (source / "SKILL.md").write_text("---\nname: wrong\n---\n", encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-m", "invalid", cwd=source)
    failed = run(tmp_path, "update", "--source", str(source), "--target", str(target))
    assert failed.returncode == 2
    assert git("rev-parse", "HEAD", cwd=target) == before
    assert (target / "SKILL.md").read_text(encoding="utf-8").find("solidworks-3d-2d") >= 0


def test_fetch_failure_does_not_reset_installed_commit(tmp_path: Path):
    source, _ = make_repo(tmp_path)
    target = tmp_path / "installed"
    assert run(tmp_path, "install", "--source", str(source), "--target", str(target)).returncode == 0
    before = git("rev-parse", "HEAD", cwd=target)
    missing = tmp_path / "missing-repository"
    failed = run(tmp_path, "update", "--source", str(missing), "--target", str(target))
    assert failed.returncode == 2
    assert git("rev-parse", "HEAD", cwd=target) == before
