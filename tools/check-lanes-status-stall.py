#!/usr/bin/env python3
"""Offline health probe for the lanes-status snapshot and its writer (Refs #1697).

``~/phanes-ctl/autodispatch/lanes-status.json`` is the queue watermark of the control
plane: the pool level, the number of parallel lanes and every supervisor decision are
taken from it.  On 2026-11-02 the ``_at`` stamp froze at 22:13:20 while the dispatcher
kept dispatching, and no reader noticed: the snapshot was written once at the very end
of a slow tick, a failed write was swallowed, and the reader of the snapshot did not
exist at all.

This probe is that reader.  It answers two questions and alarms on both:

* is the snapshot fresh?  ``_at`` older than the threshold (3 minutes by default) means
  the watermark is expired: the probe names the last ``_at`` and exits non-zero;
* is the writer still alive?  The heartbeat of the dispatcher loop and the pid file are
  two independent signals.  A dead pid is reported as dead even when the heartbeat was
  refreshed a second ago: the pid file outlives the process it names, so a fresh
  heartbeat is not proof of life.

The probe is offline and read-only: it never starts, stops or touches the dispatcher.
``swrm fleet status`` includes this verdict in its text and JSON health output.
``swrm fleet health`` exits non-zero on an alarm; ``swrm fleet health --watch 60``
rechecks both signals every minute in an independent monitor process, including when
the dispatcher exits or hangs. ``--json`` prints the same verdict for a monitor.

The paths follow the writer (``tools/lane-autodispatch/autodispatch.py``): the file is
``<state dir>/lanes-status.json``, the state dir is ``~/phanes-ctl/autodispatch`` by
default and ``OPENSWARM_AD_STATE_DIR`` (or ``OPENSWARM_LANES_STATUS_FILE`` for the
file itself) moves it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


DEFAULT_THRESHOLD_MINUTES = 3.0
DEFAULT_WRITER_STALE_MINUTES = 10.0
DEFAULT_STATE_DIR = Path("~/phanes-ctl/autodispatch")
DEFAULT_CTL_STATE_DIR = Path("~/phanes-ctl/state")
STATE_DIR_ENVS = ("OPENSWARM_AD_STATE_DIR", "XSWARM_AD_STATE_DIR")
STATUS_FILE_ENVS = ("OPENSWARM_LANES_STATUS_FILE", "XSWARM_LANES_STATUS_FILE")
PID_FILE_NAMES = ("autodispatch.pid",)


# --- the paths the writer and the reader share --------------------------------------------

def state_dir(explicit=None) -> Path:
    """The dispatcher's state dir: it holds the snapshot, the heartbeat and the log."""
    if explicit:
        return Path(explicit).expanduser()
    for name in STATE_DIR_ENVS:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return DEFAULT_STATE_DIR.expanduser()


def ctl_state_dir(explicit=None) -> Path:
    """The control-plane state dir: it holds the pid file of the dispatcher loop."""
    if explicit:
        return Path(explicit).expanduser()
    value = os.environ.get("OPENSWARM_CTL_STATE_DIR")
    if value:
        return Path(value).expanduser()
    return DEFAULT_CTL_STATE_DIR.expanduser()


def lanes_status_path(explicit=None, directory=None) -> Path:
    """The snapshot file the dispatcher writes and the control plane reads."""
    if explicit:
        return Path(explicit).expanduser()
    for name in STATUS_FILE_ENVS:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return state_dir(directory) / "lanes-status.json"


# --- the snapshot ---------------------------------------------------------------------------

