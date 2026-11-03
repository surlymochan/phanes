#!/usr/bin/env python3
"""Fleet status page: every water level of the fleet on one screen (Refs #1718).

The 2026-11-02 night shift read five places to answer one question: what is the
fleet doing right now?  The lane status, the GitHub label queue, the fleet
capacity reservations, the MiniMax liveness log and the host probes.  Every one
of them has its own clock, and a stale reading looks exactly like a fresh one.

This tool collects those readings and writes one status page: a machine-readable
JSON document and a one-screen text summary for the brain log.  **Every section
carries its own collection time and its own source**, so an expired number is
visible at a glance.  A section whose collection failed is marked ``未核实``
(unverified) and keeps its error; the rest of the page is still produced.

Usage:
  fleet-status-page.py [--lanes-status FILE] [--gh-issues FILE] [--gh-prs FILE]
                       [--capacity FILE] [--capacity-db FILE] [--hosts FILE]
                       [--broker-log FILE] [--pauses DIR] [--markers DIR]
                       [--gh-live] [--now ISO] [--max-age SECONDS]
                       [--out FILE] [--no-write] [--print text|json] [--self-test]

Every input is optional: a missing input leaves its section ``未核实`` with the
reason, so a partial run still produces a page.  The collection time of a
section is the clock of its own source: the ``_at`` the dispatcher writes into
``lanes-status.json`` for the lane and the dispatcher readings, the moment a
live reading was taken for GitHub, the mtime of the file for the other file
backed sections.  The mtime of ``lanes-status.json`` is only a diagnostic: a
copied, touched or rewritten file would make a snapshot that stopped at 21:00
look live, which is exactly the failure this page exists to make visible.  The
page marks a section ``过期`` (stale) when it is older than ``--max-age``
seconds.

``--gh-live`` reads the pool and the needs-human count from GitHub with the
``gh`` CLI; without it the tool never touches the network.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODULE_PATH = ROOT / "openswarm" / "lib" / "dispatch" / "fleet_status.py"

spec = importlib.util.spec_from_file_location("openswarm_dispatch_fleet_status", MODULE_PATH)
fleet_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet_status)

HEALTH_PATH = ROOT / "openswarm" / "lib" / "dispatch" / "health.py"
_health_spec = importlib.util.spec_from_file_location("openswarm_dispatch_health", HEALTH_PATH)
health = importlib.util.module_from_spec(_health_spec)
_health_spec.loader.exec_module(health)

GH_TIMEOUT = 60
DEFAULT_HOME = Path(os.environ.get("OPENSWARM_HOME", Path.home() / ".claude/openswarm"))


def parse_offset(text):
    """Return the seconds east of UTC of a timezone flag (``+08:00``, ``Z``, ``UTC``)."""
    if text is None:
        return None
    value = str(text).strip()
    if not value or value.upper() in ("Z", "UTC", "LOCAL"):
        return 0 if value.upper() in ("Z", "UTC") else None
    sign = 1
    if value[0] in "+-":
        sign = -1 if value[0] == "-" else 1
        value = value[1:]
    parts = value.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        raise ValueError("invalid timezone: %r" % text)
    return sign * (hours * 3600 + minutes * 60)


# ------------------------------------------------------------------- helpers

class SectionError(Exception):
    """A section that could not be collected.

    ``data`` holds what could be read anyway and ``stamp`` the clock of the
    reading that did work: the page shows the numbers next to the ``未核实``
    mark instead of dropping them, and keeps the fresh reading's time.
    """

    def __init__(self, message, data=None, stamp=None, source=None):
        super().__init__(message)
        self.data = data if data is not None else {}
        self.stamp = stamp
        self.source = source


def reading_meta(stamp, now, max_age_s, source, status="ok", error=None):
    """The metadata of one reading: its own clock, its age and its verdict."""
    moment = fleet_status.epoch(stamp)
    age = None
    stale = None
    if moment is not None and now is not None:
        age = max(0.0, float(now) - moment)
        stale = age > float(max_age_s)
    return {
        "collected_at": fleet_status.iso(moment),
        "source": source,
        "status": status,
        "verified": None if status == "absent" else status == "ok",
        "error": error,
        "age_s": age,
        "stale": stale,
    }


def read_json(path):
    """Return ``(data, mtime)`` of a JSON file; raises when it cannot be read."""
    target = Path(path)
    with open(target, encoding="utf-8") as stream:
        data = json.load(stream)
    return data, target.stat().st_mtime


def read_jsonl(path):
    """Return ``(rows, mtime, skipped)`` of a JSONL file; raises when it cannot be read."""
    target = Path(path)
    rows = []
    skipped = 0
    with open(target, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                skipped += 1
    return rows, target.stat().st_mtime, skipped


def newest_mtime(paths):
    """Return the newest mtime of the existing paths, or None when there is none."""
    stamps = []
    for path in paths:
        try:
            stamps.append(Path(path).stat().st_mtime)
        except OSError:
            continue
    return max(stamps) if stamps else None


def run_gh(args):
    """Run a ``gh`` command and return its JSON output."""
    if shutil.which("gh") is None:
        raise RuntimeError("gh is not installed")
    process = subprocess.run(["gh"] + list(args), capture_output=True, text=True,
                              timeout=GH_TIMEOUT)
    if process.returncode:
        detail = (process.stderr or process.stdout).strip().splitlines()
        raise RuntimeError("gh failed (rc=%d): %s" % (process.returncode, detail[-1] if detail else "?"))
    return json.loads(process.stdout or "[]")


def marker_scope(name):
    """Return the pause scope of a legacy marker file name, or None."""
    if name == "PAUSE":
        return "all"
    if name == "PAUSE-MINT":
        return "mint"
    if name == "NO-LANE-REVIEW":
        return "no-lane-review"
    for prefix, kind in (("PAUSE-HOST-", "host"), ("PAUSE-BACKEND-", "backend")):
        if name.startswith(prefix) and len(name) > len(prefix):
            return "%s:%s" % (kind, name[len(prefix):])
    if name.startswith("PAUSE-") and len(name) > len("PAUSE-"):
        return "lane:%s" % name[len("PAUSE-"):]
    return None


def pause_records(pause_dir, marker_dir, now):
    """Return the active pause records of the pause store and the legacy markers."""
    records = []
    if pause_dir:
        for path in sorted(Path(pause_dir).glob("*.json")):
            try:
                with open(path, encoding="utf-8") as stream:
                    record = json.load(stream)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            expires = fleet_status.epoch(record.get("expires_at"))
            if expires is not None and now is not None and expires <= now:
                continue
            records.append({"scope": record.get("scope") or path.stem,
                            "reason": record.get("reason", ""),
                            "created_at": record.get("created_at"),
                            "expires_at": record.get("expires_at")})
    if marker_dir:
        for path in sorted(Path(marker_dir).iterdir()):
            if not path.is_file():
                continue
            scope = marker_scope(path.name)
            if scope is None:
                continue
            records.append({"scope": scope, "reason": "legacy marker: " + path.name,
                            "created_at": path.stat().st_mtime, "expires_at": None})
    return records


def capacity_from_db(path):
    """Read the fleet capacity reservations from the coordinator's database."""
    target = Path(path)
    uri = "file:%s?mode=ro" % target.resolve().as_posix()
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = connection.execute(
            "SELECT host,run_id,backend,provider,created FROM reservations ORDER BY created"
        ).fetchall()
    finally:
        connection.close()
    return [{"host": row[0], "run_id": row[1], "backend": row[2], "provider": row[3],
             "created": row[4]} for row in rows]


