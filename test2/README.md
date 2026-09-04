# Level 2 Checker Package

This `test2/` folder is the clean portable Level 2 checker package copied into a student submission.
The source of truth is `level2/`; regenerate this folder with:

```bash
python level2/scripts/build_test2_package.py
```

This checker recalculates Level 1-style metrics, checks Level 2 feasibility evidence, and writes a Chinese report with concrete detected issues.

From a submission root, run:

```bash
python test2/main.py
```

Equivalent explicit commands:

```bash
python test2/main.py --submission .
python -m test2.main . --event-file input/aperiodic_n_sporadic.json --report-dir test2/reports
```

`--event-file` is optional. If omitted, the checker reads `input/aperiodic_n_sporadic.json` when present.

Reports are written to:

- `test2/reports/{submission_name}_report.json`
- `test2/reports/{submission_name}_report.txt`
- `test2/reports/charts/`
- `test2/reports/intermediate/`

`overall_status` is a checker state, not a formal grade. The text report lists concrete detected problems and remaining review questions.
