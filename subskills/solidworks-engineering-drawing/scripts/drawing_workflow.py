"""
SolidWorks 工程图操作工具
"""
import math
import re
from fractions import Fraction
from pathlib import Path

try:
    from scripts.sw_preflight import import_com_dependencies
    from scripts.sw_connect import activate_document_verified, create_empty_dispatch_variant, get_com_member
except ImportError:
    from sw_preflight import import_com_dependencies
    from sw_connect import activate_document_verified, create_empty_dispatch_variant, get_com_member

pythoncom, _win32com, VARIANT = import_com_dependencies()


PAPER_SIZES = {
    # swDwgPaperSizes: A4 横向=6, A4 纵向=7, A3=8, A2=9, A1=10, A0=11.
    # 纸张方向最终以 ISheet 回读为准；这里保留默认横向尺寸供无 COM 回退。
    "A4": {"code": 6, "width_m": 0.297, "height_m": 0.210},
    "A3": {"code": 8, "width_m": 0.420, "height_m": 0.297},
    "A2": {"code": 9, "width_m": 0.594, "height_m": 0.420},
    "A1": {"code": 10, "width_m": 0.841, "height_m": 0.594},
    "A0": {"code": 11, "width_m": 1.189, "height_m": 0.841},
}
PAPER_SIZE_CODES = {6: "A4", 7: "A4", 8: "A3", 9: "A2", 10: "A1", 11: "A0"}

STANDARD_DRAWING_SCALES = (10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)

# SolidWorks.Interop.swconst.swAlignDimensionType_e（SW2026 Interop 反射确认）。
ALIGN_DIMENSION_TYPES = {
    "auto_arrange": 0,
    "space_evenly": 1,
    "colinear": 2,
    "stagger": 3,
    "top_align_text": 4,
    "bottom_align_text": 5,
    "left_align_text": 6,
    "right_align_text": 7,
}

# SolidWorks.Interop.swconst.swAutoInsertCenterMarkTypes_e
# （SW2026 Interop 反射与官方 API 帮助交叉确认）。
AUTO_CENTER_MARK_TARGETS = {
    "holes": 1,
    "fillets": 2,
    "slots": 4,
}

# SolidWorks.Interop.swconst.swAnnotationVisibilityState_e.  半隐藏的尺寸不应
# 参与自动排列；只有 swAnnotationVisible 才能安全交给 AlignDimensions。
ANNOTATION_VISIBILITY = {
    0: "unknown",
    1: "visible",
    2: "hidden",
    3: "hidden",
}

# SolidWorks.Interop.swconst.swDimensionType_e.  系统值的长度单位为米，角度
# 尺寸（含角度坐标）则为弧度；不要把显示的毫米/度字符串当作系统值。
ANGULAR_DIMENSION_TYPES = {3, 16}


def _safe_member(obj, name, *args, default=None):
    """@brief 读取工程图 COM 成员，失败时返回默认值。"""
    if obj is None:
        return default
    try:
        value = get_com_member(obj, name, *args)
        return default if value is None else value
    except Exception:
        return default


def _as_sequence(value):
    """@brief 统一 COM 数组、元组和单对象返回值。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _sheet_dimensions(sheet):
    """@brief 从当前图纸页回读实际宽高（米），并标记证据来源。

    ``ISheet::GetProperties2``/``GetProperties`` 返回的属性数组中，SolidWorks
    将图纸宽度和高度放在索引 5、6。不同版本或 mock 可能只暴露 ``GetSize``，
    因此保留一个形状明确的兼容回退；读不到有效正数时返回 ``None``，由调用方
    使用纸张枚举的保守尺寸。
    """
    for method_name in ("GetProperties2", "GetProperties"):
        values = _as_sequence(_safe_member(sheet, method_name, default=[]))
        if len(values) <= 6:
            continue
        try:
            width_m, height_m = float(values[5]), float(values[6])
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(item) and item > 0 for item in (width_m, height_m)):
            continue
        return {
            "width_m": width_m,
            "height_m": height_m,
            "orientation": "portrait" if height_m > width_m else "landscape",
            "source": method_name,
        }

    size = _as_sequence(_safe_member(sheet, "GetSize", default=[]))
    if len(size) in (2, 3):
        try:
            # Generated COM wrappers may return (paper_enum, width, height).
            width_m, height_m = float(size[-2]), float(size[-1])
        except (TypeError, ValueError):
            width_m = height_m = 0.0
        if all(math.isfinite(item) and item > 0 for item in (width_m, height_m)):
            return {
                "width_m": width_m,
                "height_m": height_m,
                "orientation": "portrait" if height_m > width_m else "landscape",
                "source": "GetSize",
            }
    return None


def _sheet_scale(sheet):
    """@brief 回读 sheet 的真实比例，失败时明确标为 unavailable。"""
    for method_name in ("GetProperties2", "GetProperties"):
        values = _as_sequence(_safe_member(sheet, method_name, default=[]))
        if len(values) <= 3:
            continue
        try:
            numerator, denominator = float(values[2]), float(values[3])
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(item) and item > 0 for item in (numerator, denominator)):
            width_m = height_m = None
            if len(values) > 6:
                try:
                    width_m, height_m = float(values[5]), float(values[6])
                    if not all(math.isfinite(item) and item > 0 for item in (width_m, height_m)):
                        width_m = height_m = None
                except (TypeError, ValueError):
                    width_m = height_m = None
            try:
                paper_size_code = int(values[0])
            except (IndexError, TypeError, ValueError):
                paper_size_code = None
            try:
                template_in = int(values[1])
            except (IndexError, TypeError, ValueError):
                template_in = None
            return {
                "numerator": numerator,
                "denominator": denominator,
                "source": method_name,
                "first_angle": bool(values[4]) if len(values) > 4 else None,
                "paper_size_code": paper_size_code,
                "paper_size": PAPER_SIZE_CODES.get(paper_size_code),
                "template_in": template_in,
                "width_m": width_m,
                "height_m": height_m,
                "orientation": "portrait" if width_m is not None and height_m > width_m else "landscape" if width_m is not None else None,
            }
    return {
        "numerator": None, "denominator": None, "source": "unavailable", "first_angle": None,
        "paper_size_code": None, "paper_size": None, "template_in": None,
        "width_m": None, "height_m": None, "orientation": None,
    }


def _view_scale_state(view):
    """@brief 记录视图比例链，供 SetupSheet6 后逐项验证/必要时恢复。"""
    ratio = _as_sequence(_safe_member(view, "ScaleRatio", default=[]))
    try:
        numerator, denominator = float(ratio[0]), float(ratio[1])
        if not all(math.isfinite(item) and item > 0 for item in (numerator, denominator)):
            raise ValueError
        scale_ratio = [numerator, denominator]
    except (IndexError, TypeError, ValueError):
        scale_ratio = None
    return {
        "name": str(_safe_member(view, "Name", default="") or ""),
        "scale_ratio": scale_ratio,
        "use_sheet_scale": _safe_member(view, "UseSheetScale", default=None),
        "use_parent_scale": _safe_member(view, "UseParentScale", default=None),
    }


def _sheet_view_scale_states(sheet):
    return [_view_scale_state(view) for view in _as_sequence(_safe_member(sheet, "GetViews", default=[]))]


def _set_view_property(view, name, value):
    """@brief 仅在 COM 属性可写时设置；失败由调用方记录，不静默改比例。"""
    try:
        setattr(view, name, value)
        return True
    except Exception:
        return False


def _restore_view_scale_states(sheet, before_states):
    """@brief SetupSheet6 意外改变独立 view 时恢复其比例链。

    ``ScaleRatio`` 可能令视图脱离 sheet/parent scale，因此先恢复数值比例，
    再恢复两个依附标志。只在回读不一致时写入，避免对真实 COM 做无谓变更。
    """
    before_by_name = {item["name"]: item for item in before_states if item["name"]}
    outcomes = []
    for index, view in enumerate(_as_sequence(_safe_member(sheet, "GetViews", default=[]))):
        name = str(_safe_member(view, "Name", default="") or "")
        before = before_by_name.get(name) or (before_states[index] if index < len(before_states) else None)
        if before is None:
            continue
        current = _view_scale_state(view)
        changed = []
        if before["scale_ratio"] is not None and current["scale_ratio"] != before["scale_ratio"]:
            if _set_view_property(view, "ScaleRatio", tuple(before["scale_ratio"])):
                changed.append("ScaleRatio")
        for property_name, key in (("UseParentScale", "use_parent_scale"), ("UseSheetScale", "use_sheet_scale")):
            if before[key] is not None and current[key] != before[key]:
                if _set_view_property(view, property_name, before[key]):
                    changed.append(property_name)
        outcomes.append({"name": name, "restored": changed, "before": before, "after": _view_scale_state(view)})
    return outcomes


def _same_scale_ratio(expected, actual):
    """@brief 严格但浮点容忍地比较 SolidWorks 比例分子/分母。"""
    if expected is None or actual is None:
        return expected is actual
    return (
        len(expected) == len(actual) == 2
        and all(math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
                for left, right in zip(expected, actual))
    )


def _verify_view_scale_states(sheet, before_states):
    """@brief 恢复后逐项回读比例与依附标志；未知值不冒充验证通过。"""
    before_by_name = {item["name"]: item for item in before_states if item["name"]}
    checks = []
    for index, view in enumerate(_as_sequence(_safe_member(sheet, "GetViews", default=[]))):
        name = str(_safe_member(view, "Name", default="") or "")
        before = before_by_name.get(name) or (before_states[index] if index < len(before_states) else None)
        if before is None:
            checks.append({"name": name, "status": "unknown", "reason": "view_not_found_in_pre_setup_state"})
            continue
        actual = _view_scale_state(view)
        matched = (
            _same_scale_ratio(before["scale_ratio"], actual["scale_ratio"])
            and all(
                before[key] == actual[key]
                for key in ("use_sheet_scale", "use_parent_scale")
            )
        )
        checks.append({"name": name, "status": "pass" if matched else "failed", "before": before, "actual": actual})
    missing = len(checks) != len(before_states)
    if missing:
        checks.append({"name": None, "status": "failed", "reason": "view_count_changed"})
    return checks


def _setup_scale_preflight(scale_before, view_scales_before):
    """@brief 不允许已有视图在未知图纸比例下被 SetupSheet6 隐式改为 1:1。"""
    if scale_before["numerator"] is not None and scale_before["denominator"] is not None:
        return None
    if view_scales_before:
        return {
            "status": "blocked",
            "error_code": "DRAWING_SHEET_SCALE_UNAVAILABLE",
            "message": "当前页已有视图但无法读取图纸比例；拒绝以默认 1:1 覆盖。",
        }
    return None


def _effective_projection(projection, scale_before, view_scales_before):
    """@brief 未指定投影法时保留 GetProperties2[4]；仅空白页允许明确标记的默认。"""
    if projection is not None:
        normalized = str(projection).lower()
        if normalized not in {"first_angle", "third_angle"}:
            raise ValueError("projection 必须是 first_angle 或 third_angle")
        return normalized, "requested"
    if scale_before["first_angle"] is not None:
        return ("first_angle" if scale_before["first_angle"] else "third_angle"), "sheet_properties"
    if view_scales_before:
        return None, "unavailable"
    # 新建的空 drawing 没有可保留的历史属性；此处不是静默保留，结果会注明来源。
    return "first_angle", "blank_sheet_default"


def _effective_sheet_format(paper_size, orientation, scale_before, view_scales_before):
    """@brief 确定 SetupSheet6 纸张参数；有 view 时默认保留同类别旧页方向。"""
    if orientation is not None:
        requested_orientation = str(orientation).lower()
        if requested_orientation not in {"portrait", "landscape"}:
            raise ValueError("orientation 必须是 portrait 或 landscape")
    else:
        requested_orientation = None
    has_views = bool(view_scales_before)
    same_category = scale_before.get("paper_size") == paper_size
    if has_views and requested_orientation is None and same_category:
        if all(scale_before.get(key) is not None for key in ("paper_size_code", "width_m", "height_m")):
            return {
                "paper_size_code": scale_before["paper_size_code"],
                "template_width_m": scale_before["width_m"],
                "template_height_m": scale_before["height_m"],
                "orientation": scale_before["orientation"],
                "orientation_source": "existing_sheet",
                "preserved_existing_format": True,
            }
        return None
    actual_orientation = requested_orientation or "landscape"
    width_m, height_m = PAPER_SIZES[paper_size]["width_m"], PAPER_SIZES[paper_size]["height_m"]
    paper_size_code = PAPER_SIZES[paper_size]["code"]
    if actual_orientation == "portrait":
        width_m, height_m = height_m, width_m
        # SolidWorks has a dedicated A4-vertical enum; other sizes retain their
        # standard enum and use explicit width/height rather than guessing a code.
        if paper_size == "A4":
            paper_size_code = 7
    return {
        "paper_size_code": paper_size_code,
        "template_width_m": width_m,
        "template_height_m": height_m,
        "orientation": actual_orientation,
        "orientation_source": "requested" if requested_orientation else "blank_sheet_default",
        "preserved_existing_format": False,
    }


def _sheet_format_matches(expected, actual):
    """@brief 对保留/显式请求的纸张枚举与宽高做回读核验。"""
    if expected is None or actual is None:
        return False
    if actual.get("paper_size_code") != expected["paper_size_code"]:
        return False
    return all(
        math.isclose(float(actual.get(key) or 0), float(expected[expected_key]), rel_tol=1e-12, abs_tol=1e-12)
        for key, expected_key in (("width_m", "template_width_m"), ("height_m", "template_height_m"))
    )


def _normalise_box(value):
    """@brief 将 COM Outline/GetBox 数组统一为二维边界框。"""
    if isinstance(value, dict) and {"left", "bottom", "right", "top"} <= set(value):
        try:
            x1, y1, x2, y2 = (float(value[key]) for key in ("left", "bottom", "right", "top"))
        except (TypeError, ValueError):
            return None
        return {"left": min(x1, x2), "bottom": min(y1, y2), "right": max(x1, x2), "top": max(y1, y2)}
    values = _as_sequence(value)
    try:
        if len(values) >= 6:
            x1, y1, x2, y2 = float(values[0]), float(values[1]), float(values[3]), float(values[4])
        elif len(values) >= 4:
            x1, y1, x2, y2 = map(float, values[:4])
        else:
            return None
    except (TypeError, ValueError):
        return None
    return {
        "left": min(x1, x2),
        "bottom": min(y1, y2),
        "right": max(x1, x2),
        "top": max(y1, y2),
    }


def _annotation_box(owner):
    """@brief 读取尺寸、注释或表格注解的二维包围盒。"""
    annotation = _safe_member(owner, "GetAnnotation") or owner
    return _normalise_box(_safe_member(annotation, "GetBox"))


def _view_display_dimensions(view):
    """@brief 兼容数组接口与链式接口，返回视图中的真实 DisplayDimension。"""
    dimensions = _as_sequence(_safe_member(view, "GetDisplayDimensions", default=[]))
    if dimensions:
        return dimensions
    current = _safe_member(view, "GetFirstDisplayDimension5") or _safe_member(view, "GetFirstDisplayDimension")
    while current is not None:
        dimensions.append(current)
        current = _safe_member(view, "GetNextDisplayDimension", current)
    return dimensions


def auto_arrange_drawing_dimensions(drawing_model, *, spacing_m=0.01, mode="auto_arrange") -> dict:
    """@brief 使用 SolidWorks 官方 AlignDimensions 对每个视图的尺寸自动排列。

    该接口只负责 SolidWorks 自身的尺寸布局，不提供尺寸文字包围盒，也不能证明最终
    图面无重叠。调用后仍必须导出 PDF/BMP 做视觉复核。
    """
    spacing, spacing_valid = _finite_positive(spacing_m, 0.01)
    if not spacing_valid or spacing > 0.1:
        raise ValueError("spacing_m 必须是 (0, 0.1] 米范围内的有限数值。")
    if mode not in ALIGN_DIMENSION_TYPES:
        raise ValueError(f"未知尺寸排列模式: {mode}")
    extension = _safe_member(drawing_model, "Extension")
    if extension is None or not hasattr(extension, "AlignDimensions"):
        return {
            "status": "blocked",
            "stage": "arrange",
            "method": "IModelDocExtension.AlignDimensions",
            "mode": mode,
            "spacing_m": spacing,
            "views": [],
            "selected_dimension_count": 0,
            "manual_review_required": True,
            "retryable": False,
            "error_code": "DRAWING_ALIGN_DIMENSIONS_API_UNAVAILABLE",
        }

    results = []
    total_selected = 0
    sheets = _as_sequence(_safe_member(drawing_model, "GetSheetNames", default=[]))
    for sheet_name in sheets or [""]:
        sheet = _safe_member(drawing_model, "GetSheet", sheet_name) or _safe_member(drawing_model, "GetCurrentSheet")
        for view in _as_sequence(_safe_member(sheet, "GetViews", default=[])):
            dimensions = _view_display_dimensions(view)
            _safe_member(drawing_model, "ClearSelection2", True)
            selected = 0
            visible_count = 0
            for dimension in dimensions:
                annotation = _safe_member(dimension, "GetAnnotation")
                if annotation is None:
                    continue
                try:
                    visibility_value = int(_safe_member(annotation, "Visible", default=0))
                except (TypeError, ValueError):
                    visibility_value = 0
                if ANNOTATION_VISIBILITY.get(visibility_value, "unknown") != "visible":
                    continue
                visible_count += 1
                selected_ok = _safe_member(annotation, "Select2", selected > 0, 0, default=False)
                if not selected_ok:
                    selected_ok = _safe_member(annotation, "Select", selected > 0, default=False)
                if selected_ok:
                    selected += 1
            attempted = selected >= 2
            aligned = False
            error = None
            if attempted:
                try:
                    aligned = bool(get_com_member(extension, "AlignDimensions", ALIGN_DIMENSION_TYPES[mode], spacing))
                except Exception as exc:
                    error = str(exc)
            results.append({
                "sheet": str(sheet_name),
                "view": str(_safe_member(view, "Name", default="") or ""),
                "dimension_count": len(dimensions),
                "visible_dimension_count": visible_count,
                "selected_count": selected,
                "attempted": attempted,
                "aligned": aligned,
                "error": error,
            })
            total_selected += selected
    _safe_member(drawing_model, "ClearSelection2", True)
    _safe_member(drawing_model, "ForceRebuild3", False)
    _safe_member(drawing_model, "GraphicsRedraw2")
    attempted_results = [item for item in results if item["attempted"]]
    failed_results = [item for item in attempted_results if not item["aligned"]]
    if not attempted_results:
        status = "review_required"
        error_code = "DRAWING_ALIGN_DIMENSIONS_NOT_ENOUGH_PER_VIEW"
    elif failed_results:
        status = "review_required"
        error_code = "DRAWING_ALIGN_DIMENSIONS_PARTIAL"
    else:
        status = "pass"
        error_code = None
    return {
        "status": status,
        "stage": "arrange",
        "method": "IModelDocExtension.AlignDimensions",
        "mode": mode,
        "enum_value": ALIGN_DIMENSION_TYPES[mode],
        "spacing_m": spacing,
        "views": results,
        "selected_dimension_count": total_selected,
        "aligned_view_count": sum(1 for item in results if item["aligned"]),
        "manual_review_required": True,
        "retryable": bool(failed_results),
        "error_code": error_code,
        "limitations": ["官方排列接口不返回文字包围盒；排列后仍需 PDF/BMP 目视复核。"],
    }


def _finite_positive(value, default):
    """@brief 将 COM 数值转成有限正数，否则使用保守默认值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default), False
    if not math.isfinite(number) or number <= 0:
        return float(default), False
    return number, True


