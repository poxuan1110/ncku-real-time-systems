"""Parse objective schedule facts from schedule_result.json."""

from __future__ import annotations

from collections import defaultdict
import re
from statistics import pstdev
from typing import Any


HORIZON = 72
TOLERANCE = 1e-6
EXPANDED_JOB_ID_PATTERN = re.compile(r"^(?P<base>[psa]\w*?)_(?:j)?(?P<instance>[1-9]\d*)$")


def parse_schedule(context: dict[str, Any]) -> dict[str, Any]:
    data = context["data"]
    schedule_entries = _schedule_entries(data.get("schedule_result"))
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    official_events = _official_events_by_id(data.get("event_file"))
    acceptance = {
        **official_events,
        **_acceptance_entries(data.get("acceptance_test_log"), data.get("event_file"), warnings),
    }

    job_specs = _build_job_specs(data.get("task_set"), acceptance, warnings)
    executions = _executions_by_job(schedule_entries, job_specs, warnings)
    rejected_sporadic = sorted(
        {
            _base_job_id(job_id) or job_id
            for entry in schedule_entries
            for job_id in _string_list(entry.get("rejected_sporadic"))
        }
    )

    for job_id in executions:
        if job_id not in job_specs:
            job_specs[job_id] = _unknown_job_spec(job_id)
            if job_specs[job_id].get("job_type") != "charging":
                warnings.append({"section": "schedule_parser", "job_id": job_id, "message": "job appears in schedule but not in task_set or acceptance log"})

    jobs = [_summarize_job(spec, executions.get(job_id, set()), rejected_sporadic, acceptance) for job_id, spec in sorted(job_specs.items())]
    for job in jobs:
        if job["job_type"] not in {"charging", "unknown"} and not job["executed_slots"]:
            warnings.append({"section": "schedule_parser", "job_id": job["job_id"], "message": "job has no executed slots"})

    return {
        "jobs": jobs,
        "jobs_by_id": {job["job_id"]: job for job in jobs},
        "schedule_entries": schedule_entries,
        "rejected_sporadic": rejected_sporadic,
        "periodic_completion_time_jitter": _periodic_jitter(jobs),
        "warnings": warnings,
        "errors": errors,
    }


def _schedule_entries(raw_schedule: Any) -> list[dict[str, Any]]:
    raw_entries = raw_schedule.get("schedule_result") if isinstance(raw_schedule, dict) else raw_schedule
    if not isinstance(raw_entries, list):
        return []
    entries = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        t = _to_int(raw_entry.get("t"))
        if t is None:
            continue
        entries.append(
            {
                **raw_entry,
                "t": t,
                "P": raw_entry.get("P") if isinstance(raw_entry.get("P"), dict) else {},
                "k": raw_entry.get("k") if isinstance(raw_entry.get("k"), dict) else {},
                "soc": raw_entry.get("soc") if isinstance(raw_entry.get("soc"), dict) else {},
                "sell": _to_number(raw_entry.get("sell")) or 0.0,
            }
        )
    return sorted(entries, key=lambda item: item["t"])


