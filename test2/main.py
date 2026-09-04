"""CLI entry point for the Level 2 auxiliary checker."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_import_path() -> None:
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _looks_like_submission(path: Path) -> bool:
    return (path / "input").is_dir() and (path / "output").is_dir()


def _default_submission_dir() -> Path | None:
    cwd = Path.cwd().resolve()
    checker_dir = Path(__file__).resolve().parent
    package_names = {"level2", "test2"}

    if _looks_like_submission(cwd):
        return cwd

    if cwd.name in package_names and _looks_like_submission(cwd.parent):
        return cwd.parent

    if checker_dir.name in package_names and _looks_like_submission(checker_dir.parent):
        return checker_dir.parent

    return None


def main() -> int:
    checker_dir = Path(__file__).resolve().parent
    package_name = checker_dir.name
    _ensure_import_path()

    from checker.evaluation_checker import compare_evaluation_results
    from checker.feasibility_checker import check_feasibility
    from checker.io_utils import ensure_output_dirs, load_submission_files, write_json
    from checker.level2_evidence_checker import detect_level2_evidence
    from checker.metric_recalculator import recalculate_metrics
    from checker.report_writer import determine_overall_status, write_reports
    from checker.schedule_parser import parse_schedule
    from checker.schema_checker import run_schema_checks
    from checker.visualization import generate_charts

    parser = argparse.ArgumentParser(description="Run the Level 2 RTS/VPP auxiliary checker.")
    parser.add_argument(
        "submission_path",
        type=Path,
        nargs="?",
        default=None,
        help="Optional submission folder positional argument.",
    )
    parser.add_argument(
        "--submission",
        type=Path,
        default=None,
        help="Submission folder. If omitted, auto-detects the current submission folder.",
    )
    parser.add_argument(
        "--event-file",
        type=Path,
        default=None,
        help="Optional official input/aperiodic_n_sporadic.json path. Defaults to the file inside the submission.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Optional report output folder. Defaults to <submission>/<package>/reports.",
    )
    parser.add_argument("--tolerance", type=float, default=1e-3, help="Numeric comparison tolerance.")
    args = parser.parse_args()

    requested_submission = args.submission if args.submission is not None else args.submission_path
    if requested_submission is not None and not requested_submission.exists():
        print(f"Submission path does not exist: {requested_submission}", file=sys.stderr)
        return 2
    submission_dir = requested_submission.resolve() if requested_submission is not None else _default_submission_dir()
    if submission_dir is None:
        print(
            "Could not detect submission root. Run from a submission folder, from submission/level2, "
            "or pass --submission <path>.",
            file=sys.stderr,
        )
        return 2
    report_dir = args.report_dir.resolve() if args.report_dir is not None else None
    output_dirs = ensure_output_dirs(submission_dir, package_name=package_name, report_dir=report_dir)
    context = load_submission_files(submission_dir)
    if args.event_file is not None:
        from checker.io_utils import load_optional_event_file

        load_optional_event_file(submission_dir, args.event_file, context)

    schema_check = run_schema_checks(context)
    parsed = parse_schedule(context)
    recalculated = recalculate_metrics(context, parsed)
    evidence = detect_level2_evidence(context)
    consistency = compare_evaluation_results(context, recalculated, evidence, tolerance=args.tolerance)
    feasibility = check_feasibility(context, parsed, evidence)
    chart_files = generate_charts(context, parsed, recalculated, evidence, output_dirs["charts"])

    errors: list[dict] = []
    warnings: list[dict] = []
    for section in (context, schema_check, parsed, recalculated, evidence, consistency, feasibility, chart_files):
        errors.extend(section.get("errors", []))
        warnings.extend(section.get("warnings", []))

    overall_status = determine_overall_status(schema_check, consistency, feasibility, evidence)
    report = {
        "metadata": {
            "checker_name": "Level 2 RTS/VPP auxiliary checker",
            "submission_root": str(submission_dir),
            "submission_path": str(submission_dir),
            "output_path": str(output_dirs["root"]),
            "package_name": package_name,
            "tolerance": args.tolerance,
            "purpose": "inspection_support_only",
        },
        "loaded_files": context["loaded_files"],
        "auxiliary_artifacts": context.get("auxiliary_artifacts", {}),
        "schema_check": schema_check,
        "parsed_job_summary": parsed,
        "recalculated_metrics": recalculated,
        "evaluation_consistency_check": consistency,
        "feasibility_check": feasibility,
        "level2_evidence": evidence,
        "chart_files": chart_files,
        "overall_status": overall_status,
        "manual_review_required": True,
        "warnings": warnings,
        "errors": errors,
    }

    write_json(output_dirs["intermediate"] / "parsed_job_summary.json", parsed)
    write_json(output_dirs["intermediate"] / "recalculated_metrics.json", recalculated)
    report_files = write_reports(output_dirs["root"], report, submission_name=submission_dir.name)
    report["report_files"] = report_files
    write_json(Path(report_files["json"]), report)

    print(f"Level 2 checker status: {overall_status}")
    print(f"Submission folder: {submission_dir}")
    print(f"Reports written to: {output_dirs['root']}")
    return 0 if overall_status != "invalid_submission_structure" else 1


if __name__ == "__main__":
    raise SystemExit(main())