def _dimension_text_evidence(display_dimension) -> dict:
    """@brief 收集尺寸文字片段；不伪造 SolidWorks 最终格式化文本。"""
    parts = []
    for index, label in ((0, "all"), (1, "prefix"), (2, "suffix"), (3, "callout_above"), (4, "callout_below")):
        value = str(_safe_member(display_dimension, "GetText", index, default="") or "")
        if value:
            parts.append({"index": index, "kind": label, "text": value})
    all_text = next((item["text"] for item in parts if item["kind"] == "all"), "")
    if all_text:
        lines = all_text.splitlines() or [all_text]
        source = "display_dimension_get_text_all"
        rendered_value_available = True
    else:
        explicit = [item["text"] for item in parts if item["kind"] != "all"]
        # GetText() 在 SW2026 常只返回用户前后缀，不返回格式化后的主尺寸值。
        # 用 8 个等宽字符作为主值占位，避免把空字符串估成零宽度。
        lines = ["".join(explicit) + "0000.000"]
        source = "explicit_parts_plus_conservative_value_placeholder" if explicit else "conservative_value_placeholder"
        rendered_value_available = False
    return {
        "parts": parts,
        "estimation_lines": lines,
        "source": source,
        "rendered_value_available": rendered_value_available,
    }


def estimate_dimension_text_box(display_dimension, *, padding_m=None) -> dict:
    """@brief 用 IAnnotation 锚点和 ITextFormat 保守估算尺寸文字边界。

    SW2026 没有尺寸文字的原生 bounding-box API。本函数输出任意旋转文字都能
    被覆盖的轴对齐外接方框，并明确标记 ``source=estimated``；不能作为原生几何证据。
    """
    annotation = _safe_member(display_dimension, "GetAnnotation")
    position = _as_sequence(_safe_member(annotation, "GetPosition", default=[]))
    try:
        x, y = float(position[0]), float(position[1])
        position_available = math.isfinite(x) and math.isfinite(y)
    except (IndexError, TypeError, ValueError):
        x = y = 0.0
        position_available = False

    text_format = _safe_member(annotation, "GetTextFormat", 0)
    format_source = "IAnnotation.GetTextFormat(0)"
    if text_format is None:
        text_format = _safe_member(display_dimension, "GetTextFormat")
        format_source = "IDisplayDimension.GetTextFormat()"
    char_height, char_height_available = _finite_positive(
        _safe_member(text_format, "CharHeight"),
        0.0035,
    )
    width_factor, width_factor_available = _finite_positive(
        _safe_member(text_format, "WidthFactor"),
        1.0,
    )
    spacing_factor, spacing_factor_available = _finite_positive(
        _safe_member(text_format, "CharSpacingFactor"),
        1.0,
    )
    text = _dimension_text_evidence(display_dimension)
    lines = text["estimation_lines"] or ["0000.000"]

    def line_units(line):
        """@brief 估算单行字宽；中文和全角字符按一个字高计。"""
        units = 0.0
        for character in line or " ":
            units += 1.0 if ord(character) > 0xFF else 0.62
        return max(units, 0.62)

    max_units = max(line_units(line) for line in lines)
    line_count = max(1, len(lines))
    estimated_width = max_units * char_height * width_factor * spacing_factor
    estimated_height = line_count * char_height * 1.25
    padding = max(0.001, char_height * 0.4) if padding_m is None else max(0.0, float(padding_m))
    # 尺寸文字角度未由 API 暴露；使用矩形对角线作为任意旋转下的方形包络。
    half_extent = math.hypot(estimated_width, estimated_height) / 2.0 + padding
    box = None
    if position_available:
        box = {
            "left": x - half_extent,
            "bottom": y - half_extent,
            "right": x + half_extent,
            "top": y + half_extent,
        }

    available_fields = sum((position_available, char_height_available, width_factor_available, spacing_factor_available))
    if position_available and available_fields == 4 and text["rendered_value_available"]:
        confidence = "medium"
    elif position_available:
        confidence = "low"
    else:
        confidence = "unavailable"
    return {
        "box": box,
        "source": "estimated",
        "confidence": confidence,
        "method": "annotation_position_text_format_arbitrary_rotation_envelope",
        "native_bounding_box_available": False,
        "position_m": [x, y] if position_available else None,
        "text_evidence": text,
        "text_format": {
            "source": format_source if text_format is not None else "conservative_defaults",
            "char_height_m": char_height,
            "width_factor": width_factor,
            "char_spacing_factor": spacing_factor,
            "char_height_available": char_height_available,
            "width_factor_available": width_factor_available,
            "char_spacing_factor_available": spacing_factor_available,
        },
        "estimated_unrotated_size_m": {"width": estimated_width, "height": estimated_height},
        "padding_m": padding,
        "orientation_assumption": "unknown_angle_conservative_square_envelope",
        "limitations": [
            "SolidWorks 2026 IAnnotation 不提供尺寸文字原生包围盒",
            "主尺寸格式化值可能不由 GetText 返回，缺失时使用保守占位宽度",
            "估算只能用于碰撞风险筛查，最终交付仍需 PDF/BMP 目视复核",
        ],
    }


def select_drawing_template(candidates, *, paper_size="A3", require_gbt=True) -> dict:
    """@brief 从本机候选中选择图幅匹配且具有 GB/T 标识的图框模板。

    仅把文件名和扩展名作为候选证据，不把命名匹配宣称为模板内容已合规。
    """
    paper_size = str(paper_size).upper()
    if paper_size not in PAPER_SIZES:
        raise ValueError(f"不支持的图幅: {paper_size}")
    inspected = []
    for raw_path in candidates or []:
        path = Path(raw_path).expanduser().resolve()
        name = path.stem.casefold()
        compact_name = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", name)
        is_format = path.suffix.casefold() == ".slddrt"
        paper_match = paper_size.casefold() in name
        gbt_match = (
            "gbt" in compact_name
            or "国标" in compact_name
            or bool(re.search(r"(?:^|[^a-z0-9])gb(?:t)?(?:[^a-z0-9]|$)", name, re.IGNORECASE))
        )
        path_text = str(path).casefold()
        localized_candidate = any(token in path_text for token in ("chinese-simplified", "chinese_simplified", "简体中文"))
        score = int(path.is_file()) * 8 + int(is_format) * 4 + int(paper_match) * 2 + int(gbt_match) + int(localized_candidate) * 2
        inspected.append({
            "path": str(path),
            "exists": path.is_file(),
            "is_sheet_format": is_format,
            "paper_match": paper_match,
            "gbt_candidate": gbt_match,
            "localized_candidate": localized_candidate,
            "score": score,
        })
    eligible = [item for item in inspected if item["exists"] and item["is_sheet_format"] and item["paper_match"]]
    if require_gbt:
        eligible = [item for item in eligible if item["gbt_candidate"]]
    selected = max(eligible, key=lambda item: (item["score"], item["path"].casefold()), default=None)
    return {
        "status": "pass" if selected else "blocked",
        "paper_size": paper_size,
        "selected": selected["path"] if selected else None,
        "candidates": inspected,
        "gbt_content_verified": False,
        "manual_review_required": True,
        "error_code": None if selected else "DRAWING_GBT_TEMPLATE_MISSING" if require_gbt else "DRAWING_TEMPLATE_MISSING",
    }


def plan_standard_view_layout(
    model_size_m,
    *,
    paper_size="A3",
    projection="third_angle",
    requested_scale=None,
    sheet_dimensions_m=None,
    margin_m=0.012,
    gap_m=0.018,
    title_block_width_m=0.180,
    title_block_height_m=0.055,
) -> dict:
    """@brief 为三视图计算不会侵入标题栏的自适应或指定比例布局。"""
    paper_size = str(paper_size).upper()
    projection = str(projection).lower()
    if projection not in {"first_angle", "third_angle"}:
        raise ValueError("projection 必须是 first_angle 或 third_angle")
    spec = PAPER_SIZES.get(paper_size)
    if spec is None:
        raise ValueError(f"不支持的图幅: {paper_size}")
    try:
        width, height, depth = (float(item) for item in model_size_m)
    except (TypeError, ValueError) as exc:
        raise ValueError("model_size_m 必须包含三个米制外形尺寸") from exc
    if min(width, height, depth) <= 0 or not all(math.isfinite(item) for item in (width, height, depth)):
        raise ValueError("模型外形尺寸必须为有限正数")

    if sheet_dimensions_m is None:
        sheet_width, sheet_height = spec["width_m"], spec["height_m"]
        sheet_size_source = "paper_size_fallback"
    elif isinstance(sheet_dimensions_m, dict):
        sheet_width = sheet_dimensions_m.get("width_m")
        sheet_height = sheet_dimensions_m.get("height_m")
        sheet_size_source = sheet_dimensions_m.get("source", "actual")
    else:
        try:
            sheet_width, sheet_height = sheet_dimensions_m
        except (TypeError, ValueError) as exc:
            raise ValueError("sheet_dimensions_m 必须包含宽度和高度") from exc
        sheet_size_source = "actual"
    try:
        sheet_width, sheet_height = float(sheet_width), float(sheet_height)
    except (TypeError, ValueError) as exc:
        raise ValueError("sheet_dimensions_m 必须包含有效宽高") from exc
    if not all(math.isfinite(item) and item > 0 for item in (sheet_width, sheet_height)):
        raise ValueError("sheet_dimensions_m 必须包含有限正数宽高")
    working_left = margin_m
    working_bottom = margin_m + title_block_height_m + gap_m
    working_right = sheet_width - margin_m
    working_top = sheet_height - margin_m
    available_width = working_right - working_left
    available_height = working_top - working_bottom
    raw_scale = min(
        (available_width - gap_m) / (width + depth),
        (available_height - gap_m) / (height + depth),
    )
    if raw_scale <= 0:
        return {
            "status": "blocked",
            "paper_size": paper_size,
            "sheet": {"width_m": sheet_width, "height_m": sheet_height},
            "sheet_size_source": sheet_size_source,
            "views": [],
            "manual_review_required": True,
            "retryable": True,
            "error_code": "DRAWING_LAYOUT_WORKING_AREA_INVALID",
        }
    if requested_scale is None:
        scale = next((item for item in STANDARD_DRAWING_SCALES if item <= raw_scale + 1e-12), raw_scale)
    else:
        try:
            if isinstance(requested_scale, str):
                numerator_text, denominator_text = requested_scale.split(":", 1)
                scale = float(numerator_text.strip()) / float(denominator_text.strip())
            else:
                scale = float(requested_scale)
        except (TypeError, ValueError, ZeroDivisionError):
            return {
                "status": "blocked",
                "paper_size": paper_size,
                "views": [],
                "manual_review_required": True,
                "retryable": False,
                "error_code": "DRAWING_SCALE_INVALID",
            }
        if scale <= 0 or not math.isfinite(scale):
            return {
                "status": "blocked",
                "paper_size": paper_size,
                "views": [],
                "manual_review_required": True,
                "retryable": False,
                "error_code": "DRAWING_SCALE_INVALID",
            }
        if scale > raw_scale + 1e-12:
            return {
                "status": "blocked",
                "paper_size": paper_size,
                "requested_scale": requested_scale,
                "maximum_fit_scale": raw_scale,
                "views": [],
                "manual_review_required": True,
                "retryable": True,
                "error_code": "DRAWING_SCALE_DOES_NOT_FIT",
            }

    front_w, front_h = width * scale, height * scale
    top_w, top_h = width * scale, depth * scale
    right_w, right_h = depth * scale, height * scale
    layout_w = front_w + gap_m + right_w
    layout_h = front_h + gap_m + top_h
    origin_x = working_left + max(0.0, (available_width - layout_w) / 2.0)
    origin_y = working_bottom + max(0.0, (available_height - layout_h) / 2.0)

    def view_record(name, left, bottom, view_width, view_height):
        """@brief 创建单个视图的中心点与边界框记录。"""
        return {
            "name": name,
            "center": [left + view_width / 2.0, bottom + view_height / 2.0],
            "box": {"left": left, "bottom": bottom, "right": left + view_width, "top": bottom + view_height},
        }

    if projection == "first_angle":
        # GB/T 第一角投影：俯视在主视下方，右视在主视左侧。
        views = [
            view_record("*Front", origin_x + right_w + gap_m, origin_y + top_h + gap_m, front_w, front_h),
            view_record("*Top", origin_x + right_w + gap_m, origin_y, top_w, top_h),
            view_record("*Right", origin_x, origin_y + top_h + gap_m, right_w, right_h),
        ]
    else:
        views = [
            view_record("*Front", origin_x, origin_y, front_w, front_h),
            view_record("*Top", origin_x, origin_y + front_h + gap_m, top_w, top_h),
            view_record("*Right", origin_x + front_w + gap_m, origin_y, right_w, right_h),
        ]
    title_box = {
        "left": sheet_width - margin_m - title_block_width_m,
        "bottom": margin_m,
        "right": sheet_width - margin_m,
        "top": margin_m + title_block_height_m,
    }
    return {
        "status": "pass",
        "paper_size": paper_size,
        "projection": projection,
        "gap_m": gap_m,
        "sheet": {"width_m": sheet_width, "height_m": sheet_height},
        "sheet_size_source": sheet_size_source,
        "working_area": {"left": working_left, "bottom": working_bottom, "right": working_right, "top": working_top},
        "title_block_box": title_box,
        "scale": scale,
        "scale_ratio": list(_scale_ratio(scale)),
        "views": views,
        "manual_review_required": True,
    }


