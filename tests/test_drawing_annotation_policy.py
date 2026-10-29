from scripts.drawing_annotation_policy import plan_duplicate_dimensions


def dim(index, *, key="D1", value=10.0, view="front", type2=2, source="part.sldprt", config="Default",
        prefix="", suffix="", tolerance="±0.1", visible=True):
    return {
        "identity": {"source": source, "configuration": config, "canonical_key": key},
        "type2": type2,
        "semantic_view": view,
        "visibility": "visible" if visible else "hidden",
        "visible": visible,
        "system_value": {"type": type2, "unit": "mm", "value": value},
        "native_text": {"full": "10", "prefix": prefix, "suffix": suffix, "above": "", "below": "", "tolerance": tolerance},
        "kind": "linear" if type2 != 6 else "diameter",
        "semantic_kind": "linear" if type2 != 6 else "diameter",
        "datum": "",
        "is_hole_callout": False,
        "hole_callout_variables": [],
        "name": f"D{index}",
    }


def test_37_dimensions_with_19_unique_identities_retain_one_each():
    dimensions = []
    for index in range(18):
        dimensions.extend([dim(index * 2, key=f"D{index}", view="front"),
                           dim(index * 2 + 1, key=f"D{index}", view="right")])
    dimensions.append(dim(36, key="D18", view="front"))
    result = plan_duplicate_dimensions({"dimensions": dimensions})
    assert len(dimensions) == 37
    assert len(result["retained_indexes"]) == 19
    assert len(result["hidden"]) == 18
    assert result["status"] == "pass"


def test_different_identity_value_or_native_text_is_never_hidden():
    dimensions = [
        dim(0, key="A", value=10), dim(1, key="B", value=10),
        dim(2, key="A", value=11), dim(3, key="A", prefix="R"),
        dim(4, key="A", config="Other"), dim(5, key="A", source="other.sldprt"),
    ]
    result = plan_duplicate_dimensions({"dimensions": dimensions})
    assert result["hidden"] == []
    assert result["retained_indexes"] == list(range(6))


def test_incomplete_or_unknown_evidence_is_retained_for_review():
    unknown = dim(0, key="A")
    del unknown["native_text"]["tolerance"]
    result = plan_duplicate_dimensions({"dimensions": [unknown, dim(1, key="A")]})
    assert result["hidden"] == []
    assert result["retained_indexes"] == [0, 1]
    assert result["status"] == "review_required"


def test_unknown_visibility_is_never_treated_as_visible_duplicate():
    unknown = dim(0, key="A")
    unknown.pop("visibility")
    result = plan_duplicate_dimensions({"dimensions": [unknown, dim(1, key="A")]})
    assert result["hidden"] == []
    assert result["retained_indexes"] == [0, 1]
    assert result["status"] == "review_required"


def test_semantic_fields_must_match_before_hiding():
    for field, value in (("kind", "angular"), ("semantic_kind", "ordinate"), ("datum", "A"),
                         ("native_text", {"full": "10", "prefix": "", "suffix": "", "above": "X", "below": "", "tolerance": "±0.1"})):
        first, second = dim(0, key="A"), dim(1, key="A")
        if field == "native_text":
            second[field] = value
        else:
            second[field] = value
        result = plan_duplicate_dimensions({"dimensions": [first, second]})
        assert result["hidden"] == []


def test_hole_callout_is_conservatively_retained_for_review():
    first, second = dim(0, key="A"), dim(1, key="A")
    first["is_hole_callout"] = True
    result = plan_duplicate_dimensions({"dimensions": [first, second]})
    assert result["hidden"] == []
    assert result["status"] == "review_required"


def test_diameter_prefers_circular_view():
    result = plan_duplicate_dimensions(
        {"dimensions": [dim(0, key="D", type2=6, view="front"), dim(1, key="D", type2=6, view="right")]},
        circular_views=("right",),
    )
    assert result["hidden"] == [{"index": 0, "keep_index": 1, "canonical_key": "D"}]


def test_linear_prefers_front_then_balances_same_priority():
    result = plan_duplicate_dimensions(
        {"dimensions": [dim(0, key="D", view="right"), dim(1, key="D", view="front")]}
    )
    assert result["hidden"] == [{"index": 0, "keep_index": 1, "canonical_key": "D"}]


def test_diameter_circular_budget_balances_six_equivalent_instances():
    dimensions = []
    for index in range(6):
        dimensions.extend([dim(index * 2, key=f"D{index}", type2=6, view="front"),
                           dim(index * 2 + 1, key=f"D{index}", type2=6, view="right")])
    result = plan_duplicate_dimensions({"dimensions": dimensions}, circular_views=("front",))
    kept = set(result["retained_indexes"])
    assert sum(index % 2 == 0 for index in kept) == 3
    assert sum(index % 2 == 1 for index in kept) == 3


def test_unique_circular_diameter_is_reserved_and_never_removed():
    dimensions = [dim(0, key="unique", type2=6, view="front"),
                  dim(1, key="dup", type2=6, view="front"),
                  dim(2, key="dup", type2=6, view="right")]
    result = plan_duplicate_dimensions({"dimensions": dimensions}, circular_views=("front",))
    assert 0 in result["retained_indexes"]
    assert len(result["hidden"]) == 1
