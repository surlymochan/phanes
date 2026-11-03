#!/usr/bin/env python3
"""Parse executor systemd task and zombie probe output for health-check integration."""

from __future__ import annotations


def parse_systemctl_show(output: str) -> dict[str, str]:
    """Parse systemctl show key=value output; raise for incomplete samples."""
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    required = ("TasksCurrent", "TasksMax", "ControlGroup")
    missing = [key for key in required if key not in values]
    if missing:
        raise ValueError("missing systemctl fields: " + ", ".join(missing))
    return values


def inspect_service(service: str, show_output: str, cgroup_pids: set[int], ps_output: str) -> dict:
    """Return service health from systemctl, cgroup PID membership, and ps rows."""
    fields = parse_systemctl_show(show_output)
    current, maximum = int(fields["TasksCurrent"]), int(fields["TasksMax"])
    proc = {}
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        proc[pid] = {"ppid": ppid, "stat": parts[2]}
    zombies = [pid for pid in cgroup_pids if pid in proc and "Z" in proc[pid]["stat"]]
    ratio = current / maximum if maximum > 0 else 1.0
    level = "hard" if len(zombies) > 200 or ratio > .85 else (
        "soft" if len(zombies) > 50 or ratio > .6 else "ok")
    return {"service": service, "tasks_current": current, "tasks_max": maximum,
            "tasks_ratio": ratio, "zombie_count": len(zombies), "level": level,
            "zombie_parent_pids": sorted({proc[pid]["ppid"] for pid in zombies})}


def unavailable(service: str, error: str) -> dict:
    """Represent failed collection without manufacturing an alert."""
    return {"service": service, "collected": False, "error": error, "level": "uncollected"}


def collect(host: str, run=None) -> tuple[dict, str]:
    """Collect a host via SSH. A failed command never produces an alert."""
    import shlex
    import subprocess
    if run is None:
        def run(command):
            return subprocess.check_output(['ssh', '-o', 'BatchMode=yes', host, command],
                                           text=True, stderr=subprocess.PIPE, timeout=30)
    try:
        kind = run('if [ -d /run/systemd/system ]; then echo systemd; else echo skipped; fi').strip()
        if kind == 'skipped':
            return {'status': 'skipped', 'services': []}, ''
        if kind != 'systemd':
            raise ValueError('invalid systemd detection')
        listing = run("systemctl list-units --all --type=service --plain --no-legend 'openswarm-executor-*.service'")
        services = [line.split()[0] for line in listing.splitlines() if line.split()]
        ps = run('ps -eo pid,ppid,stat,etimes,args')
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        return {'status': 'uncollected', 'error': str(exc), 'services': []}, ''
    results = []
    for service in services:
        try:
            if not service.startswith('openswarm-executor-') or not service.endswith('.service'):
                raise ValueError('invalid service name')
            show = run('systemctl show ' + shlex.quote(service) + ' -p TasksCurrent -p TasksMax -p ControlGroup')
            group = parse_systemctl_show(show)['ControlGroup']
            if not group.startswith('/') or '..' in group.split('/'):
                raise ValueError('invalid cgroup')
            pids = run('find ' + shlex.quote('/sys/fs/cgroup' + group) + " -name cgroup.procs -exec cat {} +")
            result = inspect_service(service, show, {int(p) for p in pids.split()}, ps)
            result['collected'] = True
            results.append(result)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            results.append(unavailable(service, str(exc)))
    return {'status': 'collected', 'services': results}, ps


def update(host: str, health_path, alert_path, snapshot_dir, run=None):
    """Update one host; snapshots are persisted before any alert is appended."""
    import json
    import re
    from datetime import datetime, timezone
    from pathlib import Path
    result, ps = collect(host, run)
    rows = {}
    for line in ps.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2], parts[4])
    sweeps = [(pid, row) for pid, row in rows.items()
              if row[1][0] != 'Z' and re.search(r'(?:^|/)fleet-capacity\s+--sweep-loop\s+gocc(?:\s|$)', row[2])]
    if len(sweeps) > 2:
        parents = []
        for pid, row in sweeps:
            parent, seen, run_id = row[0], set(), None
            ancestor = parent
            while ancestor in rows and ancestor not in seen:
                seen.add(ancestor)
                match = re.search(r'\b\d{8}-\d{6}-[a-zA-Z0-9]+\b', rows[ancestor][2])
                if match:
                    run_id = match.group(0)
                    break
                ancestor = rows[ancestor][0]
            parents.append({'pid': pid, 'ppid': parent, 'run_id': run_id})
        result['sweep_loop'] = {'count': len(sweeps), 'processes': parents, 'level': 'soft'}
    alerts = [s for s in result['services'] if s['level'] in ('soft', 'hard')]
    if alerts or result.get('sweep_loop'):
        directory = Path(snapshot_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        safe_host = re.sub(r'[^a-zA-Z0-9_.-]', '_', host)
        snapshot = directory / f'{safe_host}-{stamp}.ps'
        snapshot.write_text(ps)
        with Path(alert_path).open('a') as output:
            for service in alerts:
                output.write(f"{host} {service['level']} {service['service']} zombie_parent_pids={service['zombie_parent_pids']} snapshot={snapshot}\n")
            if result.get('sweep_loop'):
                output.write(f"{host} soft fleet-capacity --sweep-loop gocc processes={result['sweep_loop']['processes']} snapshot={snapshot}\n")
    path = Path(health_path)
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault('hosts', {}).setdefault(host, {})['executor_tasks'] = result
    path.write_text(json.dumps(data, indent=2) + '\n')
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('host')
    parser.add_argument('--health', required=True)
    parser.add_argument('--alert', required=True)
    parser.add_argument('--snapshot-dir', required=True)
    args = parser.parse_args()
    update(args.host, args.health, args.alert, args.snapshot_dir)


if __name__ == '__main__':
    main()
