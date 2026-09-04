"""Recalculate basic schedule metrics from final schedule facts."""

from __future__ import annotations

from statistics import mean
from typing import Any


OBJECTIVE_ALPHA = 10000.0
TOLERANCE = 1e-6


def recalculate_metrics(context: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    data = context["data"]
    jobs = parsed.get("jobs", [])
    schedule_entries = parsed.get("schedule_entries", [])
    warnings: list[dict[str, Any]] = []

    hard_jobs = [job for job in jobs if job.get("job_type") == "periodic" or _accepted_sporadic(job)]
    hard_misses = [job for job in hard_jobs if job.get("is_hard_deadline_missed")]
    aperiodic_jobs = [job for job in jobs if job.get("job_type") == "aperiodic"]
    soft_misses = [job for job in aperiodic_jobs if job.get("is_soft_deadline_missed")]
    completed_jobs = [job for job in jobs if job.get("job_type") not in {"charging", "unknown"} and job.get("response_time") is not None]
    response_values = [float(job["response_time"]) for job in completed_jobs]
    response_values_plus_one = [value + 1 for value in response_values]
    tardiness_values = [
        float(job["tardiness"])
        for job in jobs
        if job.get("job_type") not in {"charging", "unknown"} and job.get("tardiness") is not None
    ]

    for job in jobs:
        if job.get("job_type") not in {"charging", "unknown"} and not job.get("is_completed"):
            warnings.append({"section": "metrics", "job_id": job.get("job_id"), "message": "unfinished job excluded from response time metrics"})

    generator_cost = _generator_cost(data.get("processor_settings"), schedule_entries, warnings)
    market_revenue = _market_revenue(data.get("price_72hr"), schedule_entries, warnings)
    sporadic_value_rate = _sporadic_value_rate(jobs, warnings)
    objective_value = OBJECTIVE_ALPHA * len(soft_misses) + generator_cost - market_revenue
    jitter_values = [item.get("stddev", 0.0) for item in parsed.get("periodic_completion_time_jitter", {}).values()]

    return {
        "hard_deadline_miss_rate": len(hard_misses) / len(hard_jobs) if hard_jobs else 0.0,
        "soft_deadline_miss_rate": len(soft_misses) / len(aperiodic_jobs) if aperiodic_jobs else 0.0,
        "average_tardiness": mean(tardiness_values) if tardiness_values else 0.0,
        "max_tardiness": max(tardiness_values) if tardiness_values else 0.0,
        "average_response_time": mean(response_values) if response_values else 0.0,
        "max_response_time": max(response_values) if response_values else 0.0,
        "average_response_time_plus_one": mean(response_values_plus_one) if response_values_plus_one else 0.0,
        "max_response_time_plus_one": max(response_values_plus_one) if response_values_plus_one else 0.0,
        "completion_time_jitter": mean(jitter_values) if jitter_values else 0.0,
        "completion_time_jitter_definition": "mean of per-periodic-task population standard deviation of completion times",
        "time_slot_convention": (
            "Accepted response-time conventions: completion_time = max(executed_slots); "
            "absolute_deadline = r + d - 1; response_time = completion_time - r or completion_time - r + 1; "
            "tardiness = max(0, completion_time - absolute_deadline)"
        ),
        "completed_response_time_job_count": len(response_values),
        "sporadic_value_rate": sporadic_value_rate,
        "basic_generator_cost": generator_cost,
        "generator_cost": generator_cost,
        "basic_market_revenue": market_revenue,
        "market_revenue": market_revenue,
        "basic_objective_value": objective_value,
        "objective_value": objective_value,
        "hard_deadline_missed_jobs": [job["job_id"] for job in hard_misses],
        "soft_deadline_missed_jobs": [job["job_id"] for job in soft_misses],
        "warnings": warnings,
        "errors": [],
    }


def _accepted_sporadic(job: dict[str, Any]) -> bool:
    return job.get("job_type") == "sporadic" and not job.get("is_rejected_sporadic")


def _sporadic_value_rate(jobs: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> float | None:
    sporadic = [job for job in jobs if job.get("job_type") == "sporadic"]
    denominator = sum(int(job.get("e") or 0) for job in sporadic)
    if denominator <= 0:
        warnings.append({"section": "metrics", "metric": "sporadic_value_rate", "message": "no sporadic jobs found"})
        return None
    numerator = sum(
        int(job.get("e") or 0)
        for job in sporadic
        if not job.get("is_rejected_sporadic") and job.get("is_completed") and not job.get("is_hard_deadline_missed")
    )
    return numerator / denominator


def _generator_cost(processor: Any, schedule_entries: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> float:
    generators = processor.get("generator") if isinstance(processor, dict) else None
    if not isinstance(generators, list):
        warnings.append({"section": "metrics", "metric": "generator_cost", "message": "processor_settings.generator missing or invalid"})
        return 0.0
    specs = {}
    for generator in generators:
        if not isinstance(generator, dict) or not isinstance(generator.get("generator_id"), str):
            continue
        fixed = _to_number(generator.get("cost_fixed"))
        variable = _to_number(generator.get("cost_variable"))
        specs[generator["generator_id"]] = {
            "fixed": fixed if fixed is not None else (_to_number(generator.get("cost_on")) or 0.0),
            "variable": variable if variable is not None else (_to_number(generator.get("cost_up")) or 0.0),
        }
    total = 0.0
    for entry in schedule_entries:
        for generator_id, spec in specs.items():
            output = _to_number(entry.get("P", {}).get(generator_id)) or 0.0
            if output > TOLERANCE:
                total += spec["fixed"] + spec["variable"] * output
    return total


def _market_revenue(price_72hr: Any, schedule_entries: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> float:
    prices = _price_by_hour(price_72hr)
    if not prices:
        warnings.append({"section": "metrics", "metric": "market_revenue", "message": "market price list missing or invalid"})
    return sum(prices.get(entry.get("t"), 0.0) * float(entry.get("sell") or 0.0) for entry in schedule_entries)


def _price_by_hour(price_72hr: Any) -> dict[int, float]:
    raw_prices = price_72hr.get("price") if isinstance(price_72hr, dict) else None
    if not isinstance(raw_prices, list):
        return {}
    prices = {}
    for index, raw_price in enumerate(raw_prices, start=1):
        if isinstance(raw_price, dict):
            hour = _to_int(raw_price.get("hour")) or index
            value = _to_number(raw_price.get("market_price"))
            if value is None:
                value = _to_number(raw_price.get("price"))
        else:
            hour = index
            value = _to_number(raw_price)
        if value is not None:
            prices[hour] = value
    return prices


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
