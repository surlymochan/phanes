#!/usr/bin/env python3
"""Verify the first run of a lane after a resume: the gate for lifting a lane pause (Refs #1747).

A lane that is paused because every run died with `exec_error` must not be unpaused on the
promise that the next run will work.  The gate is the next run itself: it has to finish with
`status=done`, it has to leave a commit behind, and its `error_class` must not be `exec_error`
again.  This tool reads the lane's own run records and prints that verdict, so the check is one
command instead of a hand-made `cat` of `result.json`.

Usage:
  check-lane-first-run.py --lane M9
  check-lane-first-run.py --lane M9 --since 2026-11-03T01:10:00Z
  check-lane-first-run.py --lane M9 --since 2026-11-03T01:10:00Z --json

Exit code 0 means the first run after the given point passed the gate, 1 means it did not
(or that no such run exists yet).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "check_pi_spawn_failure", Path(__file__).resolve().parent / "check-pi-spawn-failure.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FAILED_CLASSES = ("exec_error", "prepare_failed", "executor_io_lost")


def parse_since(value):
    """An ISO8601 timestamp or an epoch seconds value; None means "every run"."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    text = value.replace("Z", "+00:00")
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp()


def first_run(runs, since):
    """The lane's earliest run at or after `since`."""
    selected = [run for run in runs if since is None or (run.get("started") or 0) >= since]
    return selected[0] if selected else None


def verdict(run):
    """The gate: a finished run, a commit, and no exec_error."""
    if run is None:
        return False, ["no run of this lane at or after the given point"]
    problems = []
    status = run.get("status")
    if status not in ("done", "completed"):
        problems.append(f"status={status} (expected done)")
    if not run.get("commit"):
        problems.append("no commit was produced")
    error_class = run.get("error_class")
    if error_class in FAILED_CLASSES:
        problems.append(f"error_class={error_class}")
    if status not in ("done", "completed") and not run.get("error_tail"):
        problems.append("no error_tail was recorded")
    return (not problems), problems


def render(report):
    lines = [f"lane        {report.get('lane')}",
             f"since       {report.get('since') or 'no lower bound'}",
             f"run         {report.get('run_id')}  status={report.get('status')} "
             f"error_class={report.get('error_class')} seconds={report.get('seconds')}"]
    if report.get("commit"):
        lines.append(f"commit      {report.get('commit')}")
    if report.get("error_tail"):
        lines.append(f"error_tail  {report.get('error_tail')}")
    if report.get("spawn"):
        lines.append(f"spawn       {json.dumps(report['spawn'], ensure_ascii=False)}")
    lines.append(f"gate        {'PASS' if report.get('passed') else 'FAIL'}")
    for problem in report.get("problems") or []:
        lines.append(f"  -         {problem}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", help="the lane name, e.g. M9")
    parser.add_argument("--home", help="the openswarm home (default: $OPENSWARM_HOME)")
    parser.add_argument("--since", help="only runs started at or after this timestamp (ISO8601 or epoch)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()
    if not args.lane:
        parser.error("--lane is required")

    home = MODULE.openswarm_homes(args.home)[0]
    for candidate in MODULE.openswarm_homes(args.home):
        if (candidate / "runs").is_dir():
            home = candidate
            break
    since = parse_since(args.since)
    if args.since and since is None:
        parser.error(f"cannot read --since {args.since!r} as a timestamp")
    runs = MODULE.lane_runs(home, args.lane, limit=1000)
    run = first_run(runs, since)
    passed, problems = verdict(run)
    report = {"lane": args.lane, "since": args.since, "runs_seen": len(runs), "passed": passed,
              "problems": problems,
              "run_id": (run or {}).get("run_id"), "status": (run or {}).get("status"),
              "error_class": (run or {}).get("error_class"), "error_tail": (run or {}).get("error_tail"),
              "commit": (run or {}).get("commit"), "seconds": (run or {}).get("seconds"),
              "spawn": (run or {}).get("spawn")}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render(report))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