def _scale_ratio(scale):
    """@brief 将浮点比例转换为 SolidWorks 可接受的整数比。"""
    ratio = Fraction(float(scale)).limit_denominator(1000)
    return ratio.numerator, ratio.denominator


def _apply_view_layout(view, item, numerator, denominator) -> dict:
    """@brief 将已创建视图移动到规划中心并设置独立比例。"""
    center = [float(item["center"][0]), float(item["center"][1])]
    if hasattr(view, "UseParentScale"):
        view.UseParentScale = False
    if hasattr(view, "PositionLocked"):
        view.PositionLocked = False
    view.ScaleRatio = (int(numerator), int(denominator))
    position = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, center)
    method = getattr(view, "SetViewPosition", None)
    if method is not None:
        moved = bool(get_com_member(view, "SetViewPosition", position, False))
        if not moved:
            raise RuntimeError(f"SetViewPosition 返回失败: {center}")
    else:
        view.Position = position
    actual_position = _view_position(view)
    position_verified = bool(
        actual_position
        and abs(actual_position[0] - center[0]) <= 1e-5
        and abs(actual_position[1] - center[1]) <= 1e-5
    )
    if not position_verified:
        raise RuntimeError(f"视图位置回读不一致: requested={center}, actual={actual_position}")
    return {
        "name": item["name"],
        "actual_name": str(_safe_member(view, "Name", default="")),
        "orientation": str(_safe_member(view, "GetOrientationName", default="")),
        "center": center,
        "actual_center": list(actual_position),
        "position_verified": True,
        "scale_ratio": [int(numerator), int(denominator)],
    }


def _normalise_orientation(value):
    """@brief 将 SolidWorks 标准视图方向统一为 front/top/right 键。"""
    compact = re.sub(r"[^a-z\u4e00-\u9fff]", "", str(value or "").casefold())
    aliases = {
        "front": "front",
        "top": "top",
        "right": "right",
        "frontview": "front",
        "topview": "top",
        "rightview": "right",
        "前视": "front",
        "主视": "front",
        "上视": "top",
        "俯视": "top",
        "右视": "right",
    }
    return aliases.get(compact)


def _view_position(view):
    """@brief 读取工程图视图中心位置。"""
    values = _as_sequence(_safe_member(view, "Position", default=[]))
    try:
        if len(values) < 2:
            return None
        return float(values[0]), float(values[1])
    except (IndexError, TypeError, ValueError):
        return None


def _set_view_center(view, center) -> list[float]:
    """@brief 移动视图并回读中心点，供真实包围盒二次排布复用。"""
    target = [float(center[0]), float(center[1])]
    position = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, target)
    method = getattr(view, "SetViewPosition", None)
    if method is not None:
        moved = bool(get_com_member(view, "SetViewPosition", position, False))
        if not moved:
            raise RuntimeError(f"SetViewPosition 返回失败: {target}")
    else:
        view.Position = position
    actual = _view_position(view)
    if actual is None or abs(actual[0] - target[0]) > 1e-5 or abs(actual[1] - target[1]) > 1e-5:
        raise RuntimeError(f"视图位置回读不一致: requested={target}, actual={actual}")
    return [float(actual[0]), float(actual[1])]


def _refine_standard_view_spacing(view_objects, view_records, projection, gap_m=0.018) -> dict:
    """@brief 根据 SolidWorks 实际视图边界消除三视图之间的确认重叠。"""
    required = {"front", "top", "right"}
    if set(view_objects) < required:
        return {"status": "review_required", "method": "native_outline", "adjustments": [], "error_code": "DRAWING_VIEW_OUTLINES_INCOMPLETE"}
    boxes = {key: _normalise_box(_safe_member(view, "GetOutline")) for key, view in view_objects.items()}
    if any(box is None for box in boxes.values()):
        return {"status": "review_required", "method": "native_outline", "adjustments": [], "error_code": "DRAWING_VIEW_OUTLINES_INCOMPLETE"}
    try:
        gap = max(0.001, float(gap_m))
    except (TypeError, ValueError):
        gap = 0.018
    records = {item["name"]: item for item in view_records}
    names = {"front": "*Front", "top": "*Top", "right": "*Right"}
    adjustments = []

    def move_if_needed(axis, orientation, target_value):
        current_box = boxes[orientation]
        current_center = _view_position(view_objects[orientation])
        if current_center is None:
            return
        delta = float(target_value) - current_center[axis]
        if abs(delta) <= 1e-7:
            return
        new_center = list(current_center)
        new_center[axis] = float(target_value)
        actual_center = _set_view_center(view_objects[orientation], new_center)
        moved_box = _normalise_box(_safe_member(view_objects[orientation], "GetOutline"))
        boxes[orientation] = moved_box or current_box
        record = records.get(names[orientation])
        if record is not None:
            record["center"] = list(actual_center)
            record["actual_center"] = list(actual_center)
            record["layout_refined_from_native_outline"] = True
        adjustments.append({"view": names[orientation], "axis": "x" if axis == 0 else "y", "delta_m": delta, "center": list(actual_center)})

    front = boxes["front"]
    right = boxes["right"]
    top = boxes["top"]
    right_width = right["right"] - right["left"]
    top_height = top["top"] - top["bottom"]
    if projection == "first_angle":
        right_target_x = front["left"] - gap - right_width / 2.0
        top_target_y = front["bottom"] - gap - top_height / 2.0
    else:
        right_target_x = front["right"] + gap + right_width / 2.0
        top_target_y = front["top"] + gap + top_height / 2.0
    move_if_needed(0, "right", right_target_x)
    move_if_needed(1, "top", top_target_y)
    return {
        "status": "pass",
        "method": "native_outline",
        "projection": projection,
        "gap_m": gap,
        "adjustments": adjustments,
        "error_code": None,
        "manual_review_required": True,
    }


def _collect_drawing_views(drawing_model):
    """@brief 同时尝试 Sheet.GetViews 与 GetFirstView 链读取真实模型视图。"""
    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    views = _as_sequence(_safe_member(sheet, "GetViews", default=[]))
    if len(views) >= 3:
        return views
    traversed = []
    sheet_view = _safe_member(drawing_model, "GetFirstView")
    current = _safe_member(sheet_view, "GetNextView")
    guard = 0
    while current is not None and guard < 1000:
        traversed.append(current)
        current = _safe_member(current, "GetNextView")
        guard += 1
    return traversed if len(traversed) >= len(views) else views


def _map_native_standard_views(views):
    """@brief 依据方向名、基准视图关系和原始位置映射第三角三视图。"""
    by_orientation = {}
    diagnostics = []
    for view in views:
        orientation_name = str(_safe_member(view, "GetOrientationName", default=""))
        orientation = _normalise_orientation(orientation_name)
        base_view = _safe_member(view, "GetBaseView")
        position = _view_position(view)
        diagnostics.append({
            "name": str(_safe_member(view, "Name", default="")),
            "orientation": orientation_name,
            "position": list(position) if position else None,
            "has_base_view": base_view is not None,
            "referenced_model": str(_safe_member(view, "GetReferencedModelName", default="")),
        })
        if orientation and orientation not in by_orientation:
            by_orientation[orientation] = view
    if set(by_orientation) >= {"front", "top", "right"}:
        return by_orientation, diagnostics, "orientation_name"

    positioned = [(view, _view_position(view), _safe_member(view, "GetBaseView")) for view in views]
    positioned = [item for item in positioned if item[1] is not None]
    if len(positioned) >= 3:
        base_candidates = [item for item in positioned if item[2] is None]
        front = min(base_candidates or positioned, key=lambda item: item[1][0] + item[1][1])
        remaining = [item for item in positioned if item[0] is not front[0]]
        top = max(remaining, key=lambda item: item[1][1] - front[1][1])
        right_candidates = [item for item in remaining if item[0] is not top[0]]
        if right_candidates:
            right = max(right_candidates, key=lambda item: item[1][0] - front[1][0])
            return {"front": front[0], "top": top[0], "right": right[0]}, diagnostics, "base_and_position"
    return by_orientation, diagnostics, "unresolved"


def _map_standard_views_from_sheet_views(sheet_views):
    """@brief 从整张图纸视图中筛出并映射前、俯、右三个标准视图。"""
    oriented_fronts = [
        view for view in sheet_views
        if _normalise_orientation(_safe_member(view, "GetOrientationName", default="")) == "front"
    ]
    base_candidates = oriented_fronts or [
        view for view in sheet_views
        if _safe_member(view, "Type", default=None) == 7 and _safe_member(view, "GetBaseView") is None
    ]
    standard_candidates = [*base_candidates, *[
        view for view in sheet_views if _safe_member(view, "Type", default=None) == 4
    ]]
    return _map_native_standard_views(standard_candidates)


def create_adaptive_standard_views(drawing_model, part_path, layout) -> dict:
    """@brief 按预先计算的布局创建前、俯、右三个真实工程图视图。"""
    created = []
    view_objects = {}
    numerator, denominator = layout.get("scale_ratio") or _scale_ratio(layout["scale"])
    for item in layout.get("views", []):
        center = item["center"]
        view = drawing_model.CreateDrawViewFromModelView3(part_path, item["name"], center[0], center[1], 0)
        if view is None:
            if not created:
                projection = str(layout.get("projection", "third_angle")).lower()
                # 部分 SolidWorks 版本（包括当前 SW2026 COM 代理）没有
                # CreateFirstAngleViews2。先用可用的原生三视图 API 建立视图，
                # 再按 DrawingSpec 的第一角/第三角布局重新定位并回读位置。
                method_candidates = (
                    ("CreateFirstAngleViews2", "Create3rdAngleViews2")
                    if projection == "first_angle"
                    else ("Create3rdAngleViews2",)
                )
                native_created = False
                method_name = None
                for candidate in method_candidates:
                    if bool(_safe_member(drawing_model, candidate, str(part_path), default=False)):
                        native_created = True
                        method_name = candidate
                        break
                if native_created:
                    _safe_member(drawing_model, "ForceRebuild3", False)
                    _safe_member(drawing_model, "GraphicsRedraw2")
                    native_views = _collect_drawing_views(drawing_model)
                    by_orientation, diagnostics, mapping_method = _map_native_standard_views(native_views)
                    expected = {"front": "*Front", "top": "*Top", "right": "*Right"}
                    if set(by_orientation) >= set(expected):
                        layout_by_name = {entry["name"]: entry for entry in layout.get("views", [])}
                        try:
                            for orientation in ("front", "top", "right"):
                                view_objects[orientation] = by_orientation[orientation]
                                created.append(
                                    _apply_view_layout(
                                        by_orientation[orientation],
                                        layout_by_name[expected[orientation]],
                                        numerator,
                                        denominator,
                                    )
                                )
                        except Exception as exc:
                            return {
                                "status": "failed",
                                "stage": "layout",
                                "backend": f"native_{projection}",
                                "views": created,
                                "native_view_diagnostics": diagnostics,
                                "retryable": True,
                                "error_code": "DRAWING_VIEW_POSITION_FAILED",
                                "error": str(exc),
                            }
                        refinement = _refine_standard_view_spacing(
                            view_objects,
                            created,
                            projection,
                            layout.get("gap_m", 0.018),
                        )
                        backend = (
                            "native_3rd_angle"
                            if projection == "third_angle"
                            else "native_first_angle"
                            if method_name == "CreateFirstAngleViews2"
                            else "native_first_angle_via_3rd_angle"
                        )
                        return {
                            "status": "pass",
                            "stage": "create",
                            "backend": backend,
                            "creation_method": method_name,
                            "mapping_method": mapping_method,
                            "native_view_diagnostics": diagnostics,
                            "layout_refinement": refinement,
                            "views": created,
                            "view_count": len(created),
                            "retryable": False,
                            "error_code": None,
                            "manual_review_required": True,
                        }
                    return {
                        "status": "failed",
                        "stage": "map",
                        "backend": f"native_{projection}",
                        "views": [],
                        "orientations_found": sorted(by_orientation),
                        "mapping_method": mapping_method,
                        "native_view_diagnostics": diagnostics,
                        "retryable": True,
                        "error_code": "DRAWING_VIEW_ORIENTATION_MAP_FAILED",
                    }
            return {
                "status": "failed",
                "stage": "create",
                "views": created,
                "retryable": True,
                "error_code": "DRAWING_VIEW_CREATE_FAILED",
            }
        try:
            orientation = _normalise_orientation(item["name"])
            if orientation:
                view_objects[orientation] = view
            created.append(_apply_view_layout(view, item, numerator, denominator))
        except Exception as exc:
            return {
                "status": "failed",
                "stage": "layout",
                "backend": "individual_model_views",
                "views": created,
                "retryable": True,
                "error_code": "DRAWING_VIEW_POSITION_FAILED",
                "error": str(exc),
            }
    refinement = _refine_standard_view_spacing(
        view_objects,
        created,
        str(layout.get("projection", "third_angle")).lower(),
        layout.get("gap_m", 0.018),
    )
    return {
        "status": "pass",
        "stage": "create",
        "backend": "individual_model_views",
        "views": created,
        "layout_refinement": refinement,
        "view_count": len(created),
        "retryable": False,
        "error_code": None,
        "manual_review_required": True,
    }