def capacity_limits(capacity_dir):
    """Read the optional fleet topology limits of the capacity directory."""
    if not capacity_dir:
        return None
    path = Path(capacity_dir) / "topology.json"
    try:
        with open(path, encoding="utf-8") as stream:
            topology = json.load(stream)
    except (OSError, ValueError):
        return None
    limits = topology.get("limits") if isinstance(topology, dict) else None
    return limits if isinstance(limits, dict) else None


# ------------------------------------------------------------------ sections

def section_lanes(args, now):
    """The busy/idle reading of the lane status snapshot.

    The clock of this section is the ``_at`` the snapshot carries, not the
    mtime of the file: a copied, touched or rewritten file would make a
    snapshot that stopped at 21:00 look live.  A snapshot without a valid
    ``_at`` has no clock at all, so the section is marked unverified.
    """
    if not args.lanes_status:
        raise RuntimeError("no lane status configured (--lanes-status)")
    status, mtime = read_json(args.lanes_status)
    data = fleet_status.lane_data(status)
    data["source_stamp"] = status.get("_at") if isinstance(status, dict) else None
    data["file_mtime"] = fleet_status.iso(mtime)
    claimed = fleet_status.snapshot_at(status, getattr(args, "tz", None))
    if claimed is None:
        raise SectionError("lanes-status.json has no valid _at stamp: the snapshot does "
                           "not say when it was taken", data)
    return data, claimed, "lanes-status.json"


