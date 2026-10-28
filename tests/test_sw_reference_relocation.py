"""Safe offline reference-relocation regressions for copied SolidWorks packages."""
from __future__ import annotations

from pathlib import Path

from scripts import sw_delivery


class FakeSolidWorks:
    def __init__(self, *, opened=(), outcomes=(), mutate_copy: Path | None = None):
        self.opened = {str(Path(path).resolve()) for path in opened}
        self.outcomes = iter(outcomes)
        self.mutate_copy = mutate_copy
        self.calls = []

    def GetOpenDocumentByName(self, path):
        return object() if str(Path(path).resolve()) in self.opened else None

    def ReplaceReferencedDocument(self, referencing_document, old_path, new_path):
        self.calls.append((referencing_document, old_path, new_path))
        if self.mutate_copy is not None and len(self.calls) == 1:
            self.mutate_copy.write_bytes(b"changed by replacement")
        return next(self.outcomes, True)


def _files(tmp_path: Path, *, second=False):
    origin = tmp_path / "origin"
    package = tmp_path / "package"
    origin.mkdir()
    package.mkdir()
    drawing = package / "drawing-copy.slddrw"
    drawing.write_bytes(b"original drawing bytes")
    old = origin / "part-old.sldprt"
    new = package / "part-new.sldprt"
    old.write_bytes(b"exact part bytes")
    new.write_bytes(b"exact part bytes")
    mapping = {str(old): str(new)}
    if second:
        old2 = origin / "part-two-old.sldprt"
        new2 = package / "part-two-new.sldprt"
        old2.write_bytes(b"other exact bytes")
        new2.write_bytes(b"other exact bytes")
        mapping[str(old2)] = str(new2)
    return origin, package, drawing, old, new, mapping


def test_relocation_requires_present_byte_exact_renamed_copy(tmp_path: Path):
    _origin, package, drawing, old, new, mapping = _files(tmp_path)
    new.unlink()

    result = sw_delivery.relocate_copied_references(FakeSolidWorks(), drawing, mapping, package)

    assert result["status"] == "blocked"
    assert result["error_code"] == "DRAWING_REFERENCE_RELOCATION_PREFLIGHT_FAILED"
    assert result["replacements"] == []

    new.write_bytes(b"different bytes")
    result = sw_delivery.relocate_copied_references(FakeSolidWorks(), drawing, mapping, package)
    assert result["status"] == "blocked"
    assert any(item["code"] == "DRAWING_REFERENCE_RELOCATION_HASH_MISMATCH" for item in result["errors"])
    assert old.is_file()


def test_relocation_refuses_open_copy_or_destination_and_outside_package(tmp_path: Path):
    _origin, package, drawing, _old, new, mapping = _files(tmp_path)

    opened = FakeSolidWorks(opened=[drawing])
    result = sw_delivery.relocate_copied_references(opened, drawing, mapping, package)
    assert result["status"] == "blocked"
    assert any(item["code"] == "DRAWING_REFERENCE_RELOCATION_DOCUMENT_OPEN" for item in result["errors"])
    assert not opened.calls

    opened_destination = FakeSolidWorks(opened=[new])
    result = sw_delivery.relocate_copied_references(opened_destination, drawing, mapping, package)
    assert result["status"] == "blocked"
    assert any(item["role"] == "replacement_copy" for item in result["errors"])

    outside = tmp_path / "outside.sldprt"
    outside.write_bytes(new.read_bytes())
    result = sw_delivery.relocate_copied_references(FakeSolidWorks(), drawing, {next(iter(mapping)): str(outside)}, package)
    assert result["status"] == "blocked"
    assert any(item["code"] == "DRAWING_REFERENCE_RELOCATION_OUTSIDE_PACKAGE" for item in result["errors"])

    origin_copy = tmp_path / "origin-copy.slddrw"
    origin_copy.write_bytes(b"must not be relocation target")
    result = sw_delivery.relocate_copied_references(FakeSolidWorks(), origin_copy, mapping, package)
    assert result["status"] == "blocked"
    assert any(item["code"] == "DRAWING_REFERENCE_RELOCATION_COPY_OUTSIDE_PACKAGE" for item in result["errors"])


def test_false_api_result_restores_copy_and_never_reports_reopened_validation(tmp_path: Path):
    _origin, package, drawing, _old, _new, mapping = _files(tmp_path)
    original = drawing.read_bytes()
    sw = FakeSolidWorks(outcomes=[False], mutate_copy=drawing)

    result = sw_delivery.relocate_copied_references(sw, drawing, mapping, package)

    assert result["status"] == "failed"
    assert result["error_code"] == "DRAWING_REFERENCE_RELOCATION_API_FAILED"
    assert result["rollback_verified"] is True
    assert drawing.read_bytes() == original
    assert result["pending_reopen"] is True
    assert result["release_ready"] is False


def test_second_replacement_failure_restores_pre_relocation_copy(tmp_path: Path):
    _origin, package, drawing, _old, _new, mapping = _files(tmp_path, second=True)
    original = drawing.read_bytes()
    sw = FakeSolidWorks(outcomes=[True, False], mutate_copy=drawing)

    result = sw_delivery.relocate_copied_references(sw, drawing, mapping, package)

    assert result["status"] == "failed"
    assert len(sw.calls) == 2
    assert drawing.read_bytes() == original
    assert result["rollback_verified"] is True
    assert Path(result["journal_path"]).is_file()


def test_success_is_pending_reopen_not_release_pass(tmp_path: Path):
    _origin, package, drawing, _old, _new, mapping = _files(tmp_path)

    result = sw_delivery.relocate_copied_references(FakeSolidWorks(outcomes=[True]), drawing, mapping, package)

    assert result["status"] == "review_required"
    assert result["pending_reopen"] is True
    assert result["release_ready"] is False
    assert result["replacements"][0]["replaced"] is True
