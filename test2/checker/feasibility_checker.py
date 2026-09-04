"""Basic feasibility checks for final schedules."""

from __future__ import annotations

from math import ceil
from typing import Any


TOLERANCE = 1e-3


def check_feasibility(context: dict[str, Any], parsed: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    processor = context["data"].get("processor_settings")
    schedule_entries = parsed.get("schedule_entries", [])
    jobs = parsed.get("jobs", [])
    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    _check_jobs(jobs, schedule_entries, violations, warnings)
    _check_generators(processor, schedule_entries, violations, warnings)
    _check_renewable(processor, schedule_entries, evidence, violations, warnings)
    _check_storage(processor, schedule_entries, evidence, violations, warnings)
    _check_sell_and_balance(processor, schedule_entries, violations, warnings)

    return {
        "checks": {"violation_count": len(violations), "warning_count": len(warnings), "has_severe_violation": bool(violations)},
        "violations": violations,
        "warnings": warnings,
        "errors": [],
    }


def _check_jobs(jobs: list[dict[str, Any]], schedule_entries: list[dict[str, Any]], violations: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    by_t = {entry["t"]: entry for entry in schedule_entries if isinstance(entry.get("t"), int)}
    for job in jobs:
        if job.get("job_type") in {"charging", "unknown"}:
            continue
        job_id = job.get("job_id")
        slots = job.get("executed_slots") or []
        r = job.get("r")
        e = job.get("e")
        w = job.get("w")
        deadline = job.get("absolute_deadline")
        for t in slots:
            if isinstance(r, int) and t < r:
                violations.append({"check": "release_time", "job_id": job_id, "t": t, "message": "job executed before release time"})
            if isinstance(w, int | float):
                supplied = _sum_allocation(by_t.get(t, {}).get("k", {}).get(job_id))
                if abs(supplied - float(w)) > TOLERANCE:
                    violations.append({"check": "energy_demand", "job_id": job_id, "t": t, "expected": float(w), "actual": supplied})
        if job.get("job_type") == "periodic" or (job.get("job_type") == "sporadic" and not job.get("is_rejected_sporadic")):
            if isinstance(e, int) and len(slots) != e:
                violations.append({"check": "execution_time", "job_id": job_id, "expected": e, "actual": len(slots)})
            if job.get("completion_time") is None:
                violations.append({"check": "deadline", "job_id": job_id, "message": "hard deadline job not completed"})
            elif isinstance(deadline, int) and job["completion_time"] > deadline:
                violations.append({"check": "deadline", "job_id": job_id, "deadline": deadline, "completion_time": job["completion_time"]})
        if job.get("job_type") == "aperiodic" and (not job.get("is_completed") or job.get("is_soft_deadline_missed")):
            warnings.append({"check": "soft_deadline", "job_id": job_id, "message": "aperiodic job missed soft deadline or is unfinished"})
        if job.get("is_non_preemptive_violation"):
            violations.append({"check": "non_preemptive", "job_id": job_id, "executed_slots": slots})


def _check_generators(processor: Any, schedule_entries: list[dict[str, Any]], violations: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    generators = processor.get("generator") if isinstance(processor, dict) else None
    if not isinstance(generators, list):
        warnings.append({"check": "generator", "message": "processor_settings.generator missing or invalid"})
        return
    sorted_entries = _sorted_schedule_entries(schedule_entries)
    for generator in generators:
        if not isinstance(generator, dict) or not isinstance(generator.get("generator_id"), str):
            continue
        gid = generator["generator_id"]
        output_min = _to_number(generator.get("output_min")) or 0.0
        output_max = _to_number(generator.get("output_max"))
        ramp_up = _to_number(generator.get("ramp_up_rate"))
        ramp_down = _to_number(generator.get("ramp_down_rate"))
        previous = _to_number(generator.get("initial_energy")) or 0.0
        for entry in sorted_entries:
            output = _generator_output(entry, gid)
            if output > TOLERANCE and output < output_min - TOLERANCE:
                violations.append({"check": "generator_output_min", "generator_id": gid, "t": entry["t"], "actual": output, "minimum": output_min})
            if output_max is not None and output > output_max + TOLERANCE:
                violations.append({"check": "generator_output_max", "generator_id": gid, "t": entry["t"], "actual": output, "maximum": output_max})
            if ramp_up is not None and output - previous > ramp_up + TOLERANCE:
                violations.append({"check": "ramp_up", "generator_id": gid, "t": entry["t"], "previous": previous, "actual": output, "limit": ramp_up})
            if ramp_down is not None and previous - output > ramp_down + TOLERANCE:
                violations.append({"check": "ramp_down", "generator_id": gid, "t": entry["t"], "previous": previous, "actual": output, "limit": ramp_down})
            previous = output
        _check_generator_min_up_down_times(generator, sorted_entries, violations, warnings)


def _check_generator_min_up_down_times(
    generator: dict[str, Any],
    schedule_entries: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    gid = generator["generator_id"]
    horizon = _schedule_horizon(schedule_entries)
    states = _generator_on_states(gid, schedule_entries, horizon)
    initial_energy = _to_number(generator.get("initial_energy")) or 0.0
    previous_on = initial_energy > TOLERANCE

    min_up_time = _to_positive_int(generator.get("min_up_time"))
    min_down_time = _to_positive_int(generator.get("min_down_time"))
    if min_up_time is None:
        warnings.append({"check": "generator_min_up_time", "generator_id": gid, "message": "min_up_time missing or invalid; min-up check skipped"})
    if min_down_time is None:
        warnings.append({"check": "generator_min_down_time", "generator_id": gid, "message": "min_down_time missing or invalid; min-down check skipped"})

    _check_generator_initial_min_time(generator, states, horizon, violations, warnings)

    for t in range(1, horizon + 1):
        current_on = states.get(t, False)
        if min_up_time is not None and not previous_on and current_on:
            required_on_until = t + min_up_time - 1
            if required_on_until > horizon:
                warnings.append(
                    {
                        "check": "generator_min_up_time_horizon_truncated",
                        "generator_id": gid,
                        "startup_t": t,
                        "min_up_time": min_up_time,
                        "required_on_until": required_on_until,
                        "checked_until": horizon,
                    }
                )
            else:
                first_off_t = _first_state_mismatch(states, t, required_on_until, expected_on=True)
                if first_off_t is not None:
                    violations.append(
                        {
                            "check": "generator_min_up_time",
                            "generator_id": gid,
                            "startup_t": t,
                            "min_up_time": min_up_time,
                            "required_on_until": required_on_until,
                            "first_off_t": first_off_t,
                        }
                    )
        if min_down_time is not None and previous_on and not current_on:
            required_off_until = t + min_down_time - 1
            if required_off_until > horizon:
                warnings.append(
                    {
                        "check": "generator_min_down_time_horizon_truncated",
                        "generator_id": gid,
                        "shutdown_t": t,
                        "min_down_time": min_down_time,
                        "required_off_until": required_off_until,
                        "checked_until": horizon,
                    }
                )
            else:
                first_on_t = _first_state_mismatch(states, t, required_off_until, expected_on=False)
                if first_on_t is not None:
                    violations.append(
                        {
                            "check": "generator_min_down_time",
                            "generator_id": gid,
                            "shutdown_t": t,
                            "min_down_time": min_down_time,
                            "required_off_until": required_off_until,
                            "first_on_t": first_on_t,
                        }
                    )
        previous_on = current_on


def _check_generator_initial_min_time(
    generator: dict[str, Any],
    states: dict[int, bool],
    horizon: int,
    violations: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    gid = generator["generator_id"]
    initial_energy = _to_number(generator.get("initial_energy")) or 0.0
    if initial_energy > TOLERANCE:
        min_up_time = _to_positive_int(generator.get("min_up_time"))
        initial_on_time = _to_number(generator.get("initial_on_time"))
        if min_up_time is None:
            return
        if initial_on_time is None:
            warnings.append({"check": "generator_initial_on_time", "generator_id": gid, "message": "initial_on_time missing or invalid; initial min-up carryover check skipped"})
            return
        if initial_on_time >= min_up_time:
            return
        required_on_until = ceil(min_up_time - initial_on_time)
        if required_on_until > horizon:
            warnings.append(
                {
                    "check": "generator_initial_min_up_time_horizon_truncated",
                    "generator_id": gid,
                    "initial_on_time": initial_on_time,
                    "min_up_time": min_up_time,
                    "required_on_until": required_on_until,
                    "checked_until": horizon,
                }
            )
        first_off_t = _first_state_mismatch(states, 1, min(required_on_until, horizon), expected_on=True)
        if first_off_t is not None:
            violations.append(
                {
                    "check": "generator_initial_min_up_time",
                    "generator_id": gid,
                    "initial_on_time": initial_on_time,
                    "min_up_time": min_up_time,
                    "required_on_until": required_on_until,
                    "first_off_t": first_off_t,
                }
            )
    else:
        min_down_time = _to_positive_int(generator.get("min_down_time"))
        initial_off_time = _to_number(generator.get("initial_off_time"))
        if min_down_time is None:
            return
        if initial_off_time is None:
            warnings.append({"check": "generator_initial_off_time", "generator_id": gid, "message": "initial_off_time missing or invalid; initial min-down carryover check skipped"})
            return
        if initial_off_time >= min_down_time:
            return
        required_off_until = ceil(min_down_time - initial_off_time)
        if required_off_until > horizon:
            warnings.append(
                {
                    "check": "generator_initial_min_down_time_horizon_truncated",
                    "generator_id": gid,
                    "initial_off_time": initial_off_time,
                    "min_down_time": min_down_time,
                    "required_off_until": required_off_until,
                    "checked_until": horizon,
                }
            )
        first_on_t = _first_state_mismatch(states, 1, min(required_off_until, horizon), expected_on=False)
        if first_on_t is not None:
            violations.append(
                {
                    "check": "generator_initial_min_down_time",
                    "generator_id": gid,
                    "initial_off_time": initial_off_time,
                    "min_down_time": min_down_time,
                    "required_off_until": required_off_until,
                    "first_on_t": first_on_t,
                }
            )


def _sorted_schedule_entries(schedule_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((entry for entry in schedule_entries if isinstance(entry.get("t"), int)), key=lambda entry: entry["t"])


def _schedule_horizon(schedule_entries: list[dict[str, Any]]) -> int:
    times = [entry["t"] for entry in schedule_entries if isinstance(entry.get("t"), int)]
    return max(times) if times else 72


def _generator_on_states(gid: str, schedule_entries: list[dict[str, Any]], horizon: int) -> dict[int, bool]:
    output_by_t = {entry["t"]: _generator_output(entry, gid) for entry in schedule_entries if isinstance(entry.get("t"), int)}
    return {t: output_by_t.get(t, 0.0) > TOLERANCE for t in range(1, horizon + 1)}


def _generator_output(entry: dict[str, Any], gid: str) -> float:
    p_values = entry.get("P")
    if not isinstance(p_values, dict):
        return 0.0
    return _to_number(p_values.get(gid)) or 0.0


def _first_state_mismatch(states: dict[int, bool], start_t: int, end_t: int, expected_on: bool) -> int | None:
    for t in range(start_t, end_t + 1):
        if states.get(t, False) != expected_on:
            return t
    return None


def _check_renewable(processor: Any, schedule_entries: list[dict[str, Any]], evidence: dict[str, Any], violations: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    if not isinstance(processor, dict):
        return
    capacity_by_id = build_renewable_capacity_by_id(processor)
    forecast_by_id_hour = build_renewable_forecast_by_id_hour(processor)
    renewable_uncertainty = "renewable_uncertainty" in set(evidence.get("detected_extensions", []))
    if renewable_uncertainty:
        warnings.append({"check": "renewable_upper_bound", "message": "renewable upper bound may need actual output based Level 2 review"})
    for entry in schedule_entries:
        t = entry["t"]
        for source_id, output in entry.get("P", {}).items():
            source_text = str(source_id)
            if source_id not in capacity_by_id:
                if "renew" in source_text.lower() or "pv" in source_text.lower():
                    warnings.append({"check": "renewable_upper_bound", "source_id": source_id, "t": t, "message": "renewable source not found in renewable_capacity"})
                continue
            value = _to_number(output) or 0.0
            forecast = forecast_by_id_hour.get(str(source_id), {}).get(t)
            if forecast is None:
                warnings.append({"check": "renewable_upper_bound", "source_id": source_id, "t": t, "message": "renewable forecast missing for source_id and hour"})
                continue
            bound = capacity_by_id[str(source_id)] * forecast
            if value > bound + TOLERANCE:
                record = {"check": "renewable_upper_bound", "source_id": source_id, "t": t, "actual": value, "bound": bound}
                if renewable_uncertainty:
                    record["message"] = "basic forecast upper bound differs; Level 2 renewable uncertainty may require manual review"
                    warnings.append(record)
                else:
                    violations.append(record)


def build_renewable_capacity_by_id(processor_settings: Any) -> dict[str, float]:
    capacities: dict[str, float] = {}
    raw_capacity = processor_settings.get("renewable_capacity") if isinstance(processor_settings, dict) else None
    if not isinstance(raw_capacity, list):
        return capacities
    for item in raw_capacity:
        if not isinstance(item, dict) or not isinstance(item.get("renewable_id"), str):
            continue
        capacity = _to_number(item.get("capacity"))
        if capacity is not None:
            capacities[item["renewable_id"]] = capacity
    return capacities


def build_renewable_forecast_by_id_hour(processor_settings: Any) -> dict[str, dict[int, float]]:
    forecasts: dict[str, dict[int, float]] = {}
    raw_forecast = processor_settings.get("renewable_forecast") if isinstance(processor_settings, dict) else None
    if isinstance(raw_forecast, dict):
        groups = [raw_forecast]
    elif isinstance(raw_forecast, list):
        groups = raw_forecast
    else:
        return forecasts
    for group in groups:
        if not isinstance(group, dict):
            continue
        for renewable_id, entries in group.items():
            if not isinstance(renewable_id, str) or not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                hour = _to_int(entry.get("hour"))
                forecast = _to_number(entry.get("pv_forecast"))
                if hour is not None and forecast is not None:
                    forecasts.setdefault(renewable_id, {})[hour] = forecast
    return forecasts


def _check_storage(processor: Any, schedule_entries: list[dict[str, Any]], evidence: dict[str, Any], violations: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    realistic_battery = "battery_realistic_behavior" in set(evidence.get("detected_extensions", []))
    warnings.append(
        {
            "check": "storage_soc_transition",
            "message": "SOC bounds are checked directly. SOC transition equation may require manual review for Level 2 battery extensions.",
            "manual_review": True,
        }
    )
    if realistic_battery:
        warnings.append({"check": "storage_soc_update", "message": "storage SOC transition may differ due to Level 2 battery model.", "manual_review": True})
    storage = processor.get("storage") if isinstance(processor, dict) else None
    if not isinstance(storage, list):
        return
    for battery in storage:
        if not isinstance(battery, dict):
            continue
        bid = battery.get("storage_id") or battery.get("battery_id") or battery.get("id")
        if not isinstance(bid, str):
            continue
        soc_min = _to_number(battery.get("soc_min"))
        soc_max = _to_number(battery.get("soc_max"))
        for entry in schedule_entries:
            soc = _to_number(entry.get("soc", {}).get(bid))
            if soc is None:
                continue
            if soc_min is not None and soc < soc_min - TOLERANCE:
                violations.append({"check": "storage_soc_min", "storage_id": bid, "t": entry["t"], "actual": soc, "minimum": soc_min})
            if soc_max is not None and soc > soc_max + TOLERANCE:
                violations.append({"check": "storage_soc_max", "storage_id": bid, "t": entry["t"], "actual": soc, "maximum": soc_max})


def _check_sell_and_balance(processor: Any, schedule_entries: list[dict[str, Any]], violations: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    charging_jobs = _charging_jobs_from_processor(processor) | _charging_jobs_from_entries(schedule_entries)
    for entry in schedule_entries:
        sell = _to_number(entry.get("sell")) or 0.0
        if sell < -TOLERANCE:
            violations.append({"check": "sell_non_negative", "t": entry.get("t"), "actual": sell})
        supply = sum(_to_number(value) or 0.0 for value in entry.get("P", {}).values())
        external_job_demand = 0.0
        storage_charge_demand = 0.0
        for job_id, allocation in entry.get("k", {}).items():
            demand = _sum_allocation(allocation)
            if _is_charging_job(job_id, charging_jobs):
                storage_charge_demand += demand
            else:
                external_job_demand += demand
        total_k_demand = external_job_demand + storage_charge_demand
        difference = supply - total_k_demand - sell
        if abs(difference) > 1e-2:
            warnings.append(
                {
                    "check": "hourly_energy_balance",
                    "t": entry.get("t"),
                    "supply": supply,
                    "external_job_demand": external_job_demand,
                    "storage_charge_demand": storage_charge_demand,
                    "total_k_demand": total_k_demand,
                    "sell": sell,
                    "difference": difference,
                    "message": "basic balance differs; Level 2 fields may explain storage charge or other flows",
                }
            )


def _charging_jobs_from_entries(schedule_entries: list[dict[str, Any]]) -> set[str]:
    return {str(job_id) for entry in schedule_entries for job_id in entry.get("k", {}) if _is_charging_job(job_id, set())}


def _charging_jobs_from_processor(processor: Any) -> set[str]:
    raw_jobs = processor.get("charging_jobs") if isinstance(processor, dict) else None
    if not isinstance(raw_jobs, list):
        return set()
    result: set[str] = set()
    for item in raw_jobs:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict) and isinstance(item.get("job_id"), str):
            result.add(item["job_id"])
    return result


def _is_charging_job(job_id: Any, charging_jobs: set[str]) -> bool:
    return isinstance(job_id, str) and (job_id in charging_jobs or job_id.endswith("_chg"))


def _sum_allocation(allocation: Any) -> float:
    if isinstance(allocation, dict):
        return sum(_to_number(value) or 0.0 for value in allocation.values())
    return _to_number(allocation) or 0.0


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


def _to_positive_int(value: Any) -> int | None:
    number = _to_number(value)
    if number is None or number <= 0:
        return None
    integer = int(number)
    if abs(number - integer) > TOLERANCE:
        return None
    return integer
