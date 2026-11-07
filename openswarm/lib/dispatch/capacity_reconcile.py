#!/usr/bin/env python3
"""Reconcile the fleet capacity limits with the lanes that are configured (Refs #1693).

The capacity coordinator (``bin/fleet-capacity``) admits a turn only when the
fleet has room for it: one limit for the whole fleet (``total``), one limit per
backend (``pi``, ``codex``, ``agy``, ``copilot``, ...), one limit per provider
(``minimax-cn``) and one limit per host (``host:<name>``).  Those limits live in
``topology.json`` next to the capacity database; the lanes that have to fit
into them live in the control plane ``lanes.json``.

Nobody reconciled the two, so a limit could stay far below the lane count for
months (``pi``/``minimax-cn`` stuck at 11 while 15 pi lanes were configured) and
the lanes that did not fit were only found by hand, one file at a time.

This module is the pure part of that reconciliation:

``load_topology(path)``
    Read ``topology.json`` -> ``{"hosts": [...], "limits": {...}}``.
``load_lanes(path)``
    Read a ``lanes.json`` -> the list of its lane entries.
``lane_distribution(lanes)``
    Count the lanes: total, per backend, per provider, per host, per host and
    backend.
``reconcile(limits, lanes)``
    Compare every limit with the count it covers and report the rows that are
    behind (the limit value, the lane count, the deficit and a suggested
    value).
``merge_lanes(control_lanes, host_lanes, control_host)``
    Merge the control plane lanes with the lanes of every host, so the count
    covers the whole fleet and not only the control plane.  The lanes are keyed
    by (host, name): a lane that is in the control plane file and in the file
    of its host is counted once.
``reconcile_paths(topology_path, lanes_path)`` / ``reconcile_topology(...)``
    The same, reading the files; they never raise.  A topology that cannot be
    read yields the ``unverified`` status: the reconciliation reports what it
    could not check instead of guessing.
``unverified(error, ...)`` / ``mark_unverified(report, error)``
    The report of a reconciliation that could not read all of its inputs: the
    counts are kept, but nothing is claimed and the reason is named.

Raising a limit stays a human decision: this module never writes the topology.
"""

import json
import os

__all__ = [
    "CapacityConfigError",
    "DEFAULT_MARGIN",
    "DEFAULT_PROVIDERS",
    "STATUS_ALERT",
    "STATUS_OK",
    "STATUS_UNVERIFIED",
    "UNVERIFIED_LABEL",
    "lane_distribution",
    "lane_provider",
    "load_lanes",
    "load_topology",
    "mark_unverified",
    "merge_lanes",
    "reconcile",
    "reconcile_paths",
    "reconcile_topology",
    "topology_path",
    "unverified",
]

# The reconciliation status: every limit covers its lanes, a limit is behind
# the lane count, or the topology could not be read at all.
STATUS_OK = "ok"
STATUS_ALERT = "alert"
STATUS_UNVERIFIED = "unverified"

# The marker a health report carries when the topology could not be read.
UNVERIFIED_LABEL = "未核实"

# The headroom a raised limit should carry: the lane count plus one lane.
DEFAULT_MARGIN = 1

# The provider a lane uses when its entry names none; mirrors policy.json
# (pi and hermes run on the minimax-cn provider).
DEFAULT_PROVIDERS = {"pi": "minimax-cn", "hermes": "minimax-cn"}

# The limit that covers the whole fleet, and the prefix of a host limit.
TOTAL_LIMIT = "total"
HOST_LIMIT_PREFIX = "host:"


class CapacityConfigError(Exception):
    """The topology or the lane configuration cannot be read."""


def topology_path(capacity_dir=None):
    """The topology.json path: ``<capacity dir>/topology.json``.

    The default is the directory the capacity coordinator itself reads, so the
    reconciliation and the admission control always look at the same file.
    """
    root = capacity_dir or os.environ.get("OPENSWARM_FLEET_CAPACITY_DIR") or \
        os.path.join("~", ".claude", "openswarm-fleet-capacity")
    return os.path.join(os.path.expanduser(root), "topology.json")