def section_pool(args, now):
    """The pool depth in two readings: the GitHub labels and the dispatcher count.

    Both readings keep their own clock: the GitHub reading is dated by the
    moment it was taken, the dispatcher reading by the ``_at`` of the lane
    status snapshot it comes from.  The section is as fresh as its oldest
    reading, so a stalled dispatcher snapshot expires the whole pool section.
    """
    ad_ready = None
    ad_stamp = None
    ad_error = None
    ad_status = "absent"
    if args.lanes_status:
        try:
            status, _mtime = read_json(args.lanes_status)
        except (OSError, ValueError):
            status = None
        if isinstance(status, dict):
            ad_ready = status.get("_ready")
            ad_stamp = fleet_status.snapshot_at(status, getattr(args, "tz", None))
            if ad_stamp is None:
                ad_status = "unverified"
                ad_error = ("lanes-status.json has no valid _at stamp: the dispatcher "
                            "reading cannot be dated")
            else:
                ad_status = "ok"
        else:
            ad_status = "unverified"
            ad_error = "lanes-status.json could not be read as a snapshot"
    if args.gh_issues:
        issues, gh_stamp = read_json(args.gh_issues)
        gh_source = "gh issue list --label lane-ready"
    elif args.gh_live:
        issues = run_gh(["issue", "list", "--label", "lane-ready", "--state", "open",
                         "--limit", "500", "--json", "number,labels,state,title"])
        gh_stamp = now
        gh_source = "gh issue list --label lane-ready"
    else:
        raise RuntimeError("no pool source configured (--gh-issues or --gh-live)")
    if not isinstance(issues, list):
        raise RuntimeError("the pool source must hold a JSON array of issues")
    data = fleet_status.pool_data(issues, ad_ready)
    max_age = getattr(args, "max_age", fleet_status.DEFAULT_MAX_AGE_S)
    data["gh"].update(reading_meta(gh_stamp, now, max_age, gh_source))
    ad_source = "lanes-status.json _ready" if ad_status != "absent" else None
    data["ad"].update(reading_meta(ad_stamp, now, max_age, ad_source,
                                    status=ad_status, error=ad_error))
    source = gh_source + " + lanes-status.json"
    stamps = [stamp for stamp in (gh_stamp, ad_stamp) if stamp is not None]
    stamp = min(stamps) if stamps else None
    if ad_status == "unverified":
        raise SectionError(ad_error, data, stamp=gh_stamp, source=source)
    return data, stamp, source


def section_capacity(args, now):
    """The fleet capacity reservations and the age of the oldest one."""
    limits = capacity_limits(args.capacity_dir)
    if args.capacity:
        payload, stamp = read_json(args.capacity)
        reservations = payload.get("reservations") if isinstance(payload, dict) else payload
        source = "fleet-capacity status"
    elif args.capacity_db:
        reservations = capacity_from_db(args.capacity_db)
        stamp = Path(args.capacity_db).stat().st_mtime
        source = "capacity.sqlite"
    elif args.capacity_dir:
        database = Path(args.capacity_dir) / "capacity.sqlite"
        if not database.exists():
            raise RuntimeError("no capacity database in " + str(args.capacity_dir))
        reservations = capacity_from_db(database)
        stamp = database.stat().st_mtime
        source = "capacity.sqlite"
    else:
        raise RuntimeError("no capacity source configured (--capacity, --capacity-db or --capacity-dir)")
    if not isinstance(reservations, list):
        raise RuntimeError("the capacity source must hold a JSON array of reservations")
    data = fleet_status.capacity_data(reservations, now, limits, clock_states(args.hosts))
    return data, stamp, source


