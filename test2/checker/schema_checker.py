"""Schema and required-field checks."""

from __future__ import annotations

from typing import Any


SCHEDULE_ENTRY_FIELDS = ("t", "P", "k", "sell", "soc", "missed_aperiodic", "rejected_sporadic")
EVALUATION_REQUIRED = (
    "hard_deadline_miss_rate",
    "soft_deadline_miss_rate",
    "average_tardiness",
    "max_tardiness",
    "average_response_time",
    "max_response_time",
    "completion_time_jitter",
    "acceptance_test",
    "sporadic_value_rate",
    "post_acceptance_violation_rate",
    "generator_cost",
    "market_revenue",
    "objective_value",
)
PROCESSOR_REQUIRED = ("generator", "renewable_capacity", "renewable_forecast", "storage")
HORIZON = 72


def run_schema_checks(context: dict[str, Any]) -> dict[str, Any]:
    data = context["data"]
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    extra_field_evidence: list[dict[str, Any]] = []

    task_set = data.get("task_set")
    if not isinstance(task_set, dict):
        errors.append({"file": "output/task_set.json", "message": "top-level JSON value must be an object"})
    else:
        if not isinstance(task_set.get("periodic"), dict):
            errors.append({"file": "output/task_set.json", "field": "periodic", "message": "periodic must be readable"})
        for group in ("sporadic", "aperiodic"):
            if group in task_set and not isinstance(task_set[group], dict):
                warnings.append({"file": "output/task_set.json", "field": group, "message": "optional job group is not an object"})
        _record_extra_fields(task_set, {"periodic", "sporadic", "aperiodic", "frame_size", "meta"}, "output/task_set.json", extra_field_evidence)

    schedule = _schedule_array(data.get("schedule_result"))
    if schedule is None:
        errors.append({"file": "output/schedule_result.json", "field": "schedule_result", "message": "schedule_result array missing"})
    else:
        if len(schedule) != HORIZON:
            errors.append({"file": "output/schedule_result.json", "field": "schedule_result", "message": "schedule_result must contain 72 entries", "actual": len(schedule), "expected": HORIZON})
        time_values: list[int] = []
        invalid_time_indexes: list[int] = []
        for index, entry in enumerate(schedule):
            if not isinstance(entry, dict):
                errors.append({"file": "output/schedule_result.json", "index": index, "message": "schedule entry must be an object"})
                continue
            missing = [field for field in SCHEDULE_ENTRY_FIELDS if field not in entry]
            if missing:
                errors.append({"file": "output/schedule_result.json", "index": index, "message": "schedule entry missing required fields", "fields": missing})
            t_value = _to_int(entry.get("t"))
            if t_value is None:
                invalid_time_indexes.append(index)
            else:
                time_values.append(t_value)
            for field in ("P", "k", "soc"):
                if field in entry and not isinstance(entry[field], dict):
                    errors.append({"file": "output/schedule_result.json", "index": index, "field": field, "message": "schedule field must be an object"})
            for field in ("missed_aperiodic", "rejected_sporadic"):
                if field in entry and not isinstance(entry[field], list):
                    errors.append({"file": "output/schedule_result.json", "index": index, "field": field, "message": "schedule field must be a list"})
            _record_extra_fields(entry, set(SCHEDULE_ENTRY_FIELDS), f"output/schedule_result.json[{index}]", extra_field_evidence)
        expected_times = set(range(1, HORIZON + 1))
        actual_times = set(time_values)
        missing_times = sorted(expected_times - actual_times)
        duplicate_times = sorted({t for t in time_values if time_values.count(t) > 1})
        extra_times = sorted(actual_times - expected_times)
        if invalid_time_indexes or missing_times or duplicate_times or extra_times:
            errors.append(
                {
                    "file": "output/schedule_result.json",
                    "field": "t",
                    "message": "schedule t indexes must be exactly 1..72 with no duplicates",
                    "missing": missing_times[:10],
                    "duplicates": duplicate_times[:10],
                    "extra": extra_times[:10],
                    "invalid_entries": invalid_time_indexes[:10],
                }
            )

    evaluation = data.get("evaluation_results")
    if not isinstance(evaluation, dict):
        errors.append({"file": "output/evaluation_results.json", "message": "top-level JSON value must be an object"})
    else:
        missing = [field for field in EVALUATION_REQUIRED if field not in evaluation]
        for field in missing:
            errors.append({"file": "output/evaluation_results.json", "field": field, "message": "required evaluation field missing"})
        _evaluation_type_errors(evaluation, errors)
        known = set(EVALUATION_REQUIRED) | {"acceptance_test", "sporadic_value_rate", "post_acceptance_violation_rate", "detail"}
        _record_extra_fields(evaluation, known, "output/evaluation_results.json", extra_field_evidence)

    processor = data.get("processor_settings")
    if not isinstance(processor, dict):
        errors.append({"file": "input/processor_settings.json", "message": "top-level JSON value must be an object"})
    else:
        for field in PROCESSOR_REQUIRED:
            if field not in processor:
                errors.append({"file": "input/processor_settings.json", "field": field, "message": "required processor field missing"})
        _processor_type_warnings(processor, warnings)

    price = data.get("price_72hr")
    price_values = price.get("price") if isinstance(price, dict) else None
    if not isinstance(price_values, list):
        errors.append({"file": "input/price_72hr.json", "field": "price", "message": "price must be a list"})
    elif len(price_values) < 72:
        warnings.append({"file": "input/price_72hr.json", "field": "price", "message": "fewer than 72 market price records found", "count": len(price_values)})

    event_file = data.get("event_file")
    if isinstance(event_file, dict):
        for group in ("sporadic", "aperiodic"):
            if not isinstance(event_file.get(group), dict):
                warnings.append({"file": "input/aperiodic_n_sporadic.json", "field": group, "message": f"{group} should be an object for event-based checks"})

    return {"errors": errors, "warnings": warnings, "extra_field_evidence": extra_field_evidence}


