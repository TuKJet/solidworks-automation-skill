"""Drawing previews must show exported sheets even with no desktop viewport."""
from pathlib import Path

import fitz
import pytest

from scripts.sw_review import inspect_bmp_preview, save_drawing_review_previews
from scripts.sw_drawing_review import inspect_pdf_dimension_rendering


def test_drawing_preview_renders_pdf_without_using_viewport(tmp_path):
    source = tmp_path / "sheet.pdf"
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        page.draw_rect(fitz.Rect(50, 50, 545, 792), color=(0, 0, 0), width=2)
        page.insert_text((120, 120), "80 x 60 x 20", fontsize=18)
        document.save(source)
    # A plain object has no COM / SaveBMP / camera members; PDF rendering must
    # not require any of those or a connected RDP display.
    previews = save_drawing_review_previews(object(), tmp_path / "preview", pdf_path=source)
    assert len(previews) == 1
    evidence = inspect_bmp_preview(previews[0])
    assert Path(previews[0]).suffix == ".png"
    assert evidence["exists"]
    assert evidence["width"] == 1190
    assert evidence["height"] == 1684
    assert not evidence["likely_blank"]


def test_blank_pdf_preview_still_fails_blank_check(tmp_path):
    source = tmp_path / "blank.pdf"
    with fitz.open() as document:
        document.new_page()
        document.save(source)
    [preview] = save_drawing_review_previews(object(), tmp_path / "preview", pdf_path=source)
    assert inspect_bmp_preview(preview)["likely_blank"]


def test_invalid_pdf_does_not_fall_back_to_unverified_viewport(tmp_path):
    source = tmp_path / "invalid.pdf"
    source.write_bytes(b"not a PDF")
    with pytest.raises(Exception):
        save_drawing_review_previews(object(), tmp_path / "preview", pdf_path=source)


def test_portrait_pdf_dimension_matching_uses_actual_sheet_dimensions(tmp_path):
    source = tmp_path / "portrait.pdf"
    width, height = 595.276, 841.89
    positions = [(0.05, .25), (.15, .25), (.05, .18), (.15, .18), (.05, .11), (.15, .11)]
    with fitz.open() as document:
        page = document.new_page(width=width, height=height)
        for index, (x, y) in enumerate(positions):
            page.insert_text((x / .210 * width, height - y / .297 * height), str(20 + index * 10), fontsize=10)
        document.save(source)
    structure = {
        "sheets": ["Sheet1"],
        "sheet_size": {"width_m": .210, "height_m": .297},
        "dimensions": [{"sheet": "Sheet1", "name": str(index), "box_evidence": {"position_m": [x, y]}} for index, (x, y) in enumerate(positions)],
    }
    actual = inspect_pdf_dimension_rendering(source, structure)
    assert actual["status"] == "pass"
    assert len(actual["matched"]) == 6
    wrong = inspect_pdf_dimension_rendering(source, {**structure, "sheet_size": {"width_m": .297, "height_m": .210}})
    assert wrong["status"] == "review_required"
