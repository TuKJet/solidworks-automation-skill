"""Pure, evidence-aware quality helpers for bounded drawing retries."""
from __future__ import annotations

import copy
import json
import math
from collections import Counter
from typing import Any, Mapping

STANDARD_DRAWING_SCALES = (10.0, 5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01)


def layout_candidate_specs(base_spec: Mapping[str, Any], observed_scale: float | None, max_attempts: int = 3) -> list[dict[str, Any]]:
    """Widen view spacing first; shrink at most one standard scale step.

    An explicit user scale is authoritative and yields no candidates.
    """
    if not isinstance(base_spec, Mapping) or base_spec.get("scale") is not None:
        return []
    try:
        scale = float(observed_scale)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(scale) or scale <= 0:
        return []
    limit = max(0, min(int(max_attempts) - 1, 2))
    smaller = [s for s in STANDARD_DRAWING_SCALES if s < scale - 1e-12]
    candidates = []
    for index, candidate_scale in enumerate(([scale] + smaller[:1])[:limit]):
        item = copy.deepcopy(dict(base_spec))
        item["scale"] = candidate_scale
        old = item.get("layoutOptions") or {}
        item["layoutOptions"] = {"viewGapMm": max(float(old.get("viewGapMm", 18)), 36.0 if index == 0 else 48.0),
                                 "marginMm": max(float(old.get("marginMm", 12)), 22.0 if index == 0 else 26.0)}
        candidates.append(item)
    return candidates


def quality_metrics(structure: Mapping[str, Any] | None, review: Mapping[str, Any] | None) -> dict[str, Any]:
    structure = structure if isinstance(structure, Mapping) else {}
    review = review if isinstance(review, Mapping) else {}
    findings = list(review.get("findings") or [])
    rendering = review.get("pdf_dimension_rendering") or review.get("dimension_rendering") or {}
    # The renderer's collision list already comes from real matched PDF boxes.
    # The raw PDF list also catches overlapping text that cannot be uniquely
    # associated with a model dimension, so ambiguous matching cannot hide it.
    confirmed = list(rendering.get("collisions") or [])
    raw_pdf_overlaps = [c for c in (review.get("pdf_evidence") or {}).get("overlaps", [])
                        if c.get("confirmedGeometryOverlap") or c.get("confirmedVisualDefect")]
    boundary_codes = {"DRAWING_OUTSIDE_SHEET", "DRAWING_VIEW_TITLE_BLOCK_INTRUSION", "DRAWING_DIMENSION_TITLE_BLOCK_INTRUSION"}
    outside = [f for f in findings if f.get("code") in boundary_codes and
               (f.get("confirmed_collision") is True or f.get("evidence_source") in {"native", "pdf_vector_text_bbox", "pdf_vector_text"})]
    dims = list(structure.get("dimensions") or [])
    visible = [(i, d) for i, d in enumerate(dims) if d.get("visibility") not in ("hidden", 3) and d.get("visible") is not False]
    unmatched_indexes = set(rendering.get("unmatched_dimension_indexes") or [])
    matched_indexes = {m.get("dimension_index") for m in rendering.get("matched") or []}
    unmatched = [d for i, d in visible if i in unmatched_indexes or i not in matched_indexes]
    estimated = [f for f in findings if f.get("evidence_source") in {"estimated", "unavailable"} or "estimated" in str(f.get("code", "")).lower()]
    retry = bool(visible and (confirmed or outside or raw_pdf_overlaps))
    return {"confirmed_overlap_count": len(confirmed), "pdf_text_overlap_count": len(raw_pdf_overlaps),
            "outside_or_title_intrusion_count": len(outside), "unmatched_visible_dimension_count": len(unmatched),
            "visible_dimension_count": len(visible), "estimated_risk_count": len(estimated), "needs_layout_retry": retry}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def dimension_signature(structure: Mapping[str, Any] | None):
    if not isinstance(structure, Mapping):
        return None
    rows = []
    for d in structure.get("dimensions") or []:
        identity = d.get("identity") or {}
        system = d.get("system_value") or {}
        if any(identity.get(k) in (None, "") for k in ("source", "configuration", "canonical_key")):
            return None
        value = system.get("value")
        if not _finite(value):
            return None
        semantics = {key: d.get(key) for key in ("text", "rendered_text", "kind", "semantic_kind", "tolerance", "datum", "is_hole_callout", "hole_callout_variables")}
        semantics["native_text_parts"] = ((d.get("box_evidence") or {}).get("text_evidence") or {}).get("parts")
        rows.append((str(identity["source"]).casefold(), str(identity["configuration"]), str(identity["canonical_key"]),
                     str(d.get("sheet")), str(d.get("semantic_view") or d.get("view")),
                     str(system.get("type", d.get("type"))), str(system.get("unit")), float(value), str(d.get("visibility")), d.get("visible") is not False,
                     json.dumps(semantics, ensure_ascii=False, sort_keys=True, default=str)))
    return tuple(sorted(Counter(rows).items()))


def accept_candidate(before_structure: Mapping[str, Any], after_structure: Mapping[str, Any], before_review: Mapping[str, Any], after_review: Mapping[str, Any]) -> bool:
    """Accept only an evidence-preserving candidate with strict improvement."""
    before_sig, after_sig = dimension_signature(before_structure), dimension_signature(after_structure)
    if not before_sig or before_sig != after_sig:
        return False
    def semantic_failures(review):
        failures = []
        for finding in review.get("findings") or []:
            code = str(finding.get("code", ""))
            if finding.get("severity", finding.get("status")) != "fail" or any(term in code for term in ("OVERLAP", "COLLISION", "OUTSIDE", "INTRUSION")):
                continue
            evidence = {key: finding.get(key) for key in ("missing", "missing_structure", "missing_pdf", "missing_position", "mismatches", "requirement_id")}
            failures.append((code, json.dumps(evidence, sort_keys=True, default=str)))
        return Counter(failures)
    if semantic_failures(after_review) - semantic_failures(before_review):
        return False
    old, new = quality_metrics(before_structure, before_review), quality_metrics(after_structure, after_review)
    if new["visible_dimension_count"] < old["visible_dimension_count"] or new["unmatched_visible_dimension_count"] > old["unmatched_visible_dimension_count"]:
        return False
    keys = ("confirmed_overlap_count", "pdf_text_overlap_count", "outside_or_title_intrusion_count", "unmatched_visible_dimension_count")
    if any(new[key] > old[key] for key in keys):
        return False
    # Reducing estimated risk alone is never evidence of improvement.
    return any(new[key] < old[key] for key in keys)
