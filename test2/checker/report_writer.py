"""Report writing for the Level 2 checker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import write_json


LINE = "=" * 78
SUBLINE = "-" * 78


def determine_overall_status(schema_check: dict[str, Any], consistency: dict[str, Any], feasibility: dict[str, Any], evidence: dict[str, Any]) -> str:
    if schema_check.get("errors"):
        return "invalid_submission_structure"
    if feasibility.get("checks", {}).get("has_severe_violation"):
        return "feasibility_violation_detected"
    if consistency.get("mismatch_count", 0) >= 3:
        return "metric_inconsistency_detected"
    if evidence.get("warnings"):
        return "manual_review_required"
    return "basic_checks_completed_with_manual_review"


def write_reports(output_dir: Path, report: dict[str, Any], submission_name: str) -> dict[str, str]:
    json_path = output_dir / f"{submission_name}_report.json"
    text_path = output_dir / f"{submission_name}_report.txt"
    write_json(json_path, report)
    text_path.write_text(_text_report(report), encoding="utf-8")

    for stale_name in ("checker_report.json", "checker_report.txt"):
        stale_path = output_dir / stale_name
        if stale_path.exists():
            stale_path.unlink()

    return {"json": str(json_path), "text": str(text_path)}


def _text_report(report: dict[str, Any]) -> str:
    lines: list[str] = []
    metadata = report.get("metadata", {})
    schema = report.get("schema_check", {})
    consistency = report.get("evaluation_consistency_check", {})
    feasibility = report.get("feasibility_check", {})
    evidence = report.get("level2_evidence", {})
    charts = report.get("chart_files", {})

    _section(lines, "Level 2 自動檢查報告", LINE)
    lines.extend(
        [
            f"繳交資料夾：{metadata.get('submission_path', 'N/A')}",
            f"整體狀態：{_status_text(str(report.get('overall_status')))}",
            "",
            "說明：本報告列出自動檢查可判定的問題。Level 2 延伸設計、公式假設、",
            "      market/battery/renewable 等自訂模型仍需搭配 report.pdf 與 source code 判讀。",
            "",
            "Response time 容納兩種慣例：completion_time - r，以及 completion_time - r + 1。",
        ]
    )

    _section(lines, "1. 檔案載入狀態")
    for name, item in report.get("loaded_files", {}).items():
        status = "OK" if item.get("loaded") else ("存在但未載入" if item.get("exists") else "缺少")
        required = "必要" if item.get("required") else "選用"
        lines.append(f"- {name:<24} {status:<10} {required:<4} {item.get('path', '')}")

    _section(lines, "2. 輔助檔案")
    artifacts = report.get("auxiliary_artifacts", {})
    if not artifacts:
        lines.append("- 無輔助檔案資訊")
    for name, item in artifacts.items():
        status = "存在" if item.get("exists") else "缺少"
        paths = item.get("paths") or item.get("patterns") or []
        lines.append(f"- {name:<24} {status:<4} {_join(paths)}")

    _section(lines, "3. JSON 結構檢查")
    _summary_counts(lines, errors=schema.get("errors", []), warnings=schema.get("warnings", []))
    _issue_block(lines, "錯誤", schema.get("errors", []), limit=12)
    _issue_block(lines, "警告", schema.get("warnings", []), limit=12)

    _section(lines, "4. 重算指標摘要")
    _metric_table(lines, report.get("recalculated_metrics", {}))

    _section(lines, "5. evaluation_results 一致性")
    comparisons = consistency.get("comparisons", [])
    mismatch_items = [item for item in comparisons if item.get("status") != "match"]
    lines.append(f"- 不一致：{consistency.get('mismatch_count', 0)}")
    lines.append(f"- 缺欄位：{consistency.get('missing_count', 0)}")
    if mismatch_items:
        lines.append("")
        lines.append("需要注意的欄位：")
        for item in mismatch_items:
            _comparison_lines(lines, item)
    else:
        lines.append("- 所有可比對指標皆一致。")

    response_items = [item for item in comparisons if item.get("field") in {"average_response_time", "max_response_time"}]
    if response_items:
        lines.append("")
        lines.append("Response time 算法判定：")
        for item in response_items:
            if item.get("note"):
                lines.append(f"- {item.get('field')}：{item.get('note')}")

    _section(lines, "6. 可行性檢查")
    _summary_counts(lines, errors=feasibility.get("violations", []), warnings=feasibility.get("warnings", []))
    _issue_block(lines, "違規", feasibility.get("violations", []), limit=15)
    _issue_group_summary(lines, "警告類型摘要", feasibility.get("warnings", []))
    _issue_block(lines, "警告樣本", feasibility.get("warnings", []), limit=10)

    _section(lines, "7. Level 2 延伸證據")
    detected = evidence.get("detected_extensions", [])
    lines.append(f"- 偵測到的延伸：{_join(detected) if detected else '未偵測到明確延伸'}")
    if evidence.get("scan_root"):
        lines.append(f"- 掃描路徑：{evidence.get('scan_root')}")
    if evidence.get("warnings"):
        _issue_block(lines, "延伸提醒", evidence.get("warnings", []), limit=10)
    if evidence.get("interpretation_note"):
        lines.append("")
        lines.append("判讀提醒：")
        lines.append(f"- {evidence.get('interpretation_note')}")

    _section(lines, "8. 圖表輸出")
    files = charts.get("files", {})
    if files:
        for name, path in files.items():
            lines.append(f"- {name:<28} {path or '未產生'}")
    else:
        lines.append("- 無圖表輸出資訊")
    _issue_block(lines, "圖表警告", charts.get("warnings", []), limit=8)

    _section(lines, "9. 問題總表")
    problems = _problem_items(report)
    if not problems:
        lines.append("- 未發現自動檢查錯誤或警告。")
    for index, item in enumerate(problems, start=1):
        lines.append(f"{index}. [{item['severity']}] {item['category']}")
        lines.append(f"   問題：{item['message']}{_count_suffix(item.get('count'))}")
        if item.get("details"):
            lines.append(f"   細節：{item['details']}")
        lines.append("")

    _section(lines, "10. 建議審查問題")
    questions = evidence.get("review_questions", [])
    if questions:
        for question in questions:
            lines.append(f"- {question}")
    else:
        lines.append("- 無額外建議問題。")

    _section(lines, "11. 技術摘要")
    _issue_block(lines, "Top-level errors", report.get("errors", []), limit=20)
    _issue_group_summary(lines, "Top-level warning 類型摘要", report.get("warnings", []))
    _issue_block(lines, "Top-level warning 樣本", report.get("warnings", []), limit=20)

    return "\n".join(lines).rstrip() + "\n"


def _section(lines: list[str], title: str, line: str = SUBLINE) -> None:
    if lines:
        lines.append("")
    lines.append(title)
    lines.append(line)


def _summary_counts(lines: list[str], *, errors: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> None:
    lines.append(f"- 錯誤/違規：{len(errors)}")
    lines.append(f"- 警告：{len(warnings)}")


def _metric_table(lines: list[str], metrics: dict[str, Any]) -> None:
    groups = [
        (
            "Deadline / tardiness",
            ("hard_deadline_miss_rate", "soft_deadline_miss_rate", "average_tardiness", "max_tardiness"),
        ),
        (
            "Response time",
            (
                "average_response_time",
                "max_response_time",
                "average_response_time_plus_one",
                "max_response_time_plus_one",
                "completed_response_time_job_count",
            ),
        ),
        (
            "Cost / revenue / objective",
            ("generator_cost", "market_revenue", "objective_value", "basic_generator_cost", "basic_market_revenue", "basic_objective_value"),
        ),
        (
            "Other",
            ("completion_time_jitter", "sporadic_value_rate"),
        ),
    ]
    for group_name, fields in groups:
        lines.append(f"{group_name}:")
        for field in fields:
            if field in metrics:
                lines.append(f"  - {field:<36} {_format_value(metrics.get(field))}")
        lines.append("")
    if metrics.get("completion_time_jitter_definition"):
        lines.append(f"- completion_time_jitter 定義：{metrics.get('completion_time_jitter_definition')}")
    if metrics.get("time_slot_convention"):
        lines.append(f"- 時段慣例：{metrics.get('time_slot_convention')}")


def _comparison_lines(lines: list[str], item: dict[str, Any]) -> None:
    lines.append(f"- {item.get('field')}：{_comparison_status_text(item.get('status'))}")
    lines.append(f"  繳交值：{_format_value(item.get('submitted_value'))}")
    lines.append(f"  重算值：{_format_value(item.get('recalculated_value'))}")
    lines.append(f"  差異：{_format_value(item.get('difference'))}")
    if item.get("note"):
        lines.append(f"  說明：{item.get('note')}")


def _issue_block(lines: list[str], title: str, issues: list[dict[str, Any]], *, limit: int) -> None:
    if not issues:
        lines.append(f"- {title}：無")
        return
    lines.append(f"{title}（前 {min(limit, len(issues))} 筆 / 共 {len(issues)} 筆）：")
    for issue in issues[:limit]:
        lines.append(f"- {_issue_message(issue)}")
        details = _compact_issue_details(issue)
        if details:
            lines.append(f"  {details}")
    if len(issues) > limit:
        lines.append(f"- 其餘 {len(issues) - limit} 筆請見 JSON report。")


def _issue_group_summary(lines: list[str], title: str, issues: list[dict[str, Any]]) -> None:
    if not issues:
        return
    counts: dict[str, int] = {}
    for issue in issues:
        key = str(issue.get("check") or issue.get("field") or issue.get("category") or issue.get("section") or issue.get("message") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    lines.append(title + "：")
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]:
        lines.append(f"- {key}: {count}")


def _problem_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for issue in report.get("schema_check", {}).get("errors", []):
        items.append(_problem("錯誤", "JSON 格式/必要欄位", _issue_message(issue), issue))
    for issue in report.get("schema_check", {}).get("warnings", []):
        items.append(_problem("警告", "JSON 格式/必要欄位", _issue_message(issue), issue))

    for comparison in report.get("evaluation_consistency_check", {}).get("comparisons", []):
        status = comparison.get("status")
        if status == "match":
            continue
        severity = "錯誤" if status in {"mismatch", "missing"} else "警告"
        message = (
            f"{comparison.get('field')} {_comparison_status_text(status)}；"
            f"繳交={_format_value(comparison.get('submitted_value'))}，"
            f"重算={_format_value(comparison.get('recalculated_value'))}，"
            f"差異={_format_value(comparison.get('difference'))}"
        )
        if comparison.get("note"):
            message += f"；{comparison.get('note')}"
        items.append(_problem(severity, "evaluation_results 指標一致性", message, comparison))

    for issue in report.get("feasibility_check", {}).get("violations", []):
        items.append(_problem("錯誤", "排程/模型可行性", _issue_message(issue), issue))
    items.extend(_grouped_warning_problems("排程/模型可行性", report.get("feasibility_check", {}).get("warnings", [])))
    for issue in report.get("level2_evidence", {}).get("warnings", []):
        items.append(_problem("警告", "Level 2 延伸證據", _issue_message(issue), issue))
    for issue in report.get("chart_files", {}).get("warnings", []):
        items.append(_problem("警告", "圖表輸出", _issue_message(issue), issue))
    return items


def _problem(severity: str, category: str, message: str, issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "message": message,
        "count": issue.get("count"),
        "details": _compact_issue_details(issue),
    }


def _grouped_warning_problems(category: str, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        key = str(issue.get("check") or issue.get("category") or issue.get("field") or issue.get("section") or _issue_message(issue))
        grouped.setdefault(key, []).append(issue)

    problems: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        first = group[0]
        summary = f"{key}: {len(group)} 筆警告"
        if first.get("message"):
            summary += f"；代表訊息：{first.get('message')}"
        detail = {
            "check": key,
            "count": len(group),
            "sample": first,
        }
        problems.append(_problem("警告", category, summary, detail))
    return problems


def _issue_message(issue: dict[str, Any]) -> str:
    message = issue.get("message")
    if isinstance(message, str) and message:
        return message
    check = issue.get("check") or issue.get("field") or issue.get("category") or issue.get("section")
    if check is not None:
        return str(check)
    return "未提供訊息"


def _compact_issue_details(issue: dict[str, Any]) -> str:
    omitted = {"message", "count", "samples"}
    parts = [f"{key}={_format_value(value)}" for key, value in issue.items() if key not in omitted]
    samples = issue.get("samples")
    if isinstance(samples, list) and samples:
        parts.append(f"samples={_format_value(samples[:3])}")
    return "；".join(parts)


def _status_text(status: str) -> str:
    mapping = {
        "invalid_submission_structure": "FAIL：必要 JSON 或欄位缺失",
        "feasibility_violation_detected": "FAIL：偵測到排程或模型可行性違規",
        "metric_inconsistency_detected": "FAIL：多項 evaluation_results 指標不一致",
        "manual_review_required": "WARN：有 Level 2 延伸證據或警告，需人工確認",
        "basic_checks_completed_with_manual_review": "PASS/WARN：基本檢查完成，Level 2 設計仍需人工審查",
    }
    return mapping.get(status, status)


def _comparison_status_text(status: Any) -> str:
    mapping = {
        "match": "一致",
        "mismatch": "不一致",
        "missing": "缺少欄位",
        "manual_review": "需人工確認",
    }
    return mapping.get(str(status), str(status))


def _count_suffix(count: Any) -> str:
    return f"；筆數={count}" if isinstance(count, int | float) and not isinstance(count, bool) else ""


def _join(values: Any) -> str:
    if not isinstance(values, list):
        return _format_value(values)
    return "、".join(str(value) for value in values)


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        items = list(value.items())
        body = ", ".join(f"{key}: {_format_value(item)}" for key, item in items[:8])
        return "{" + body + (", ..." if len(items) > 8 else "") + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(item) for item in value[:5]) + (", ..." if len(value) > 5 else "") + "]"
    return str(value)
