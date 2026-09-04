# NCKU Real-Time Systems — Level 2 Virtual Power Plant Scheduler

Coursework archive for a Level 2 virtual power plant (VPP) scheduling project.

| Item | Value |
| --- | --- |
| Institution | National Cheng Kung University (NCKU) |
| Course | Real-Time Systems |
| Student ID | F74122048 |
| Project scope | Level 2 final demo / submission |

## Overview

This project models a 72-hour virtual power plant with thermal generators, photovoltaic generation, battery storage, periodic jobs, sporadic jobs, and aperiodic jobs. It uses mixed-integer linear programming (MILP) with PuLP/CBC.

The Level 2 extension adds event-triggered model predictive control (MPC). When a simulated photovoltaic shortfall makes the current plan infeasible, the scheduler captures the current system state and re-optimizes the remaining horizon. The model also considers battery charge/discharge efficiency, battery aging cost, generator start-up cost, day-ahead energy-sale commitments, reserve capacity, line capacity, and generator fuel limits.

## Repository layout

- `src/` — task generator, scheduler, and evaluator.
- `input/` — system settings, market price, and dynamic-job inputs.
- `output/` — submitted scheduling and evaluation results.
- `test2/` — course-provided auxiliary checker and the preserved checker report.
- `course-materials/` — final-demo slides and the program-flow document.

The submission structure, course materials, output JSON files, and helper checker are intentionally retained as part of the course record. Python bytecode caches were omitted because they are generated files.

## Run locally

Requires Python 3.10+ and the packages in `requirements.txt`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -X utf8 src/task_generator.py
.\.venv\Scripts\python -X utf8 src/scheduler.py
.\.venv\Scripts\python -X utf8 src/evaluator.py
.\.venv\Scripts\python -X utf8 test2/main.py --submission .
```

`-X utf8` is required on Windows terminals configured with a legacy code page because the original coursework source prints Chinese text and emoji.

## Verified submission snapshot

The scheduler and evaluator were re-run from this archived submission. The auxiliary checker's core checks report:

- 0 schema, parser, metric, consistency, or feasibility violations.
- 0 hard-deadline misses and a sporadic value rate of 1.0.
- 1 soft-deadline warning: aperiodic job `a2` is unfinished or misses its soft deadline.
- 1 manual-review warning for the Level 2 battery SOC transition.

## Interpretation notes

- `output/evaluation_results.json` preserves the course-compatible metrics. Its `objective_value` uses generator cost and market revenue only; it does **not** include all Level 2 terms such as the shortfall penalty, start-up cost, and battery-aging cost. Do not interpret it as the complete Level 2 optimization objective.
- The `test2/` package and course documents are retained for archival and reproducibility. Reuse is subject to the course materials' original terms.

