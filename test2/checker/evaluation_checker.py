"""Compare submitted evaluation_results.json with recalculated metrics."""

from __future__ import annotations

from typing import Any


STANDARD_FIELDS = (
    "hard_deadline_miss_rate",
    "soft_deadline_miss_rate",
    "average_tardiness",
    "max_tardiness",
    "average_response_time",
    "max_response_time",
    "completion_time_jitter",
    "sporadic_value_rate",
    "generator_cost",
    "market_revenue",
    "objective_value",
)
MARKET_EXTENSION_FIELDS = {"market_revenue", "objective_value"}


def compare_evaluation_results(
    context: dict[str, Any],
    recalculated: dict[str, Any],
    evidence: dict[str, Any],
    tolerance: float = 1e-3,
) -> dict[str, Any]:
    evaluation = context["data"].get("evaluation_results")
    warnings: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    has_market_extension = "market_mechanism" in set(evidence.get("detected_extensions", []))
    response_convention = _detect_response_time_convention(evaluation, recalculated, tolerance)

    for field in STANDARD_FIELDS:
        recalculated_value = recalculated.get(field)
        submitted_value = evaluation.get(field) if isinstance(evaluation, dict) else None
        if submitted_value is None:
            comparisons.append({"field": field, "recalculated_value": recalculated_value, "submitted_value": None, "difference": None, "status": "missing", "note": "field missing from evaluation_results.json"})
            continue
        if field in {"average_response_time", "max_response_time"}:
            comparison = _response_time_comparison(field, submitted_value, recalculated, response_convention, tolerance)
            comparisons.append(comparison)
            continue
        difference = _difference(submitted_value, recalculated_value)
        note = "possible Level 2 market mechanism changes this formula" if has_market_extension and field in MARKET_EXTENSION_FIELDS else ""
        if difference is None:
            comparisons.append({"field": field, "recalculated_value": recalculated_value, "submitted_value": submitted_value, "difference": None, "status": "mismatch", "note": "non-numeric submitted or recalculated value"})
        elif abs(difference) <= tolerance:
            comparisons.append({"field": field, "recalculated_value": recalculated_value, "submitted_value": submitted_value, "difference": difference, "status": "match", "note": note})
        else:
            mismatch_note = f"difference exceeds tolerance {tolerance}"
            if note:
                mismatch_note = f"{mismatch_note}; {note}"
            comparisons.append({"field": field, "recalculated_value": recalculated_value, "submitted_value": submitted_value, "difference": difference, "status": "mismatch", "note": mismatch_note})

    mismatch_count = sum(1 for item in comparisons if item["status"] == "mismatch")
    missing_count = sum(1 for item in comparisons if item["status"] == "missing")
    if mismatch_count:
        warnings.append({"section": "evaluation_consistency", "message": "submitted metrics differ from recalculated metrics", "count": mismatch_count})
    if missing_count:
        warnings.append({"section": "evaluation_consistency", "message": "submitted metrics missing", "count": missing_count})
    return {"comparisons": comparisons, "mismatch_count": mismatch_count, "missing_count": missing_count, "warnings": warnings, "errors": []}


def _difference(submitted: Any, recalculated: Any) -> float | None:
    if isinstance(submitted, bool) or isinstance(recalculated, bool):
        return None
    if not isinstance(submitted, int | float) or not isinstance(recalculated, int | float):
        return None
    return float(submitted) - float(recalculated)


def _detect_response_time_convention(evaluation: Any, recalculated: dict[str, Any], tolerance: float) -> str:
    if not isinstance(evaluation, dict):
        return "unknown"
    submitted_average = evaluation.get("average_response_time")
    submitted_max = evaluation.get("max_response_time")
    no_plus_one = (
        _matches(submitted_average, recalculated.get("average_response_time"), tolerance)
        and _matches(submitted_max, recalculated.get("max_response_time"), tolerance)
    )
    plus_one = (
        _matches(submitted_average, recalculated.get("average_response_time_plus_one"), tolerance)
        and _matches(submitted_max, recalculated.get("max_response_time_plus_one"), tolerance)
    )
    if no_plus_one and plus_one:
        return "ambiguous_both_match"
    if plus_one:
        return "completion_time - release_time + 1"
    if no_plus_one:
        return "completion_time - release_time"
    return "no_accepted_convention_matched"


def _response_time_comparison(
    field: str,
    submitted_value: Any,
    recalculated: dict[str, Any],
    convention: str,
    tolerance: float,
) -> dict[str, Any]:
    normal_value = recalculated.get(field)
    plus_one_value = recalculated.get(f"{field}_plus_one")
    selected_value = plus_one_value if convention == "completion_time - release_time + 1" else normal_value
    difference = _difference(submitted_value, selected_value)
    note = (
        f"detected response time convention: {convention}; "
        f"without +1={normal_value}; with +1={plus_one_value}"
    )
    if convention in {"completion_time - release_time", "completion_time - release_time + 1", "ambiguous_both_match"}:
        return {
            "field": field,
            "recalculated_value": selected_value,
            "submitted_value": submitted_value,
            "difference": difference,
            "status": "match",
            "note": note,
        }
    if difference is None:
        return {
            "field": field,
            "recalculated_value": normal_value,
            "submitted_value": submitted_value,
            "difference": None,
            "status": "mismatch",
            "note": f"non-numeric submitted or recalculated value; {note}",
        }
    return {
        "field": field,
        "recalculated_value": normal_value,
        "submitted_value": submitted_value,
        "difference": difference,
        "status": "mismatch",
        "note": f"difference exceeds tolerance {tolerance}; {note}",
    }


def _matches(submitted: Any, recalculated: Any, tolerance: float) -> bool:
    difference = _difference(submitted, recalculated)
    return difference is not None and abs(difference) <= tolerance
