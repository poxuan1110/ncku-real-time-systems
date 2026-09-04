"""File loading and checker output helpers."""

from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any


REQUIRED_JSON_FILES = {
    "processor_settings": Path("input/processor_settings.json"),
    "price_72hr": Path("input/price_72hr.json"),
    "task_set": Path("output/task_set.json"),
    "schedule_result": Path("output/schedule_result.json"),
    "evaluation_results": Path("output/evaluation_results.json"),
    "acceptance_test_log": Path("output/acceptance_test_log.json"),
}
OPTIONAL_JSON_FILES = {
    "event_file": Path("input/aperiodic_n_sporadic.json"),
}
AUXILIARY_ARTIFACT_PATTERNS = {
    "README.md": ("README.md",),
    "report.pdf": ("report.pdf",),
    "src": ("src",),
    "src/task_generator.*": ("src/task_generator.*",),
    "src/scheduler.*": ("src/scheduler.*",),
    "src/evaluator.*": ("src/evaluator.*",),
    "src/advanced_scheduler.*": ("src/advanced_scheduler.*",),
    "runtime_config.*": ("runtime_config.*",),
    "crontab.txt": ("crontab.txt",),
    "src/runtime_config.*": ("src/runtime_config.*",),
    "src/crontab.txt": ("src/crontab.txt",),
}


def ensure_output_dirs(submission_dir: Path, package_name: str = "level2", report_dir: Path | None = None) -> dict[str, Path]:
    root = report_dir if report_dir is not None else submission_dir / package_name / "reports"
    charts = root / "charts"
    intermediate = root / "intermediate"
    for path in (root, charts, intermediate):
        path.mkdir(parents=True, exist_ok=True)
    return {"root": root, "charts": charts, "intermediate": intermediate}


def load_submission_files(submission_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    loaded_files: dict[str, dict[str, Any]] = {}
    auxiliary_artifacts: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for name, relative_path in REQUIRED_JSON_FILES.items():
        _load_json_file(submission_dir, name, relative_path, data, loaded_files, errors, required=True)

    for name, relative_path in OPTIONAL_JSON_FILES.items():
        _load_json_file(submission_dir, name, relative_path, data, loaded_files, warnings, required=False)

    for label, patterns in AUXILIARY_ARTIFACT_PATTERNS.items():
        matches = _artifact_matches(submission_dir, patterns)
        auxiliary_artifacts[label] = {
            "patterns": list(patterns),
            "exists": bool(matches),
            "paths": matches,
        }

    return {
        "submission_dir": submission_dir,
        "data": data,
        "loaded_files": loaded_files,
        "auxiliary_artifacts": auxiliary_artifacts,
        "errors": errors,
        "warnings": warnings,
    }


def load_optional_event_file(submission_dir: Path, event_file: Path, context: dict[str, Any]) -> None:
    path = event_file if event_file.is_absolute() else submission_dir / event_file
    relative_label = str(event_file)
    loaded_files = context["loaded_files"]
    data = context["data"]
    warnings = context["warnings"]
    loaded_files["event_file"] = {
        "path": relative_label,
        "required": False,
        "exists": path.exists(),
        "loaded": False,
    }
    if not path.exists():
        data["event_file"] = None
        warnings.append(
            {
                "section": "io",
                "path": relative_label,
                "message": (
                    "official event file input/aperiodic_n_sporadic.json not found; "
                    "sporadic/aperiodic checks will rely on task_set and acceptance_test_log only."
                ),
            }
        )
        return
    if not path.is_file():
        data["event_file"] = None
        warnings.append({"section": "io", "path": relative_label, "message": "optional JSON path is not a file"})
        return
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            data["event_file"] = json.load(file_obj)
        loaded_files["event_file"]["loaded"] = True
    except JSONDecodeError as exc:
        data["event_file"] = None
        warnings.append({"section": "io", "path": relative_label, "message": f"invalid optional JSON: {exc.msg}"})
    except OSError as exc:
        data["event_file"] = None
        warnings.append({"section": "io", "path": relative_label, "message": f"could not read file: {exc}"})


def _load_json_file(
    submission_dir: Path,
    name: str,
    relative_path: Path,
    data: dict[str, Any],
    loaded_files: dict[str, dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    required: bool,
) -> None:
    path = submission_dir / relative_path
    loaded_files[name] = {
        "path": str(relative_path),
        "required": required,
        "exists": path.exists(),
        "loaded": False,
    }
    if not path.exists():
        data[name] = None
        if required:
            issues.append({"section": "io", "path": str(relative_path), "message": "required JSON file missing"})
        else:
            issues.append(
                {
                    "section": "io",
                    "path": str(relative_path),
                    "message": (
                        "official event file input/aperiodic_n_sporadic.json not found; "
                        "sporadic/aperiodic checks will rely on task_set and acceptance_test_log only."
                    ),
                }
            )
        return
    if not path.is_file():
        data[name] = None
        message = "required JSON path is not a file" if required else "optional JSON path is not a file"
        issues.append({"section": "io", "path": str(relative_path), "message": message})
        return
    try:
        with path.open("r", encoding="utf-8") as file_obj:
            data[name] = json.load(file_obj)
        loaded_files[name]["loaded"] = True
    except JSONDecodeError as exc:
        data[name] = None
        severity = "required" if required else "optional"
        issues.append({"section": "io", "path": str(relative_path), "message": f"invalid {severity} JSON: {exc.msg}"})
    except OSError as exc:
        data[name] = None
        issues.append({"section": "io", "path": str(relative_path), "message": f"could not read file: {exc}"})


def _artifact_matches(submission_dir: Path, patterns: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for pattern in patterns:
        for path in submission_dir.glob(pattern):
            try:
                matches.append(str(path.relative_to(submission_dir)))
            except ValueError:
                matches.append(str(path))
    return sorted(set(matches))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(_json_safe(payload), file_obj, ensure_ascii=False, indent=2)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
