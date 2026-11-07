#!/usr/bin/env python3
"""Report lane budget timeouts per host, backend and work kind (Refs #1605).

Answers the operations question "is the host slow, or is the card too big?"
by grouping every run found under the given runs directories by
host x backend x kind (implementation / review) and reporting the
budget_exceeded rate plus the seconds distribution (p50/p90/max) and how many
timed-out runs still carry a diff a human can salvage.

Usage:
  python3 tools/lane-budget-report.py --runs DIR [--runs DIR ...] \
      --lanes lanes.json [--since ISO-8601] [--json]

--runs may be given several times: the box's local runs directory plus the
copies pulled back from the remote hosts.  The same run found in two
directories is counted once.

--lanes is an operations-owned lane -> (host, backend) mapping.  swrm's
own ~/.claude/swrm/lanes.json carries no host field (the host lives in
the fleet inventory), so it cannot be used as-is.  Three shapes are accepted:
  {"CX9": {"host": "gores2", "backend": "codex"}}
  [{"lane": "CX9", "host": "gores2", "backend": "codex"}]
  {"hosts": {"gores2": {"lanes": [{"name": "CX9", "backend": "codex"}]}}}

--since keeps only the runs that finished at or after the given timestamp.

RESCUABLE counts the timed-out runs that still carry a real diff: a diffstat,
a non-empty patch or the files the run reported as changed.  The worktree
path swrm records for every lane run is not evidence (finalize_workspace
writes it whatever happened), and review cards run read-only, so their
changes are dropped by design and are never counted.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# swrm run ids are "%Y%m%d-%H%M%S-<hex>" (new_run_id), so the directory
# name itself carries the creation time.
RUN_ID_RE = re.compile(r"(\d{8})-(\d{6})")
DIFFSTAT_RE = re.compile(r"(\d+)\s+insertions?\(\+\).*?(\d+)\s+deletions?\(-\)", re.S)
NUMBER_RE = re.compile(r"\b(\d+)\b")
# A group is only flagged when the sample is big enough to mean something.
SUSPECT_MIN_RUNS = 4
SUSPECT_MIN_RATIO = 0.30


def parse_ts(value):
    """Parse an epoch or ISO-8601 timestamp into an aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_ts(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def run_id_time(text):
    """Parse the ``%Y%m%d-%H%M%S`` prefix of a run id into a UTC datetime."""
    match = RUN_ID_RE.search(text or "")
    if not match:
        return None
    try:
        stamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    # new_run_id() uses local time, so convert the naive stamp the same way.
    return stamp.astimezone(timezone.utc)


def load_json(path):
    """Read a JSON file; None when it is missing or unparsable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mapping_lanes(mapping):
    """Lane entries of a {lane: {host, backend}} mapping."""
    lanes = {}
    for name, info in mapping.items():
        if isinstance(info, dict):
            lanes[name] = (info.get("host") or "unknown", info.get("backend") or "unknown")
        elif isinstance(info, str):
            lanes[name] = (info, "unknown")
    return lanes


def list_lanes(entries):
    """Lane entries of a [{lane, host, backend}] list."""
    lanes = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("lane") or entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        lanes[name] = (entry.get("host") or "unknown", entry.get("backend") or "unknown")
    return lanes


def inventory_lanes(hosts):
    """Lane entries of a fleet inventory {"hosts": {host: {"lanes": [...]}}}."""
    lanes = {}
    for host, entry in hosts.items():
        entries = entry.get("lanes") if isinstance(entry, dict) else None
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("lane")
            if not name:
                continue
            lanes[name] = (item.get("host") or host, item.get("backend") or "unknown")
    return lanes


def load_lanes(path):
    """Read the lane -> (host, backend) mapping.

    Accepts a {lane: {host, backend}} object, a [{lane, host, backend}] list
    (a top level list is handled before the dict normalisation) or a fleet
    inventory {"hosts": {host: {"lanes": [...]}}} object.  A file that yields
    no lane entry is an error: silently mapping every lane to "unknown" would
    make the per-host conclusion meaningless.
    """
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        lanes = list_lanes(data)
    elif isinstance(data, dict):
        if isinstance(data.get("hosts"), dict):
            lanes = inventory_lanes(data["hosts"])
        elif isinstance(data.get("lanes"), list):
            lanes = list_lanes(data["lanes"])
        elif isinstance(data.get("lanes"), dict):
            lanes = mapping_lanes(data["lanes"])
        else:
            lanes = mapping_lanes(data)
    else:
        raise ValueError(f"invalid lanes file {path!r}: expected an object or a list")
    if not lanes:
        raise ValueError(f"invalid lanes file {path!r}: no lane entry found")
    return lanes


def diff_lines(value):
    """Lines touched by a run, from a diffstat blob, a count or a summary line."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        for key in ("lines", "total", "changed_lines"):
            if isinstance(value.get(key), (int, float)) and not isinstance(value.get(key), bool):
                return int(value[key])
        value = value.get("text", "")
    if not isinstance(value, str) or not value.strip():
        return 0
    match = DIFFSTAT_RE.search(value)
    if match:
        return int(match.group(1)) + int(match.group(2))
    numbers = NUMBER_RE.findall(value)
    return int(numbers[-1]) if numbers else 0


def changed_files(item):
    """Files a run touched, as reported in the result payload."""
    payload = item.get("result")
    files = payload.get("files_changed") if isinstance(payload, dict) else None
    if files is None:
        files = item.get("files_changed")
    if isinstance(files, list):
        return len(files)
    if isinstance(files, (int, float)) and not isinstance(files, bool):
        return int(files)
    return 0


def patch_evidence(item):
    """True when the run left a patch with content behind."""
    patch = item.get("patch")
    if not patch:
        return False
    if isinstance(patch, str):
        if "\n" in patch or patch.lstrip().startswith("diff "):
            return bool(patch.strip())
        try:
            path = Path(patch)
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False
    return bool(patch)


def worktree_residual(item):
    """True when the leftover worktree still carries uncommitted changes.

    swrm records the worktree root for every lane run (the read-only
    branch writes the literal path, finalize_workspace returns it whenever
    ``keep`` is set), so the mere presence of the path proves nothing.  Only
    a dirty worktree counts, and only when it is the root of a git worktree
    of its own.
    """
    root = item.get("worktree")
    if not isinstance(root, str) or not root.strip():
        return False
    path = Path(root)
    if not path.is_dir():
        return False
    try:
        top = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    if top.returncode != 0:
        return False
    try:
        if Path(top.stdout.strip()).resolve() != path.resolve():
            return False
    except OSError:
        return False
    try:
        status = subprocess.run(["git", "-C", str(path), "-c", "status.showUntrackedFiles=all",
                                 "status", "--porcelain"],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    if status.returncode != 0:
        return False
    return bool(status.stdout.strip())


def has_diff_evidence(item, kind=None, check_worktree=True):
    """True when a run left a diff a human can salvage.

    The worktree path is not evidence: swrm writes it for every lane
    run, so trusting it would mark every timed-out run as rescuable.  A diff
    is a diffstat, a non-empty patch or the files the run reported as
    changed; a leftover worktree only counts when it is really dirty.  Review
    cards are dispatched read-only and their changes are dropped by design
    (``review_changes_discarded``), so they never qualify.
    """
    if item.get("review_changes_discarded"):
        return False
    if kind == "review":
        return False
    if diff_lines(item.get("diffstat")) > 0:
        return True
    if changed_files(item) > 0:
        return True
    if patch_evidence(item):
        return True
    return bool(check_worktree) and worktree_residual(item)


def run_timestamp(run_dir, item, status):
    """Wall-clock time of a run.

    swrm's result.json carries no timestamp field; the real times live in
    the sibling status.json (started/finished/updated, epoch seconds), in the
    run id and, as a last resort, in the result.json mtime.
    """
    for key in ("finished", "started", "updated", "attempt_started"):
        when = parse_ts(status.get(key))
        if when:
            return when
    for source in (item.get("id"), run_dir.name):
        when = run_id_time(str(source or ""))
        if when:
            return when
    for key in ("ts", "created_at", "started_at", "finished_at"):
        when = parse_ts(item.get(key))
        if when:
            return when
    try:
        return datetime.fromtimestamp((run_dir / "result.json").stat().st_mtime, timezone.utc)
    except OSError:
        return None


def is_timeout(item):
    """True for a run that hit its own time budget.

    The daily quota denial (``budget_exceeded_daily``) is not a slow run and
    must not be counted as one.
    """
    error_class = item.get("error_class")
    if error_class == "budget_exceeded_daily":
        return False
    return error_class == "budget_exceeded" or item.get("status") == "budget_exceeded"


def read_runs(directories, lanes, since=None):
    """Collect the runs below the given directories.

    A run is identified by its id, so a run present in the box's local
    directory and in a pulled-back copy is only counted once.  With ``since``
    the runs that are older than the cut-off are dropped.
    """
    runs = {}
    for directory in directories:
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"runs directory not found: {directory}")
        for path in sorted(root.rglob("result.json")):
            item = load_json(path)
            if not isinstance(item, dict):
                continue  # a broken result.json must not kill the report
            run_dir = path.parent
            status = load_json(run_dir / "status.json")
            if not isinstance(status, dict):
                status = {}
            when = run_timestamp(run_dir, item, status)
            if since is not None and (when is None or when < since):
                continue
            key = str(item.get("id") or run_dir.name)
            known = runs.get(key)
            if known is not None:
                if known["when"] is None and when is not None:
                    known["when"] = when
                continue
            lane = item.get("lane") or status.get("lane") or "unknown"
            host, lane_backend = lanes.get(lane, ("unknown", "unknown"))
            title = str(item.get("title") or status.get("title") or "")
            kind = "review" if title.lower().startswith("review") else "implementation"
            seconds = item.get("seconds")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                seconds = None
            timeout = is_timeout(item)
            runs[key] = {
                "host": host,
                "backend": item.get("backend") or lane_backend,
                "kind": kind,
                "seconds": seconds,
                "timeout": timeout,
                "diff_lines": diff_lines(item.get("diffstat")),
                # Only the timed-out runs need the salvagable verdict, so the
                # worktree is only inspected for those.
                "rescuable": has_diff_evidence(item, kind=kind, check_worktree=timeout),
                "when": when,
            }
    return list(runs.values())


def lanes_missing_host(lanes):
    """Lane entries the mapping could not resolve to a host."""
    return sorted(name for name, (host, _backend) in lanes.items() if host == "unknown")


def percentile(values, fraction):
    """Nearest-rank percentile, stable for the small samples an ops report sees."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(len(ordered) * fraction)
    return ordered[max(0, min(rank, len(ordered)) - 1)]


def report(runs):
    """Aggregate the runs per host x backend x kind."""
    groups = defaultdict(list)
    for run in runs:
        groups[(run["host"], run["backend"], run["kind"])].append(run)
    rows = []
    for (host, backend, kind), items in sorted(groups.items()):
        timeouts = [item for item in items if item["timeout"]]
        seconds = [item["seconds"] for item in items if item["seconds"] is not None]
        rate = len(timeouts) / len(items)
        rows.append({"host": host, "backend": backend, "kind": kind, "runs": len(items),
                     "budget_exceeded": len(timeouts), "ratio": rate,
                     "seconds_p50": percentile(seconds, .5), "seconds_p90": percentile(seconds, .9),
                     "seconds_max": max(seconds) if seconds else None,
                     "timeout_with_diff": sum(item["rescuable"] for item in timeouts),
                     "flag": "SUSPECT" if len(items) >= SUSPECT_MIN_RUNS and rate >= SUSPECT_MIN_RATIO else ""})
    return rows


def self_test():
    """Contract check: grouping, quantiles, SUSPECT flag, --since, lanes parsing."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "box"
        root.mkdir()
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).timestamp()
        stale = (now - timedelta(days=10)).timestamp()
        lanes = {"A": ("box", "codex"), "B": ("remote", "pi")}
        fixtures = [
            # lane, backend, title, seconds, status, diffstat, when, status.json
            ("A", "codex", "implement 1", 10, "ok", "", recent, False),
            ("A", "codex", "implement 2", 20, "budget_exceeded",
             "2 files changed, 3 insertions(+), 1 deletion(-)", recent, True),
            ("A", "codex", "implement 3", 30, "budget_exceeded", "", recent, False),
            ("A", "codex", "implement 4", 40, "budget_exceeded",
             "1 file changed, 8 insertions(+), 2 deletions(-)", recent, False),
            ("A", "codex", "Review PR #1605", 50, "ok", "", recent, False),
            ("B", "pi", "implement 5", 5, "budget_exceeded", "", stale, True),
            ("B", "pi", "review PR #1605", 15, "ok", "", stale, False),
            ("B", "pi", "review PR #1605 again", 25, "budget_exceeded", "4 lines", stale, False),
        ]
        for index, (lane, backend, title, seconds, status, diffstat, when, with_status) in enumerate(fixtures):
            stamp = datetime.fromtimestamp(when).strftime("%Y%m%d-%H%M%S")
            run_dir = root / f"{stamp}-{index:06x}"
            run_dir.mkdir()
            record = {"lane": lane, "backend": backend, "title": title, "seconds": seconds,
                      "status": status, "error_class": status if status == "budget_exceeded" else "",
                      "diffstat": diffstat}
            if index != 0:  # one run deliberately exercises missing fields
                record["ts"] = datetime.fromtimestamp(when, timezone.utc).isoformat()
            (run_dir / "result.json").write_text(json.dumps(record))
            if with_status:
                (run_dir / "status.json").write_text(json.dumps({"state": "finished", "finished": when}))
        bad = root / "bad"
        bad.mkdir()
        (bad / "result.json").write_text("{")

        rows = report(read_runs([root], lanes))
        box = next(row for row in rows if row["host"] == "box" and row["kind"] == "implementation")
        assert box["runs"] == 4 and box["budget_exceeded"] == 3
        assert box["seconds_p50"] == 20 and box["seconds_p90"] == 40
        assert box["flag"] == "SUSPECT" and box["timeout_with_diff"] == 2
        assert len(rows) == 4

        # --since really filters: the stale runs (lane B) must disappear.
        cutoff = datetime.fromtimestamp((now - timedelta(days=5)).timestamp(), timezone.utc)
        fresh = report(read_runs([root], lanes, cutoff))
        assert {row["host"] for row in fresh} == {"box"}, fresh
        assert len(fresh) == 2 and fresh[0]["runs"] == 4, fresh
        assert all(row["backend"] == "codex" for row in fresh), fresh

        # The same run found in two --runs directories is counted once.
        copy = Path(tmp) / "pulled-back"
        shutil.copytree(root, copy)
        assert len(read_runs([root], lanes)) == 8
        assert len(read_runs([root, copy], lanes)) == 8
        assert len(read_runs([root, copy], lanes, cutoff)) == 5

        # Both lanes file shapes resolve to the same host and backend.
        dict_file = root / "lanes-dict.json"
        dict_file.write_text(json.dumps({"A": {"host": "box", "backend": "codex"},
                                         "B": {"host": "remote", "backend": "pi"}}))
        list_file = root / "lanes-list.json"
        list_file.write_text(json.dumps([{"lane": "A", "host": "box", "backend": "codex"},
                                         {"lane": "B", "host": "remote", "backend": "pi"}]))
        for path in (dict_file, list_file):
            parsed = load_lanes(path)
            assert parsed["A"] == ("box", "codex"), parsed
            assert parsed["B"] == ("remote", "pi"), parsed
        assert report(read_runs([root], load_lanes(list_file))) == report(read_runs([root], load_lanes(dict_file)))

        # A lanes file that cannot be parsed is an error, not an empty mapping.
        broken = root / "lanes-broken.json"
        broken.write_text("{")
        try:
            load_lanes(broken)
        except ValueError:
            pass
        else:
            raise AssertionError("a broken lanes file must be rejected")

        # A run without a commit still counts as rescuable when it changed files.
        assert has_diff_evidence({"result": {"files_changed": ["a.py"]}})
        assert not has_diff_evidence({"diffstat": "", "result": {"files_changed": []}})

        # A worktree path alone is not a diff: swrm records it for every
        # lane run, so trusting it would mark every timeout as rescuable.
        stale_worktree = root / "worktree"
        stale_worktree.mkdir()
        assert not has_diff_evidence({"worktree": str(stale_worktree), "diffstat": "",
                                      "result": {"files_changed": []}})
        assert not has_diff_evidence({"worktree": str(root / "missing"), "diffstat": "",
                                      "result": {"files_changed": []}})
        # A worktree that really carries uncommitted changes is evidence.
        dirty = Path(tmp) / "dirty-worktree"
        dirty.mkdir()
        try:
            subprocess.run(["git", "init", "-q", str(dirty)], check=True, capture_output=True, timeout=30)
            (dirty / "leftover.txt").write_text("x\n")
            assert has_diff_evidence({"worktree": str(dirty), "diffstat": "",
                                      "result": {"files_changed": []}})
        except (OSError, subprocess.SubprocessError):
            pass  # no git here: the check degrades to "no evidence"
        # Review cards run read-only: their changes are dropped by design.
        assert not has_diff_evidence({"diffstat": "1 file changed, 8 insertions(+), 2 deletions(-)"},
                                      kind="review")
        assert not has_diff_evidence({"review_changes_discarded": True,
                                      "diffstat": "1 file changed, 8 insertions(+), 2 deletions(-)"},
                                      kind="implementation")

        # End to end: a timed-out run that only carries a worktree path is
        # not counted as rescuable.
        wt_runs = Path(tmp) / "wt-runs"
        wt_runs.mkdir()
        wt_run = wt_runs / "20260101-000000-aaaaaa"
        (wt_run / "worktree").mkdir(parents=True)
        (wt_run / "result.json").write_text(json.dumps(
            {"lane": "A", "backend": "codex", "title": "implement 9", "seconds": 99,
             "status": "budget_exceeded", "error_class": "budget_exceeded", "diffstat": "",
             "worktree": str(wt_run / "worktree")}))
        wt_rows = report(read_runs([wt_runs], lanes))
        assert len(wt_rows) == 1, wt_rows
        assert wt_rows[0]["budget_exceeded"] == 1 and wt_rows[0]["timeout_with_diff"] == 0, wt_rows

        # A mapping without a host is reported, not silently turned into
        # host=unknown for the whole table.
        assert lanes_missing_host({"A": ("box", "codex"), "B": ("unknown", "pi")}) == ["B"]
    print("self-test: ok")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", action="append", default=[], metavar="DIR",
                        help="runs directory to scan (repeatable: box and the pulled-back remote copies)")
    parser.add_argument("--lanes", metavar="FILE",
                        help="lane -> host/backend mapping: {lane: {host, backend}}, "
                             "[{lane, host, backend}], or a fleet inventory {hosts: {host: {lanes: [...]}}}")
    parser.add_argument("--since", metavar="TS",
                        help="only count the runs finished at or after this ISO-8601 (or epoch) timestamp")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--self-test", action="store_true", help="run the built-in contract check")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.runs:
        parser.error("at least one --runs DIR is required")
    try:
        since = parse_ts(args.since) if args.since else None
        if args.since and since is None:
            parser.error(f"--since must be an ISO-8601 or epoch timestamp, got {args.since!r}")
        lanes = load_lanes(args.lanes)
        rows = report(read_runs(args.runs, lanes, since))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.lanes:
        print("warning: no --lanes mapping given, every run is reported as host=unknown", file=sys.stderr)
    else:
        missing = lanes_missing_host(lanes)
        if missing:
            print("warning: the --lanes mapping has no host for lane(s): "
                  + ", ".join(missing[:10]) + ("..." if len(missing) > 10 else ""), file=sys.stderr)
        unknown = sum(row["runs"] for row in rows if row["host"] == "unknown")
        if unknown:
            print(f"warning: {unknown} run(s) have no host mapping (host=unknown); "
                  "check the --lanes file", file=sys.stderr)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("HOST       BACKEND    KIND           RUNS  TIMEOUT  RATE   P50/P90/MAX(s)  RESCUABLE  FLAG")
        for row in rows:
            times = "/".join("-" if row[key] is None else f"{row[key]:g}"
                             for key in ("seconds_p50", "seconds_p90", "seconds_max"))
            print(f"{row['host']:<10} {row['backend']:<10} {row['kind']:<14} {row['runs']:>4}  "
                  f"{row['budget_exceeded']:>7}  {row['ratio']:>5.0%}  {times:<15} "
                  f"{row['timeout_with_diff']:>9}  {row['flag']}")
        print("note: RESCUABLE counts timed-out runs that still carry a diff; "
              "review cards run read-only, so they never qualify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
