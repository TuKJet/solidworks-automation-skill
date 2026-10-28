"""Install and safely update the standalone SolidWorks 3D-to-2D skill.

The manager intentionally uses only Python's standard library and an existing
Git executable.  It never runs repository hooks, resets a dirty checkout, or
replaces an existing installation during the initial install.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


SKILL_NAME = "solidworks-3d-2d"
DEFAULT_REPO = "https://github.com/TuKJet/solidworks-automation-skill.git"
DEFAULT_BRANCH = "vm-sw-3d-2d"
REQUIRED = ("SKILL.md", "scripts/solidworks_3d_2d.py")
META_NAME = ".solidworks-3d-2d-install.json"
IGNORED_STATUS_PREFIXES = ("?? .venv/", "?? .venv\\", "?? __pycache__/", "?? __pycache__\\", f"?? {META_NAME}")


class ManagerError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise ManagerError(f"无法执行 {args[0]}: {exc}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ManagerError(f"命令失败 ({result.returncode}): {' '.join(args)}\n{detail}")
    return result


def _git(args: list[str], cwd: Path | None = None, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-c", "core.hooksPath=NUL", *args], cwd=cwd, check=check)


@contextmanager
def _exclusive_lock(target: Path):
    lock = target.parent / f".{target.name}.manager-lock"
    try:
        lock.mkdir(parents=False)
    except FileExistsError as exc:
        raise ManagerError(f"另一个安装/更新进程正在操作: {target}") from exc
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _default_target() -> Path:
    base = os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    return Path(base) / "skills" / SKILL_NAME


def _metadata(target: Path) -> Path:
    return target / META_NAME


def _write_metadata(target: Path, source: str, branch: str, commit: str) -> None:
    payload = {"skill": SKILL_NAME, "repo": source, "branch": branch, "commit": commit, "manager": 1}
    _metadata(target).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_metadata(target: Path) -> dict:
    try:
        value = json.loads(_metadata(target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagerError(f"安装记录缺失或损坏: {_metadata(target)}") from exc
    if value.get("skill") != SKILL_NAME or value.get("branch") != DEFAULT_BRANCH:
        raise ManagerError("安装记录的 Skill 或分支不匹配")
    return value


def _validate_tree(root: Path) -> None:
    if not root.is_dir():
        raise ManagerError(f"Skill 目录不存在: {root}")
    marker = root / "SKILL.md"
    if not marker.is_file() or f"name: {SKILL_NAME}" not in marker.read_text(encoding="utf-8", errors="replace"):
        raise ManagerError("SKILL.md 的 name 不是 solidworks-3d-2d")
    for relative in REQUIRED:
        if not (root / relative).is_file():
            raise ManagerError(f"Skill 缺少必需文件: {relative}")
    py_files = sorted(root.rglob("*.py"))
    if any(part in {".git", ".venv", "__pycache__"} for path in py_files for part in path.parts):
        py_files = [path for path in py_files if not {".git", ".venv", "__pycache__"}.intersection(path.parts)]
    for path in py_files:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (OSError, SyntaxError) as exc:
            raise ManagerError(f"Python 编译校验失败: {path}: {exc}") from exc


def _git_root(target: Path) -> Path:
    root = _git(["rev-parse", "--show-toplevel"], cwd=target).stdout.strip()
    if not root:
        raise ManagerError("目标目录不是 Git 仓库")
    return Path(root).resolve()


def _git_status(target: Path) -> list[str]:
    lines = _git(["status", "--porcelain", "--untracked-files=all"], cwd=target).stdout.splitlines()
    dirty = []
    for line in lines:
        path_text = line[3:].replace("\\", "/") if len(line) >= 3 else line
        parts = set(path_text.split("/"))
        if line.startswith("?? ") and path_text == META_NAME:
            continue
        dirty.append(line)
    return dirty


def _assert_checkout(target: Path, source: str, branch: str) -> dict:
    if _git_root(target) != target.resolve():
        raise ManagerError("目标路径不是安装仓库根目录")
    meta = _read_metadata(target)
    if str(meta.get("repo", "")).rstrip("/") != source.rstrip("/"):
        raise ManagerError("安装记录 repo 与请求源不匹配")
    remote = _git(["remote", "get-url", "origin"], cwd=target).stdout.strip()
    if remote.rstrip("/") != source.rstrip("/"):
        raise ManagerError(f"origin 不匹配: {remote}")
    current = _git(["branch", "--show-current"], cwd=target).stdout.strip()
    if current != branch:
        raise ManagerError(f"当前分支不是 {branch}: {current or '(detached)'}")
    dirty = _git_status(target)
    if dirty:
        raise ManagerError("安装目录有未提交改动，拒绝继续:\n" + "\n".join(dirty))
    _validate_tree(target)
    if meta.get("commit") != _commit(target):
        raise ManagerError("安装记录 commit 与当前 HEAD 不一致")
    return meta


def _commit(target: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=target).stdout.strip()


def _fetch_remote(target: Path, branch: str) -> str:
    _git(["fetch", "--no-tags", "origin", branch], cwd=target)
    return _git(["rev-parse", f"origin/{branch}"], cwd=target).stdout.strip()


def _dependencies(target: Path) -> str | None:
    requirements = target / "requirements.txt"
    if not requirements.is_file():
        return None
    venv = target / ".venv"
    venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not venv_python.is_file():
        _run([sys.executable, "-m", "venv", str(venv)], cwd=target)
    if not venv_python.is_file():
        raise ManagerError(f"venv Python 创建失败: {venv_python}")
    _run([str(venv_python), "-m", "pip", "install", "-r", str(requirements)], cwd=target)
    return str(venv)


def _install_locked(target: Path, source: str, branch: str, with_dependencies: bool) -> int:
    target = target.resolve()
    if target.exists():
        raise ManagerError(f"安装目标已存在，拒绝覆盖: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.install-{uuid.uuid4().hex}"
    try:
        _git(["clone", "--depth", "1", "--branch", branch, "--single-branch", source, str(temp)])
        _validate_tree(temp)
        commit = _commit(temp)
        _write_metadata(temp, source, branch, commit)
        temp.replace(target)
        if with_dependencies:
            try:
                _dependencies(target)
            except Exception as exc:
                print(json.dumps({"status": "partial", "dependencies_failed": True, "error": str(exc),
                                  "target": str(target), "commit": commit}, ensure_ascii=False, indent=2))
                return 3
        print(json.dumps({"status": "installed", "skill": SKILL_NAME, "target": str(target), "repo": source,
                          "branch": branch, "commit": commit}, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        raise


def install(target: Path, source: str, branch: str, with_dependencies: bool) -> int:
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(target):
        return _install_locked(target, source, branch, with_dependencies)


def check(target: Path, expected_source: str | None = None) -> int:
    target = target.resolve()
    meta = _read_metadata(target)
    if expected_source and meta["repo"].rstrip("/") != expected_source.rstrip("/"):
        raise ManagerError(f"安装记录的 origin 不匹配: {meta['repo']}")
    with _exclusive_lock(target):
        _assert_checkout(target, str(meta["repo"]), DEFAULT_BRANCH)
        remote = _fetch_remote(target, DEFAULT_BRANCH)
        current = _commit(target)
    print(json.dumps({"status": "ok", "target": str(target), "repo": meta["repo"], "branch": DEFAULT_BRANCH,
                      "commit": current, "remote_commit": remote, "update_available": remote != current}, ensure_ascii=False, indent=2))
    return 0


def update(target: Path, source: str, branch: str, with_dependencies: bool) -> int:
    target = target.resolve()
    with _exclusive_lock(target):
        meta = _assert_checkout(target, source, branch)
        before = _commit(target)
        merged = False
        try:
            remote = _fetch_remote(target, branch)
            if _git_status(target):
                raise ManagerError("fetch 后检测到安装目录被修改，拒绝更新")
            if remote == before:
                if with_dependencies:
                    try:
                        _dependencies(target)
                    except Exception as exc:
                        print(json.dumps({"status": "partial", "dependencies_failed": True, "error": str(exc), "commit": before}, indent=2))
                        return 3
                print(json.dumps({"status": "up-to-date", "before": before, "after": before}, indent=2))
                return 0
            if _git(["merge-base", "--is-ancestor", before, remote], cwd=target, check=False).returncode:
                shallow = target / ".git" / "shallow"
                if shallow.is_file():
                    _git(["fetch", "--no-tags", "--unshallow", "origin", branch], cwd=target)
                    remote = _git(["rev-parse", f"origin/{branch}"], cwd=target).stdout.strip()
                if _git(["merge-base", "--is-ancestor", before, remote], cwd=target, check=False).returncode:
                    raise ManagerError("远端不是当前安装的快进后继，或浅克隆无法建立祖先关系")
            _git(["merge", "--ff-only", f"origin/{branch}"], cwd=target)
            merged = True
            _validate_tree(target)
            after = _commit(target)
            _write_metadata(target, source, branch, after)
            if with_dependencies:
                try:
                    _dependencies(target)
                except Exception as exc:
                    print(json.dumps({"status": "partial", "dependencies_failed": True, "error": str(exc), "commit": after}, indent=2))
                    return 3
            print(json.dumps({"status": "updated", "before": before, "after": after, "branch": branch}, indent=2))
            return 0
        except Exception as original:
            if merged:
                if _git_status(target):
                    raise ManagerError("更新校验失败且检测到并发编辑，拒绝自动回滚")
                rollback = _git(["reset", "--hard", before], cwd=target, check=False)
                if rollback.returncode or _commit(target) != before:
                    raise ManagerError("更新校验失败，自动回滚也失败；已保留真实 metadata，未假称恢复") from original
                _write_metadata(target, source, branch, before)
            raise


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        raise ManagerError("需要 Python 3.10 或更高版本")
    parser = argparse.ArgumentParser(description="Install/check/update the solidworks-3d-2d skill")
    parser.add_argument("command", choices=("install", "check", "update"))
    parser.add_argument("--target", type=Path, default=_default_target())
    parser.add_argument("--source", default=None)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--dependencies", action="store_true")
    args = parser.parse_args(argv)
    if args.branch != DEFAULT_BRANCH:
        parser.error(f"只支持固定分支 {DEFAULT_BRANCH}")
    if args.command == "install":
        return install(args.target, args.source or DEFAULT_REPO, args.branch, args.dependencies)
    if args.command == "check":
        return check(args.target, args.source)
    return update(args.target, args.source or DEFAULT_REPO, args.branch, args.dependencies)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except ManagerError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
