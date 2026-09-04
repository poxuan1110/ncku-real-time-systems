"""Detect Level 2 evidence without automatic assessment."""

from __future__ import annotations

from pathlib import Path
from typing import Any


KEYWORDS = {
    "renewable_uncertainty": {"uncertainty", "forecast_error", "pv_actual", "actual renewable"},
    "battery_realistic_behavior": {"efficiency", "aging", "self-discharge", "self_discharge", "cycle", "soc-dependent", "charge_efficiency", "discharge_efficiency"},
    "market_mechanism": {"commitment", "penalty", "real-time", "real_time", "day-ahead", "day_ahead"},
    "precedence_constraint": {"precedence", "predecessor", "successor"},
    "dynamic_rescheduling": {"level 2", "dynamic", "rolling horizon", "rolling_horizon", "replan", "update_time", "event_time", "decision_time"},
}


def detect_level2_evidence(context: dict[str, Any]) -> dict[str, Any]:
    submission_dir: Path = context["submission_dir"]
    data = context["data"]
    detected: set[str] = set()
    evidence: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    _scan_artifact_paths(submission_dir, detected, evidence)
    _scan_readme(submission_dir / "README.md", detected, evidence)
    if (submission_dir / "report.pdf").exists():
        evidence.append({"source": "report.pdf", "path": "report.pdf", "message": "report.pdf exists for manual review"})

    for file_name, payload in (
        ("output/schedule_result.json", data.get("schedule_result")),
        ("output/evaluation_results.json", data.get("evaluation_results")),
        ("output/acceptance_test_log.json", data.get("acceptance_test_log")),
        ("output/task_set.json", data.get("task_set")),
        ("input/processor_settings.json", data.get("processor_settings")),
    ):
        for path, key in _walk_keys(payload):
            category = _classify_key(key)
            if category:
                detected.add(category)
                evidence.append({"source": file_name, "path": path, "field": key, "category": category})

    if "market_mechanism" in detected:
        warnings.append({"category": "market_mechanism", "message": "market_revenue may require manual review due to Level 2 market fields"})
    if "battery_realistic_behavior" in detected:
        warnings.append({"category": "battery_realistic_behavior", "message": "storage SOC update may require manual review due to Level 2 storage fields"})
    if "renewable_uncertainty" in detected:
        warnings.append({"category": "renewable_uncertainty", "message": "renewable upper bound may need actual output based Level 2 review"})

    return {
        "manual_review_required": True,
        "interpretation_note": (
            "Level 2 evidence detection is only a hint for manual review. "
            "No detected evidence does not necessarily mean the submission has no Level 2 work. "
            "Please verify Level 2 assumptions, modeling, and dynamic scheduling design from README, report.pdf, source code, and demo explanation."
        ),
        "scan_root": str(submission_dir),
        "scan_paths": _scan_paths(),
        "detected_extensions": sorted(detected),
        "evidence": evidence,
        "warnings": warnings,
        "review_questions": _review_questions(sorted(detected)),
        "errors": [],
    }


def _scan_artifact_paths(submission_dir: Path, detected: set[str], evidence: list[dict[str, Any]]) -> None:
    for pattern, category in _scan_patterns():
        for path in submission_dir.glob(pattern):
            detected.add(category)
            evidence.append({"source": "filesystem", "path": str(path.relative_to(submission_dir)), "category": category})


def _scan_patterns() -> list[tuple[str, str]]:
    return [
        ("src/advanced_scheduler.*", "dynamic_rescheduling"),
        ("runtime_config.*", "dynamic_rescheduling"),
        ("crontab.txt", "dynamic_rescheduling"),
        ("src/runtime_config.*", "dynamic_rescheduling"),
        ("src/crontab.txt", "dynamic_rescheduling"),
    ]


def _scan_paths() -> list[str]:
    return [pattern for pattern, _category in _scan_patterns()]


def _scan_readme(path: Path, detected: set[str], evidence: list[dict[str, Any]]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    lowered = text.lower()
    for category, keywords in KEYWORDS.items():
        hits = sorted(keyword for keyword in keywords if keyword.lower() in lowered)
        if hits:
            detected.add(category)
            evidence.append({"source": "README.md", "category": category, "keywords": hits})


def _walk_keys(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}"
            keys.append((path, key_text))
            keys.extend(_walk_keys(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value[:5]):
            keys.extend(_walk_keys(item, f"{prefix}[{index}]"))
    return keys


def _classify_key(key: str) -> str | None:
    key_lower = key.lower()
    for category, keywords in KEYWORDS.items():
        if any(keyword.lower() in key_lower for keyword in keywords):
            return category
    extras = {
        "pv_actual",
        "forecast_error",
        "replan_id",
        "update_time",
        "rolling_horizon",
        "commitment",
        "penalty",
        "real_time_price",
        "charge_efficiency",
        "discharge_efficiency",
        "self_discharge",
        "cycle_count",
        "precedence",
        "predecessor",
        "successor",
    }
    return "unknown_extra_fields" if key_lower in extras else None


def _review_questions(detected: list[str]) -> list[str]:
    questions = [
        "請學生說明 Level 2 放寬了哪些 assumptions。",
        "請學生說明新增欄位如何影響限制式與 objective value。",
        "請學生說明 Level 2 方法和 Level 1 static schedule 的差異。",
        "請學生根據甘特圖說明 miss、reject、replan 或成本上升的原因。",
        "請學生說明 rejected sporadic jobs 的原因。",
    ]
    if "dynamic_rescheduling" in detected:
        questions.append("請學生說明每次 replan 的觸發時機。")
    if "market_mechanism" in detected:
        questions.append("請學生說明 market revenue 是否包含 commitment penalty。")
    if "battery_realistic_behavior" in detected:
        questions.append("請學生說明 battery SOC 變化是否考慮效率或自放電。")
    if "precedence_constraint" in detected:
        questions.append("請學生說明 job precedence 如何在 schedule_result 中被滿足。")
    return questions
