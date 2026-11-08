#!/usr/bin/env python3
"""Report evalid stage overhead; queue time never consumes the run budget.

Read-only reporter over a swrm runs directory. Every run directory holds a
``status.json`` (and usually a ``result.json``); the reporter aggregates the
L2 role pipeline stages -- Spec, spec-check, red, green, holdout and Review --
into run counts, queue seconds, execution seconds, budget seconds and usage.

Two rules drive the numbers:

* Queue time is capacity wait, not work. ``queued_seconds`` is reported on its
  own and never enters ``budget_seconds``: a run's budget is derived from its
  queue-adjusted ``started`` timestamp, including rate-limit backoff between
  execution attempts. Queue time is never charged and never deducted twice.
* Extra runs are the overhead of the L2 pipeline. The legacy flow already spent
  one run each on red, green, holdout and Review per change; every further run
  -- the Spec and spec-check runs, plus retries -- is extra quota and extra
  concurrency. ``change_id`` in status, result or the saved ``task.md`` header
  identifies a change. Legacy runs without it share a single baseline group;
  separate experiments must supply distinct change IDs (or be reported from
  separate runs directories).

Missing timestamps and negative durations are reported as deterministic data
quality errors instead of silently skewing the totals. The same input always
renders the same JSON bytes.

Usage:
    python3 swrm/bin/evalid_overhead.py [RUNS_DIR] [--stage STAGE]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

SCHEMA = "evalid-overhead/1"

#: Pipeline stages in execution order; always reported, even when empty.
STAGES = ("Spec", "spec-check", "red", "green", "holdout", "Review")

#: Stages the pre-Spec flow already ran once per change.
BASELINE_STAGES = ("red", "green", "holdout", "Review")

#: Accepted stage spellings, normalized to the canonical stage names.
STAGE_ALIASES = {
    "spec": "Spec",
    "spec-check": "spec-check",
    "red": "red",
    "green": "green",
    "holdout": "holdout",
    "review": "Review",
    "delivery": "Review",
}

#: Queue percentiles reported for the experiment conclusion.
PERCENTILES = (("p50", 50.0), ("p90", 90.0), ("p95", 95.0), ("p99", 99.0))

SECONDS_PER_MINUTE = 60.0


def default_runs_dir() -> str:
    """Return the swrm runs directory of the current host."""
    home = os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME")
    if not home:
        home = os.path.join(os.path.expanduser("~"), ".claude", "swrm")
    return os.path.join(home, "runs")


def _load_json(path: pathlib.Path) -> Optional[dict]:
    """Read a JSON object, or None when the file is absent or unusable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> Optional[Any]:
    """Return a finite number, or None when the value is not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _seconds(value: Any) -> float:
    """Round a duration to milliseconds so the JSON stays comparable."""
    return round(float(value), 3)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Accept ISO-8601 text or epoch seconds; None when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _timestamp_error(raw: Any) -> str:
    """Classify an unusable timestamp as missing or invalid."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "missing"
    return "invalid"


def _stage_of(status: dict, result: dict) -> tuple[Optional[str], Optional[str]]:
    """Normalize the recorded stage; None stage comes with its error kind."""
    raw = status.get("stage")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = status.get("evalid_stage")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = result.get("evalid_stage")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "missing"
    stage = STAGE_ALIASES.get(str(raw).strip().lower())
    return (stage, None) if stage else (None, "unknown")


def _percentile(samples: Sequence[float], percent: float) -> float:
    """Nearest-rank percentile of an already sorted sample list."""
    if not samples:
        return 0.0
    rank = math.ceil(percent / 100.0 * len(samples))
    index = min(max(rank - 1, 0), len(samples) - 1)
    return float(samples[index])