def _build_job_specs(task_set: Any, acceptance: dict[str, dict[str, Any]], warnings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    if not isinstance(task_set, dict):
        return specs

    periodic = task_set.get("periodic")
    if isinstance(periodic, dict):
        for task_id, raw_task in periodic.items():
            parsed = _parse_task(task_id, raw_task, "periodic")
            if parsed is None:
                warnings.append({"section": "schedule_parser", "task_id": task_id, "message": "could not parse periodic task"})
                continue
            release = parsed["r"]
            instance = 1
            while release <= HORIZON:
                job_id = f"{task_id}_{instance}"
                specs[job_id] = {
                    **parsed,
                    "job_id": job_id,
                    "task_id": str(task_id),
                    "r": release,
                    "deadline_window_end": min(release + parsed["d"] - 1, HORIZON),
                    "absolute_deadline": release + parsed["d"] - 1,
                }
                instance += 1
                release += parsed["p"]

    for job_type in ("sporadic", "aperiodic"):
        raw_jobs = task_set.get(job_type)
        if isinstance(raw_jobs, dict):
            for job_id, raw_job in raw_jobs.items():
                parsed = _parse_task(job_id, raw_job, job_type)
                if parsed is not None:
                    specs[str(job_id)] = {
                        **parsed,
                        "job_id": str(job_id),
                        "task_id": str(job_id),
                        "deadline_window_end": parsed["r"] + parsed["d"] - 1,
                        "absolute_deadline": parsed["r"] + parsed["d"] - 1,
                    }

    for job_id, raw_entry in acceptance.items():
        if job_id in specs:
            continue
        job_type = raw_entry.get("type")
        if job_type not in {"sporadic", "aperiodic"}:
            continue
        r = _to_int(raw_entry.get("release_time"))
        absolute_deadline = _to_int(raw_entry.get("abs_deadline"))
        e = _to_int(raw_entry.get("execution_time"))
        w = _to_number(raw_entry.get("energy_demand"))
        if r is None or absolute_deadline is None or e is None:
            continue
        specs[job_id] = {
            "job_id": job_id,
            "job_type": job_type,
            "task_id": job_id,
            "r": r,
            "e": e,
            "d": absolute_deadline - r + 1,
            "deadline_window_end": absolute_deadline,
            "absolute_deadline": absolute_deadline,
            "w": w,
            "preempt": raw_entry.get("preempt"),
        }

    return specs


def _parse_task(task_id: Any, raw_task: Any, job_type: str) -> dict[str, Any] | None:
    if not isinstance(raw_task, dict):
        return None
    r = _to_int(raw_task.get("r", 1))
    p = _to_int(raw_task.get("p"))
    e = _to_int(raw_task.get("e"))
    d = _to_int(raw_task.get("d"))
    w = _to_number(raw_task.get("w"))
    if r is None or e is None or d is None:
        return None
    if job_type == "periodic" and p is None:
        return None
    return {
        "job_id": str(task_id),
        "job_type": job_type,
        "task_id": str(task_id),
        "r": r,
        "p": p,
        "e": e,
        "d": d,
        "deadline_window_end": r + d - 1,
        "absolute_deadline": r + d - 1,
        "w": w,
        "preempt": raw_task.get("preempt"),
    }


def _acceptance_entries(raw_log: Any, event_file: Any, warnings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    raw_entries = raw_log.get("acceptance_test_log") if isinstance(raw_log, dict) else None
    if not isinstance(raw_entries, list):
        if raw_log is not None:
            warnings.append(
                {
                    "section": "schedule_parser",
                    "check": "acceptance_test_log_format",
                    "message": "acceptance_test_log format not recognized; fallback to schedule_result.rejected_sporadic and final schedule facts.",
                }
            )
        return {}
    entries: dict[str, dict[str, Any]] = {}
    unrecognized = 0
    for entry in raw_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("job_id"), str):
            unrecognized += 1
            continue
        normalized = dict(entry)
        official_event = _official_event_fields(event_file, entry["job_id"])
        if official_event is not None:
            normalized.update(official_event)
        decision = _acceptance_decision(entry)
        if decision is not None:
            normalized["accepted"] = decision
        entries[entry["job_id"]] = normalized
    if unrecognized:
        warnings.append(
            {
                "section": "schedule_parser",
                "check": "acceptance_test_log_format",
                "message": "some acceptance_test_log entries were not recognized; fallback to schedule_result facts for those entries.",
                "count": unrecognized,
            }
        )
    return entries


def _official_event_fields(event_file: Any, job_id: str) -> dict[str, Any] | None:
    return _official_events_by_id(event_file).get(job_id)


def _official_events_by_id(event_file: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(event_file, dict):
        return {}
    events: dict[str, dict[str, Any]] = {}
    for job_type in ("sporadic", "aperiodic"):
        raw_jobs = event_file.get(job_type)
        if not isinstance(raw_jobs, dict):
            continue
        for job_id, raw_job in raw_jobs.items():
            if not isinstance(job_id, str) or not isinstance(raw_job, dict):
                continue
            release_time = _to_int(raw_job.get("r"))
            relative_deadline = _to_int(raw_job.get("d"))
            execution_time = _to_int(raw_job.get("e"))
            energy_demand = _to_number(raw_job.get("w"))
            if release_time is None or relative_deadline is None or execution_time is None:
                continue
            result: dict[str, Any] = {
                "job_id": job_id,
                "type": job_type,
                "release_time": release_time,
                "abs_deadline": release_time + relative_deadline - 1,
                "execution_time": execution_time,
            }
            if energy_demand is not None:
                result["energy_demand"] = energy_demand
            events[job_id] = result
    return events


def _executions_by_job(
    schedule_entries: list[dict[str, Any]],
    job_specs: dict[str, dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, set[int]]:
    executions: dict[str, set[int]] = defaultdict(set)
    compatibility_samples: list[dict[str, Any]] = []
    for entry in schedule_entries:
        normalized_k: dict[str, Any] = {}
        for raw_job_id, allocation in entry.get("k", {}).items():
            if not isinstance(raw_job_id, str) or _sum_allocation(allocation) <= TOLERANCE:
                continue
            canonical_id, suggested_key, is_compat = _canonical_job_id(raw_job_id, entry["t"], job_specs)
            executions[canonical_id].add(entry["t"])
            _merge_allocation(normalized_k, canonical_id, allocation)
            if is_compat:
                compatibility_samples.append(
                    {
                        "t": entry["t"],
                        "raw_k_key": raw_job_id,
                        "suggested_key": suggested_key,
                        "base_task_id": suggested_key,
                    }
                )
        if normalized_k:
            entry["k_raw"] = entry.get("k", {})
            entry["k"] = normalized_k
    if compatibility_samples:
        warnings.append(
            {
                "section": "schedule_parser",
                "check": "expanded_job_id_compatibility",
                "message": "expanded job ids such as p1_j1 are accepted for compatibility, but the standard schedule_result.k format should use base task ids such as p1.",
                "count": len(compatibility_samples),
                "samples": compatibility_samples[:10],
            }
        )
    return executions


def _canonical_job_id(raw_job_id: str, t: int, job_specs: dict[str, dict[str, Any]]) -> tuple[str, str, bool]:
    if raw_job_id.endswith("_chg"):
        return raw_job_id, raw_job_id, False
    if raw_job_id in job_specs:
        return raw_job_id, raw_job_id, False

    expanded = EXPANDED_JOB_ID_PATTERN.fullmatch(raw_job_id)
    if expanded is not None:
        base = expanded.group("base")
        instance = expanded.group("instance")
        periodic_id = f"{base}_{instance}"
        if periodic_id in job_specs:
            return periodic_id, base, True
        if base in job_specs:
            return base, base, True
        return raw_job_id, base, True

    if raw_job_id in {str(spec.get("task_id")) for spec in job_specs.values() if spec.get("job_type") == "periodic"}:
        job_id = _periodic_instance_for_time(raw_job_id, t, job_specs)
        if job_id is not None:
            return job_id, raw_job_id, False
    return raw_job_id, raw_job_id, False


def _periodic_instance_for_time(task_id: str, t: int, job_specs: dict[str, dict[str, Any]]) -> str | None:
    candidates = [
        spec
        for spec in job_specs.values()
        if spec.get("job_type") == "periodic"
        and spec.get("task_id") == task_id
        and isinstance(spec.get("r"), int)
        and isinstance(spec.get("deadline_window_end"), int)
        and spec["r"] <= t <= spec["deadline_window_end"]
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda spec: (spec["r"], spec["job_id"]))
    return str(candidates[0]["job_id"])


def _base_job_id(job_id: str) -> str | None:
    if job_id.endswith("_chg"):
        return None
    match = EXPANDED_JOB_ID_PATTERN.fullmatch(job_id)
    return match.group("base") if match is not None else None


def _merge_allocation(target: dict[str, Any], job_id: str, allocation: Any) -> None:
    if job_id not in target:
        target[job_id] = allocation
        return
    if isinstance(target[job_id], dict) and isinstance(allocation, dict):
        for source_id, value in allocation.items():
            target[job_id][source_id] = (_to_number(target[job_id].get(source_id)) or 0.0) + (_to_number(value) or 0.0)
    else:
        target[job_id] = _sum_allocation(target[job_id]) + _sum_allocation(allocation)


def _acceptance_decision(entry: dict[str, Any]) -> bool | None:
    for field in ("accepted", "accept", "is_accepted"):
        value = entry.get(field)
        if isinstance(value, bool):
            return value
    for field in ("decision", "result", "status"):
        value = entry.get(field)
        if not isinstance(value, str):
            continue
        lowered = value.strip().lower()
        if lowered in {"accept", "accepted"}:
            return True
        if lowered in {"reject", "rejected"}:
            return False
    return None


def _summarize_job(
    spec: dict[str, Any],
    executed_slots_set: set[int],
    rejected_sporadic: list[str],
    acceptance: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    executed_slots = sorted(executed_slots_set)
    completion_time = max(executed_slots) if executed_slots else None
    r = spec.get("r")
    e = spec.get("e")
    deadline = spec.get("absolute_deadline")
    # Match the Level 1 metric convention: executed hour t completes at t, so
    # response_time is measured as completion_time - release_time.
    response_time = completion_time - r if completion_time is not None and isinstance(r, int) else None
    tardiness = max(0, completion_time - deadline) if completion_time is not None and isinstance(deadline, int) else None
    is_completed = len(executed_slots) >= e if isinstance(e, int) else bool(executed_slots)
    job_type = spec.get("job_type")
    accepted = acceptance.get(spec["job_id"], {}).get("accepted")
    is_rejected_sporadic = spec["job_id"] in rejected_sporadic or accepted is False
    hard_job = job_type == "periodic" or (job_type == "sporadic" and not is_rejected_sporadic)
    soft_job = job_type == "aperiodic"
    hard_miss = hard_job and (not is_completed or (completion_time is not None and isinstance(deadline, int) and completion_time > deadline))
    soft_miss = soft_job and (not is_completed or (completion_time is not None and isinstance(deadline, int) and completion_time > deadline))
    return {
        **spec,
        "executed_slots": executed_slots,
        "total_executed_slots": len(executed_slots),
        "completion_time": completion_time,
        "response_time": response_time,
        "tardiness": tardiness,
        "is_completed": is_completed,
        "is_hard_deadline_missed": bool(hard_miss),
        "is_soft_deadline_missed": bool(soft_miss),
        "is_non_preemptive_violation": _non_preemptive_violation(executed_slots, e, spec.get("preempt")),
        "is_rejected_sporadic": is_rejected_sporadic,
    }


def _periodic_jitter(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for job in jobs:
        if job.get("job_type") == "periodic" and job.get("completion_time") is not None:
            grouped[str(job.get("task_id"))].append(int(job["completion_time"]))
    return {
        task_id: {
            "definition": "population standard deviation of completion times",
            "completion_times": values,
            "stddev": pstdev(values) if len(values) > 1 else 0.0,
            "max_minus_min": max(values) - min(values) if values else 0.0,
        }
        for task_id, values in grouped.items()
    }


def _unknown_job_spec(job_id: str) -> dict[str, Any]:
    return {"job_id": job_id, "job_type": _infer_job_type(job_id), "task_id": None, "r": None, "e": None, "d": None, "deadline_window_end": None, "absolute_deadline": None, "w": None, "preempt": None}


def _infer_job_type(job_id: str) -> str:
    lowered = job_id.lower()
    if lowered.endswith("_chg") or "charge" in lowered:
        return "charging"
    if lowered.startswith("s"):
        return "sporadic"
    if lowered.startswith("a"):
        return "aperiodic"
    if lowered.startswith("p"):
        return "periodic"
    return "unknown"


def _non_preemptive_violation(executed_slots: list[int], e: Any, preempt: Any) -> bool:
    if preempt not in {0, False, "0", "false", "False"} or not isinstance(e, int) or e <= 1 or len(executed_slots) <= 1:
        return False
    return executed_slots != list(range(min(executed_slots), min(executed_slots) + len(executed_slots)))


def _sum_allocation(allocation: Any) -> float:
    if isinstance(allocation, dict):
        return sum(_to_number(value) or 0.0 for value in allocation.values())
    return _to_number(allocation) or 0.0


def _string_list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


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
