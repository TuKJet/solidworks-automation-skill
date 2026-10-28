"""图纸页实际宽高回读与纵向图幅布局回归。"""

from scripts.sw_drawing import _MODULE, plan_standard_view_layout


_sheet_dimensions = _MODULE._sheet_dimensions


class _SheetProperties:
    def __init__(self, values):
        self.values = values

    def GetProperties2(self):
        return self.values


def test_sheet_dimensions_reads_portrait_properties_indices():
    values = [None, None, None, None, None, 0.210, 0.297]

    result = _sheet_dimensions(_SheetProperties(values))

    assert result == {
        "width_m": 0.210,
        "height_m": 0.297,
        "orientation": "portrait",
        "source": "GetProperties2",
    }


def test_layout_uses_actual_portrait_sheet_dimensions():
    layout = plan_standard_view_layout(
        (0.080, 0.060, 0.020),
        paper_size="A4",
        projection="first_angle",
        sheet_dimensions_m={"width_m": 0.210, "height_m": 0.297, "source": "GetProperties2"},
    )

    assert layout["status"] == "pass"
    assert layout["sheet"] == {"width_m": 0.210, "height_m": 0.297}
    assert layout["sheet_size_source"] == "GetProperties2"
    for view in layout["views"]:
        box = view["box"]
        assert 0 <= box["left"] <= box["right"] <= 0.210
        assert 0 <= box["bottom"] <= box["top"] <= 0.297


def test_sheet_dimensions_falls_back_when_properties_are_unavailable():
    class EmptySheet:
        pass

    assert _sheet_dimensions(EmptySheet()) is None


def test_sheet_dimensions_get_size_does_not_treat_enum_as_width():
    class Sheet:
        def GetSize(self):
            return (7, 0.210, 0.297)
    result = _sheet_dimensions(Sheet())
    assert result["width_m"] == 0.210
    assert result["height_m"] == 0.297


def test_structure_reports_actual_sheet_dimensions_and_source():
    class Drawing:
        def GetSheetNames(self):
            return []

        def GetCurrentSheet(self):
            return self

        def GetTemplateName(self):
            return "A4_GBT.slddrt"

        def GetProperties2(self):
            return [None, None, None, None, None, 0.210, 0.297]

        def GetViews(self):
            return []

    from scripts.sw_drawing import inspect_drawing_structure

    result = inspect_drawing_structure(Drawing())

    assert result["sheet_size"] == {"width_m": 0.210, "height_m": 0.297}
    assert result["sheet_size_source"] == "GetProperties2"