def create_standard_views(drawing_model, part_path):
    """
    创建标准三视图（第三角投影法）

    参数:
        drawing_model: IDrawingDoc
        part_path: 零件文件路径
    """
    return drawing_model.Create3rdAngleViews2(part_path)


def create_standard_views_with_projection(drawing_model, part_path, projection="first_angle"):
    """@brief 按指定投影法创建原生三视图并返回明确失败状态。"""
    projection = str(projection).lower()
    if projection not in {"first_angle", "third_angle"}:
        raise ValueError("projection 必须是 first_angle 或 third_angle")
    method_name = "CreateFirstAngleViews2" if projection == "first_angle" else "Create3rdAngleViews2"
    method = getattr(drawing_model, method_name, None)
    if method is None:
        return {
            "status": "blocked",
            "projection": projection,
            "method": method_name,
            "error_code": "DRAWING_PROJECTION_API_UNAVAILABLE",
            "manual_review_required": True,
        }
    created = bool(method(part_path))
    return {
        "status": "pass" if created else "failed",
        "projection": projection,
        "method": method_name,
        "created": created,
        "error_code": None if created else "DRAWING_STANDARD_VIEWS_CREATE_FAILED",
        "manual_review_required": True,
    }


def add_view(drawing_model, part_path, view_name, x, y, scale=None):
    """
    添加单个视图

    参数:
        view_name: 视图方向名称
            "*Front", "*Back", "*Top", "*Bottom",
            "*Left", "*Right", "*Isometric",
            "*Trimetric", "*Dimetric"
        x, y: 视图放置位置（米）
        scale: 视图比例（如 0.5 表示 1:2），None 使用图纸默认
    """
    view = drawing_model.CreateDrawViewFromModelView3(
        part_path, view_name, x, y, 0
    )
    if view and scale:
        view.ScaleRatio = (1.0, 1.0 / scale)
    return view


def add_section_view(drawing_model, x, y):
    """在当前选择的剖切线位置创建剖视图"""
    return drawing_model.CreateSectionViewAt5(x, y, 0, "", 0, None, 0)


def add_detail_view(drawing_model, x, y, scale=2.0):
    """创建局部放大视图"""
    return drawing_model.CreateDetailViewAt4(x, y, 0, 0, scale, 0, "")


def insert_dimensions(drawing_model, view=None):
    """
    自动标注尺寸（模型项目）

    参数:
        view: 目标视图对象，None 则标注所有视图
    """
    # SolidWorks 2024 新增 InsertModelAnnotations4，返回实际插入的 IAnnotation
    # 数组；旧版/动态代理仍可能只有 InsertModelAnnotations3。优先使用 4，
    # 再回退到 3，避免把“方法返回 True”误当成已经插入尺寸。
    # SW2024 swInsertAnnotation_e：32768=标记为工程图的模型尺寸。
    # 8 是通用尺寸类型、524288 是未标记尺寸；本机 SW2024 对二者组合静默
    # 返回 None，而 32768 会返回真实 IAnnotation 数组。
    args4 = (
        0,          # swImportModelItemsFromEntireModel
        32768,      # swInsertDimensionsMarkedForDrawing
        True, False, False, False,
        False, False,
    )
    args3 = args4[:6]
    # 官方 API 示例要求先激活一个工程图视图；未激活时 SW2024 会静默返回空结果。
    try:
        sheet = _safe_member(drawing_model, "GetCurrentSheet")
        views = _as_sequence(_safe_member(sheet, "GetViews", default=[]))
        if views:
            # 三视图第一个通常是前视图；通过对象 Name 读取失败时使用已知顺序，
            # 但仍要求 SelectByID2/ActivateView 真正返回成功。
            first_view = views[0]
            name = _safe_member(first_view, "Name", default="")
            if name:
                extension = getattr(drawing_model, "Extension", None)
                if extension is not None:
                    get_com_member(
                        extension, "SelectByID2",
                        name, "DRAWINGVIEW", 0, 0, 0, False, 0,
                        create_empty_dispatch_variant(), 0,
                    )
                get_com_member(drawing_model, "ActivateView", name)
    except Exception:
        # 激活失败仍继续尝试，让结构复核决定是否有真实尺寸证据。
        pass
    owners = (drawing_model, getattr(drawing_model, "Extension", None))
    for owner in owners:
        if owner is None:
            continue
        try:
            method4 = getattr(owner, "InsertModelAnnotations4", None)
            if method4 is not None:
                inserted = get_com_member(owner, "InsertModelAnnotations4", *args4)
                if inserted is not None:
                    return inserted
            method3 = getattr(owner, "InsertModelAnnotations3", None)
            if method3 is not None:
                return get_com_member(owner, "InsertModelAnnotations3", *args3)
        except (AttributeError, TypeError, pythoncom.com_error):
            continue
    # 新建三视图后 SW2024 有时尚未建立可导入的视图缓存；强制重建后仅重试
    # 一次。使用“标记为工程图”与消重语义，不会靠重复调用制造重复尺寸。
    try:
        get_com_member(drawing_model, "ForceRebuild3", False)
        get_com_member(drawing_model, "GraphicsRedraw2")
        for owner in owners:
            if owner is not None and getattr(owner, "InsertModelAnnotations4", None) is not None:
                inserted = get_com_member(owner, "InsertModelAnnotations4", *args4)
                if inserted is not None:
                    return inserted
    except (AttributeError, TypeError, pythoncom.com_error):
        pass
    # 调用方可以继续做结构复核，但必须把缺少真实尺寸证据显示为 warning。
    return False


def _note_text(note):
    """@brief 读取 INote 的实际文字，兼容动态 COM 未公开成员名称的情况。"""
    text = _safe_member(note, "GetText")
    if isinstance(text, str):
        return text
    ole_object = getattr(note, "_oleobj_", None)
    if ole_object is None:
        return ""
    try:
        # SW2026 sldworks.tlb: INote.GetText 的 DISPID 为 2，返回 BSTR。
        return str(ole_object.InvokeTypes(2, 0, 1, (8, 0), ()) or "")
    except Exception:
        return ""


def _normalise_note_text(value):
    """@brief 按逻辑行比较 Note 文本，避免 CRLF/LF COM 往返造成假失败。"""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _note_annotation(note):
    """@brief 获取 INote 对应的 IAnnotation；动态 COM 不公开该成员时按类型库调用。"""
    annotation = _safe_member(note, "GetAnnotation")
    if annotation is not None:
        return annotation
    ole_object = getattr(note, "_oleobj_", None)
    if ole_object is None:
        return None
    try:
        # SW2026 sldworks.tlb: INote.GetAnnotation 的 DISPID 为 85。
        from win32com.client.dynamic import Dispatch

        return Dispatch(ole_object.InvokeTypes(85, 0, 1, (9, 0), ()))
    except Exception:
        return None


def _set_annotation_position(annotation, x, y, z=0.0):
    """@brief 设置 IAnnotation 的图纸坐标，返回 SolidWorks 的布尔结果。"""
    try:
        result = get_com_member(annotation, "SetPosition2", x, y, z)
        if isinstance(result, bool):
            return result
    except Exception:
        pass
    ole_object = getattr(annotation, "_oleobj_", None)
    if ole_object is None:
        return False
    try:
        # SW2026 sldworks.tlb: IAnnotation.SetPosition2 的 DISPID 为 91。
        return bool(ole_object.InvokeTypes(91, 0, 1, (11, 0), ((5, 1), (5, 1), (5, 1)), x, y, z))
    except Exception:
        return False


