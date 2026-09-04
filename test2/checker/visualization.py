"""Generate matplotlib charts for manual review."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def generate_charts(context: dict[str, Any], parsed: dict[str, Any], recalculated: dict[str, Any], evidence: dict[str, Any], charts_dir: Path) -> dict[str, Any]:
    charts_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str | None] = {}
    warnings: list[dict[str, Any]] = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return {"files": files, "warnings": [{"chart": "all", "message": f"matplotlib unavailable: {exc}"}], "errors": []}

    _safe("job_gantt", files, warnings, lambda: _job_gantt(plt, parsed, charts_dir / "job_gantt.png"))
    _safe("generator_output_timeline", files, warnings, lambda: _generator_output(plt, parsed, charts_dir / "generator_output_timeline.png"))
    _safe("battery_soc_timeline", files, warnings, lambda: _battery_soc(plt, parsed, context, charts_dir / "battery_soc_timeline.png"))
    _safe("sell_timeline", files, warnings, lambda: _sell_timeline(plt, parsed, context, charts_dir / "sell_timeline.png"))
    _safe("deadline_miss_timeline", files, warnings, lambda: _deadline_miss(plt, parsed, charts_dir / "deadline_miss_timeline.png"))
    return {"files": files, "warnings": warnings, "errors": []}


def _safe(name: str, files: dict[str, str | None], warnings: list[dict[str, Any]], create) -> None:
    try:
        path = create()
        files[name] = str(path) if path is not None else None
    except Exception as exc:
        files[name] = None
        warnings.append({"chart": name, "message": f"could not generate chart: {exc}"})


def _job_gantt(plt, parsed: dict[str, Any], path: Path) -> Path | None:
    jobs = [job for job in parsed.get("jobs", []) if job.get("executed_slots")]
    if not jobs:
        return None
    jobs = jobs[:80]
    fig, ax = plt.subplots(figsize=(12, max(4, min(18, len(jobs) * 0.25))))
    labels = []
    for y, job in enumerate(jobs):
        labels.append(job["job_id"])
        color = "tab:red" if job.get("is_hard_deadline_missed") or job.get("is_soft_deadline_missed") else "tab:blue"
        for t in job.get("executed_slots", []):
            ax.broken_barh([(t - 0.5, 1)], (y - 0.35, 0.7), facecolors=color)
        if isinstance(job.get("absolute_deadline"), int):
            ax.plot(job["absolute_deadline"], y, marker="x", color="black", markersize=4)
    ax.set_xlim(0.5, 72.5)
    ax.set_xlabel("t")
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_title("Job Gantt (x marker = deadline, red = missed)")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _generator_output(plt, parsed: dict[str, Any], path: Path) -> Path | None:
    series: dict[str, list[tuple[int, float]]] = {}
    for entry in parsed.get("schedule_entries", []):
        for source_id, value in entry.get("P", {}).items():
            series.setdefault(str(source_id), []).append((entry["t"], _num(value)))
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(12, 5))
    for source_id, points in series.items():
        xs, ys = zip(*points)
        ax.plot(xs, ys, label=source_id)
    ax.set_xlabel("t")
    ax.set_ylabel("P")
    ax.set_title("Generator / Renewable / Battery Output Timeline")
    ax.legend(fontsize=7, ncol=2)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _battery_soc(plt, parsed: dict[str, Any], context: dict[str, Any], path: Path) -> Path | None:
    series: dict[str, list[tuple[int, float]]] = {}
    for entry in parsed.get("schedule_entries", []):
        for storage_id, value in entry.get("soc", {}).items():
            series.setdefault(str(storage_id), []).append((entry["t"], _num(value)))
    if not series:
        return None
    bounds = _storage_bounds(context["data"].get("processor_settings"))
    fig, ax = plt.subplots(figsize=(12, 5))
    for storage_id, points in series.items():
        xs, ys = zip(*points)
        ax.plot(xs, ys, label=storage_id)
        if storage_id in bounds:
            low, high = bounds[storage_id]
            if low is not None:
                ax.axhline(low, color="gray", linestyle="--", linewidth=0.7)
            if high is not None:
                ax.axhline(high, color="gray", linestyle=":", linewidth=0.7)
    ax.set_xlabel("t")
    ax.set_ylabel("SOC")
    ax.set_title("Battery SOC Timeline")
    ax.legend(fontsize=7)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _sell_timeline(plt, parsed: dict[str, Any], context: dict[str, Any], path: Path) -> Path | None:
    entries = parsed.get("schedule_entries", [])
    if not entries:
        return None
    prices = _price_by_hour(context["data"].get("price_72hr"))
    xs = [entry["t"] for entry in entries]
    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.plot(xs, [_num(entry.get("sell")) for entry in entries], label="sell", color="tab:blue")
    ax1.set_xlabel("t")
    ax1.set_ylabel("sell")
    ax2 = ax1.twinx()
    ax2.plot(xs, [prices.get(t, 0.0) for t in xs], label="market_price", color="tab:orange")
    ax2.set_ylabel("market price")
    ax1.set_title("Sell and Market Price Timeline")
    ax1.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _deadline_miss(plt, parsed: dict[str, Any], path: Path) -> Path | None:
    events = []
    for job in parsed.get("jobs", []):
        if job.get("is_hard_deadline_missed") or job.get("is_soft_deadline_missed") or job.get("is_rejected_sporadic"):
            t = job.get("completion_time") or job.get("absolute_deadline") or job.get("r")
            if isinstance(t, int):
                events.append((t, job["job_id"], job.get("job_type")))
    if not events:
        return None
    fig, ax = plt.subplots(figsize=(12, 4))
    for index, (t, job_id, job_type) in enumerate(events):
        ax.scatter(t, index)
        ax.text(t + 0.2, index, f"{job_id} ({job_type})", fontsize=7)
    ax.set_xlim(0.5, 72.5)
    ax.set_xlabel("t")
    ax.set_yticks([])
    ax.set_title("Deadline Miss / Rejected Sporadic Timeline")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _storage_bounds(processor: Any) -> dict[str, tuple[float | None, float | None]]:
    storage = processor.get("storage") if isinstance(processor, dict) else None
    if not isinstance(storage, list):
        return {}
    bounds = {}
    for item in storage:
        if not isinstance(item, dict):
            continue
        storage_id = item.get("storage_id") or item.get("battery_id") or item.get("id")
        if isinstance(storage_id, str):
            bounds[storage_id] = (_maybe_num(item.get("soc_min")), _maybe_num(item.get("soc_max")))
    return bounds


def _price_by_hour(price_72hr: Any) -> dict[int, float]:
    raw_prices = price_72hr.get("price") if isinstance(price_72hr, dict) else None
    if not isinstance(raw_prices, list):
        return {}
    prices = {}
    for index, item in enumerate(raw_prices, start=1):
        if isinstance(item, dict):
            hour = int(item.get("hour", index)) if isinstance(item.get("hour", index), int | float) else index
            prices[hour] = _num(item.get("market_price", item.get("price", 0.0)))
        else:
            prices[index] = _num(item)
    return prices


def _num(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _maybe_num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