def clock_states(path):
    """The clock of every host, read from the fleet-health state (Refs #1703).

    A reservation is stamped by the host that holds it and its age is compared
    with the clock of the control plane: the age of a host with a skewed clock
    is not a fact, and the page has to say so.
    """
    if not path:
        return {}
    try:
        state, _stamp = read_json(path)
    except (OSError, ValueError):
        return {}
    return health.clock_states(state)


def section_hosts(args, now):
    """One line per fleet host: alive or not."""
    if not args.hosts:
        raise RuntimeError("no host health source configured (--hosts)")
    state, stamp = read_json(args.hosts)
    data = fleet_status.hosts_data(state, now, getattr(args, "tz", None))
    return data, stamp, "fleet-health.json"


def section_burn(args, now):
    """The MiniMax consumption of the last 15 minutes (liveness reading)."""
    if not args.broker_log:
        raise RuntimeError("no broker log configured (--broker-log)")
    rows, stamp, skipped = read_jsonl(args.broker_log)
    data = fleet_status.burn_data(rows, now, fleet_status.BURN_WINDOW_S)
    data["skipped_lines"] = skipped
    return data, stamp, "broker requests jsonl"


def section_needs_human(args, now):
    """How many pull requests wait for a human."""
    if args.gh_prs:
        rows, stamp = read_json(args.gh_prs)
        source = "gh pr list --label needs-human"
    elif args.gh_live:
        rows = run_gh(["pr", "list", "--label", "needs-human", "--state", "open",
                       "--limit", "500", "--json", "number,title,url"])
        stamp = now
        source = "gh pr list --label needs-human"
    else:
        raise RuntimeError("no needs-human source configured (--gh-prs or --gh-live)")
    if not isinstance(rows, list):
        raise RuntimeError("the needs-human source must hold a JSON array of pull requests")
    data = fleet_status.needs_human_data(rows)
    return data, stamp, source


def section_pauses(args, now):
    """The PAUSE switches that are currently on."""
    if not args.pauses and not args.markers:
        raise RuntimeError("no pause source configured (--pauses or --markers)")
    records = pause_records(args.pauses, args.markers, now)
    data = fleet_status.pause_data(records, now)
    stamp = newest_mtime([Path(args.pauses)] if args.pauses and Path(args.pauses).is_dir() else [])
    if args.markers:
        marker_stamp = newest_mtime(list(Path(args.markers).iterdir())
                                    if Path(args.markers).is_dir() else [])
        stamp = marker_stamp if stamp is None else max(stamp, marker_stamp)
    return data, stamp if stamp is not None else now, "pause store + PAUSE markers"


COLLECTORS = (
    ("lanes", "车道忙闲与在途卡", section_lanes, "lanes-status.json"),
    ("pool", "池深度（gh 口径 / AD _ready 双口径）", section_pool,
     "gh issue list --label lane-ready + lanes-status.json"),
    ("capacity", "容量预留", section_capacity, "fleet-capacity status"),
    ("hosts", "主机存活", section_hosts, "fleet-health.json"),
    ("burn", "MiniMax 近 15 分钟烧量（liveness 口径）", section_burn, "broker requests jsonl"),
    ("needs_human", "needs-human 数量", section_needs_human, "gh pr list --label needs-human"),
    ("pauses", "PAUSE 开关清单", section_pauses, "pause store + PAUSE markers"),
)


def build_page(args, now):
    """Collect every section and assemble the page.

    A section that fails to collect never takes the page down: it is marked
    unverified and keeps its error.  What the section could read anyway is
    kept as well, so the reader still sees the numbers next to the mark.
    """
    sections = []
    for key, title, collector, default_source in COLLECTORS:
        try:
            data, stamp, source = collector(args, now)
        except SectionError as error:
            sections.append(fleet_status.make_section(
                key, title, error.source or default_source, error.stamp, error.data,
                str(error)))
            continue
        except Exception as error:  # a broken section must not break the page
            sections.append(fleet_status.make_section(
                key, title, default_source, None, {},
                "%s: %s" % (type(error).__name__, error)))
            continue
        sections.append(fleet_status.make_section(key, title, source, stamp, data))
    return fleet_status.aggregate(sections, now=now, max_age_s=args.max_age)


# ---------------------------------------------------------------------- main