def _annotation_position(annotation):
    """@brief 回读 IAnnotation 图纸坐标，坐标不可用时返回 None。"""
    position = _safe_member(annotation, "GetPosition")
    values = _as_sequence(position)
    try:
        x, y = float(values[0]), float(values[1])
    except (IndexError, TypeError, ValueError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return [x, y, float(values[2]) if len(values) > 2 else 0.0]


def _note_box(note, annotation):
    """@brief 优先读取 INote 的图纸空间范围；没有时退回通用注释边界。"""
    extent = _safe_member(note, "GetExtent")
    if extent is None:
        ole_object = getattr(note, "_oleobj_", None)
        if ole_object is not None:
            try:
                # SW2026 sldworks.tlb: INote.GetExtent 的 DISPID 为 22。
                extent = ole_object.InvokeTypes(22, 0, 1, (12, 0), ())
            except Exception:
                extent = None
    return _normalise_box(extent) or _annotation_box(annotation)


def _note_evidence(note):
    """@brief 收集注释的文字、位置和边界，作为工程图结构证据。"""
    annotation = _note_annotation(note)
    return {
        "text": _note_text(note),
        "position_m": _annotation_position(annotation),
        "box": _note_box(note, annotation),
    }


def _leave_template_edit_mode(drawing_model):
    """@brief 普通图纸 Note 不能落在 sheet format；必要时先切回 EditSheet。"""
    editing_sheet = _safe_member(drawing_model, "GetEditSheet", default=None)
    if editing_sheet is False:
        _safe_member(drawing_model, "EditSheet")
        return _safe_member(drawing_model, "GetEditSheet", default=None) is True
    return editing_sheet is not False


def _disable_annotation_leader(annotation):
    """@brief 强制普通 Note 为无引线，并回读可用的样式证据。"""
    if annotation is None:
        return None
    result = _safe_member(annotation, "SetLeader3", 0, 0, False, False, False, False, default=None)
    leader_style = _safe_member(annotation, "GetLeaderStyle", default=None)
    if leader_style is not None:
        try:
            return int(leader_style) == 0
        except (TypeError, ValueError):
            return False
    if result is not None:
        # Some generated COM wrappers expose SetLeader3's success Boolean while
        # others expose a zero HRESULT.  A void result deliberately falls through
        # to ``None`` above and cannot certify an absent leader.
        if isinstance(result, bool):
            return result
        try:
            return int(result) == 0
        except (TypeError, ValueError):
            return None
    return None


def update_notes(note_updates):
    """@brief 用 INote.SetText 更新已解析的 Note，并逐项回读验证。

    ``note_updates`` 的每项为 ``(note, text)`` 或 ``{"note": note,
    "text": text}``。函数不按显示值或文本去重，以免合并不同位置的同文 Note。
    """
    results = []
    for item in _as_sequence(note_updates):
        if isinstance(item, dict):
            note, requested_text = item.get("note"), item.get("text")
        else:
            try:
                note, requested_text = item
            except (TypeError, ValueError):
                results.append({"updated": False, "status": "failed", "error_code": "DRAWING_NOTE_UPDATE_INPUT_INVALID"})
                continue
        expected_text = str(requested_text)
        if note is None:
            results.append({"text": expected_text, "updated": False, "status": "failed", "error_code": "DRAWING_NOTE_UPDATE_INPUT_INVALID"})
            continue
        try:
            set_result = get_com_member(note, "SetText", expected_text)
            wrote = set_result is not False
        except Exception:
            wrote = False
        actual_text = _note_text(note)
        verified = bool(wrote and _normalise_note_text(actual_text) == _normalise_note_text(expected_text))
        results.append({
            "text": expected_text,
            "text_evidence": actual_text,
            "updated": wrote,
            "verified": verified,
            "status": "pass" if verified else "failed",
            "error_code": None if verified else "DRAWING_NOTE_TEXT_UPDATE_UNVERIFIED",
        })
    return {
        "status": "pass" if results and all(item["verified"] for item in results) else "failed",
        "updates": results,
        "updated_count": sum(1 for item in results if item.get("updated")),
    }


def add_note(drawing_model, x, y, text):
    """@brief 创建并验证工程图文字注释。

    @param drawing_model SolidWorks IModelDoc2 工程图对象。
    @param x 注释锚点 X 坐标（米）。
    @param y 注释锚点 Y 坐标（米）。
    @param text 需要写入图纸的非空文字。
    @return 包含 COM 回读文字、坐标和边界的可审计结果。
    """
    expected_text = str(text)
    if not expected_text.strip() or not all(math.isfinite(float(value)) for value in (x, y)):
        return {
            "text": expected_text,
            "created": False,
            "status": "failed",
            "error_code": "DRAWING_NOTE_INPUT_INVALID",
        }
    if not _leave_template_edit_mode(drawing_model):
        return {
            "text": expected_text,
            "created": False,
            "status": "failed",
            "error_code": "DRAWING_NOTE_EDIT_SHEET_REQUIRED",
        }
    # InsertNote 从当前选择集取 leader attachment；必须先清空。
    _safe_member(drawing_model, "ClearSelection2", True)
    note = _safe_member(drawing_model, "InsertNote", expected_text)
    if note is None:
        return {
            "text": expected_text,
            "created": False,
            "status": "failed",
            "error_code": "DRAWING_NOTE_INSERT_FAILED",
        }
    annotation = _note_annotation(note)
    leader_disabled = _disable_annotation_leader(annotation)
    positioned = _set_annotation_position(annotation, float(x), float(y)) if annotation is not None else False
    if positioned:
        # 刷新文字与坐标；边界仍必须在视图重枚举后的结构审查阶段读取。
        _safe_member(drawing_model, "ForceRebuild3", False)
    evidence = _note_evidence(note)
    position = evidence.get("position_m") or []
    text_matches = _normalise_note_text(evidence.get("text")) == _normalise_note_text(expected_text)
    position_matches = (
        len(position) >= 2
        and math.isclose(position[0], float(x), abs_tol=1e-6)
        and math.isclose(position[1], float(y), abs_tol=1e-6)
    )
    # A void SetLeader3 call is not evidence.  Only a successful setter return or
    # a subsequent GetLeaderStyle readback of ``0`` can prove this ordinary note
    # has no leader attachment.
    verified = bool(positioned and text_matches and position_matches and leader_disabled is True)
    return {
        "text": expected_text,
        "created": True,
        "position_requested_m": [float(x), float(y), 0.0],
        "positioned": positioned,
        "leader_disabled": leader_disabled,
        "text_evidence": evidence.get("text"),
        "position_evidence_m": position,
        "box": None,
        "box_source": "deferred_to_structure_inspection",
        "verified": verified,
        "status": "pass" if verified else "failed",
        "error_code": None if verified else "DRAWING_NOTE_LEADER_UNVERIFIED" if leader_disabled is None else "DRAWING_NOTE_COM_EVIDENCE_MISSING",
    }


def insert_bom_table(drawing_model, template_path, x, y, bom_type=1, config_name=""):
    """
    插入 BOM 表

    参数:
        template_path: BOM 模板路径（.sldbomtbt）
        x, y: 表格放置位置（米）
        bom_type: 1=顶层, 2=仅零件, 3=缩进
        config_name: 配置名称
    """
    return drawing_model.InsertBomTable4(
        template_path, x, y, bom_type, config_name, "", False
    )


def set_sheet_format(drawing_model, format_path):
    """
    设置图纸格式（图框）

    参数:
        format_path: 图纸格式文件路径（.slddrt）
    """
    sheet = drawing_model.GetCurrentSheet()
    return sheet.SetTemplateName(format_path)


def add_sheet(drawing_model, paper_size=PAPER_SIZES["A2"]["code"], template_path="", first_angle=True):
    """
    添加新图纸

    参数:
        paper_size: 纸张大小
            默认 A2；6=A4 横向, 7=A4 纵向, 8=A3, 9=A2, 10=A1, 11=A0
        template_path: 图纸格式模板路径
    """
    return drawing_model.NewSheet4(
        "", paper_size, 12, 1.0, 1.0, bool(first_angle), template_path, 0, 0, "", 0, 0, 0, 0, 0, 0
    )


def add_a3_sheet(drawing_model, template_candidates, *, require_gbt=True, projection="first_angle") -> dict:
    """@brief 选择本机 A3 图框并创建横向 A3 工程图页。"""
    selection = select_drawing_template(template_candidates, paper_size="A3", require_gbt=require_gbt)
    if selection["selected"] is None:
        return {
            **selection,
            "stage": "preflight",
            "retryable": True,
            "created": False,
        }
    created = bool(add_sheet(
        drawing_model,
        paper_size=PAPER_SIZES["A3"]["code"],
        template_path=selection["selected"],
        first_angle=str(projection).lower() == "first_angle",
    ))
    return {
        **selection,
        "status": "pass" if created else "failed",
        "stage": "create",
        "retryable": not created,
        "created": created,
        "projection": str(projection).lower(),
        "error_code": None if created else "DRAWING_A3_SHEET_CREATE_FAILED",
    }


def setup_current_sheet_as_a3(
    drawing_model,
    template_candidates,
    *,
    require_gbt=True,
    projection=None,
    preserve_format_notes=True,
) -> dict:
    """@brief 使用 IDrawingDoc.SetupSheet6 将当前页设置为横向 A3。"""
    selection = select_drawing_template(template_candidates, paper_size="A3", require_gbt=require_gbt)
    if selection["selected"] is None:
        return {
            **selection,
            "stage": "preflight",
            "retryable": True,
            "configured": False,
        }
    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    sheet_name = str(_safe_member(sheet, "GetName", default="") or "")
    if not sheet_name:
        return {
            **selection,
            "status": "blocked",
            "stage": "preflight",
            "retryable": True,
            "configured": False,
            "error_code": "DRAWING_CURRENT_SHEET_MISSING",
        }
    scale_before = _sheet_scale(sheet)
    view_scales_before = _sheet_view_scale_states(sheet)
    scale_block = _setup_scale_preflight(scale_before, view_scales_before)
    projection, projection_source = _effective_projection(projection, scale_before, view_scales_before)
    if scale_block or projection is None:
        blocker = scale_block or {
            "status": "blocked",
            "error_code": "DRAWING_SHEET_PROJECTION_UNAVAILABLE",
            "message": "当前页已有视图但无法读取投影法；拒绝以默认投影覆盖。",
        }
        return {
            **selection,
            **blocker,
            "stage": "preflight",
            "retryable": True,
            "configured": False,
            "sheet_scale_before": scale_before,
            "view_scales_before": view_scales_before,
            "projection": projection,
            "projection_source": projection_source,
        }
    scale1 = scale_before["numerator"] if scale_before["numerator"] is not None else 1.0
    scale2 = scale_before["denominator"] if scale_before["denominator"] is not None else 1.0
    try:
        configured = bool(get_com_member(
            drawing_model,
            "SetupSheet6",
            sheet_name,
            PAPER_SIZES["A3"]["code"],
            12,
            scale1,
            scale2,
            projection == "first_angle",
            selection["selected"],
            0.0,
            0.0,
            "",
            not bool(preserve_format_notes),
            0.0,
            0.0,
            0.0,
            0.0,
            0,
            0,
        ))
    except Exception as exc:
        return {
            **selection,
            "status": "failed",
            "stage": "create",
            "retryable": True,
            "configured": False,
            "error_code": "DRAWING_A3_SETUP_FAILED",
            "error": str(exc),
        }
    configured_sheet = _safe_member(drawing_model, "GetCurrentSheet") if configured else sheet
    view_scale_restoration = _restore_view_scale_states(configured_sheet, view_scales_before) if configured else []
    view_scale_verification = _verify_view_scale_states(configured_sheet, view_scales_before) if configured else []
    view_scales_preserved = bool(configured) and all(item["status"] == "pass" for item in view_scale_verification)
    scale_after = _sheet_scale(configured_sheet) if configured else None
    sheet_scale_preserved = bool(configured) and _same_scale_ratio(
        [scale_before["numerator"], scale_before["denominator"]],
        [scale_after["numerator"], scale_after["denominator"]],
    ) if scale_before["numerator"] is not None else bool(configured)
    verified = configured and view_scales_preserved and sheet_scale_preserved
    return {
        **selection,
        "status": "pass" if verified else "review_required" if configured else "failed",
        "stage": "create",
        "retryable": not configured,
        "configured": configured,
        "sheet_name": sheet_name,
        "projection": projection,
        "projection_source": projection_source,
        "sheet_scale_before": scale_before,
        "sheet_scale_after": scale_after,
        "sheet_scale_preserved": sheet_scale_preserved,
        "view_scales_before": view_scales_before,
        "view_scale_restoration": view_scale_restoration,
        "view_scale_verification": view_scale_verification,
        "view_scales_preserved": view_scales_preserved,
        "preserve_format_notes": bool(preserve_format_notes),
        "remove_modified_format_notes": not bool(preserve_format_notes),
        "error_code": None if verified else "DRAWING_VIEW_SCALE_PRESERVATION_UNVERIFIED" if configured else "DRAWING_A3_SETUP_FAILED",
    }


def save_current_sheet_format(drawing_model, output_path) -> dict:
    """@brief 以 ISheet.SaveFormat 导出当前图纸嵌入的 sheet format 副本。

    这用于原始 TemplateName 指向已不可用的外部文件、但图纸内嵌图框仍可正常
    使用的场景。只保存明确请求的副本，不修改当前 sheet 或猜测其他模板路径。
    """
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".slddrt":
        return {
            "status": "blocked", "saved": False,
            "error_code": "DRAWING_SHEET_FORMAT_EXTENSION_INVALID",
            "manual_review_required": True,
        }
    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    sheet_name = str(_safe_member(sheet, "GetName", default="") or "")
    if not sheet_name:
        return {
            "status": "blocked", "saved": False,
            "error_code": "DRAWING_CURRENT_SHEET_MISSING",
            "manual_review_required": True,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    before = (target.stat().st_mtime_ns, target.stat().st_size) if target.is_file() else None
    try:
        saved = bool(get_com_member(sheet, "SaveFormat", str(target)))
    except Exception as exc:
        return {
            "status": "failed", "saved": False, "sheet_name": sheet_name,
            "path": str(target), "error_code": "DRAWING_SHEET_FORMAT_SAVE_FAILED",
            "error": str(exc), "manual_review_required": True,
        }
    exists = target.is_file() and target.stat().st_size > 0
    after = (target.stat().st_mtime_ns, target.stat().st_size) if exists else None
    fresh = exists and (before is None or before != after)
    verified = saved and fresh
    return {
        "status": "pass" if verified else "failed",
        "saved": saved,
        "verified": verified,
        "sheet_name": sheet_name,
        "path": str(target),
        "source_template_name": str(_safe_member(sheet, "GetTemplateName", default="") or ""),
        "size_bytes": target.stat().st_size if exists else 0,
        "error_code": None if verified else "DRAWING_SHEET_FORMAT_SAVE_UNVERIFIED",
        "manual_review_required": not verified,
    }


def get_all_views(drawing_model):
    """获取当前图纸上的所有视图"""
    sheet = get_com_member(drawing_model, "GetCurrentSheet")
    views = get_com_member(sheet, "GetViews")
    result = []
    if views:
        for view in views:
            result.append({
                "name": view.Name,
                "type": view.Type,
                "scale": view.ScaleRatio,
            })
    return result


def _paper_size_from_template(template_path):
    """@brief 从模板文件名推断标准图幅；无法判断时返回 None。"""
    name = Path(str(template_path or "")).stem.upper()
    for paper_size in PAPER_SIZES:
        if re.search(rf"(?:^|[^A-Z0-9]){paper_size}(?:[^A-Z0-9]|$)", name):
            return paper_size
    return None


TABLE_ANNOTATION_KINDS = {
    0: "general",
    1: "hole_chart",
    2: "bom",
    3: "revision",
    4: "weldment_cut_list",
    5: "title_block",
    6: "general_tolerance",
}


def _table_cell_text(table, row, column):
    """@brief 按新版优先、旧版回退的顺序读取表格显示文字。"""
    for member, args in (
        ("DisplayedText2", (row, column, False)),
        ("DisplayedText", (row, column)),
        ("Text2", (row, column, False)),
        ("Text", (row, column)),
    ):
        value = _safe_member(table, member, *args, default=None)
        if value is not None:
            return str(value)
    return ""


def _table_evidence(table, sheet_name):
    """@brief 回读表格类型、数据行、单元格及 BOM 配置证据。"""
    table_type = _safe_member(table, "Type", default=None)
    try:
        normalized_type = int(table_type)
    except (TypeError, ValueError):
        normalized_type = table_type
    try:
        row_count = int(_safe_member(table, "RowCount", default=0) or 0)
        column_count = int(_safe_member(table, "ColumnCount", default=0) or 0)
    except (TypeError, ValueError):
        row_count, column_count = 0, 0
    cells = []
    if 0 < row_count <= 500 and 0 < column_count <= 100 and row_count * column_count <= 5000:
        cells = [
            [_table_cell_text(table, row, column) for column in range(column_count)]
            for row in range(row_count)
        ]
    configuration = ""
    if normalized_type == 2:
        bom_feature = _safe_member(table, "BomFeature", default=None)
        configuration = str(_safe_member(bom_feature, "Configuration", default="") or "")
    return {
        "sheet": str(sheet_name),
        "type": normalized_type,
        "kind": TABLE_ANNOTATION_KINDS.get(normalized_type, "unknown"),
        "title": str(_safe_member(table, "Title", default="") or ""),
        "row_count": row_count,
        "column_count": column_count,
        "configuration": configuration,
        "cells": cells,
        "box": _annotation_box(table),
    }


def _annotation_text_parts(annotation_owner):
    """@brief 回读专业标注的可见文字片段，保留原始顺序。"""
    try:
        count = int(_safe_member(annotation_owner, "GetTextCount", default=0) or 0)
    except (TypeError, ValueError):
        count = 0
    if not 0 <= count <= 1000:
        count = 0
    parts = []
    for index in range(count):
        text = str(_safe_member(annotation_owner, "GetTextAtIndex", index, default="") or "")
        if text:
            parts.append(text)
    return parts


def _professional_annotation_record(owner, kind, view_record):
    """@brief 构造带所属视图、文字和边界的专业标注证据。"""
    return {
        "kind": kind,
        "sheet": view_record["sheet"],
        "view": view_record["name"],
        "semantic_view": view_record.get("semantic_view") or "",
        "text_parts": _annotation_text_parts(owner),
        "box": _annotation_box(owner),
    }


def first_center_mark(view):
    """@brief 仅对缺失成员回退旧接口，真实调用错误继续向上传递。"""
    for name in ("GetFirstCenterMark2", "GetFirstCenterMark"):
        value = get_com_member(view, name, default=None)
        if value is not None:
            return value
    return None


def _view_center_marks(view):
    """@brief 遍历视图中心标记，并兼容 SW2025 SP1 之前的回读接口。"""
    current = first_center_mark(view)
    marks = []
    seen = set()
    while current is not None and len(marks) < 10000:
        identity = id(current)
        if identity in seen:
            break
        seen.add(identity)
        marks.append(current)
        current = _safe_member(current, "GetNext")
    if marks:
        return marks
    # GetCenterMarks 不能返回注解型中心标记，仅作为旧版本兼容回退。
    return _as_sequence(_safe_member(view, "GetCenterMarks", default=[]))


def _collect_view_professional_annotations(view, view_record):
    """@brief 使用 SW2026 Interop 已反射确认的 IView 回读接口收集专业标注。"""
    result = {
        "center_marks": [],
        "center_lines": [],
        "datum_tags": [],
        "geometric_tolerances": [],
        "surface_finish_symbols": [],
        "weld_symbols": [],
    }
    for owner in _view_center_marks(view):
        record = _professional_annotation_record(owner, "center_mark", view_record)
        record.update({
            "size_m": _safe_member(owner, "Size", default=None),
            "show_lines": _safe_member(owner, "ShowLines", default=None),
            "style": _safe_member(owner, "Style", default=None),
        })
        result["center_marks"].append(record)
    for owner in _as_sequence(_safe_member(view, "GetCenterLines", default=[])):
        result["center_lines"].append(_professional_annotation_record(owner, "center_line", view_record))
    for owner in _as_sequence(_safe_member(view, "GetDatumTags", default=[])):
        record = _professional_annotation_record(owner, "datum", view_record)
        record["label"] = str(_safe_member(owner, "GetLabel", default="") or "")
        result["datum_tags"].append(record)
    for owner in _as_sequence(_safe_member(view, "GetGTols", default=[])):
        record = _professional_annotation_record(owner, "geometric_tolerance", view_record)
        try:
            frame_count = int(_safe_member(owner, "GetFrameCount", default=0) or 0)
        except (TypeError, ValueError):
            frame_count = 0
        frame_count = frame_count if 0 <= frame_count <= 100 else 0
        record["datum_identifier"] = str(_safe_member(owner, "GetDatumIdentifier", default="") or "")
        record["frames"] = [
            {
                "index": index,
                "symbols": _as_sequence(_safe_member(owner, "GetFrameSymbols3", index, default=[])),
                "values": _as_sequence(_safe_member(owner, "GetFrameValues", index, default=[])),
            }
            for index in range(frame_count)
        ]
        result["geometric_tolerances"].append(record)
    for owner in _as_sequence(_safe_member(view, "GetSFSymbols", default=[])):
        record = _professional_annotation_record(owner, "surface_finish", view_record)
        record.update({
            "symbol_type": _safe_member(owner, "GetSymbolType", default=None),
            "symbol": _safe_member(owner, "GetSymbol", default=None),
            "direction_of_lay": _safe_member(owner, "GetDirectionOfLay", default=None),
        })
        result["surface_finish_symbols"].append(record)
    for owner in _as_sequence(_safe_member(view, "GetWeldSymbols", default=[])):
        result["weld_symbols"].append(_professional_annotation_record(owner, "weld_symbol", view_record))
    return result


def _display_dimension_model_dimension(display_dimension):
    """@brief 优先使用未废弃的 GetDimension2(0)，兼容旧版本回退。"""
    return (
        _safe_member(display_dimension, "GetDimension2", 0)
        or _safe_member(display_dimension, "GetDimension")
    )


def _dimension_visibility(display_dimension, model_dimension):
    """@brief 仅依 IAnnotation.Visible 判定可见性；模型尺寸存在不等于图纸可见。"""
    annotation = _safe_member(display_dimension, "GetAnnotation")
    raw_visible = _safe_member(annotation, "Visible", default=None)
    try:
        visibility = ANNOTATION_VISIBILITY.get(int(raw_visible), "unknown")
    except (TypeError, ValueError):
        visibility = "unknown"
    if visibility in {"visible", "hidden"}:
        return visibility, raw_visible, model_dimension is not None
    return "unknown", raw_visible, model_dimension is not None


def _normalise_model_dimension_name(model_dimension_name, view_name):
    """@brief 只剥离与已知绘图视图完全匹配的末尾 ``@View``，保留特征名内的 @。"""
    raw_name = str(model_dimension_name or "")
    known_view_name = str(view_name or "").strip().lstrip("*")
    if not raw_name or not known_view_name:
        return raw_name, False
    suffix = "@" + known_view_name
    if raw_name.casefold().endswith(suffix.casefold()):
        return raw_name[:-len(suffix)].rstrip(), True
    return raw_name, False


def _dimension_identity(view, display_dimension, model_dimension):
    """@brief 构造不含数值的 source/config/model-dimension 规范身份。"""
    source_document = _safe_member(view, "ReferencedDocument")
    source = (
        _safe_member(source_document, "GetPathName", default=None)
        or _safe_member(source_document, "GetTitle", default=None)
        or _safe_member(view, "ReferencedDocumentName", default=None)
    )
    configuration = _safe_member(view, "ReferencedConfiguration", default=None)
    model_dimension_name = (
        _safe_member(model_dimension, "FullName", default=None)
        or _safe_member(model_dimension, "GetNameForSelection", default=None)
        or _safe_member(display_dimension, "GetNameForSelection", default=None)
        or _safe_member(display_dimension, "Name", default=None)
    )
    normalized_name, view_suffix_removed = _normalise_model_dimension_name(
        model_dimension_name,
        _safe_member(view, "Name", default=""),
    )
    identity = {
        "source": str(source) if source else None,
        "configuration": str(configuration) if configuration else None,
        "model_dimension": normalized_name or None,
        "model_dimension_raw": str(model_dimension_name) if model_dimension_name else None,
        "model_dimension_view_suffix_removed": view_suffix_removed,
    }
    if all(identity[key] for key in ("source", "configuration", "model_dimension")):
        identity["canonical_key"] = "|".join((identity["source"], identity["configuration"], identity["model_dimension"]))
    else:
        identity["canonical_key"] = None
    return identity


def _dimension_system_value(display_dimension, model_dimension):
    """@brief 读取模型系统值；长度=米，角度=弧度，绝不按显示文本推断。"""
    dimension_type = _safe_member(display_dimension, "Type2", default=None)
    if dimension_type is None:
        dimension_type = _safe_member(display_dimension, "Type", default=None)
    try:
        normalized_type = int(dimension_type)
    except (TypeError, ValueError):
        normalized_type = dimension_type
    raw_value = _safe_member(model_dimension, "SystemValue", default=None)
    try:
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError):
        value = None
    angular = normalized_type in ANGULAR_DIMENSION_TYPES
    return {
        "value": value,
        "unit": "rad" if angular else "m",
        "value_mm": value * 1000.0 if value is not None and not angular else None,
        "value_degrees": math.degrees(value) if value is not None and angular else None,
        "type": normalized_type,
        "source": "IDimension.SystemValue" if value is not None else "unavailable",
    }


def _view_projection_relationships(sheet_views):
    """Derive a projected view's movable axis only from its actual base relation.

    ``IView.GetBaseView`` is the parent evidence.  A view name such as ``Top`` is
    not positional evidence: a section/detail/auxiliary view may use that name,
    so it stays non-movable unless its measured center delta to the base is axis
    aligned.  Child linkage must also be observable before an automated move.
    """
    views = list(sheet_views or [])
    names = [str(_safe_member(view, "Name", default="") or "") for view in views]
    name_count = {name: names.count(name) for name in names if name}
    base_info = {}
    linkage_observable = all(getattr(view, "GetBaseView", None) is not None for view in views)
    for view, name in zip(views, names):
        base = _safe_member(view, "GetBaseView") if getattr(view, "GetBaseView", None) is not None else None
        base_name = str(_safe_member(base, "Name", default="") or "") if base is not None else ""
        position, base_position = _view_position(view), _view_position(base)
        axis = None
        if base is not None and position is not None and base_position is not None:
            delta_x, delta_y = position[0] - base_position[0], position[1] - base_position[1]
            tolerance = 1e-6
            if abs(delta_x) > tolerance and abs(delta_y) <= tolerance:
                axis = "horizontal"
            elif abs(delta_y) > tolerance and abs(delta_x) <= tolerance:
                axis = "vertical"
        base_info[id(view)] = {
            "base_view_name": base_name or None,
            "projection_axis": axis,
            "projection_axis_evidence": "base_view_position_delta" if axis else "insufficient",
            "parent_relation_verified": bool(base is not None and base_name and name_count.get(base_name) == 1 and base_position is not None),
        }
    for view, name in zip(views, names):
        children = [
            child_name for child, child_name in zip(views, names)
            if child is not view and base_info.get(id(child), {}).get("base_view_name") == name and name_count.get(name) == 1
        ]
        base_info[id(view)].update({
            "child_view_names": children,
            "child_view_count": len(children),
            "child_linkage_verified": bool(linkage_observable and name and name_count.get(name) == 1),
        })
    return base_info


def inspect_drawing_structure(drawing_model, *, paper_size_hint=None, title_block_box=None) -> dict:
    """@brief 读取工程图结构并返回可审计报告，不修改文档。"""
    sheets = _as_sequence(_safe_member(drawing_model, "GetSheetNames", default=[]))
    if not sheets:
        current_sheet = _safe_member(drawing_model, "GetCurrentSheet")
        current_name = _safe_member(current_sheet, "GetName", default="")
        if current_name:
            sheets = [current_name]
    views = []
    dimensions = []
    notes = []
    tables = []
    professional_annotations = {
        "center_marks": [],
        "center_lines": [],
        "hole_callouts": [],
        "datum_tags": [],
        "geometric_tolerances": [],
        "surface_finish_symbols": [],
        "weld_symbols": [],
    }
    for sheet_name in sheets or [""]:
        sheet = _safe_member(drawing_model, "GetSheet", sheet_name) or _safe_member(drawing_model, "GetCurrentSheet")
        sheet_views = _as_sequence(_safe_member(sheet, "GetViews", default=[]))
        mapped_views, _, _ = _map_standard_views_from_sheet_views(sheet_views)
        semantic_by_object = {id(view): name for name, view in mapped_views.items()}
        relationship_by_object = _view_projection_relationships(sheet_views)
        for view in sheet_views:
            relationship = relationship_by_object.get(id(view), {})
            position = _view_position(view)
            view_record = {
                "sheet": str(sheet_name),
                "name": _safe_member(view, "Name", default=""),
                "type": _safe_member(view, "Type", default=None),
                "orientation": _safe_member(view, "GetOrientationName", default=""),
                "semantic_view": semantic_by_object.get(id(view), ""),
                "scale": _safe_member(view, "ScaleRatio", default=None),
                "position": list(position) if position is not None else None,
                "box": _normalise_box(_safe_member(view, "GetOutline")),
                **relationship,
            }
            views.append(view_record)
            view_annotations = _collect_view_professional_annotations(view, view_record)
            for key, records in view_annotations.items():
                professional_annotations[key].extend(records)
            view_dimensions = _view_display_dimensions(view)
            for dimension in view_dimensions:
                model_dimension = _display_dimension_model_dimension(dimension)
                visibility, raw_visibility, stored = _dimension_visibility(dimension, model_dimension)
                identity = _dimension_identity(view, dimension, model_dimension)
                system_value = _dimension_system_value(dimension, model_dimension)
                native_box = _annotation_box(dimension)
                estimated_box_evidence = estimate_dimension_text_box(dimension) if native_box is None else None
                dimension_record = {
                    "sheet": str(sheet_name),
                    "view": _safe_member(view, "Name", default=""),
                    "semantic_view": view_record["semantic_view"],
                    "name": (
                        _safe_member(dimension, "Name", default="")
                        or _safe_member(dimension, "GetNameForSelection", default="")
                        or _safe_member(_safe_member(dimension, "GetDimension"), "FullName", default="")
                    ),
                    "type": _safe_member(dimension, "Type", default=None),
                    "type2": system_value["type"],
                    "text": _safe_member(dimension, "GetText", 0, default=""),
                    "visibility": visibility,
                    "annotation_visible": raw_visibility,
                    "stored": stored,
                    "identity": identity,
                    "system_value": system_value,
                    "box": native_box or estimated_box_evidence.get("box"),
                    "box_source": "native" if native_box else estimated_box_evidence.get("source"),
                    "box_confidence": "high" if native_box else estimated_box_evidence.get("confidence"),
                    "box_evidence": {
                        "box": native_box,
                        "source": "native",
                        "confidence": "high",
                        "method": "annotation_get_box",
                    } if native_box else estimated_box_evidence,
                    "is_hole_callout": bool(_safe_member(dimension, "IsHoleCallout", default=False)),
                    "hole_callout_variables": _as_sequence(_safe_member(dimension, "GetHoleCalloutVariables", default=[])),
                }
                dimensions.append(dimension_record)
                if dimension_record["is_hole_callout"]:
                    professional_annotations["hole_callouts"].append({
                        "kind": "hole_callout",
                        "sheet": dimension_record["sheet"],
                        "view": dimension_record["view"],
                        "semantic_view": dimension_record["semantic_view"],
                        "name": dimension_record["name"],
                        "text_parts": [dimension_record["text"]] if dimension_record["text"] else [],
                        "variables": dimension_record["hole_callout_variables"],
                        "box": dimension_record["box"],
                    })
            for note in _as_sequence(_safe_member(view, "GetNotes", default=[])):
                note_evidence = _note_evidence(note)
                note_text = str(note_evidence.get("text") or "").strip()
                owner_view_type = view_record["type"]
                notes.append({
                    "sheet": str(sheet_name),
                    "owner_view": view_record["name"],
                    "owner_view_type": owner_view_type,
                    "note_kind": "view_label" if owner_view_type in {2, 3} and not note_text else "note",
                    **note_evidence,
                })
            for table in _as_sequence(_safe_member(view, "GetTableAnnotations", default=[])):
                tables.append(_table_evidence(table, sheet_name))
    current_sheet = _safe_member(drawing_model, "GetCurrentSheet")
    template = _safe_member(current_sheet, "GetTemplateName", default="")
    actual_sheet_size = _sheet_dimensions(current_sheet)
    paper_size = str(paper_size_hint or _paper_size_from_template(template) or "").upper() or None
    spec = PAPER_SIZES.get(paper_size)
    if actual_sheet_size is not None:
        sheet_size = {
            "width_m": actual_sheet_size["width_m"],
            "height_m": actual_sheet_size["height_m"],
        }
        sheet_size_source = actual_sheet_size["source"]
    elif spec is not None:
        sheet_size = {"width_m": spec["width_m"], "height_m": spec["height_m"]}
        sheet_size_source = "paper_size_fallback"
    else:
        sheet_size = None
        sheet_size_source = "unavailable"
    inferred_title_box = title_block_box
    if inferred_title_box is None and sheet_size is not None:
        inferred_title_box = {
            "left": sheet_size["width_m"] - 0.012 - 0.180,
            "bottom": 0.012,
            "right": sheet_size["width_m"] - 0.012,
            "top": 0.067,
        }
    compact_template_name = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", Path(str(template or "")).stem.casefold())
    template_name = Path(str(template or "")).stem.casefold()
    gbt_candidate = (
        "gbt" in compact_template_name
        or "国标" in compact_template_name
        or bool(re.search(r"(?:^|[^a-z0-9])gb(?:t)?(?:[^a-z0-9]|$)", template_name, re.IGNORECASE))
    )
    outline_count = sum(item.get("box") is not None for item in views)
    dimension_box_count = sum(item.get("box") is not None for item in dimensions)
    native_dimension_box_count = sum(item.get("box_source") == "native" for item in dimensions)
    estimated_dimension_box_count = sum(item.get("box_source") == "estimated" and item.get("box") is not None for item in dimensions)
    low_confidence_dimension_box_count = sum(item.get("box_confidence") in {"low", "unavailable"} for item in dimensions)
    stored_dimension_count = sum(1 for item in dimensions if item.get("stored"))
    result = {
        "status": "pass" if views else "blocked",
        "stage": "review",
        "sheets": [str(item) for item in sheets],
        "views": views,
        "dimensions": dimensions,
        "notes": notes,
        "tables": tables,
        "professional_annotations": professional_annotations,
        "template_path": str(template or ""),
        "paper_size": paper_size,
        "sheet_size": sheet_size,
        "sheet_size_source": sheet_size_source,
        "title_block": {
            "candidate": bool(template),
            "gbt_candidate": gbt_candidate,
            "content_verified": any(item.get("kind") == "title_block" and item.get("row_count", 0) > 0 for item in tables),
            "fields": {},
            "box": _normalise_box(title_block_box) if title_block_box is not None else inferred_title_box,
        },
        "view_count": len(views),
        "dimension_count": len(dimensions),
        "stored_dimension_count": stored_dimension_count,
        "table_count": len(tables),
        "view_outline_count": outline_count,
        "dimension_box_count": dimension_box_count,
        "native_dimension_box_count": native_dimension_box_count,
        "estimated_dimension_box_count": estimated_dimension_box_count,
        "low_confidence_dimension_box_count": low_confidence_dimension_box_count,
        "manual_review_required": True,
        "retryable": not bool(views),
        "error_code": None if views else "DRAWING_VIEWS_MISSING",
        "checks": [
            {"id": "drawing-views", "status": "pass" if views else "fail", "message": "工程图包含视图" if views else "未读取到工程图视图"},
            {"id": "drawing-template", "status": "pass" if template else "warning", "message": "已读取图框模板" if template else "图框模板需要人工确认"},
            {"id": "drawing-a3-sheet", "status": "pass" if paper_size == "A3" else "warning", "message": "当前图幅识别为 A3" if paper_size == "A3" else "当前图幅不是 A3 或无法识别"},
            {"id": "drawing-gbt-template", "status": "pass" if gbt_candidate else "warning", "message": "模板名称是 GB/T 候选，内容仍需目视复核" if gbt_candidate else "未发现 GB/T 图框候选证据"},
            {"id": "drawing-dimensions", "status": "pass" if dimensions else "warning", "message": "已读取真实尺寸实体" if dimensions else "未读取到尺寸实体"},
            {"id": "drawing-view-outlines", "status": "pass" if views and outline_count == len(views) else "warning", "message": "已读取全部视图边界" if views and outline_count == len(views) else "部分视图缺少边界，无法完整检查碰撞"},
            {
                "id": "drawing-dimension-boxes",
                "status": "pass" if dimensions and native_dimension_box_count == len(dimensions) else "warning",
                "message": (
                    "已读取全部尺寸文字原生边界"
                    if dimensions and native_dimension_box_count == len(dimensions)
                    else f"原生边界 {native_dimension_box_count}/{len(dimensions)}，保守估算 {estimated_dimension_box_count}/{len(dimensions)}；估算不等于原生证据"
                ),
            },
        ],
    }
    return result


def repair_native_drawing_layout(drawing_model, pdf_path, sw_app=None, max_attempts=3):
    """Run the bounded layout planner against the live drawing COM object.

    Each observation exports a fresh PDF and re-enumerates the drawing structure.
    Only a unique projected child view with an axis proven from ``GetBaseView`` and
    center positions can move.  Views with their own children are refused because
    ``IView.Position`` can propagate to dependent views; this helper never writes
    scale, annotation visibility, or entity deletion APIs.
    """
    try:
        from scripts.sw_review import repair_drawing_layout
    except ImportError:
        from sw_review import repair_drawing_layout
    try:
        target_pdf = Path(pdf_path).expanduser().resolve()
    except (TypeError, ValueError):
        return {
            "status": "blocked", "stage": "preflight", "error_code": "DRAWING_LAYOUT_PDF_PATH_INVALID",
            "manual_review_required": True,
        }
    if target_pdf.suffix.lower() != ".pdf":
        return {
            "status": "blocked", "stage": "preflight", "error_code": "DRAWING_LAYOUT_PDF_PATH_INVALID",
            "manual_review_required": True,
        }
    latest = {"structure": None, "exports": 0}

    def inspect_fresh():
        if not export_sheet_to_pdf(drawing_model, str(target_pdf), sw_app=sw_app):
            raise RuntimeError("DRAWING_LAYOUT_FRESH_PDF_EXPORT_FAILED")
        latest["exports"] += 1
        structure = inspect_drawing_structure(drawing_model)
        if structure.get("status") != "pass":
            raise RuntimeError("DRAWING_LAYOUT_FRESH_STRUCTURE_UNAVAILABLE")
        latest["structure"] = structure
        return structure

    def apply_native_action(action):
        if not isinstance(action, dict) or action.get("kind") != "move_view":
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_ACTION_UNSUPPORTED"}
        delta = action.get("delta_m") or {}
        try:
            dx, dy = float(delta["x"]), float(delta["y"])
        except (KeyError, TypeError, ValueError):
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_ACTION_DELTA_INVALID"}
        if not all(math.isfinite(value) for value in (dx, dy)):
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_ACTION_DELTA_INVALID"}
        target_name = str(action.get("target") or "")
        matching = []
        for sheet_name in _as_sequence(_safe_member(drawing_model, "GetSheetNames", default=[])):
            sheet = _safe_member(drawing_model, "GetSheet", sheet_name)
            for view in _as_sequence(_safe_member(sheet, "GetViews", default=[])):
                if str(_safe_member(view, "Name", default="") or "") == target_name:
                    matching.append((str(sheet_name), sheet, view))
        if len(matching) != 1:
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_TARGET_NOT_UNIQUE", "target": target_name, "count": len(matching)}
        sheet_name, sheet, view = matching[0]
        structure = latest.get("structure") or {}
        records = [
            record for record in structure.get("views") or []
            if str(record.get("sheet") or "") == sheet_name and str(record.get("name") or "") == target_name
        ]
        if len(records) != 1:
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_TARGET_EVIDENCE_UNAVAILABLE", "target": target_name}
        record = records[0]
        if (
            record.get("projection_axis") not in {"horizontal", "vertical"}
            or not record.get("parent_relation_verified")
            or not record.get("child_linkage_verified")
            or int(record.get("child_view_count") or 0) != 0
        ):
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_VIEW_RELATION_UNVERIFIED", "target": target_name}
        current = _view_position(view)
        if current is None:
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_VIEW_POSITION_UNAVAILABLE", "target": target_name}
        # Recheck the live relation, rather than trusting a stale wrapper record.
        base = _safe_member(view, "GetBaseView") if getattr(view, "GetBaseView", None) is not None else None
        base_position = _view_position(base)
        if base is None or base_position is None:
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_VIEW_RELATION_UNVERIFIED", "target": target_name}
        live_dx, live_dy = current[0] - base_position[0], current[1] - base_position[1]
        live_axis = "horizontal" if abs(live_dx) > 1e-6 and abs(live_dy) <= 1e-6 else "vertical" if abs(live_dy) > 1e-6 and abs(live_dx) <= 1e-6 else None
        if live_axis != record.get("projection_axis"):
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_VIEW_RELATION_UNVERIFIED", "target": target_name}
        all_views = _as_sequence(_safe_member(sheet, "GetViews", default=[]))
        if any(
            child is not view
            and str(_safe_member(_safe_member(child, "GetBaseView"), "Name", default="") or "") == target_name
            for child in all_views
        ):
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_VIEW_HAS_CHILDREN", "target": target_name}
        scale_before = _view_scale_state(view)
        try:
            actual = _set_view_center(view, (current[0] + dx, current[1] + dy))
            rebuild = get_com_member(drawing_model, "EditRebuild3")
            if rebuild is False:
                raise RuntimeError("EditRebuild3 returned false")
        except Exception as exc:
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_NATIVE_MOVE_FAILED", "target": target_name, "error": str(exc)}
        scale_after = _view_scale_state(view)
        if not (
            _same_scale_ratio(scale_before["scale_ratio"], scale_after["scale_ratio"])
            and scale_before["use_sheet_scale"] == scale_after["use_sheet_scale"]
            and scale_before["use_parent_scale"] == scale_after["use_parent_scale"]
        ):
            return {"status": "failed", "error_code": "DRAWING_LAYOUT_MOVE_CHANGED_SCALE", "target": target_name}
        return {
            "status": "pass", "target": target_name, "sheet": sheet_name,
            "position_before_m": list(current), "position_after_m": actual,
            "scale_before": scale_before, "scale_after": scale_after,
        }

    try:
        result = repair_drawing_layout(inspect_fresh, apply_native_action, max_attempts=max_attempts)
    except Exception as exc:
        return {
            "status": "blocked", "stage": "inspect", "error_code": "DRAWING_LAYOUT_FRESH_EVIDENCE_FAILED",
            "error": str(exc), "pdf_path": str(target_pdf), "fresh_pdf_exports": latest["exports"],
            "manual_review_required": True,
        }
    return {
        **result,
        "stage": "native_layout_repair",
        "pdf_path": str(target_pdf),
        "fresh_pdf_exports": latest["exports"],
        # Unit tests can only exercise a mock.  A live caller still needs final
        # visual/release review even if this bounded structural loop converges.
        "manual_review_required": True,
    }


def auto_insert_center_marks(drawing_model, requirements) -> dict:
    """@brief 按视图自动插入中心标记，并以实体数量回读作为成功依据。"""
    requested = list(requirements or [])
    if not requested:
        return {
            "status": "not_requested",
            "requested": False,
            "requirements": [],
            "manual_review_required": False,
            "error_code": None,
        }

    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    sheet_views = _as_sequence(_safe_member(sheet, "GetViews", default=[]))
    mapped_views, diagnostics, mapping_method = _map_standard_views_from_sheet_views(sheet_views)
    drawing_path = str(_safe_member(drawing_model, "GetPathName", default="") or "")
    results = []
    for requirement in requested:
        requirement_id = str(requirement.get("id") or "")
        orientation = _normalise_orientation(requirement.get("view"))
        expected_count = int(requirement.get("count") or 0)
        targets = list(requirement.get("targets") or [])
        insert_type = 0
        for target in targets:
            insert_type |= AUTO_CENTER_MARK_TARGETS.get(str(target), 0)
        view = mapped_views.get(orientation)
        if view is None or not orientation or insert_type == 0 or expected_count <= 0:
            results.append({
                "id": requirement_id,
                "status": "failed",
                "view": orientation or str(requirement.get("view") or ""),
                "targets": targets,
                "expected_count": expected_count,
                "before_count": 0,
                "after_count": 0,
                "created_count": 0,
                "api_returned": None,
                "error_code": "DRAWING_CENTER_MARK_REQUIREMENT_UNRESOLVED",
            })
            continue

        before_count = len(_view_center_marks(view))
        api_returned = None
        error = None
        if before_count < expected_count:
            view_name = str(_safe_member(view, "Name", default="") or "")
            if view_name:
                _safe_member(drawing_model, "ActivateView", view_name)
            try:
                api_returned = bool(get_com_member(
                    view,
                    "AutoInsertCenterMarks2",
                    insert_type,
                    0,      # swCenterMarkConnectionLine_None
                    True,   # 线性槽使用槽中心
                    True,   # 圆弧槽使用槽中心
                    True,   # 使用文档默认尺寸与间隙
                    0.0,
                    0.0,
                    False,
                    True,
                    0.0,
                ))
            except Exception as exc:
                error = str(exc)
            _safe_member(drawing_model, "ForceRebuild3", False)
            _safe_member(drawing_model, "GraphicsRedraw2")
        after_count = len(_view_center_marks(view))
        passed = after_count >= expected_count
        failure_code = None
        if not passed:
            failure_code = (
                "DRAWING_CENTER_MARK_DRAWING_SAVE_REQUIRED"
                if api_returned is True and not drawing_path
                else "DRAWING_CENTER_MARK_INSERT_OR_READBACK_FAILED"
            )
        results.append({
            "id": requirement_id,
            "status": "pass" if passed else "failed",
            "view": orientation,
            "actual_view": str(_safe_member(view, "Name", default="") or ""),
            "targets": targets,
            "insert_type": insert_type,
            "expected_count": expected_count,
            "before_count": before_count,
            "after_count": after_count,
            "created_count": max(0, after_count - before_count),
            "api_returned": api_returned,
            "error": error,
            "error_code": failure_code,
        })

    failed = [item for item in results if item["status"] != "pass"]
    aggregate_error = None
    if failed:
        aggregate_error = (
            "DRAWING_CENTER_MARK_DRAWING_SAVE_REQUIRED"
            if all(item.get("error_code") == "DRAWING_CENTER_MARK_DRAWING_SAVE_REQUIRED" for item in failed)
            else "DRAWING_CENTER_MARK_INSERT_OR_READBACK_FAILED"
        )
    return {
        "status": "failed" if failed else "pass",
        "requested": True,
        "requirements": results,
        "mapping_method": mapping_method,
        "mapping_diagnostics": diagnostics,
        "drawing_path": drawing_path,
        "manual_review_required": True,
        "retryable": aggregate_error == "DRAWING_CENTER_MARK_DRAWING_SAVE_REQUIRED",
        "error_code": aggregate_error,
    }


def export_sheet_to_pdf(model, output_path, sheet_names=None, sw_app=None):
    """
    将工程图导出为 PDF

    参数:
        model: IModelDoc2（工程图文档）
        output_path: 输出 PDF 路径
        sheet_names: 图纸名称列表，None=所有图纸
        sw_app: 可选的 SldWorks.Application 对象；传入会话对象可避免 SW2024
            动态 IModelDoc2 未暴露 GetSldWorksObject 的兼容性问题。
    """
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    before = (output.stat().st_mtime_ns, output.stat().st_size) if output.is_file() else None
    sw = sw_app
    if sw is None:
        for prog_id in ("SldWorks.Application.32", "SldWorks.Application"):
            try:
                sw = _win32com.GetActiveObject(prog_id)
                break
            except Exception:
                continue
    if sw is None:
        return False
    # A saved drawing must be activatable before export.  Do not silently
    # export a different document merely because SolidWorks switched focus.
    source_path = str(_safe_member(model, "GetPathName", default="") or "")
    if source_path:
        try:
            model = activate_document_verified(sw, model, source_path, expected_type=3)
        except Exception as exc:
            print({
                "event": "pdf_export_activation_failed",
                "source_path": source_path,
                "error": str(exc),
            })
            return False
    try:
        pdf_data = get_com_member(sw, "GetExportFileData", 1)  # 1 = swExportPDFData
    except Exception:
        return False
    try:
        # A viewer process is not part of export-success evidence.
        pdf_data.ViewPdfAfterSaving = False
    except Exception as exc:
        print({"event": "pdf_export_viewer_option_failed", "error": str(exc)})
        return False

    if sheet_names is None:
        drawing = model
        sheet_names = get_com_member(drawing, "GetSheetNames")

    try:
        names = tuple(str(name) for name in _as_sequence(sheet_names))
        if not names:
            return False
        sheets = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, names)
        # swExportDataSheetsToExport_e: All=1, Current=2, Specified=3.
        if not pdf_data.SetSheets(3, sheets):
            return False
    except Exception as exc:
        print(f"PDF 图纸选择失败: {exc}")
        return False

    # SaveAs can otherwise be influenced by the current selection.  This
    # hygiene does not override an API error.
    _safe_member(model, "ClearSelection2", True)
    errors = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    success = model.Extension.SaveAs(str(output), 0, 1, pdf_data, errors, warnings)
    save_evidence = {
        "event": "pdf_export_saveas",
        "return_bool": bool(success),
        "error_mask": int(errors.value),
        "warning_mask": int(warnings.value),
        "output_path": str(output),
    }
    print(save_evidence)
    if not success or save_evidence["error_mask"]:
        print(f"PDF 导出失败, 错误码: {save_evidence['error_mask']}, 警告码: {save_evidence['warning_mask']}")
        return False
    if not output.is_file() or output.stat().st_size <= 0:
        return False
    after = (output.stat().st_mtime_ns, output.stat().st_size)
    if before is not None and after == before:
        return False
    try:
        import fitz
        with fitz.open(str(output)) as document:
            if document.page_count == 0:
                return False
    except Exception as exc:
        print(f"PDF 输出验证失败: {exc}")
        return False
    print(f"PDF 导出成功: {output}")
    return True


