"""@brief 产物读取的真实文件与边界测试，不依赖 SolidWorks。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
from artifact_access import ArtifactAccess
import server


def test_ledger_artifact_roundtrip_and_chunks(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    artifact = root / "part.step"
    artifact.write_bytes(b"ISO-10303-21;geometry")
    access = ArtifactAccess(root, tmp_path / "queue")
    refs = access.publish({"output_path": str(artifact)})
    assert len(refs) == 1
    path, data, metadata = access.read(refs[0]["artifact_id"], max_bytes=5)
    assert data == b"ISO-1"
    assert metadata["next_offset"] == 5
    _, rest, _ = access.read(refs[0]["artifact_id"], offset=5)
    assert data + rest == artifact.read_bytes()


def test_arbitrary_paths_and_outside_outputs_are_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("private")
    access = ArtifactAccess(root, tmp_path / "queue")
    assert access.publish({"output_path": str(private)}) == []
    with pytest.raises(ValueError):
        access.read("../../private.txt")


def test_modified_file_cannot_reuse_delivery_hash(tmp_path):
    artifact = tmp_path / "part.step"
    artifact.write_bytes(b"old")
    access = ArtifactAccess(tmp_path, tmp_path / "queue")
    ref = access.publish({"output_path": str(artifact)})[0]
    artifact.write_bytes(b"new")
    with pytest.raises(ValueError, match="内容已变化"):
        access.read(ref["artifact_id"])


def test_tampered_ledger_cannot_escape_output_root(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    artifact = root / "part.step"
    artifact.write_bytes(b"geometry")
    queue = tmp_path / "queue"
    access = ArtifactAccess(root, queue)
    ref = access.publish({"output_path": str(artifact)})[0]
    file = next((queue / "ledgers").glob("*.json"))
    record = json.loads(file.read_text())
    record["artifacts"][0]["path"] = str(tmp_path / "private.txt")
    file.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        access.read(ref["artifact_id"])


def test_binary_artifact_chunk_uses_fixed_json_response(monkeypatch):
    """Binary chunks must not access the absent ArtifactReadInput.response_format."""
    class FakeAccess:
        def read(self, artifact_id, offset, max_bytes):
            assert artifact_id == "mcp-0123456789abcdef0123456789abcdef-1"
            assert offset == 0
            assert max_bytes == 3
            return (
                Path("part.step"),
                b"abc",
                {"sha256": "a" * 64, "total_bytes": 3, "offset": 0, "next_offset": None},
            )

    monkeypatch.setattr(server, "_artifact_access", lambda: FakeAccess())
    result = server.cadstudio_read_artifact(server.ArtifactReadInput(
        artifact_id="mcp-0123456789abcdef0123456789abcdef-1",
        max_bytes=3,
        as_preview=False,
    ))
    payload = json.loads(result)
    assert payload["status"] == "ok"
    assert payload["encoding"] == "base64"
    assert payload["data"] == "YWJj"