def _task_change_id(run_dir: pathlib.Path) -> Optional[str]:
    """Read the change ID swrm persists in the controlled task card header."""
    try:
        lines = (run_dir / "task.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator and key.strip() == "change_id":
            # Match the string scalar and ID contract in evalid_roles.py.
            change_id = value.strip().strip("\"'")
            return change_id if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", change_id) else None
    return None


def _usage_of(status: dict, result: dict) -> dict:
    """Sum attempt usage; swrm's top-level usage describes only the last attempt."""
    attempts = result.get("attempts")
    usages = []
    if isinstance(attempts, list):
        for attempt in attempts:
            meta = attempt.get("meta") if isinstance(attempt, dict) else None
            usage = meta.get("usage") if isinstance(meta, dict) else None
            if isinstance(usage, dict):
                usages.append(usage)
    if not usages:
        usage = status.get("usage")
        if not isinstance(usage, dict) or not usage:
            usage = result.get("usage")
        if isinstance(usage, dict):
            usages.append(usage)
    total: dict[str, Any] = {}
    for usage in usages:
        for key, value in usage.items():
            number = _number(value)
            if number is not None:
                total[str(key)] = total.get(str(key), 0) + number
    return total


def _run_record(run_dir: pathlib.Path) -> Optional[dict]:
    """Read one run directory into stage, durations, queue time and usage."""
    status_path = run_dir / "status.json"
    if not status_path.exists():
        return None
    status = _load_json(status_path)
    name = run_dir.name
    errors: list[dict] = []
    if status is None:
        errors.append({"run_id": name, "field": "status.json", "error": "invalid"})
        return {"run_id": name, "stage": None, "errors": errors, "queued_seconds": 0.0,
                "execution_seconds": None, "budget_seconds": 0.0,
                "budget_elapsed_seconds": 0.0, "usage": {}}
    result = _load_json(run_dir / "result.json") or {}

    stage, stage_error = _stage_of(status, result)
    if stage_error:
        errors.append({"run_id": name, "field": "stage", "error": stage_error})

    queued_raw = status.get("queued_at")
    started_raw = status.get("started")
    finished_raw = status.get("finished")
    queued_at = _parse_timestamp(queued_raw)
    started = _parse_timestamp(started_raw)
    finished = _parse_timestamp(finished_raw)
    if queued_at is None:
        errors.append({"run_id": name, "field": "queued_at", "error": _timestamp_error(queued_raw)})
    if started is None:
        errors.append({"run_id": name, "field": "started", "error": _timestamp_error(started_raw)})
    if finished is None:
        errors.append({"run_id": name, "field": "finished", "error": _timestamp_error(finished_raw)})

    # Validate timestamps independently of the attempt duration in result.json.
    # finished is written after verification/cleanup, so the wall window is only
    # a fallback for older runs without a recorded execution duration.
    window = None
    if started is not None and finished is not None:
        window = (finished - started).total_seconds()
    execution = _number(result.get("seconds"))
    negative = (window is not None and window < 0) or (execution is not None and execution < 0)
    if negative:
        errors.append({"run_id": name, "field": "execution_seconds", "error": "negative"})
    if execution is None:
        execution = window if window is not None and window >= 0 else None
    elif execution < 0:
        execution = None

    queued = _number(status.get("queued_seconds"))
    if queued is None:
        queued = _number(result.get("queued_seconds"))
    if queued is None:
        queued = 0.0
    elif queued < 0:
        errors.append({"run_id": name, "field": "queued_seconds", "error": "negative"})
        queued = 0.0

    # swrm shifts started forward for each capacity wait, but rate-limit
    # backoff still consumes deadline budget. Do not subtract queued_seconds
    # again, or use result.seconds (which omits all time between attempts).
    elapsed = window if window is not None and window >= 0 else execution
    attempts = result.get("attempts")
    last = attempts[-1] if isinstance(attempts, list) and attempts else None
    attempt_started = _parse_timestamp(status.get("attempt_started"))
    if isinstance(last, dict) and attempt_started is not None and started is not None:
        last_seconds = _number(last.get("seconds"))
        # An expired/stopped run may have spent its remaining time in backoff
        # after the last executor returned. Its finish window includes that wait.
        interrupted = (result.get("error_class") in ("budget_exceeded", "stopped")
                       or last.get("error_class") in ("budget_exceeded", "stopped"))
        if last_seconds is not None and last_seconds >= 0 and not interrupted:
            attempt_end = (attempt_started - started).total_seconds() + last_seconds
            if attempt_end >= 0 and (window is None or attempt_end <= window):
                elapsed = attempt_end
    # Without an attempt endpoint, finished is the best available wall window;
    # without a wall window, retain the recorded execution duration fallback.
    # A run killed at its deadline cannot consume more than its allowance.
    budget_minutes = _number(status.get("budget_minutes"))
    allowance = None
    if budget_minutes is not None and budget_minutes >= 0:
        allowance = float(budget_minutes) * SECONDS_PER_MINUTE
    if execution is None:
        budget = 0.0
    elif allowance is None:
        budget = elapsed
    else:
        budget = min(elapsed, allowance)

    change_id = status.get("change_id") or result.get("change_id")
    if change_id is None:
        change_id = _task_change_id(run_dir)
    return {"run_id": name, "stage": stage, "errors": errors,
            "change_id": str(change_id) if change_id is not None else None,
            "queued_seconds": float(queued), "execution_seconds": execution,
            "budget_seconds": budget, "budget_elapsed_seconds": elapsed or 0.0,
            "usage": _usage_of(status, result)}


def _empty_bucket() -> dict:
    """Return a fresh per-stage accumulator."""
    return {"run_count": 0, "queued_seconds": 0.0, "execution_seconds": 0.0,
            "budget_seconds": 0.0, "usage": {}}


def build_report(runs_dir: Any, stages: Optional[Sequence[str]] = None) -> dict:
    """Aggregate a runs directory into the stage overhead report."""
    selected = tuple(stages) if stages else STAGES
    root = pathlib.Path(runs_dir)
    buckets = {stage: _empty_bucket() for stage in selected}
    queue_samples: list[float] = []
    budget_checks: list[bool] = []
    errors: list[dict] = []
    baseline_runs: set[tuple[Optional[str], str]] = set()

    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if not entry.is_dir():
            continue
        record = _run_record(entry)
        if record is None:
            continue
        errors.extend(record["errors"])
        stage = record["stage"]
        if stage is None or stage not in buckets:
            continue
        budget_checks.append(record["budget_seconds"] <= record["budget_elapsed_seconds"] + 1e-9)
        if stage in BASELINE_STAGES:
            baseline_runs.add((record["change_id"], stage))
        bucket = buckets[stage]
        bucket["run_count"] += 1
        bucket["queued_seconds"] += record["queued_seconds"]
        if record["execution_seconds"] is not None:
            bucket["execution_seconds"] += record["execution_seconds"]
        bucket["budget_seconds"] += record["budget_seconds"]
        for key, value in record["usage"].items():
            bucket["usage"][key] = bucket["usage"].get(key, 0) + value
        queue_samples.append(record["queued_seconds"])

    stages_report: dict[str, dict] = {}
    for stage, bucket in buckets.items():
        stages_report[stage] = {
            "run_count": bucket["run_count"],
            "queued_seconds": _seconds(bucket["queued_seconds"]),
            "execution_seconds": _seconds(bucket["execution_seconds"]),
            "budget_seconds": _seconds(bucket["budget_seconds"]),
            "usage": {key: bucket["usage"][key] for key in sorted(bucket["usage"])},
        }

    totals_usage: dict[str, Any] = {}
    for bucket in buckets.values():
        for key, value in bucket["usage"].items():
            totals_usage[key] = totals_usage.get(key, 0) + value

    total_run_count = sum(bucket["run_count"] for bucket in buckets.values())
    baseline_run_count = len(baseline_runs)
    queue_samples.sort()
    percentiles = {name: _percentile(queue_samples, percent) for name, percent in PERCENTILES}

    return {
        "schema": SCHEMA,
        "stages": stages_report,
        "totals": {
            "run_count": total_run_count,
            "queued_seconds": _seconds(sum(bucket["queued_seconds"] for bucket in buckets.values())),
            "execution_seconds": _seconds(sum(bucket["execution_seconds"] for bucket in buckets.values())),
            "budget_seconds": _seconds(sum(bucket["budget_seconds"] for bucket in buckets.values())),
            "usage": {key: totals_usage[key] for key in sorted(totals_usage)},
        },
        "baseline_run_count": baseline_run_count,
        "extra_run_count": total_run_count - baseline_run_count,
        "queue_percentiles_seconds": {name: _seconds(value) for name, value in percentiles.items()},
        "budget_excludes_queue_seconds": all(budget_checks),
        "data_quality_errors": errors,
    }


def render_report(report: dict) -> str:
    """Render the report as stable JSON: the same input gives the same bytes."""
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report evalid stage overhead from a swrm runs directory.")
    parser.add_argument("runs_dir", nargs="?", default=default_runs_dir(),
                        help="runs directory (default: $OPENSWARM_HOME/runs)")
    parser.add_argument("--stage", action="append", choices=STAGES,
                        help="only report these stages (repeatable)")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.runs_dir, stages=args.stage)
    except OSError as error:
        print(json.dumps({"error": str(error)[:200]}), file=sys.stderr)
        return 2
    sys.stdout.write(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