def setup_current_sheet(
    drawing_model,
    template_candidates,
    *,
    paper_size="A3",
    projection=None,
    orientation=None,
    require_gbt=True,
    preserve_format_notes=True,
    embedded_format_path=None,
) -> dict:
    """@brief 按 DrawingSpec 配置当前图纸页并回读基本配置证据。"""
    paper_size = str(paper_size).upper()
    if paper_size not in PAPER_SIZES:
        raise ValueError(f"不支持的图幅: {paper_size}")
    sheet = _safe_member(drawing_model, "GetCurrentSheet")
    sheet_name = str(_safe_member(sheet, "GetName", default="") or "")
    if not sheet_name:
        return {"status": "blocked", "stage": "preflight", "configured": False, "error_code": "DRAWING_CURRENT_SHEET_MISSING"}
    scale_before = _sheet_scale(sheet)
    view_scales_before = _sheet_view_scale_states(sheet)
    original_template = str(_safe_member(sheet, "GetTemplateName", default="") or "")
    selection = select_drawing_template(template_candidates, paper_size=paper_size, require_gbt=require_gbt)
    embedded_fallback = None
    if not selection["selected"] and preserve_format_notes and view_scales_before and embedded_format_path is not None:
        target = Path(embedded_format_path).expanduser().resolve()
        if target.is_file():
            return {
                **selection, "status": "blocked", "stage": "preflight", "configured": False, "retryable": True,
                "error_code": "DRAWING_EMBEDDED_FORMAT_OUTPUT_EXISTS", "original_template_path": original_template,
                "manual_review_required": True,
            }
        exported = save_current_sheet_format(drawing_model, target)
        if exported.get("status") == "pass" and exported.get("saved"):
            embedded_fallback = {
                "used": True, "path": exported["path"], "original_template_path": original_template,
                # An exported filename is not a standards-content inspection.
                "gbt_content_verified": False,
            }
            selection = {
                **selection, "status": "pass", "selected": exported["path"],
                "gbt_candidate": False, "gbt_content_verified": False,
                "embedded_format_fallback": embedded_fallback,
            }
        else:
            return {
                **selection, "status": "blocked", "stage": "preflight", "configured": False, "retryable": True,
                "error_code": exported.get("error_code") or "DRAWING_EMBEDDED_FORMAT_EXPORT_FAILED",
                "original_template_path": original_template, "embedded_format_export": exported,
                "manual_review_required": True,
            }
    if not selection["selected"]:
        return {**selection, "status": "blocked", "stage": "preflight", "configured": False, "retryable": True, "original_template_path": original_template}
    scale_block = _setup_scale_preflight(scale_before, view_scales_before)
    projection, projection_source = _effective_projection(projection, scale_before, view_scales_before)
    sheet_format = _effective_sheet_format(paper_size, orientation, scale_before, view_scales_before)
    if scale_block or projection is None or sheet_format is None:
        blocker = scale_block or {
            "status": "blocked",
            "error_code": "DRAWING_SHEET_PROJECTION_UNAVAILABLE" if projection is None else "DRAWING_SHEET_FORMAT_UNAVAILABLE",
            "message": "当前页已有视图但无法读取投影法或同类别纸张格式；拒绝以默认方向覆盖。",
        }
        return {
            **selection,
            **blocker,
            "stage": "preflight",
            "configured": False,
            "retryable": True,
            "sheet_scale_before": scale_before,
            "view_scales_before": view_scales_before,
            "projection": projection,
            "projection_source": projection_source,
            "orientation": orientation,
        }
    scale1 = scale_before["numerator"] if scale_before["numerator"] is not None else 1.0
    scale2 = scale_before["denominator"] if scale_before["denominator"] is not None else 1.0
    try:
        configured = bool(get_com_member(
            drawing_model,
            "SetupSheet6",
            sheet_name,
            sheet_format["paper_size_code"],
            12,
            scale1,
            scale2,
            projection == "first_angle",
            selection["selected"],
            sheet_format["template_width_m"],
            sheet_format["template_height_m"],
            "",
            not bool(preserve_format_notes),
            0.0,
            0.0,
            0.0,
            0.0,
            0,
            0,
        ))
    except Exception as exc:
        return {**selection, "status": "failed", "stage": "create", "configured": False, "projection": projection, "error_code": "DRAWING_SHEET_SETUP_FAILED", "error": str(exc)}
    if configured:
        _safe_member(drawing_model, "ForceRebuild3", False)
    configured_sheet = _safe_member(drawing_model, "GetCurrentSheet") if configured else sheet
    view_scale_restoration = _restore_view_scale_states(configured_sheet, view_scales_before) if configured else []
    view_scale_verification = _verify_view_scale_states(configured_sheet, view_scales_before) if configured else []
    view_scales_preserved = bool(configured) and all(item["status"] == "pass" for item in view_scale_verification)
    scale_after = _sheet_scale(configured_sheet) if configured else None
    sheet_scale_preserved = bool(configured) and _same_scale_ratio(
        [scale_before["numerator"], scale_before["denominator"]],
        [scale_after["numerator"], scale_after["denominator"]],
    ) if scale_before["numerator"] is not None else bool(configured)
    sheet_format_preserved = bool(configured) and _sheet_format_matches(sheet_format, scale_after)
    verified = configured and view_scales_preserved and sheet_scale_preserved and sheet_format_preserved
    sheet_size = _sheet_dimensions(configured_sheet) if configured else None
    return {
        **selection,
        "status": "pass" if verified else "review_required" if configured else "failed",
        "stage": "create",
        "configured": configured,
        "paper_size": paper_size,
        "projection": projection,
        "projection_source": projection_source,
        "orientation": sheet_format["orientation"],
        "orientation_source": sheet_format["orientation_source"],
        "preserved_existing_format": sheet_format["preserved_existing_format"],
        "paper_size_code": sheet_format["paper_size_code"],
        "sheet_name": sheet_name,
        "original_template_path": original_template,
        "embedded_format_fallback": embedded_fallback,
        "sheet_size": sheet_size,
        "sheet_size_source": sheet_size.get("source") if sheet_size else "paper_size_fallback",
        "sheet_scale_before": scale_before,
        "sheet_scale_after": scale_after,
        "sheet_scale_preserved": sheet_scale_preserved,
        "sheet_format_preserved": sheet_format_preserved,
        "view_scales_before": view_scales_before,
        "view_scale_restoration": view_scale_restoration,
        "view_scale_verification": view_scale_verification,
        "view_scales_preserved": view_scales_preserved,
        "preserve_format_notes": bool(preserve_format_notes),
        "remove_modified_format_notes": not bool(preserve_format_notes),
        "error_code": None if verified else "DRAWING_SHEET_FORMAT_PRESERVATION_UNVERIFIED" if configured and not sheet_format_preserved else "DRAWING_VIEW_SCALE_PRESERVATION_UNVERIFIED" if configured else "DRAWING_SHEET_SETUP_FAILED",
        "manual_review_required": True,
    }


