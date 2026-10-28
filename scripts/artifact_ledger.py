"""@brief 独立工程图任务交付物账本。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from datetime import datetime, timezone, timedelta
CN_TZ = timezone(timedelta(hours=8))

def safe_job_id(value: Any) -> str:
    """@brief 返回适合文件名和审计日志的任务 ID。"""
    text = str(value or "")
    safe = "".join(ch for ch in text if ch.isascii() and (ch.isalnum() or ch in "-_"))
    if not safe:
        raise ValueError("任务缺少有效 id")
    return safe[:96]

def now_iso() -> str:
    """@brief 返回中国时区 ISO 时间。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


class RunLedger:
    """Persistent, hash-addressed lifecycle ledger for one bounded run."""

    def __init__(self, root: Path, run_id: str, owner: str, *, policy: dict[str, Any] | None = None):
        self.root = self._safe_root(Path(root))
        self.run_id = str(run_id)
        self.owner = str(owner)
        self.path = self.root / f"{safe_job_id(self.run_id)}.run-ledger.json"
        self.marker_path = self.root / ".run-owner.json"
        if self.marker_path.is_file():
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if marker.get("runId") != self.run_id or marker.get("owner") != self.owner or Path(marker.get("root", "")).resolve() != self.root:
                raise ValueError("run root is owned by another run")
        elif any(item.name not in {self.path.name} for item in self.root.iterdir()):
            raise ValueError("non-empty run root requires an ownership marker")
        else:
            self.marker_path.write_text(json.dumps({"runId": self.run_id, "owner": self.owner, "root": str(self.root)}, ensure_ascii=False), encoding="utf-8")
        self._initial_files = {str(item.resolve()) for item in self.root.rglob("*") if item.is_file() and item.name != self.marker_path.name}
        self.data = self._load() or {
            "schemaVersion": "1.0", "runId": self.run_id, "owner": self.owner,
            "root": str(self.root), "policy": policy or {}, "artifacts": [],
            "checkpoints": {}, "errorCounts": {}, "createdAt": now_iso(),
        }

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            return True
        try:
            return bool(getattr(path.stat(), "st_file_attributes", 0) & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))
        except (FileNotFoundError, OSError):
            return False

    @classmethod
    def _safe_root(cls, root: Path) -> Path:
        candidate = root.expanduser().absolute()
        cursor = candidate
        while cursor != cursor.parent:
            if cls._is_reparse(cursor):
                raise ValueError(f"run root contains a reparse point: {cursor}")
            cursor = cursor.parent
        resolved = candidate.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        if cls._is_reparse(resolved):
            raise ValueError("run root cannot be a reparse point")
        return resolved

    def inside(self, path: Path) -> Path:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        cursor = candidate.absolute()
        while cursor != cursor.parent:
            if self._is_reparse(cursor):
                raise ValueError(f"artifact path contains reparse point: {cursor}")
            if cursor == self.root:
                break
            cursor = cursor.parent
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"artifact outside run root: {candidate}") from exc
        cursor = self.root
        for part in resolved.relative_to(self.root).parts:
            cursor = cursor / part
            if self._is_reparse(cursor):
                raise ValueError(f"artifact path contains reparse point: {cursor}")
        return resolved

    def _load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("runId") != self.run_id or data.get("owner") != self.owner or Path(data.get("root", "")).resolve() != self.root:
            raise ValueError("ledger ownership mismatch")
        return data

    def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".run-ledger-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def register_artifact(self, path: Path, *, role: str, dependencies: list[Path] | None = None, created_by_run: bool = False) -> dict[str, Any]:
        if role not in {"scratch", "final", "evidence"}:
            raise ValueError("role must be scratch, final, or evidence")
        resolved = self.inside(path)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        known_owned = any(item.get("path") == str(resolved) and item.get("createdByRun") for item in self.data.get("artifacts", []))
        if created_by_run and str(resolved) in self._initial_files and not known_owned:
            raise ValueError("existing file cannot be claimed as run-created artifact")
        record = {
            "path": str(resolved), "sha256": sha256_file(resolved), "sizeBytes": resolved.stat().st_size,
            "role": role, "createdByRun": bool(created_by_run), "dependencies": [str(Path(item).expanduser().resolve()) for item in (dependencies or [])],
            "retention": "retain" if role in {"final", "evidence"} else "cleanup_candidate",
        }
        self.data["artifacts"] = [item for item in self.data.get("artifacts", []) if item.get("path") != record["path"]]
        self.data["artifacts"].append(record)
        self.save()
        return record

    def checkpoint(self, item_id: str, *, phase: str, status: str, fingerprint: str, outputs: list[Path] | None = None, error_code: str | None = None, result: dict[str, Any] | None = None) -> None:
        self.data.setdefault("checkpoints", {}).setdefault(str(item_id), {})[phase] = {
            "status": status, "fingerprint": fingerprint,
            "outputs": [str(self.inside(item)) for item in (outputs or [])],
            "outputHashes": [sha256_file(self.inside(item)) for item in (outputs or [])],
            "result": result or {}, "error_code": error_code, "updatedAt": now_iso(),
        }
        self.save()

    def fingerprint(self, source: Path, dependencies: list[Path], policy: dict[str, Any]) -> str:
        source_path = Path(source).expanduser().resolve()
        dependency_paths = [Path(item).expanduser().resolve() for item in dependencies]
        payload = {"source": {"path": str(source_path), "sha256": sha256_file(source_path)}, "dependencies": [{"path": str(path), "sha256": sha256_file(path)} for path in sorted(dependency_paths, key=str)], "policy": policy}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def cleanup(self, *, dry_run: bool = True, reopened_dependency_audit: bool = False) -> dict[str, Any]:
        if not reopened_dependency_audit:
            return {"dry_run": dry_run, "deleted": [], "preserved": [{"reason": "reopened_dependency_audit_incomplete"}], "audit_complete": False}
        marker = json.loads(self.marker_path.read_text(encoding="utf-8")) if self.marker_path.is_file() else {}
        if marker.get("runId") != self.run_id or marker.get("owner") != self.owner or Path(marker.get("root", "")).resolve() != self.root:
            raise ValueError("run ownership verification failed")
        records = self.data.get("artifacts", [])
        protected: set[str] = {str(item.get("path")) for item in records if item.get("role") in {"final", "evidence"}}
        changed_protected = True
        while changed_protected:
            changed_protected = False
            for item in records:
                if str(item.get("path")) in protected:
                    for dependency in item.get("dependencies", []):
                        if str(dependency) not in protected:
                            protected.add(str(dependency))
                            changed_protected = True
        deleted, preserved = [], []
        for item in records:
            if item.get("role") != "scratch":
                continue
            try:
                path = self.inside(Path(str(item.get("path"))))
            except ValueError:
                preserved.append({"path": str(item.get("path")), "reason": "outside_or_reparse_path"})
                continue
            if item.get("sha256") != (sha256_file(path) if path.is_file() else None):
                preserved.append({"path": str(path), "reason": "hash_changed_or_unknown"})
                continue
            if not item.get("createdByRun"):
                preserved.append({"path": str(path), "reason": "not_created_by_run"})
            elif str(path) in protected:
                preserved.append({"path": str(path), "reason": "dependency_closure"})
            elif not path.is_file():
                preserved.append({"path": str(path), "reason": "missing"})
            elif dry_run:
                preserved.append({"path": str(path), "reason": "dry_run"})
            else:
                path.unlink()
                deleted.append(str(path))
        return {"dry_run": dry_run, "deleted": deleted, "preserved": preserved, "audit_complete": True}