def write_page(report, path):
    """Write the machine-readable page."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return target


def append_brain_log(report, path):
    """Append the one-screen summary to a brain log file."""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(report["text"] + "\n")
    return target


def self_test():
    """Offline self test: a full page and a broken section."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "lanes-status.json").write_text(json.dumps({
            "M1": "busy #980", "M2": "idle", "M3": "ssh: timeout",
            "_busy": 1, "_ready": 2, "_blocked": 0, "_at": "2026-11-02 21:04:12"}), encoding="utf-8")
        (root / "gh-issues.json").write_text(json.dumps([
            {"number": 1711, "state": "OPEN", "labels": [{"name": "lane-ready"}]},
            {"number": 1712, "state": "OPEN", "labels": ["lane-ready", "in-progress"]}]), encoding="utf-8")
        (root / "gh-prs.json").write_text(json.dumps(
            [{"number": 900, "title": "needs a human"}]), encoding="utf-8")
        (root / "capacity.json").write_text(json.dumps({"ok": True, "reservations": [
            {"host": "gocc", "run_id": "r-1", "backend": "codex", "provider": None,
             "created": 1793624100.0}]}),
            encoding="utf-8")
        (root / "fleet-health.json").write_text(json.dumps({"hosts": {
            "gocc": {"status": "ok", "ssh_fail_streak": 0},
            "gores1": {"status": "unreachable", "ssh_fail_streak": 3}}}), encoding="utf-8")
        (root / "broker.jsonl").write_text("\n".join(json.dumps(row) for row in [
            {"ts": "2026-11-02T13:04:00+00:00", "lane": "M1", "status": 200, "in_tok": 10, "out_tok": 5},
            {"ts": "2026-11-02T13:04:30+00:00", "lane": "M1", "status": 429}]), encoding="utf-8")
        pauses = root / "pauses"
        pauses.mkdir()
        (pauses / "all.json").write_text(json.dumps(
            {"scope": "all", "created_at": 1793624100.0, "expires_at": None, "reason": "手工暂停"}),
            encoding="utf-8")
        stamp = 1793624600.0
        for path in root.iterdir():
            if path.is_file():
                os.utime(path, (stamp, stamp))
        os.utime(pauses, (stamp, stamp))

        args = argparse.Namespace(
            lanes_status=str(root / "lanes-status.json"),
            gh_issues=str(root / "gh-issues.json"), gh_prs=str(root / "gh-prs.json"),
            capacity=str(root / "capacity.json"), capacity_db=None, capacity_dir=None,
            hosts=str(root / "fleet-health.json"), broker_log=str(root / "broker.jsonl"),
            pauses=str(pauses), markers=None, gh_live=False, max_age=300.0, tz=28800)
        report = build_page(args, 1793624700.0)
        summary = report["summary"]
        assert summary["total"] == 7, summary
        assert summary["unverified"] == 0, report
        lanes = report["sections"][0]["data"]
        assert lanes["busy"] == 1 and lanes["idle"] == 1 and lanes["unknown"] == 1, lanes
        assert lanes["in_flight"] == [980], lanes
        # The clock of the lane section is the _at of the snapshot, not the mtime.
        assert report["sections"][0]["collected_at_epoch"] == 1793624652.0, report["sections"][0]
        pool = report["sections"][1]["data"]
        assert pool["gh"]["ready"] == 1 and pool["ad"]["ready"] == 2, pool
        assert pool["delta"] == -1, pool
        assert pool["ad"]["collected_at"] == "2026-11-02T13:04:12+00:00", pool
        capacity = report["sections"][2]["data"]
        assert capacity["total"] == 1 and capacity["oldest_age_s"] == 600.0, capacity
        assert capacity["oldest_age_reliable"] is True, capacity
        hosts = report["sections"][3]["data"]
        assert hosts["alive"] == 1 and hosts["down"] == 1, hosts
        burn = report["sections"][4]["data"]
        assert burn["requests"] == 2 and burn["rate_limited"] == 1, burn
        assert report["sections"][5]["data"]["count"] == 1
        assert report["sections"][6]["data"]["count"] == 1

        # A broken section: the page is still produced and the section says 未核实.
        args.hosts = str(root / "does-not-exist.json")
        broken = build_page(args, 1793624700.0)
        assert broken["summary"]["total"] == 7, broken
        assert broken["summary"]["unverified"] == 1, broken
        assert broken["sections"][3]["verified"] is False, broken
        assert "未核实" in broken["text"], broken["text"]
        assert broken["sections"][0]["verified"] is True, broken

        # A host whose clock is off: the age of its reservation is not a fact
        # (Refs #1703).
        (root / "fleet-health.json").write_text(json.dumps({"hosts": {
            "gocc": {"status": "soft", "ssh_fail_streak": 0,
                     "clock_offset_s": 300.0, "clock_unreliable": True},
            "gores1": {"status": "unreachable", "ssh_fail_streak": 3}}}), encoding="utf-8")
        args.hosts = str(root / "fleet-health.json")
        skewed = build_page(args, 1793624700.0)
        skewed_capacity = skewed["sections"][2]["data"]
        assert skewed_capacity["oldest_age_reliable"] is False, skewed_capacity
        assert skewed_capacity["clock_unreliable_hosts"] == ["gocc"], skewed_capacity
        assert "clock_unreliable=gocc" in skewed["text"], skewed["text"]

        # A snapshot without a clock cannot be dated: the lane section and the
        # dispatcher reading of the pool section are marked 未核实.
        status = json.loads((root / "lanes-status.json").read_text(encoding="utf-8"))
        del status["_at"]
        (root / "lanes-status.json").write_text(json.dumps(status), encoding="utf-8")
        undated = build_page(args, 1793624700.0)
        assert undated["summary"]["total"] == 7, undated
        assert undated["sections"][0]["verified"] is False, undated
        assert "_at" in undated["sections"][0]["error"], undated
        assert undated["sections"][1]["verified"] is False, undated
        assert undated["sections"][1]["data"]["gh"]["ready"] == 1, undated
        assert "未核实" in undated["text"], undated["text"]
    print("fleet status page self-test: 7 sections, dual pool reading, oldest reservation, "
          "burn window, pause switches, a broken section and an undated snapshot "
          "all passed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lanes-status", help="lanes-status.json of the dispatcher")
    parser.add_argument("--gh-issues", help="JSON file with the gh lane-ready issue list")
    parser.add_argument("--gh-prs", help="JSON file with the gh needs-human pull request list")
    parser.add_argument("--capacity", help="JSON file with the fleet-capacity status output")
    parser.add_argument("--capacity-db", help="the capacity coordinator database (read only)")
    parser.add_argument("--capacity-dir", help="the capacity coordinator directory")
    parser.add_argument("--hosts", help="fleet-health.json of the host probes")
    parser.add_argument("--broker-log", help="the MiniMax broker request log (JSONL)")
    parser.add_argument("--pauses", help="the pause store directory")
    parser.add_argument("--markers", help="the dispatcher directory with the PAUSE markers")
    parser.add_argument("--gh-live", action="store_true", help="read the pool and needs-human from GitHub")
    parser.add_argument("--now", help="the time the page belongs to (ISO-8601), default: now")
    parser.add_argument("--tz", help="the timezone of the naive source stamps (+08:00), default: local")
    parser.add_argument("--max-age", type=float, default=fleet_status.DEFAULT_MAX_AGE_S,
                        help="seconds after which a section counts as expired")
    parser.add_argument("--out", help="where to write the page (default: <home>/fleet-status.json)")
    parser.add_argument("--no-write", action="store_true", help="do not write the page to a file")
    parser.add_argument("--brain-log", help="append the one-screen summary to this brain log file")
    parser.add_argument("--print", dest="output", choices=("text", "json"), default="text",
                        help="what to print on stdout")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when a section is unverified or expired")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0

    now = fleet_status.epoch(args.now) if args.now else None
    if now is None:
        now = fleet_status.epoch(time.time())
    try:
        args.tz = parse_offset(args.tz)
    except ValueError as error:
        parser.error(str(error))
    report = build_page(args, now)
    if not args.no_write:
        target = Path(args.out) if args.out else Path(DEFAULT_HOME) / "fleet-status.json"
        write_page(report, target)
        report["written_to"] = str(target)
    if args.brain_log:
        append_brain_log(report, args.brain_log)
    if args.output == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["text"])
    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