def validate_generic_drawing_generation(spec) -> dict:
    """@brief 在修改文档前声明通用生成器无法可靠执行的 DrawingSpec 字段。"""
    issues = []
    views = dict(spec.get("views") or {})
    standard_views = {name for name in ("front", "top", "right") if name in views}
    if standard_views != {"front", "top", "right"}:
        issues.append({
            "code": "DRAWING_STANDARD_VIEW_SET_UNSUPPORTED",
            "path": "views",
            "message": "通用生成器当前只支持同时生成 front/top/right 三视图。",
        })
    unsupported_views = [name for name in ("bottom", "left", "isometric") if name in views]
    unsupported_views.extend(name for name in ("sections", "details") if views.get(name))
    if unsupported_views:
        issues.append({
            "code": "DRAWING_REQUESTED_VIEWS_UNSUPPORTED",
            "path": "views",
            "views": unsupported_views,
            "message": "通用生成器尚不能创建请求的辅助、轴测、剖视或局部视图。",
        })
    configured_views = [name for name in ("front", "top", "right") if views.get(name)]
    if configured_views:
        issues.append({
            "code": "DRAWING_VIEW_OPTIONS_UNSUPPORTED",
            "path": "views",
            "views": configured_views,
            "message": "通用生成器尚不能执行标准视图对象内的名称、方向或位置选项。",
        })
    if spec.get("requiredDimensions"):
        issues.append({
            "code": "DRAWING_REQUIRED_DIMENSIONS_UNSUPPORTED",
            "path": "requiredDimensions",
            "message": "通用生成器只能导入模型尺寸，不能可靠创建并绑定指定 ID、视图和语义的尺寸。",
        })
    if spec.get("holeRequirements"):
        issues.append({
            "code": "DRAWING_HOLE_REQUIREMENTS_UNSUPPORTED",
            "path": "holeRequirements",
            "message": "通用生成器尚不能创建孔标注或孔表；请使用专项脚本并回读证据。",
        })
    professional_annotations = dict(spec.get("professionalAnnotations") or {})
    unsupported_professional_fields = sorted(
        name for name, values in professional_annotations.items()
        if name != "centerMarks" and values
    )
    if unsupported_professional_fields:
        issues.append({
            "code": "DRAWING_PROFESSIONAL_ANNOTATIONS_UNSUPPORTED",
            "path": "professionalAnnotations",
            "fields": unsupported_professional_fields,
            "message": "通用生成器目前只支持已回读验证的自动中心标记；其余专业标注仍须使用专项脚本。",
        })
    title_fields = sorted(set((spec.get("titleBlock") or {})) - {"required", "format"})
    if title_fields:
        issues.append({
            "code": "DRAWING_TITLE_BLOCK_FIELDS_UNSUPPORTED",
            "path": "titleBlock",
            "fields": title_fields,
            "message": "通用生成器尚不能绑定标题栏业务字段。",
        })
    if spec.get("sheetMetal"):
        issues.append({
            "code": "DRAWING_SHEET_METAL_OPTIONS_UNSUPPORTED",
            "path": "sheetMetal",
            "message": "通用生成器尚不能执行展开图、折弯表或钣金专用选项。",
        })
    return {
        "status": "blocked" if issues else "pass",
        "stage": "capability_preflight",
        "issues": issues,
        "error_code": "DRAWING_SPEC_CAPABILITY_UNSUPPORTED" if issues else None,
        "manual_review_required": bool(issues),
    }