def ledger_dir(queue_dir: Path) -> Path:
    """@brief 返回交付物账本目录。"""
    directory = Path(queue_dir) / "ledgers"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ledger_path_for(queue_dir: Path, job_id: Any) -> Path:
    """@brief 返回指定任务的交付物账本路径。"""
    return ledger_dir(queue_dir) / f"{safe_job_id(job_id)}.ledger.json"


def sha256_file(path: Path) -> str:
    """@brief 计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_paths(value: Any) -> list[tuple[str, Path]]:
    """@brief 从 result/artifacts 字段提取可能的交付物路径。"""
    paths: list[tuple[str, Path]] = []
    if isinstance(value, str):
        paths.append(("artifact", Path(value)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                paths.append((f"artifact_{index}", Path(item)))
            elif isinstance(item, dict):
                raw_path = item.get("path") or item.get("outputPath")
                if raw_path:
                    paths.append((str(item.get("kind") or item.get("type") or f"artifact_{index}"), Path(str(raw_path))))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                paths.append((str(key), Path(item)))
            elif isinstance(item, dict):
                raw_path = item.get("path") or item.get("outputPath")
                if raw_path:
                    paths.append((str(key), Path(str(raw_path))))
    return paths


def _artifact_base_dir(job: dict[str, Any]) -> Path:
    """@brief 返回相对交付物路径的解析基准目录。"""
    if job.get("cwd"):
        return Path(str(job["cwd"]))
    if job.get("projectPath"):
        project_path = Path(str(job["projectPath"]))
        if project_path.suffix:
            return project_path.parent
        return project_path
    return Path.cwd()


def collect_artifact_paths(job: dict[str, Any], result: dict[str, Any]) -> list[tuple[str, Path]]:
    """@brief 从任务和执行结果中收集交付物路径。"""
    collected: list[tuple[str, Path]] = []
    if result.get("outputPath"):
        collected.append(("codex_output", Path(str(result["outputPath"]))))
    collected.extend(_candidate_paths(result.get("outputs")))
    collected.extend(_candidate_paths(result.get("artifacts")))
    collected.extend(_candidate_paths(job.get("artifacts")))

    seen: set[str] = set()
    unique: list[tuple[str, Path]] = []
    base_dir = _artifact_base_dir(job)
    for kind, path in collected:
        if not path.is_absolute():
            path = base_dir / path
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append((kind, path))
    return unique


def describe_artifact(kind: str, path: Path, artifact_baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """@brief 返回单个交付物的存在性、大小和 hash。"""
    resolved = Path(path).expanduser()
    exists = resolved.exists()
    item: dict[str, Any] = {
        "kind": kind,
        "path": str(resolved),
        "exists": exists,
        "isDirectory": resolved.is_dir() if exists else False,
    }
    if exists and resolved.is_file():
        stat = resolved.stat()
        item["sizeBytes"] = stat.st_size
        item["sha256"] = sha256_file(resolved)
        if artifact_baseline is not None:
            before = artifact_baseline.get(str(resolved.resolve()))
            if isinstance(before, dict) and before.get("sha256"):
                item["producedThisRun"] = before.get("sha256") != item["sha256"]
            else:
                item["producedThisRun"] = before is None
    return item


def build_artifact_ledger(queue_dir: Path, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """@brief 构建任务交付物账本对象。"""
    baseline = result.get("artifactBaseline") if isinstance(result.get("artifactBaseline"), dict) else None
    artifacts = [describe_artifact(kind, path, baseline) for kind, path in collect_artifact_paths(job, result)]
    return {
        "schemaVersion": "1.0",
        "jobId": job.get("id"),
        "runId": job.get("runId"),
        "kind": job.get("kind"),
        "executor": job.get("executor", "mock"),
        "target": job.get("target"),
        "objective": job.get("objective"),
        "detail": job.get("detail"),
        "strictRules": job.get("strictRules") if isinstance(job.get("strictRules"), list) else [],
        "expectedOutput": job.get("expectedOutput"),
        "localCadAutomation": bool(
            job.get("uiConfig", {}).get("cadRuntime", {}).get("localCadAutomation")
            if isinstance(job.get("uiConfig"), dict)
            and isinstance(job.get("uiConfig", {}).get("cadRuntime"), dict)
            else False
        ),
        "status": job.get("status"),
        "generatedAt": now_iso(),
        "queueDir": str(Path(queue_dir)),
        "artifacts": artifacts,
        "verification": result.get("verification", []),
        "risks": result.get("risks", []),
        "drawingEvidence": result.get("drawingEvidence"),
        "bomEvidence": result.get("bomEvidence"),
        "dfmEvidence": result.get("dfmEvidence"),
        "reviewFindings": result.get("reviewFindings", []),
        "artifactRelations": result.get("artifactRelations", []),
        "resultMessage": result.get("message"),
    }


def write_artifact_ledger(queue_dir: Path, job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """@brief 写入任务交付物账本并返回账本对象。"""
    ledger = build_artifact_ledger(queue_dir, job, result)
    path = ledger_path_for(queue_dir, job.get("id"))
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    ledger["ledgerPath"] = str(path)
    return ledger