def load_topology(path):
    """Read ``topology.json`` and return its hosts and limits.

    A missing file, a broken file, or a file without a usable ``limits`` object
    raises ``CapacityConfigError``: the reconciliation must not guess the limits.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, ValueError, UnicodeError) as error:
        raise CapacityConfigError("topology %r is unreadable: %s" % (path, error)) from error
    if not isinstance(config, dict):
        raise CapacityConfigError("topology %r is not an object" % path)
    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise CapacityConfigError("topology %r has no limits object" % path)
    clean = {}
    for key, value in limits.items():
        if not isinstance(key, str) or not key:
            raise CapacityConfigError("topology %r has an invalid limit key" % path)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CapacityConfigError("topology %r has an invalid limit for %r" % (path, key))
        clean[key] = value
    hosts = config.get("hosts")
    if not isinstance(hosts, list) or any(not isinstance(host, str) for host in hosts):
        raise CapacityConfigError("topology %r has no hosts list" % path)
    return {"hosts": list(hosts), "limits": clean}


def load_lanes(path):
    """Read a ``lanes.json`` and return its lane entries.

    A lane entry without a name is still counted: the reconciliation compares
    counts, not identities.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            config = json.load(stream)
    except (OSError, ValueError, UnicodeError) as error:
        raise CapacityConfigError("lane config %r is unreadable: %s" % (path, error)) from error
    lanes = config.get("lanes") if isinstance(config, dict) else None
    if not isinstance(lanes, list):
        raise CapacityConfigError("lane config %r has no lanes list" % path)
    return [lane for lane in lanes if isinstance(lane, dict)]


def lane_provider(lane):
    """The provider a lane runs on; the backend default when it names none."""
    provider = lane.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    backend = lane.get("backend")
    return DEFAULT_PROVIDERS.get(backend) if isinstance(backend, str) else None


def lane_distribution(lanes):
    """Count the lanes: total, per backend, per provider, per host, per host+backend."""
    by_backend, by_provider, by_host, by_host_backend = {}, {}, {}, {}
    total = 0
    for lane in lanes:
        total += 1
        backend = lane.get("backend")
        backend = backend.strip() if isinstance(backend, str) and backend.strip() else None
        host = lane.get("host")
        host = host.strip() if isinstance(host, str) and host.strip() else None
        provider = lane_provider(lane)
        if backend:
            by_backend[backend] = by_backend.get(backend, 0) + 1
        if provider:
            by_provider[provider] = by_provider.get(provider, 0) + 1
        if host:
            by_host[host] = by_host.get(host, 0) + 1
            if backend:
                hosts = by_host_backend.setdefault(host, {})
                hosts[backend] = hosts.get(backend, 0) + 1
    return {"total": total, "by_backend": by_backend, "by_provider": by_provider,
            "by_host": by_host, "by_host_backend": by_host_backend}


def merge_lanes(control_lanes, host_lanes, control_host=None):
    """Merge the control plane lanes with the lanes of every host.

    The fleet lane count is the count of all the hosts, not the count of the
    control plane file: a lane that moved to a host the control plane does not
    know about still fills the fleet.  ``host_lanes`` maps a host name to the
    lane entries of that host; ``control_host`` is the host the control plane
    lane file belongs to (its lanes are read already, so they are not read
    twice).

    The lanes are keyed by (host, name), so a lane that is in the control plane
    file and in the file of its host is counted once.  The second return value
    names what could not be merged: a lane without a host cannot be counted
    against a host limit, and the caller turns that into ``unverified``.
    """
    lanes, incomplete, hostless = {}, [], set()
    for lane in control_lanes or []:
        if not isinstance(lane, dict):
            incomplete.append("invalid lane entry in the control plane lane config")
            continue
        name = lane.get("name")
        host = lane.get("host")
        host = host.strip() if isinstance(host, str) and host.strip() else None
        if not isinstance(name, str) or not name:
            incomplete.append("control-plane lane without a name")
            continue
        entry = dict(lane)
        if host is None:
            host = control_host
            if host is None:
                hostless.add(name)
            else:
                entry["host"] = host
        lanes[(host, name)] = entry
    for host, entries in (host_lanes or {}).items():
        for lane in entries or []:
            if not isinstance(lane, dict):
                incomplete.append("invalid lane entry on %s" % host)
                continue
            lane = dict(lane)
            name = lane.get("name")
            if not isinstance(name, str) or not name:
                incomplete.append("lane on %s has no name" % host)
                continue
            lane.setdefault("host", host)
            lanes.pop((None, name), None)
            lanes[(lane.get("host"), name)] = lane
            hostless.discard(name)
    incomplete.extend("control-plane lane %s has no host" % name for name in sorted(hostless))
    return list(lanes.values()), incomplete


def _limit_count(key, distribution):
    """The lane count a limit key covers, and the kind of limit it is."""
    if key == TOTAL_LIMIT:
        return "total", distribution["total"]
    if key.startswith(HOST_LIMIT_PREFIX):
        return "host", distribution["by_host"].get(key[len(HOST_LIMIT_PREFIX):], 0)
    if key in distribution["by_backend"]:
        return "backend", distribution["by_backend"][key]
    if key in distribution["by_provider"]:
        return "provider", distribution["by_provider"][key]
    return "backend", 0