def generate_drawing_from_spec(drawing_model, spec, source_model_path, template_candidates=None, embedded_format_path=None) -> dict:
    """@brief 按已校验 DrawingSpec 创建标准工程图结构和制造标注入口。"""
    capability = validate_generic_drawing_generation(spec)
    if capability["status"] != "pass":
        return capability
    paper_size = str(spec.get("paperSize", "A3")).upper()
    projection = str(spec.get("projection", "first_angle")).lower()
    model_size_mm = spec.get("modelSizeMm")
    if not isinstance(model_size_mm, (list, tuple)) or len(model_size_mm) != 3:
        return {
            "status": "blocked",
            "stage": "planning",
            "error_code": "DRAWING_MODEL_SIZE_MISSING",
            "message": "DrawingSpec 必须提供 modelSizeMm，避免凭空推断工程图比例和视图布局。",
            "manual_review_required": True,
        }
    candidates = list(template_candidates or spec.get("templateCandidates") or [])
    sheet_setup = setup_current_sheet(
        drawing_model,
        candidates,
        paper_size=paper_size,
        projection=projection,
        require_gbt=spec.get("standard") == "GB_T",
        embedded_format_path=embedded_format_path,
    )
    if sheet_setup.get("status") != "pass":
        return {"status": sheet_setup.get("status", "blocked"), "stage": "sheet", "sheetSetup": sheet_setup, "error_code": sheet_setup.get("error_code"), "manual_review_required": True}
    model_size_m = tuple(float(value) / 1000.0 for value in model_size_mm)
    layout = plan_standard_view_layout(
        model_size_m,
        paper_size=paper_size,
        projection=projection,
        requested_scale=spec.get("scale"),
        sheet_dimensions_m=sheet_setup.get("sheet_size"),
    )
    if layout.get("status") != "pass":
        return {"status": "blocked", "stage": "layout", "sheetSetup": sheet_setup, "layout": layout, "error_code": layout.get("error_code"), "manual_review_required": True}
    views = create_adaptive_standard_views(drawing_model, str(source_model_path), layout)
    if views.get("status") != "pass":
        return {"status": "failed", "stage": "views", "sheetSetup": sheet_setup, "layout": layout, "views": views, "error_code": views.get("error_code"), "manual_review_required": True}
    professional_annotations = dict(spec.get("professionalAnnotations") or {})
    center_marks = auto_insert_center_marks(drawing_model, professional_annotations.get("centerMarks"))
    if center_marks.get("status") == "failed":
        save_required = center_marks.get("error_code") == "DRAWING_CENTER_MARK_DRAWING_SAVE_REQUIRED"
        return {
            "status": "blocked" if save_required else "failed",
            "stage": "professional_annotations",
            "professionalAnnotations": {"centerMarks": center_marks},
            "sheetSetup": sheet_setup,
            "layout": layout,
            "views": views,
            "error_code": center_marks.get("error_code"),
            "retryable": bool(center_marks.get("retryable")),
            "manual_review_required": True,
        }
    dimension_result = insert_dimensions(drawing_model) if spec.get("requiredDimensions") or spec.get("insertModelDimensions", True) else False
    arrangement = auto_arrange_drawing_dimensions(drawing_model) if dimension_result else {"status": "review_required", "error_code": "DRAWING_DIMENSIONS_NOT_REQUESTED"}
    bom_spec = spec.get("bom") or {}
    bom_requested = bool(bom_spec.get("required")) or spec.get("documentType") == "assembly"
    bom_result = {"requested": bom_requested, "created": False, "status": "not_requested"}
    if bom_requested:
        template_path = str(bom_spec.get("templatePath") or "")
        if not template_path or not Path(template_path).expanduser().is_file():
            return {"status": "blocked", "stage": "bom", "error_code": "DRAWING_BOM_TEMPLATE_MISSING", "message": "BOM 已要求但模板不存在，不能伪造表格交付。", "sheetSetup": sheet_setup, "layout": layout, "views": views, "manual_review_required": True}
        position = bom_spec.get("positionMm") or [250.0, 45.0]
        bom_table = insert_bom_table(drawing_model, template_path, float(position[0]) / 1000.0, float(position[1]) / 1000.0, int(bom_spec.get("bomType", 1)), str(bom_spec.get("configuration", "")))
        bom_result = {"requested": True, "created": bom_table is not None, "status": "pass" if bom_table is not None else "failed", "template_path": template_path, "position_mm": position, "error_code": None if bom_table is not None else "DRAWING_BOM_INSERT_FAILED"}
        if bom_table is None:
            return {"status": "failed", "stage": "bom", "bom": bom_result, "sheetSetup": sheet_setup, "layout": layout, "views": views, "manual_review_required": True}
    notes = []
    for index, note_text in enumerate(spec.get("notes") or []):
        note_result = add_note(drawing_model, 0.02, 0.02 + index * 0.006, str(note_text))
        notes.append(note_result)
        if note_result.get("status") != "pass":
            return {
                "status": "failed",
                "stage": "notes",
                "error_code": note_result.get("error_code") or "DRAWING_NOTE_COM_EVIDENCE_MISSING",
                "notes": notes,
                "sheetSetup": sheet_setup,
                "layout": layout,
                "views": views,
                "manual_review_required": True,
            }
    structure = inspect_drawing_structure(
        drawing_model,
        paper_size_hint=paper_size,
        title_block_box=layout.get("title_block_box"),
    )
    status = "pass" if structure.get("status") == "pass" and dimension_result else "review_required"
    return {
        "status": status,
        "stage": "review",
        "source_model": str(source_model_path),
        "standard": spec.get("standard"),
        "projection": projection,
        "sheetSetup": sheet_setup,
        "layout": layout,
        "views": views,
        "professionalAnnotations": {"centerMarks": center_marks},
        "dimensions": {"inserted": bool(dimension_result), "arrangement": arrangement},
        "bom": bom_result,
        "notes": notes,
        "structure": structure,
        "manual_review_required": True,
        "error_code": None if status == "pass" else "DRAWING_GENERATION_REVIEW_REQUIRED",
    }