def _schedule_array(raw: Any) -> list[Any] | None:
    if isinstance(raw, dict) and isinstance(raw.get("schedule_result"), list):
        return raw["schedule_result"]
    if isinstance(raw, list):
        return raw
    return None


def _record_extra_fields(data: dict[str, Any], known_fields: set[str], path: str, evidence: list[dict[str, Any]]) -> None:
    extra = sorted(str(field) for field in data if field not in known_fields)
    if extra:
        evidence.append({"path": path, "extra_fields": extra})


def _evaluation_type_errors(evaluation: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    rate_fields = (
        "hard_deadline_miss_rate",
        "soft_deadline_miss_rate",
        "sporadic_value_rate",
        "post_acceptance_violation_rate",
    )
    non_negative_fields = (
        "average_tardiness",
        "max_tardiness",
        "average_response_time",
        "max_response_time",
        "completion_time_jitter",
    )
    number_fields = ("generator_cost", "market_revenue", "objective_value")

    for field in rate_fields:
        number = _to_number(evaluation.get(field))
        if field in evaluation and (number is None or not 0.0 <= number <= 1.0):
            errors.append({"file": "output/evaluation_results.json", "field": field, "message": "field must be a number in [0, 1]"})
    for field in non_negative_fields:
        number = _to_number(evaluation.get(field))
        if field in evaluation and (number is None or number < 0):
            errors.append({"file": "output/evaluation_results.json", "field": field, "message": "field must be a non-negative number"})
    for field in number_fields:
        if field in evaluation and _to_number(evaluation.get(field)) is None:
            errors.append({"file": "output/evaluation_results.json", "field": field, "message": "field must be a number"})
    if "acceptance_test" in evaluation and not isinstance(evaluation["acceptance_test"], dict):
        errors.append({"file": "output/evaluation_results.json", "field": "acceptance_test", "message": "field must be an object"})


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _processor_type_warnings(processor: dict[str, Any], warnings: list[dict[str, Any]]) -> None:
    expected_types = {
        "generator": list,
        "renewable_capacity": list,
        "storage": list,
    }
    for field, expected_type in expected_types.items():
        if field in processor and not isinstance(processor[field], expected_type):
            warnings.append({"file": "input/processor_settings.json", "field": field, "message": f"{field} should be a list for basic checks"})
    if "renewable_forecast" in processor and not isinstance(processor["renewable_forecast"], list | dict):
        warnings.append({"file": "input/processor_settings.json", "field": "renewable_forecast", "message": "renewable_forecast should be a list or dict for basic checks"})

    generator_required = ("generator_id", "output_min", "output_max", "ramp_up_rate", "ramp_down_rate")
    for index, generator in enumerate(processor.get("generator", []) if isinstance(processor.get("generator"), list) else []):
        if not isinstance(generator, dict):
            warnings.append({"file": "input/processor_settings.json", "field": "generator", "index": index, "message": "generator entry should be an object"})
            continue
        missing = [field for field in generator_required if field not in generator]
        if missing:
            warnings.append({"file": "input/processor_settings.json", "field": "generator", "index": index, "message": "generator entry missing fields", "missing_fields": missing})

    storage_required = ("storage_id", "soc_min", "soc_max", "discharge_max", "charge_max", "soc_init")
    for index, storage in enumerate(processor.get("storage", []) if isinstance(processor.get("storage"), list) else []):
        if not isinstance(storage, dict):
            warnings.append({"file": "input/processor_settings.json", "field": "storage", "index": index, "message": "storage entry should be an object"})
            continue
        missing = [field for field in storage_required if field not in storage]
        if missing:
            warnings.append({"file": "input/processor_settings.json", "field": "storage", "index": index, "message": "storage entry missing fields", "missing_fields": missing})

    renewable_required = ("renewable_id", "capacity")
    for index, renewable in enumerate(processor.get("renewable_capacity", []) if isinstance(processor.get("renewable_capacity"), list) else []):
        if not isinstance(renewable, dict):
            warnings.append({"file": "input/processor_settings.json", "field": "renewable_capacity", "index": index, "message": "renewable_capacity entry should be an object"})
            continue
        missing = [field for field in renewable_required if field not in renewable]
        if missing:
            warnings.append({"file": "input/processor_settings.json", "field": "renewable_capacity", "index": index, "message": "renewable_capacity entry missing fields", "missing_fields": missing})
