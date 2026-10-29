"""Pure planning policy for conservative duplicate drawing dimensions.

This module only plans visibility changes.  A caller must apply the plan through
the native SolidWorks annotation API and re-audit the resulting drawing.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence


def _text_parts(dimension):
    evidence = dimension.get("text_evidence")
    if not isinstance(evidence, Mapping):
        box = dimension.get("box_evidence")
        evidence = box.get("text_evidence") if isinstance(box, Mapping) else None
    parts = evidence.get("parts") if isinstance(evidence, Mapping) else None
    values = {"full": None, "prefix": None, "suffix": None, "above": None, "below": None,
              "tolerance": dimension.get("tolerance")}
    if isinstance(parts, Sequence) and not isinstance(parts, (str, bytes)):
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            kind = str(part.get("kind", "")).casefold()
            aliases = {"all": "full", "callout_above": "above", "callout_below": "below"}
            key = aliases.get(kind, kind)
            if key in values:
                values[key] = part.get("text")
    native = dimension.get("native_text")
    if isinstance(native, Mapping):
        for key in values:
            if native.get(key) is not None:
                values[key] = native[key]
    for key in values:
        if dimension.get(key) is not None:
            values[key] = dimension[key]
    return values


def _json_value(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(value)


def _candidate(dimension, index):
    identity = dimension.get("identity")
    system = dimension.get("system_value")
    if not isinstance(identity, Mapping) or not isinstance(system, Mapping):
        return None
    source = identity.get("source", dimension.get("source"))
    configuration = identity.get("configuration", dimension.get("configuration"))
    canonical = identity.get("canonical_key", dimension.get("canonical_key"))
    type2 = system.get("type", dimension.get("type2", dimension.get("type")))
    unit = system.get("unit", dimension.get("unit"))
    value_key = next((key for key in ("value", "value_mm", "value_degrees", "value_radians")
                      if system.get(key) is not None), None)
    value = system.get(value_key) if value_key else None
    text = _text_parts(dimension)
    hole_callout = dimension.get("is_hole_callout")
    hole_variables = dimension.get("hole_callout_variables")
    if hole_callout is True or (isinstance(hole_variables, (list, tuple)) and hole_variables):
        return None
    semantic = {
        "kind": dimension.get("kind"),
        "semantic_kind": dimension.get("semantic_kind"),
        "datum": dimension.get("datum"),
        "is_hole_callout": hole_callout,
        "hole_callout_variables": hole_variables,
    }
    complete = all(value not in (None, "") for value in (source, configuration, canonical, type2, unit, value))
    complete = complete and all(text[key] is not None for key in ("full", "prefix", "suffix", "above", "below", "tolerance"))
    # kind/semantic_kind/datum are optional metadata; when present they remain
    # part of the equality key.  Visibility and hole-callout state are safety
    # gates and must be explicitly known before hiding anything.
    complete = complete and semantic["is_hole_callout"] is not None and semantic["hole_callout_variables"] is not None
    if not complete:
        return None
    try:
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
    except (TypeError, ValueError):
        return None
    view = str(dimension.get("semantic_view") or dimension.get("view") or "").casefold()
    return {
        "index": index,
        "source": str(source).casefold(),
        "configuration": str(configuration),
        "canonical_key": str(canonical),
        "type2": type2,
        "unit": str(unit),
        "value": numeric,
        "value_key": value_key,
        "prefix": _json_value(text["prefix"]),
        "suffix": _json_value(text["suffix"]),
        "full": _json_value(text["full"]),
        "above": _json_value(text["above"]),
        "below": _json_value(text["below"]),
        "tolerance": _json_value(text["tolerance"]),
        "kind": _json_value(semantic["kind"]),
        "semantic_kind": _json_value(semantic["semantic_kind"]),
        "datum": _json_value(semantic["datum"]),
        "is_hole_callout": _json_value(semantic["is_hole_callout"]),
        "hole_callout_variables": _json_value(semantic["hole_callout_variables"]),
        "view": view,
    }


def _is_visible(dimension):
    return dimension.get("visibility") == "visible" and dimension.get("visible") is not False


def _visibility_known(dimension):
    return dimension.get("visibility") in {"visible", "hidden", 0, 1, 3}


def _choose_keep(candidates, circular_views, load, diameter_load):
    circular = {str(value).casefold() for value in circular_views}
    is_diameter = any(str(item["type2"]).casefold() in {"6", "diameter"} for item in candidates)
    view_priority = {"front": 0, "right": 1, "top": 2}

    if is_diameter:
        circular_candidates = [item for item in candidates if item["view"] in circular]
        profile_candidates = [item for item in candidates if item["view"] not in circular]
        if circular_candidates and profile_candidates:
            if diameter_load[0] < 3:
                diameter_load[0] += 1
                return min(circular_candidates, key=lambda item: (load[item["view"]], item["index"]))
            return min(profile_candidates, key=lambda item: (load[item["view"]], view_priority.get(item["view"], 3), item["index"]))
        if circular_candidates:
            diameter_load[0] += 1
            return min(circular_candidates, key=lambda item: (load[item["view"]], item["index"]))
        return min(candidates, key=lambda item: (load[item["view"]], view_priority.get(item["view"], 3), item["index"]))

    best_priority = min(view_priority.get(item["view"], 3) for item in candidates)
    eligible = [item for item in candidates if view_priority.get(item["view"], 3) == best_priority]
    return min(eligible, key=lambda item: (load[item["view"]], item["index"]))


def plan_duplicate_dimensions(structure, circular_views=()):
    """Plan only safe, evidence-complete duplicate visibility changes."""
    dimensions = structure.get("dimensions") if isinstance(structure, Mapping) else None
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        return {"retained_indexes": [], "hidden": [], "status": "review_required"}
    retained = set(range(len(dimensions)))
    groups = defaultdict(list)
    review_required = False
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, Mapping):
            review_required = True
            continue
        if not _visibility_known(dimension):
            review_required = True
            continue
        if not _is_visible(dimension):
            continue
        candidate = _candidate(dimension, index)
        if candidate is None:
            review_required = True
            continue
        key = (candidate["source"], candidate["configuration"], candidate["canonical_key"],
               candidate["type2"], candidate["unit"], candidate["value_key"],
               candidate["value"], candidate["prefix"], candidate["suffix"], candidate["full"],
               candidate["above"], candidate["below"], candidate["tolerance"], candidate["kind"],
               candidate["semantic_kind"], candidate["datum"], candidate["is_hole_callout"],
               candidate["hole_callout_variables"])
        groups[key].append(candidate)

    load = defaultdict(int)
    diameter_load = [0]
    hidden = []
    circular = {str(value).casefold() for value in circular_views}
    # Reserve unique circular diameter references before choosing among
    # duplicate circle/profile instances; these references cannot be removed.
    for candidates in groups.values():
        if (len(candidates) == 1
                and str(candidates[0]["type2"]).casefold() in {"6", "diameter"}
                and candidates[0]["view"] in circular):
            diameter_load[0] += 1
        if len(candidates) == 1:
            load[candidates[0]["view"]] += 1
    for candidates in groups.values():
        if len(candidates) < 2:
            continue
        keep = _choose_keep(candidates, circular_views, load, diameter_load)
        load[keep["view"]] += 1
        for candidate in candidates:
            if candidate is keep:
                continue
            retained.discard(candidate["index"])
            hidden.append({"index": candidate["index"], "keep_index": keep["index"],
                           "canonical_key": keep["canonical_key"]})
    hidden.sort(key=lambda item: item["index"])
    return {"retained_indexes": sorted(retained), "hidden": hidden,
            "status": "review_required" if review_required else "pass"}
