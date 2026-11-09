#!/usr/bin/env python3
"""Lease audit: the read-only half of the lease dispatch (Refs #1795, Epic #1785).

The lease system (P2-1/P2-2) gives every dispatched card a claim.  Two things
can go wrong that nobody sees from the lease file alone: a card that was
claimed twice (two leases at the same time) and a lease that expired and was
never reclaimed (a ghost lease -- the card stays invisible to the pool while
nobody works on it).  This tool scans the leases and the runs, reports the
findings as structured alerts and writes the lease health numbers.

The tool only reads the lease stores.  It never grants, renews or releases a
lease and never relabels a card: the reclaim path of the dispatcher stays the
only writer, so the audit can not become the second writer of the lease file.

What it writes, all of it in the output directory (``--out``, by default the
openswarm home):

``lease-audit-alerts.jsonl``
    one line per finding, appended the first time the finding is seen.  A
    finding that is still there is not written again: the same anomaly must
    not flood the patrol of the control plane every round;
``lease-audit-state.json``
    the ledger of the findings (when they were first seen, how often they
    were seen), the observed grants and the rounds -- the history the rolling
    window is computed from;
``lease-health.json``
    the health reading: the numbers of the moment (ghost leases, the average
    and the maximum wait of the cards) and the numbers of the rolling 24 hour
    window.

The stores are read by the independent ``lib/dispatch/lease_audit.py`` reader.

Usage::

    python3 tools/lease-audit.py --leases ~/.claude/swrm \\
        --state state.json --json

``--leases`` is the lease store (a file) or the directory that holds the
stores of every host (``leases.json``, ``leases-<host>.json``); ``--state``
is the lane state of the dispatcher (``lane -> {issue, run}``) and ``--runs``
the run records (a directory of runs or a file with run records).

The tool is a round, not a daemon: run it periodically (a cron entry or the
control plane patrol) and let the exit code carry the verdict -- 0 when
nothing was found, 1 when there is a finding.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "swrm" / "lib" / "dispatch" / "lease_audit.py"

STATE_FILE = "lease-audit-state.json"
ALERTS_FILE = "lease-audit-alerts.jsonl"
HEALTH_FILE = "lease-health.json"

# The lane state and the run records the tool reads by default.
DEFAULT_STATE = "state.json"
DEFAULT_RUNS = "runs"


def load_audit_module():
    """The audit library, loaded by path (``lib/dispatch`` is a namespace package)."""
    spec = importlib.util.spec_from_file_location("openswarm_dispatch_lease_audit", LIB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def openswarm_home() -> Path:
    """The openswarm home: the directory the lease store and the runs live in."""
    home = os.environ.get("OPENSWARM_HOME") or os.environ.get("XSWARM_HOME")
    return Path(home) if home else Path.home() / ".claude" / "swrm"


def write_json(path: Path, payload: Any) -> None:
    """Write a JSON file atomically (temporary file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def append_alerts(path: Path, alerts: List[Dict[str, Any]]) -> None:
    """Append the findings that are new to the alert log."""
    if not alerts:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        for alert in alerts:
            stream.write(json.dumps(alert, ensure_ascii=False, sort_keys=True) + "\n")


def load_state(audit: Any, path: Path) -> Dict[str, Any]:
    """The audit state of the last rounds (an unreadable file is an empty state)."""
    if not path.exists():
        return {}
    payload = audit.read_json(path)
    return payload if isinstance(payload, dict) else {}


def run_audit(audit: Any, leases: List[Dict[str, Any]], runs: List[Dict[str, Any]], now: float,
              window_s: float, out: Path) -> Dict[str, Any]:
    """One audit round: the findings, the state and the health reading."""
    result = audit.audit(leases, runs, now)

    state_path = out / STATE_FILE
    state = load_state(audit, state_path)
    updated = audit.apply_round(state, result, now, window_s)
    new_alerts = updated.pop("new_alerts", [])
    write_json(state_path, updated)
    append_alerts(out / ALERTS_FILE, new_alerts)

    report = audit.health_report(result, updated, now, window_s,
                                 at_label=time.strftime("%F %T", time.localtime(now)))
    report["new_alerts"] = new_alerts
    write_json(out / HEALTH_FILE, report)
    return report


def render(report: Dict[str, Any]) -> str:
    """The one-screen reading of the audit."""
    metrics = report.get("leases") or {}
    window = report.get("window_24h") or {}
    lines = [
        "lease audit: {}".format("OK" if report.get("ok") else "FINDINGS"),
        "  leases={} active={} expired={} ghost={} dual_claims={} dual_executions={}".format(
            metrics.get("leases", 0), metrics.get("active_leases", 0),
            metrics.get("expired_leases", 0), metrics.get("ghost_leases", 0),
            metrics.get("dual_claims", 0), metrics.get("dual_executions", 0)),
        "  wait: avg={} max={} samples={} unknown={}".format(
            _duration(metrics.get("wait_avg_s")), _duration(metrics.get("wait_max_s")),
            metrics.get("wait_samples", 0), metrics.get("wait_unknown", 0)),
        "  window 24h: ghost={} dual_claims={} dual_executions={} rounds={}".format(
            window.get("ghost_leases", 0), window.get("dual_claims", 0),
            window.get("dual_executions", 0), window.get("rounds", 0)),
    ]
    for alert in report.get("alerts") or []:
        lines.append("  [{}] {} {}".format(alert.get("severity", "?"),
                                           alert.get("kind", "?"),
                                           alert.get("summary", "")))
    new_alerts = report.get("new_alerts") or []
    if new_alerts:
        lines.append("  new findings: {}".format(len(new_alerts)))
    return "\n".join(lines)


def _duration(value: Any) -> str:
    """A short human duration (``42s``, ``3m07s``, ``-`` when there is none)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    total = int(round(float(value)))
    if total < 60:
        return "{}s".format(total)
    if total < 3600:
        return "{}m{:02d}s".format(total // 60, total % 60)
    return "{}h{:02d}m".format(total // 3600, (total % 3600) // 60)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="audit the dispatch leases and report the findings")
    parser.add_argument("--leases", help="the lease store (a file or a directory of stores)")
    parser.add_argument("--state", help="the lane state of the dispatcher (lane -> {issue, run})")
    parser.add_argument("--runs", help="the run records (a directory of runs or a file with run records)")
    parser.add_argument("--out", help="where the alerts, the state and the health file are written")
    parser.add_argument("--window-hours", type=float, default=24.0,
                        help="the rolling window of the health numbers (default: 24)")
    parser.add_argument("--now", type=float, help="the clock of this round (default: the wall clock)")
    parser.add_argument("--json", action="store_true", help="print the health report as JSON")
    args = parser.parse_args(argv)

    home = openswarm_home()
    leases_path = Path(args.leases) if args.leases else home
    state_path = Path(args.state) if args.state else home / DEFAULT_STATE
    runs_path = Path(args.runs) if args.runs else home / DEFAULT_RUNS
    out = Path(args.out) if args.out else home

    audit = load_audit_module()
    leases = audit.read_leases(leases_path)
    runs = audit.read_runs(runs_path)
    runs.extend(audit.read_lane_state(state_path))

    now = args.now if args.now is not None else time.time()
    window_s = max(1.0, float(args.window_hours) * 3600.0)
    report = run_audit(audit, leases, runs, now, window_s, out)
    report["sources"] = {"leases": str(leases_path), "state": str(state_path),
                         "runs": str(runs_path), "out": str(out)}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
