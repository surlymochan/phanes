#!/usr/bin/env python3
"""Executor task / zombie watermark probe of the fleet hosts (Refs #1826).

The executor services of a lane leak: their cgroup fills up with zombie children until
``TasksMax`` is hit and ``fork`` fails (``exec_error`` storm).  Neither the disk/mem/ssh
probes of the control plane nor the lane reaper carries a task or zombie counter, so the
leak stays invisible for hours.

This probe samples one fleet host over ssh and

* enumerates every ``openswarm-executor-*.service`` of the host,
* reads ``TasksCurrent`` / ``TasksMax`` of the unit and the pids of its cgroup,
* counts the ``Z`` state processes of that cgroup and remembers their parent pids,
* writes the result into the ``health.json`` of the control plane,
* raises a ``soft`` / ``hard`` watermark alert - and only that: nothing is restarted,
  stopped or killed.

The watermark of the card:

* ``Z > 50``  or ``TasksCurrent / TasksMax > 0.6``  -> ``soft``
* ``Z > 200`` or ``TasksCurrent / TasksMax > 0.85`` -> ``hard``

A host without systemd (the macOS box ``gocc``) is skipped, a failed sample is recorded
as *not collected* and never alerts.  When an alert fires, the full
``ps -eo pid,ppid,stat,etimes,args`` snapshot is written to disk **before** the alert is
appended, so the evidence of the leak survives.

The follow-up part of the card counts the duplicate ``fleet-capacity --sweep-loop``
processes by their full command line: more than two identical commands alert with the
parent processes and the run id of the parent.

Usage (the control plane health check calls it as a command):

    python3 tools/executor-task-health.py <host> \\
        --health-json ~/phanes-ctl/autodispatch/health.json \\
        --alert-file ~/phanes-ctl/autodispatch/health.alert \\
        --snapshot-dir ~/phanes-ctl/autodispatch/executor-task-snapshots

The exit code is 0 when the probe ran - the state of the host is in the payload it
prints (``collected`` / ``skipped`` / ``uncollected``), 1 when the probe itself failed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


# The unit family of the card: one openswarm-executor-<lane>.service per lane.
UNIT_PREFIX = "openswarm-executor-"
UNIT_SUFFIX = ".service"

# The watermark of the card (see the module docstring).
ZOMBIE_SOFT = 50
ZOMBIE_HARD = 200
RATIO_SOFT = 0.6
RATIO_HARD = 0.85

# The duplicate sweep loops of the card: more than SWEEP_LIMIT of them alert.
SWEEP_MARKER = "fleet-capacity --sweep-loop"
SWEEP_LIMIT = 2

# The map of health.json that carries the payload of the probe.  The host status of the
# control plane (health.json "hosts": {host: "ok"|"degraded"}) stays untouched: the
# probe only adds its own payload.
HEALTH_KEY = "executor_tasks"

# The run id of a lane run (20261103-214705-b66c3f) - the parent of a sweep loop carries
# it in its command line.
RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6,}")

# The STAT column of ps: S, Ss, R, Z, Z+, D, Sl, T, ... (a zombie starts with Z).
STAT_RE = re.compile(r"^[A-Za-z?][A-Za-z+<>]{0,3}$")

# The remote commands of the probe.  They only read: no restart, no stop, no kill.
DETECT_COMMAND = "test -d /run/systemd/system && echo systemd || echo skipped"
LIST_COMMAND = (
    "systemctl list-units '%s*.service' --all --no-legend --no-pager" % UNIT_PREFIX
)
SHOW_COMMAND = (
    "systemctl show %s --property=TasksCurrent,TasksMax,ControlGroup --no-pager"
)
PS_COMMAND = "ps -eo pid,ppid,stat,etimes,args"
CGROUP_PROCS_COMMAND = "cat /sys/fs/cgroup%s/cgroup.procs"
# One dump of the cgroup of every process: a zombie that is already reaped from
# cgroup.procs still shows its cgroup here, so it can be attributed to its unit.
CGROUP_MAP_COMMAND = (
    "for p in /proc/[0-9]*; do printf '%s ' \"${p#/proc/}\"; "
    "head -n 1 \"$p/cgroup\" 2>/dev/null; printf '\\n'; done"
)

DEFAULT_TIMEOUT = 60.0


# --------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------
def parse_systemctl_show(output: str) -> Dict[str, str]:
    """The ``key=value`` pairs of a ``systemctl show`` sample."""
    values: Dict[str, str] = {}
    for line in (output or "").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        values[key.strip()] = value.strip()
    return values


def parse_ps(output: str) -> List[Dict[str, object]]:
    """The rows of a ``ps -eo pid,ppid,stat,etimes,args`` table."""
    rows: List[Dict[str, object]] = []
    for line in (output or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        pid, ppid, stat = parts[0], parts[1], parts[2]
        if not pid.isdigit() or not ppid.isdigit():
            continue  # the header row
        if not STAT_RE.match(stat):
            continue  # not a process row
        if len(parts) > 3 and not parts[3].isdigit():
            continue  # not a process row
        rows.append({
            "pid": int(pid),
            "ppid": int(ppid),
            "stat": stat,
            "args": parts[4] if len(parts) > 4 else "",
        })
    return rows


def parse_unit_list(output: str) -> List[str]:
    """The executor units of a ``systemctl list-units`` sample."""
    units: List[str] = []
    for line in (output or "").splitlines():
        fields = line.split()
        if fields and fields[0] == "●":
            fields = fields[1:]  # systemctl marks failed units before their name
        if not fields:
            continue
        name = fields[0]
        if name.startswith(UNIT_PREFIX) and name.endswith(UNIT_SUFFIX):
            units.append(name)
    return units


def parse_pid_list(output: str) -> Set[int]:
    """The pids of a ``cgroup.procs`` sample."""
    pids: Set[int] = set()
    for token in (output or "").split():
        if token.isdigit():
            pids.add(int(token))
    return pids


def parse_cgroup_map(output: str) -> Dict[int, str]:
    """The ``pid -> cgroup`` pairs of the bulk dump; malformed lines are ignored."""
    mapping: Dict[int, str] = {}
    for line in (output or "").splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        path = fields[1]
        if ":" not in path:
            continue  # not a cgroup line (0::/system.slice/... or 1:name=systemd:/...)
        mapping[int(fields[0])] = path.split(":", 2)[-1]
    return mapping


# --------------------------------------------------------------------------------
# the watermark of one service
# --------------------------------------------------------------------------------
def unavailable(service: str, error: object) -> Dict[str, object]:
    """A service whose sample failed: recorded, never alerting."""
    return {
        "service": service,
        "collected": False,
        "level": "uncollected",
        "error": str(error),
        "tasks_current": None,
        "tasks_max": None,
        "tasks_ratio": None,
        "zombie_count": None,
        "zombie_parent_pids": [],
        "control_group": "",
    }


def zombie_rows(rows: Sequence[Dict[str, object]], pids: Set[int],
                control_group: str, cgroup_map: Dict[int, str]) -> List[Dict[str, object]]:
    """The zombie processes of a service cgroup.

    A zombie belongs to the cgroup of the service when

    * the cgroup lists it (``cgroup.procs``),
    * or its parent lives in that cgroup (the leaking lane serve process - the cgroup
      file of a not yet reaped child can miss it), or
    * the cgroup of the process itself is that cgroup or a descendant (the bulk
      ``/proc`` dump).
    """
    zombies: List[Dict[str, object]] = []
    for row in rows:
        if not str(row["stat"]).startswith("Z"):
            continue
        pid = int(row["pid"])
        ppid = int(row["ppid"])
        if pid in pids or ppid in pids:
            zombies.append(row)
            continue
        path = cgroup_map.get(pid, "")
        if control_group and (path == control_group or
                              path.startswith(control_group.rstrip("/") + "/")):
            zombies.append(row)
    return zombies


def inspect_service(service: str, show_output: str, cgroup_pids: Iterable[int],
                    ps_output: str,
                    cgroup_map: Optional[Dict[int, str]] = None) -> Dict[str, object]:
    """The watermark of one executor service.

    ``show_output`` is the ``systemctl show`` sample, ``cgroup_pids`` the pids of the
    cgroup of the unit, ``ps_output`` the full process table of the host.
    """
    values = parse_systemctl_show(show_output)
    try:
        tasks_current = int(values["TasksCurrent"])
        tasks_max = int(values["TasksMax"])
    except (KeyError, TypeError, ValueError):
        return unavailable(service, "incomplete systemctl show sample")

    control_group = values.get("ControlGroup", "")
    pids = set(cgroup_pids or ())
    zombies = zombie_rows(parse_ps(ps_output), pids, control_group, cgroup_map or {})
    zombie_count = len(zombies)
    parents = sorted({int(row["ppid"]) for row in zombies})
    ratio = (float(tasks_current) / float(tasks_max)) if tasks_max else 0.0

    level = "ok"
    if zombie_count > ZOMBIE_HARD or ratio > RATIO_HARD:
        level = "hard"
    elif zombie_count > ZOMBIE_SOFT or ratio > RATIO_SOFT:
        level = "soft"

    return {
        "service": service,
        "collected": True,
        "level": level,
        "tasks_current": tasks_current,
        "tasks_max": tasks_max,
        "tasks_ratio": ratio,
        "zombie_count": zombie_count,
        "zombie_parent_pids": parents,
        "control_group": control_group,
    }


# --------------------------------------------------------------------------------
# the duplicate sweep loops of the card
# --------------------------------------------------------------------------------
def detect_sweep(ps_output: str, limit: int = SWEEP_LIMIT) -> Dict[str, object]:
    """The ``fleet-capacity --sweep-loop`` processes of the host.

    The card asks for the number of resident processes with that command line, their
    parent processes and the run id of the parent. More than ``limit`` instances of
    the same full command line leak. Top-level fields summarize all sweep processes;
    only the per-command groups determine alerts.
    """
    rows = parse_ps(ps_output)
    by_pid = {int(row["pid"]): row for row in rows}
    sweep = [row for row in rows if SWEEP_MARKER in str(row["args"])]

    def summarize(processes: Sequence[Dict[str, object]]) -> Dict[str, object]:
        parents: Set[int] = set()
        run_ids: Set[str] = set()
        for row in processes:
            pid = int(row["ppid"])
            seen: Set[int] = set()
            while pid and pid not in seen:
                seen.add(pid)
                ancestor = by_pid.get(pid)
                if ancestor is None:
                    break
                parents.add(int(ancestor["pid"]))
                run_ids.update(RUN_ID_RE.findall(str(ancestor["args"])))
                pid = int(ancestor["ppid"])
        return {
            "count": len(processes),
            "limit": limit,
            "pids": [int(row["pid"]) for row in processes],
            "parent_pids": sorted(parents),
            "run_ids": sorted(run_ids),
        }

    commands: Dict[str, List[Dict[str, object]]] = {}
    for row in sweep:
        commands.setdefault(str(row["args"]), []).append(row)
    result = summarize(sweep)
    result["groups"] = [dict(summarize(processes), command=command)
                        for command, processes in sorted(commands.items())]
    return result


# --------------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------------
def ssh_runner(host: str, user: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
               ssh_options: Sequence[str] = ()) -> Callable[[str], str]:
    """The remote command runner of the probe: one ssh call per command."""
    def run(command: str) -> str:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        argv.extend(ssh_options)
        argv.append("%s@%s" % (user, host) if user else host)
        # SSH joins remote arguments with spaces; keep the shell quoting intact.
        argv.append(command)
        completed = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode, argv, completed.stdout, completed.stderr)
        return completed.stdout
    return run


def collect_service(runner: Callable[[str], str], unit: str, ps_output: str,
                    cgroup_map: Dict[int, str]) -> Dict[str, object]:
    """Sample one executor service; a failure is recorded, never raised."""
    try:
        show = runner(SHOW_COMMAND % unit)
    except Exception as error:  # noqa: BLE001 - a failed sample is data, not a crash
        return unavailable(unit, error)
    control_group = parse_systemctl_show(show).get("ControlGroup", "")
    if not control_group:
        return unavailable(unit, "missing ControlGroup in systemctl show sample")
    try:
        pids = parse_pid_list(runner(CGROUP_PROCS_COMMAND % shlex.quote(control_group)))
    except Exception as error:  # noqa: BLE001 - a failed sample must not look healthy
        return unavailable(unit, error)
    return inspect_service(unit, show, pids, ps_output, cgroup_map)


def empty_payload(host: str, status: str) -> Dict[str, object]:
    """The payload of a host without any sample."""
    return {
        "status": status,
        "host": host,
        "at": time.strftime("%F %T"),
        "services": [],
        "sweep_loop": {"count": 0, "limit": SWEEP_LIMIT, "pids": [],
                       "parent_pids": [], "run_ids": [], "groups": []},
    }


def collect(host: str, run: Optional[Callable[[str], str]] = None,
            user: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
            ssh_options: Sequence[str] = ()) -> Tuple[Dict[str, object], str]:
    """Sample the executor services of one host.

    Returns the payload of the host and the ``ps`` table of the host (the snapshot that
    is saved before an alert is written).
    """
    runner = run or ssh_runner(host, user=user, timeout=timeout, ssh_options=ssh_options)

    try:
        detection = runner(DETECT_COMMAND)
    except Exception as error:  # noqa: BLE001 - a failed probe is data, not a crash
        payload = empty_payload(host, "uncollected")
        payload["error"] = str(error)
        return payload, ""
    if "systemd" not in detection:
        # No systemd (the macOS box gocc): the host is skipped, not degraded.
        payload = empty_payload(host, "skipped")
        payload["reason"] = "no systemd on the host"
        return payload, ""

    try:
        units = parse_unit_list(runner(LIST_COMMAND))
    except Exception as error:  # noqa: BLE001 - a failed probe is data, not a crash
        payload = empty_payload(host, "uncollected")
        payload["error"] = str(error)
        return payload, ""
    try:
        ps_output = runner(PS_COMMAND)
    except Exception as error:  # noqa: BLE001 - a failed probe is data, not a crash
        payload = empty_payload(host, "uncollected")
        payload["error"] = str(error)
        return payload, ""
    try:
        cgroup_map = parse_cgroup_map(runner(CGROUP_MAP_COMMAND))
    except Exception as error:  # noqa: BLE001 - incomplete attribution is not healthy
        payload = empty_payload(host, "uncollected")
        payload["error"] = str(error)
        payload["services"] = [unavailable(unit, error) for unit in units]
        return payload, ps_output

    services = [collect_service(runner, unit, ps_output, cgroup_map) for unit in units]
    payload = {
        "status": "collected",
        "host": host,
        "at": time.strftime("%F %T"),
        "services": services,
        "sweep_loop": detect_sweep(ps_output),
    }
    return payload, ps_output


# --------------------------------------------------------------------------------
# the health summary and the alerts
# --------------------------------------------------------------------------------
def write_health_payload(health_path: Path, host: str,
                         payload: Dict[str, object]) -> Dict[str, object]:
    """Write the payload of the probe into health.json.

    The summary of the control plane (``at``, ``hosts``, ``hosts_ok``,
    ``hosts_degraded``) is preserved: the probe never rewrites the status of a host.
    """
    # Lock a stable sidecar rather than health.json's inode, which changes on
    # atomic replacement. Every summary writer must use this same lock and reread
    # health.json after acquiring it (including the external health-check loop).
    health_path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(health_path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            loaded = json.loads(health_path.read_text())
        except FileNotFoundError:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("health.json must contain an object")
        data = loaded

        hosts = data.get("hosts")
        if not isinstance(hosts, dict):
            hosts = {}
            data["hosts"] = hosts
        entry = hosts.get(host)
        if isinstance(entry, dict):
            entry["executor_tasks"] = payload  # a host entry that is already a mapping
        tasks = data.get(HEALTH_KEY)
        if not isinstance(tasks, dict):
            tasks = {}
            data[HEALTH_KEY] = tasks
        tasks[host] = payload

        # A reader sees either the old complete document or the new one. On a
        # failed write/replace, preserve the existing summary and remove the temp.
        temporary = None
        try:
            try:
                mode = stat.S_IMODE(health_path.stat().st_mode)
            except FileNotFoundError:
                mode = None
            with tempfile.NamedTemporaryFile(
                    mode="w", dir=health_path.parent, prefix=health_path.name + ".",
                    suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(data, indent=2) + "\n")
                handle.flush()
                if mode is not None:
                    os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
            os.replace(temporary, health_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return data


def build_alerts(payload: Dict[str, object]) -> List[str]:
    """The alert lines of a payload (without the snapshot reference)."""
    stamp = time.strftime("%F %T")
    host = str(payload.get("host", ""))
    lines: List[str] = []
    for service in payload.get("services", []):
        level = str(service.get("level", ""))
        if level not in ("soft", "hard"):
            continue  # an uncollected service never alerts
        parents = ",".join(str(pid) for pid in service.get("zombie_parent_pids", []))
        lines.append(
            "%s level=%s source=executor-tasks host=%s service=%s zombies=%s tasks=%s/%s "
            "ratio=%.2f parents=%s" % (
                stamp, level, host, service.get("service"), service.get("zombie_count"),
                service.get("tasks_current"), service.get("tasks_max"),
                float(service.get("tasks_ratio") or 0.0), parents or "-"))
    for sweep in (payload.get("sweep_loop") or {}).get("groups", []):
        if int(sweep.get("count", 0)) <= int(sweep.get("limit", SWEEP_LIMIT)):
            continue
        lines.append(
            "%s level=hard source=executor-tasks host=%s kind=sweep-loop count=%s pids=%s "
            "parents=%s run_ids=%s command=%s" % (
                stamp, host, sweep.get("count"),
                ",".join(str(pid) for pid in sweep.get("pids", [])) or "-",
                ",".join(str(pid) for pid in sweep.get("parent_pids", [])) or "-",
                ",".join(str(run_id) for run_id in sweep.get("run_ids", [])) or "-",
                shlex.quote(str(sweep.get("command", "")))))
    return lines


def snapshot_stamp() -> str:
    """The stamp of a snapshot file: the second plus the microsecond of the clock."""
    now = time.time()
    return "%s-%06d" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)),
                        int((now % 1) * 1000000))


def save_snapshot(snapshot_dir: Path, host: str, ps_output: str) -> Optional[Path]:
    """Save the full ps table of the host; None when the snapshot cannot be saved."""
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / ("executor-tasks-%s-%s.ps" % (host, snapshot_stamp()))
        path.write_text(ps_output or "")
        return path
    except OSError:
        return None


def update(host: str, health_path: Path, alert_path: Path, snapshot_dir: Path,
           run: Optional[Callable[[str], str]] = None, **options: object) -> Dict[str, object]:
    """Sample the host, write the payload into health.json and raise the alerts.

    The ps snapshot is written **before** the alert is appended: without the evidence
    there is no alert.
    """
    payload, ps_output = collect(host, run=run, **options)
    write_health_payload(Path(health_path), host, payload)

    alerts = build_alerts(payload)
    if not alerts:
        return payload

    snapshot = save_snapshot(Path(snapshot_dir), host, ps_output)
    if snapshot is None:
        # No evidence, no alert: the probe never raises an alert it cannot back up.
        return payload
    with Path(alert_path).open("a") as handle:
        for line in alerts:
            handle.write("%s snapshot=%s\n" % (line, snapshot))
    return payload


# --------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="executor-task-health.py",
        description=("Sample the executor task / zombie watermark of a fleet host and "
                     "raise the soft/hard alerts of #1826 (nothing is restarted)."))
    parser.add_argument("host", help="the fleet host to sample")
    parser.add_argument(
        "--health-json", default=str(Path.home() / "phanes-ctl" / "autodispatch" / "health.json"),
        help="the health.json of the control plane (the payload of the host is added)")
    parser.add_argument(
        "--alert-file", default=str(Path.home() / "phanes-ctl" / "autodispatch" / "health.alert"),
        help="the health.alert file the watermark alerts are appended to")
    parser.add_argument(
        "--snapshot-dir",
        default=str(Path.home() / "phanes-ctl" / "autodispatch" / "executor-task-snapshots"),
        help="the directory the ps snapshot of an alerting host is saved to")
    parser.add_argument("--ssh-user", default=None, help="the ssh user of the host")
    parser.add_argument("--ssh-option", action="append", default=[],
                        help="an extra ssh option (repeatable)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="the timeout of one ssh call in seconds")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = update(
            args.host,
            Path(args.health_json),
            Path(args.alert_file),
            Path(args.snapshot_dir),
            user=args.ssh_user,
            timeout=args.timeout,
            ssh_options=args.ssh_option,
        )
    except Exception as error:  # noqa: BLE001 - the command line reports, it does not crash
        sys.stderr.write("executor-task-health: %s\n" % error)
        return 1
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