def parse_stamp(value):
    """The epoch seconds of a ``%Y-%m-%d %H:%M:%S`` stamp (None when unreadable)."""
    if not isinstance(value, str):
        return None
    try:
        return time.mktime(time.strptime(value.strip(), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def snapshot_report(path, moment=None, threshold_seconds=180) -> dict:
    """Read the snapshot and judge its freshness.

    ``_at`` is the watermark every supervisor reads: when it is older than the threshold
    the snapshot is expired, and the report names the last stamp so the alarm can be
    read without opening the file.
    """
    now = time.time() if moment is None else moment
    report = {"path": str(path), "status": "missing", "at": None, "age_seconds": None,
              "threshold_seconds": int(threshold_seconds), "busy": None, "ready": None,
              "blocked": None, "phase": None}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return report
    except (OSError, ValueError) as error:
        report["status"] = "unreadable"
        report["error"] = str(error)
        return report
    if not isinstance(data, dict):
        report["status"] = "unreadable"
        report["error"] = "the snapshot is not a JSON object"
        return report
    stamp = data.get("_at")
    report["at"] = stamp if isinstance(stamp, str) else None
    phase = data.get("_phase")
    report["phase"] = phase if isinstance(phase, str) else None
    for key, name in (("_busy", "busy"), ("_ready", "ready"), ("_blocked", "blocked")):
        value = data.get(key)
        report[name] = value if isinstance(value, int) and not isinstance(value, bool) else None
    moment_of_stamp = parse_stamp(stamp)
    if moment_of_stamp is None:
        report["status"] = "invalid"
        report["error"] = f"no usable _at stamp: {stamp!r}"
        return report
    report["age_seconds"] = max(0, int(now - moment_of_stamp))
    report["status"] = "fresh" if report["age_seconds"] <= report["threshold_seconds"] else "stale"
    return report


# --- the writer -----------------------------------------------------------------------------

def process_start_time(pid):
    """The start time of a pid in epoch seconds (/proc on Linux, None elsewhere)."""
    try:
        with open(f"/proc/{pid}/stat") as stream:
            stat = stream.read()
        fields = stat[stat.rfind(")") + 2:].split()
        with open("/proc/stat") as stream:
            boot_time = float(stream.read().split("btime ", 1)[1].split()[0])
        return boot_time + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def process_alive(pid, started_at=None) -> bool:
    """Is the pid a live process?  A recorded start time also rules out a reused pid."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if started_at:
        began = process_start_time(pid)
        if began is not None and abs(float(started_at) - began) > 5:
            return False  # the pid was taken over by another process
    return True


def writer_report(state=None, control=None, moment=None, stale_seconds=600) -> dict:
    """Is the dispatcher that writes the snapshot still alive?

    Two independent signals: the heartbeat file the dispatcher refreshes on every tick
    and on every snapshot publish, and the pid file the launcher writes.  A dead pid is
    reported as dead even when the heartbeat is fresh: the pid file survives the process
    it names, so a fresh heartbeat is not proof of life (Refs #1697).
    """
    now = time.time() if moment is None else moment
    state = state_dir() if state is None else Path(state).expanduser()
    control = ctl_state_dir() if control is None else Path(control).expanduser()
    heartbeat_path = state / "heartbeat"
    report = {"status": "unknown", "pid": None, "pid_alive": None, "heartbeat_at": None,
              "heartbeat_age_seconds": None, "heartbeat_path": str(heartbeat_path),
              "pid_path": None, "stale_after_seconds": int(stale_seconds)}
    stamp = None
    try:
        stamp = heartbeat_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        stamp = None
    except OSError as error:
        report["heartbeat_error"] = str(error)
    if stamp:
        report["heartbeat_at"] = stamp
        beat = parse_stamp(stamp)
        report["heartbeat_age_seconds"] = max(0, int(now - beat)) if beat is not None else None
    for name in PID_FILE_NAMES:
        candidate = control / name
        if not candidate.exists():
            candidate = state / name
        if not candidate.exists():
            continue
        report["pid_path"] = str(candidate)
        try:
            record = json.loads(candidate.read_text(encoding="utf-8"))
            pid = int(record["pid"])
            started_at = float(record.get("started_at") or 0) or None
        except (OSError, ValueError, KeyError, TypeError):
            report["pid_error"] = f"the pid file {candidate} is unreadable"
            continue
        report["pid"] = pid
        report["pid_alive"] = process_alive(pid, started_at)
        break
    if report["pid_alive"] is False:
        report["status"] = "dead"
    elif report["heartbeat_age_seconds"] is not None:
        report["status"] = "alive" if report["heartbeat_age_seconds"] <= report["stale_after_seconds"] else "stalled"
    elif report["pid_alive"] is True:
        report["status"] = "alive"
    else:
        report["status"] = "unknown"
    return report


# --- the verdict ----------------------------------------------------------------------------

def alerts(snapshot: dict, writer: dict) -> list:
    """The alarms of the control plane: a frozen snapshot or a dead/stalled dispatcher."""
    found = []
    status = snapshot.get("status")
    if status == "stale":
        found.append({"kind": "lanes-snapshot-stale",
                      "message": f"the lanes-status snapshot is stale: last _at {snapshot.get('at')} "
                                 f"({snapshot.get('age_seconds')}s old, threshold "
                                 f"{snapshot.get('threshold_seconds')}s)",
                      "at": snapshot.get("at"), "age_seconds": snapshot.get("age_seconds"),
                      "path": snapshot.get("path")})
    elif status in ("missing", "unreadable", "invalid"):
        found.append({"kind": "lanes-snapshot-" + status,
                      "message": f"the lanes-status snapshot is {status}: "
                                 f"{snapshot.get('error') or snapshot.get('path')}",
                      "at": snapshot.get("at"), "age_seconds": snapshot.get("age_seconds"),
                      "path": snapshot.get("path")})
    if writer.get("status") in ("dead", "stalled"):
        found.append({"kind": "autodispatch-writer-" + writer["status"],
                      "message": f"the autodispatch writer is {writer['status']} "
                                 f"(pid {writer.get('pid')}, alive={writer.get('pid_alive')}, "
                                 f"heartbeat {writer.get('heartbeat_at')}, "
                                 f"{writer.get('heartbeat_age_seconds')}s old, threshold "
                                 f"{writer.get('stale_after_seconds')}s)",
                      "at": writer.get("heartbeat_at"),
                      "age_seconds": writer.get("heartbeat_age_seconds"),
                      "path": writer.get("heartbeat_path")})
    return found


def render(snapshot: dict, writer: dict, found: list) -> str:
    """The human readable verdict of the probe."""
    if snapshot.get("at"):
        line = (f"last _at {snapshot['at']}, {snapshot['age_seconds']}s old "
                f"(threshold {snapshot['threshold_seconds']}s)")
        if snapshot.get("phase"):
            line += f", phase {snapshot['phase']}"
    else:
        line = "no readable snapshot"
    if writer.get("heartbeat_at"):
        writer_line = (f"pid {writer.get('pid')}, heartbeat {writer['heartbeat_at']} "
                       f"({writer['heartbeat_age_seconds']}s old, threshold "
                       f"{writer['stale_after_seconds']}s)")
    elif writer.get("status") == "dead":
        writer_line = f"pid {writer.get('pid')} is dead, no heartbeat ({writer.get('heartbeat_path')})"
    else:
        writer_line = f"pid {writer.get('pid')}, no heartbeat ({writer.get('heartbeat_path')})"
    lines = [f"lanes-status  {snapshot['status']:<9}  {line}  {snapshot.get('path')}",
             f"autodispatch  {writer['status']:<9}  {writer_line}"]
    for alert in found:
        lines.append(f"WARNING {alert['message']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Judge the freshness of the lanes-status snapshot and the liveness of its writer.")
    parser.add_argument("--lanes-status-file", default=None,
                        help="the snapshot to judge (default: <state dir>/lanes-status.json)")
    parser.add_argument("--state-dir", default=None,
                        help="the dispatcher state dir (default: ~/phanes-ctl/autodispatch)")
    parser.add_argument("--ctl-state-dir", default=None,
                        help="the control-plane state dir that holds the pid file "
                             "(default: ~/phanes-ctl/state)")
    parser.add_argument("--threshold-minutes", type=float, default=DEFAULT_THRESHOLD_MINUTES,
                        help=f"maximum age of the snapshot in minutes (default: {DEFAULT_THRESHOLD_MINUTES})")
    parser.add_argument("--writer-stale-minutes", type=float, default=DEFAULT_WRITER_STALE_MINUTES,
                        help=f"maximum age of the dispatcher heartbeat in minutes "
                             f"(default: {DEFAULT_WRITER_STALE_MINUTES})")
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON")
    args = parser.parse_args(argv)
    if args.threshold_minutes <= 0:
        parser.error("--threshold-minutes must be greater than zero")
    if args.writer_stale_minutes <= 0:
        parser.error("--writer-stale-minutes must be greater than zero")

    snapshot = snapshot_report(lanes_status_path(args.lanes_status_file, args.state_dir),
                               threshold_seconds=args.threshold_minutes * 60)
    writer = writer_report(args.state_dir, args.ctl_state_dir,
                            stale_seconds=args.writer_stale_minutes * 60)
    found = alerts(snapshot, writer)
    if args.json:
        print(json.dumps({"ok": not found, "snapshot": snapshot, "writer": writer,
                          "alerts": found}, ensure_ascii=False, indent=1))
    else:
        print(render(snapshot, writer, found))
    if not found:
        return 0
    # A snapshot that cannot be judged at all is a broken watermark (2); a frozen or dead
    # writer is an alarm (1).
    if snapshot["status"] in ("missing", "unreadable", "invalid") and len(found) == 1:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