def _alert_text(row):
    """The one line description of a limit that is behind the lane count."""
    return "%s: limit %d, lanes %d, deficit %d, suggested %d" % (
        row["limit"], row["limit_value"], row["lanes"], row["deficit"], row["suggested"])


def reconcile(limits, lanes, margin=DEFAULT_MARGIN):
    """Compare every limit with the lane count it covers.

    The result carries one row per limit (``checks``) and the rows that are
    behind (``alerts``).  A limit that is behind names the limit, its current
    value, the lane count, the deficit and a suggested value (the lane count
    plus the margin); the topology itself is never changed.
    """
    distribution = lane_distribution(lanes)
    checks, alerts = [], []
    for key in sorted(limits):
        limit = limits[key]
        kind, count = _limit_count(key, distribution)
        deficit = max(0, count - limit)
        row = {"limit": key, "kind": kind, "limit_value": limit, "lanes": count,
               "deficit": deficit, "suggested": count + margin if deficit else None,
               "status": STATUS_ALERT if deficit else STATUS_OK}
        checks.append(row)
        if deficit:
            alerts.append(row)
    report = {
        "status": STATUS_ALERT if alerts else STATUS_OK,
        "ok": not alerts,
        "verified": True,
        "margin": margin,
        "lane_total": distribution["total"],
        "by_backend": distribution["by_backend"],
        "by_provider": distribution["by_provider"],
        "by_host": distribution["by_host"],
        "by_host_backend": distribution["by_host_backend"],
        "limits": dict(limits),
        "hosts": [],
        "checks": checks,
        "alerts": alerts,
        "error": None,
        "signature": ";".join("%s:%d/%d" % (row["limit"], row["limit_value"], row["lanes"])
                               for row in alerts),
    }
    report["summary"] = "; ".join(_alert_text(row) for row in alerts)
    report["message"] = _report_message(report)
    return report


def _report_message(report):
    """The human readable one liner of a reconciliation report."""
    alerts = report.get("alerts") or []
    if not report.get("verified"):
        return "%s: %s" % (UNVERIFIED_LABEL, report.get("error") or "the topology is unreadable")
    if not alerts:
        return "no fleet limit is behind the %d configured lane(s)" % report.get("lane_total", 0)
    return "%d fleet limit(s) behind the %d configured lane(s): %s" % (
        len(alerts), report.get("lane_total", 0), report["summary"])


def _unverified(error, topology, lanes):
    """The report of a reconciliation that could not read its inputs."""
    report = {
        "status": STATUS_UNVERIFIED,
        "ok": False,
        "verified": False,
        "margin": DEFAULT_MARGIN,
        "lane_total": 0,
        "by_backend": {},
        "by_provider": {},
        "by_host": {},
        "by_host_backend": {},
        "limits": {},
        "checks": [],
        "alerts": [],
        "signature": "",
        "summary": "",
        "error": str(error),
        "topology": topology,
        "lanes": lanes,
    }
    report["message"] = _report_message(report)
    return report


def unverified(error, topology=None, lanes=None):
    """The report of a reconciliation that could not read its inputs at all."""
    return _unverified(error, topology, lanes)


def mark_unverified(report, error):
    """Turn a report into an unverified one: the counts stay, nothing is claimed.

    A fleet whose lane configuration could not be read completely must not be
    presented as sufficient: the report keeps the counts it could read, but
    the status says ``unverified`` and the reason is named.
    """
    marked = dict(report or {})
    marked.update(status=STATUS_UNVERIFIED, ok=False, verified=False, alerts=[], checks=[],
                  signature="", summary="", error=str(error))
    marked["message"] = _report_message(marked)
    return marked


def reconcile_topology(topology_path, lanes, margin=DEFAULT_MARGIN):
    """Reconcile a topology file with the given lanes; never raises."""
    try:
        topology = load_topology(topology_path)
    except CapacityConfigError as error:
        return _unverified(error, topology_path, None)
    report = reconcile(topology["limits"], lanes, margin)
    report["topology"] = topology_path
    report["lanes"] = None
    report["hosts"] = topology["hosts"]
    return report


def reconcile_paths(topology_path, lanes_path, margin=DEFAULT_MARGIN):
    """Reconcile a topology file with a lane configuration file; never raises."""
    try:
        topology = load_topology(topology_path)
    except CapacityConfigError as error:
        return _unverified(error, topology_path, lanes_path)
    try:
        lanes = load_lanes(lanes_path)
    except CapacityConfigError as error:
        return _unverified(error, topology_path, lanes_path)
    report = reconcile(topology["limits"], lanes, margin)
    report["topology"] = topology_path
    report["lanes"] = lanes_path
    report["hosts"] = topology["hosts"]
    return report
