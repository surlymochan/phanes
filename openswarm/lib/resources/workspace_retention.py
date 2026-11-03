#!/usr/bin/env python3
"""Executor workspace retention: expire the run workspaces that are due.

Every executor run leaves a workspace on its host:

* the run directory under ``$OPENSWARM_HOME/runs/<run id>`` (task, logs, result), and
* the git worktree the executor worked in.  By default that worktree is a
  sibling of the base repository (``<repo parent>/.openswarm-wt-<repo>-<run id>``),
  so it does *not* live under the executor home; with ``workspace.sibling_worktrees``
  off it is ``$OPENSWARM_HOME/worktrees/<repo>-<run id>``.

Nothing removed them, so a host filled up (govpn2 reached 91.7% used with 3.26G
free on 2026-11-02), new runs could not start, and dispatch stalled behind the
full disk.  The retention policy expires the workspaces that are due:

* a run that is still in flight is never a candidate;
* a successful run keeps its workspace for ``success_hours`` (24h by default)
  so a reviewer can still inspect what it produced;
* a failed run keeps its workspace for ``failure_hours`` (72h by default),
  because a failure is what gets re-run and debugged;
* nothing is deleted while the disk watermark is below
  ``disk_threshold_pct`` (85% used by default): a host that still has room
  keeps the material, only a host that needs the space gives it up;
* only paths under an allowed workspace root are ever touched, and a worktree
  outside the executor home must carry the worktree name of its run.

The policy is deliberately conservative.  A run whose state is still in flight
is kept even when its runner process is gone: the executor can leave children
behind that still write into the workspace.  Only a policy that explicitly
sets ``executor.retention.reclaim_abandoned`` may reclaim such a run, and only
when no process of the run is alive any more.

Every deletion is written to an audit log (JSON lines) with the run id, the
age of the workspace and the number of bytes it released.  The released bytes
are the blocks the filesystem really gives back: the blocks of a file that is
hard linked outside the workspace, or that a process still holds open, stay
allocated until that link or descriptor is gone, so they are not counted.

The deletion re-reads the process table for every target it removes, right
before it removes it: a process that entered the workspace after the scan is
seen, and the workspace is kept.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
import time


# The retention windows and the disk watermarks.  The hours are how long a
# workspace of that tier is kept after the run ended; the percentages are the
# used share of the filesystem that holds the executor home.
DEFAULT_RETENTION = {
    "success_hours": 24.0,
    "failure_hours": 72.0,
    "disk_threshold_pct": 85.0,
    "disk_alert_pct": 85.0,
    # An in-flight run is never reclaimed unless the policy says so.
    "reclaim_abandoned": False,
}

# The run states that mean the run is still going.
ACTIVE_STATES = frozenset({"queued", "routing", "running", "finalizing", "verifying"})
TERMINAL_STATES = frozenset({"finished", "unroutable", "dry-run", "budget_rejected"})
SUCCESS_STATUSES = frozenset({"done", "ok", "success", "passed"})

# The retention tiers.
TIER_SUCCESS = "success"
TIER_FAILURE = "failure"
TIER_RUNNING = "running"

# The audit log of the deletions, next to the run directories.
AUDIT_NAME = "retention-audit.jsonl"

# The name of a worktree that lives next to the base repository.
WORKTREE_PREFIX = ".openswarm-wt-"

# The unit of ``st_blocks``.
BLOCK_SIZE = 512


# ---------------------------------------------------------------- policy

def retention_config(policy=None):
    """The retention policy: ``executor.retention`` over the conservative defaults.

    A value that is not a number in range is an error, never a silently wider
    cleanup: a typo in policy.local.json must not expire the workspaces of
    runs that are still worth keeping.
    """
    config = dict(DEFAULT_RETENTION)
    section = ((policy or {}).get("executor") or {}).get("retention")
    if section is None:
        return config
    if not isinstance(section, dict):
        raise ValueError("executor.retention must be an object")
    for key in ("success_hours", "failure_hours", "disk_threshold_pct", "disk_alert_pct"):
        if key not in section or section[key] is None:
            continue
        value = section[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"executor.retention.{key} must be a finite number")
        if value < 0:
            raise ValueError(f"executor.retention.{key} must not be negative")
        if key.endswith("_pct") and value > 100:
            raise ValueError(f"executor.retention.{key} must be a percentage from 0 to 100")
        config[key] = float(value)
    reclaim = section.get("reclaim_abandoned")
    if reclaim is not None:
        if not isinstance(reclaim, bool):
            raise ValueError("executor.retention.reclaim_abandoned must be a boolean")
        config["reclaim_abandoned"] = reclaim
    return config


def tier_hours(tier, config=None):
    """How long a workspace of this tier is kept.  ``None`` means never."""
    config = config or DEFAULT_RETENTION
    if tier == TIER_SUCCESS:
        return config["success_hours"]
    if tier == TIER_FAILURE:
        return config["failure_hours"]
    return None


# ---------------------------------------------------------------- processes

def pid_alive(pid):
    """Whether a process id is alive.  A process we may not signal is alive."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_ppid(pid, proc_root="/proc"):
    """The parent pid of a process, or None when it cannot be read."""
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), encoding="utf-8", errors="replace") as stream:
            data = stream.read()
    except OSError:
        return None
    # The comm field may contain spaces and parentheses: everything after the
    # last ")" is "state ppid ...".
    tail = data.rpartition(")")[2].split()
    if len(tail) < 2 or not tail[1].isdigit():
        return None
    return int(tail[1])


def child_pids(pid, proc_root="/proc"):
    """The pids whose parent is ``pid``."""
    try:
        parent = int(pid)
    except (TypeError, ValueError):
        return []
    if parent <= 0:
        return []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    children = []
    for name in names:
        if not name.isdigit():
            continue
        if _proc_ppid(name, proc_root) == parent:
            children.append(int(name))
    return children


def descendant_pids(pid, proc_root="/proc"):
    """The pid and every process below it in the process tree."""
    try:
        root = int(pid)
    except (TypeError, ValueError):
        return []
    if root <= 0:
        return []
    found = [root]
    queue = [root]
    while queue:
        current = queue.pop()
        for child in child_pids(current, proc_root):
            if child in found:
                continue
            found.append(child)
            queue.append(child)
    return found


def workspace_holders(path, proc_root="/proc"):
    """The pids whose working directory is inside ``path``.

    ``None`` means the process table could not be read: the caller must then
    assume that somebody is working in the workspace.
    """
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    target = os.path.realpath(path)
    holders = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            cwd = os.readlink(os.path.join(proc_root, name, "cwd"))
        except OSError:
            continue
        real = os.path.realpath(cwd)
        if real == target or real.startswith(target + os.sep):
            holders.append(int(name))
    return holders


class HolderProbe:
    """The processes that work inside a path.

    The scan of many workspaces reads the process table once and then answers
    from that snapshot, so it does not walk ``/proc`` for every workspace.
    The snapshot is dropped before a deletion: a process that entered the
    workspace after the scan must still stop the cleanup, so the check right
    before the deletion reads the process table again.
    """

    def __init__(self, proc_root="/proc"):
        self.proc_root = proc_root
        self._table = None

    def refresh(self):
        """Forget the snapshot: the next lookup reads the process table again."""
        self._table = None

    def _read(self):
        table = {}
        try:
            names = os.listdir(self.proc_root)
        except OSError:
            return None
        for name in names:
            if not name.isdigit():
                continue
            try:
                cwd = os.readlink(os.path.join(self.proc_root, name, "cwd"))
            except OSError:
                continue
            table.setdefault(os.path.realpath(cwd), []).append(int(name))
        return table

    def __call__(self, path):
        """The pids whose working directory is inside ``path``.

        ``None`` means the process table could not be read: the caller must
        then assume that somebody is working in the workspace.
        """
        if self._table is None:
            self._table = self._read()
        if self._table is None:
            return None
        target = os.path.realpath(path)
        found = []
        for cwd, pids in self._table.items():
            if cwd == target or cwd.startswith(target + os.sep):
                found.extend(pids)
        return found


def holder_probe(proc_root="/proc"):
    """A probe of the processes that work inside a path."""
    return HolderProbe(proc_root)


# ---------------------------------------------------------------- run records

def _jload(path):
    """The JSON of a file, or an empty object when it is missing or broken."""
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value):
    """A finite number, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mtime(path):
    """The modification time of a path, or None."""
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


def _last_activity(run_dir):
    """The newest modification time in a run directory.

    The age of an abandoned run is measured from its last activity, not from
    the moment it started: a run that worked for six hours and died is not
    six hours old when it is finally noticed.
    """
    newest = _mtime(run_dir) or 0.0
    try:
        names = os.listdir(run_dir)
    except OSError:
        return newest
    for name in names:
        stamp = _mtime(os.path.join(run_dir, name))
        if stamp and stamp > newest:
            newest = stamp
    return newest


def _run_paths(status, result):
    """The base repository and the worktree a run recorded, if any."""
    workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
    repo = workspace.get("repo") if isinstance(workspace.get("repo"), str) else None
    worktree = result.get("worktree") if isinstance(result.get("worktree"), str) else None
    if not worktree and isinstance(status.get("worktree"), str):
        worktree = status["worktree"]
    if not repo and isinstance(status.get("cwd"), str) and status["cwd"]:
        repo = status["cwd"]
    return repo, worktree


def classify_run(record, config=None):
    """The retention tier of one run: ``success``, ``failure`` or ``running``.

    A run that reached a terminal state is tiered by its status.  A run that
    is still in flight is ``running``: such a run is never a cleanup candidate,
    not even when its runner process is gone, because the executor can leave
    children behind that still write into the workspace.  Only a policy that
    sets ``reclaim_abandoned`` may treat an in-flight run as an abandoned
    failure, and only when no process of the run is alive any more.
    """
    config = config or DEFAULT_RETENTION
    state = str(record.get("state") or "")
    status = str(record.get("status") or "").lower()
    if state in ACTIVE_STATES:
        # In flight: never a candidate, not even when the runner is gone.
        if config.get("reclaim_abandoned") and not record.get("alive") and not record.get("busy"):
            return TIER_FAILURE
        return TIER_RUNNING
    if state in TERMINAL_STATES or record.get("finished"):
        return TIER_SUCCESS if status in SUCCESS_STATUSES else TIER_FAILURE
    # No in-flight state: the run never wrote a status, or it was abandoned.
    if config.get("reclaim_abandoned") and not record.get("alive") and not record.get("busy"):
        return TIER_FAILURE
    return TIER_RUNNING


def holders_in(path, holders=None):
    """The pids that work inside a path, or None when that cannot be read."""
    return holders(path) if holders is not None else workspace_holders(path)


def read_run(run_dir, alive=None, holders=None, config=None):
    """Read one run directory into a retention record.

    ``alive`` is the optional ``pid -> bool`` probe of a process and
    ``holders`` the optional ``path -> [pid]`` probe of the processes that
    work inside the run directory.
    """
    status = _jload(os.path.join(run_dir, "status.json"))
    result = _jload(os.path.join(run_dir, "result.json"))
    probe = alive or pid_alive
    runner_pid = status.get("runner_pid")
    alive_pids = []
    if runner_pid:
        for pid in [runner_pid] + descendant_pids(runner_pid):
            if probe(pid):
                alive_pids.append(pid)
    holder_pids = holders_in(run_dir, holders)
    if holder_pids is None:
        # The process table could not be read: assume somebody is working here.
        holder_pids = []
        unknown_holders = True
    else:
        unknown_holders = False
    holder_pids = [pid for pid in holder_pids if probe(pid)]
    repo, worktree = _run_paths(status, result)
    finished = _number(status.get("finished"))
    record = {
        "id": str(status.get("id") or os.path.basename(os.path.abspath(run_dir))),
        "path": os.path.abspath(run_dir),
        "state": str(status.get("state") or ""),
        "status": str(result.get("status") or status.get("status") or "").lower(),
        "finished": finished,
        "started": _number(status.get("started")),
        "runner_pid": runner_pid,
        "alive_pids": alive_pids,
        "holder_pids": holder_pids,
        "unknown_holders": unknown_holders,
        "alive": bool(alive_pids or holder_pids),
        "busy": bool(alive_pids or holder_pids or unknown_holders),
        "repo": os.path.abspath(repo) if repo else None,
        "worktree": os.path.abspath(worktree) if worktree else None,
        "size_bytes": 0,
    }
    record["tier"] = classify_run(record, config)
    # The age of a workspace is measured from the moment the run ended; a run
    # that never ended is measured from its last activity.
    record["reference"] = finished if finished is not None else _last_activity(run_dir)
    return record


# ---------------------------------------------------------------- workspaces

def sibling_worktree(repo, run_id):
    """The worktree a run gets next to its base repository."""
    repo = os.path.abspath(repo)
    return os.path.join(os.path.dirname(repo), f"{WORKTREE_PREFIX}{os.path.basename(repo)}-{run_id}")


def managed_worktree(home_root, repo, run_id):
    """The worktree a run gets under the executor home."""
    repo = os.path.abspath(repo)
    return os.path.join(os.path.abspath(home_root), "worktrees", f"{os.path.basename(repo)}-{run_id}")


def workspace_targets(record, home_root=None):
    """The paths that belong to one run: its run directory and its worktree.

    The worktree is the path the run recorded and the two places
    ``prepare_workspace`` puts it: next to the base repository and under the
    executor home.  A path that does not exist is kept in the list (the
    planner drops it) so a stale record still names the workspace it had.
    """
    targets = [{"kind": "run_dir", "path": record["path"]}]
    run_id = str(record.get("id") or "")
    repo = record.get("repo")
    candidates = []
    if record.get("worktree"):
        candidates.append(record["worktree"])
    if repo:
        candidates.append(sibling_worktree(repo, run_id))
        if home_root:
            candidates.append(managed_worktree(home_root, repo, run_id))
    seen = {record["path"]}
    for path in candidates:
        if not path:
            continue
        path = os.path.abspath(path)
        if path in seen:
            continue
        seen.add(path)
        targets.append({"kind": "worktree", "path": path})
    return targets


def scan_runs(runs_root, alive=None, holders=None, config=None, home_root=None):
    """The retention records of every run directory under ``runs_root``."""
    records = []
    holders = holders or holder_probe()
    try:
        names = sorted(os.listdir(runs_root))
    except OSError:
        return records
    for name in names:
        path = os.path.join(runs_root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        record = read_run(path, alive=alive, holders=holders, config=config)
        record["targets"] = workspace_targets(record, home_root)
        record["size_bytes"] = sum(allocated_bytes(target["path"]) for target in record["targets"])
        # A process that works in the worktree of the run holds the workspace
        # as much as one that works in the run directory does.
        probe = alive or pid_alive
        for target in record["targets"]:
            found = holders_in(target["path"], holders)
            if found is None:
                record["unknown_holders"] = True
                record["busy"] = True
                break
            for pid in found:
                if probe(pid) and pid not in record["holder_pids"]:
                    record["holder_pids"].append(pid)
                    record["alive"] = True
                    record["busy"] = True
        records.append(record)
    return records


# ---------------------------------------------------------------- roots

def _inside(path, root):
    """Whether ``path`` lies inside ``root``, lexically and after resolving links."""
    try:
        if os.path.commonpath([path, root]) != root:
            return False
    except ValueError:
        return False
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def workspace_roots(home_root, records=()):
    """The roots the cleanup may touch: the executor home and the worktree parents.

    The executor home holds the run directories and the managed worktrees.  A
    worktree that lives next to the base repository is outside the home, so
    the parent directory of every repository a run used is allowed as well --
    but only for the worktrees of that run.
    """
    roots = {"home": os.path.abspath(home_root), "siblings": []}
    for record in records or ():
        repo = record.get("repo")
        if not repo:
            continue
        parent = os.path.dirname(os.path.abspath(repo))
        if parent and parent != roots["home"] and parent not in roots["siblings"]:
            roots["siblings"].append(parent)
    return roots


def allows_deletion(path, roots, run_id, kind=None):
    """Whether the cleanup may delete this path.

    Inside the executor home only the workspaces of the run may go: the run
    directory ``<home>/runs/<run id>`` and the managed worktrees below
    ``<home>/worktrees`` whose name carries the run id.  Outside the home,
    only a worktree that belongs to the run: its name carries the run id
    and it sits in the parent directory of the repository the run used.

    ``kind`` is the kind of the target (``run_dir`` or ``worktree``).  When
    it is unknown the path is only checked against the roots, which is the
    old, wider rule.
    """
    path = os.path.abspath(str(path or ""))
    home = os.path.abspath(str((roots or {}).get("home") or ""))
    if not path or not home or path == home:
        return False
    if _inside(path, home):
        if kind == "run_dir":
            return os.path.dirname(path) == os.path.join(home, "runs")
        if kind == "worktree":
            worktrees = os.path.join(home, "worktrees")
            if os.path.dirname(path) != worktrees:
                return False
            return bool(run_id) and os.path.basename(path).endswith("-" + str(run_id))
        return True
    name = os.path.basename(path)
    if not run_id or not name.endswith("-" + str(run_id)):
        return False
    if not name.startswith(WORKTREE_PREFIX):
        return False
    for sibling in (roots or {}).get("siblings") or []:
        sibling = os.path.abspath(sibling)
        if os.path.dirname(path) == sibling and _inside(path, sibling):
            return True
    return False


# ---------------------------------------------------------------- planning

def plan_retention(records, now, config=None, disk_used_pct=None):
    """The workspaces that may be deleted now, and the ones that are kept.

    ``disk_used_pct`` is the measured watermark of the filesystem that holds
    the executor home.  Below ``disk_threshold_pct`` nothing is planned: the
    host has room and the material is still worth keeping.  An unknown
    watermark plans nothing either, because a cleanup that cannot see the
    disk must not delete anything.
    """
    config = config or DEFAULT_RETENTION
    now = float(now)
    disk = None if disk_used_pct is None else float(disk_used_pct)
    threshold = float(config["disk_threshold_pct"])
    alert_pct = float(config["disk_alert_pct"])
    candidates, retained, expired = [], [], []
    for record in records:
        tier = record.get("tier") or classify_run(record, config)
        hours = tier_hours(tier, config)
        age = max(0.0, now - float(record.get("reference") or now))
        row = {"run_id": record.get("id"), "path": record.get("path"), "run_dir": record.get("path"),
               "tier": tier, "status": record.get("status"), "state": record.get("state"),
               "age_seconds": age, "size_bytes": int(record.get("size_bytes") or 0),
               "targets": record.get("targets") or []}
        if hours is None:
            retained.append({**row, "reason": "run in flight"})
            continue
        if record.get("busy"):
            retained.append({**row, "reason": "workspace in use"})
            continue
        if age <= hours * 3600:
            retained.append({**row, "reason": f"kept for {hours:g}h"})
            continue
        expired.append(row)
    plan = {"candidates": [], "retained": retained, "expired": expired,
            "disk_used_pct": disk, "threshold_pct": threshold, "alert_pct": alert_pct,
            "priority": "normal", "reason": ""}
    if disk is None:
        plan["reason"] = "disk watermark unknown"
        return plan
    if disk < threshold:
        plan["reason"] = f"disk watermark {disk:.1f}% below threshold {threshold:g}%"
        return plan
    # The host needs the space: the expired workspaces are candidates, the
    # oldest first.  Above the alert watermark the cleanup runs with the
    # highest priority and frees the biggest workspaces first.
    plan["priority"] = "high" if disk >= alert_pct else "normal"
    candidates = list(expired)
    if plan["priority"] == "high":
        candidates.sort(key=lambda row: (-row["size_bytes"], -row["age_seconds"]))
    else:
        candidates.sort(key=lambda row: (-row["age_seconds"], row["run_id"] or ""))
    plan["candidates"] = candidates
    plan["reason"] = f"disk watermark {disk:.1f}% at or above threshold {threshold:g}%"
    return plan


# ---------------------------------------------------------------- deleting

def allocated_bytes(path):
    """The disk blocks a path occupies.

    The bytes are the ones the filesystem really allocated (``st_blocks``),
    not the apparent size of the files: a sparse file, a hard link or a
    directory entry all make the apparent size differ from the space that is
    freed when the path goes away.  A hard link is counted once, because the
    blocks stay as long as another link exists.
    """
    if not os.path.lexists(path):
        return 0
    try:
        root = os.lstat(path)
    except OSError:
        return 0
    if os.path.islink(path) or not os.path.isdir(path):
        return root.st_blocks * BLOCK_SIZE
    total = root.st_blocks * BLOCK_SIZE
    seen = {(root.st_dev, root.st_ino)}
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += info.st_blocks * BLOCK_SIZE
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
    return total


def directory_size(path):
    """The apparent size of a directory tree.  Symlinks count as themselves."""
    total = 0
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in list(dirs) + list(files):
            try:
                total += os.lstat(os.path.join(parent, name)).st_size
            except OSError:
                continue
    return total


def tree_inodes(path):
    """The inodes of a tree: the blocks they hold and the links to them.

    Every inode is counted once, with the number of links that are inside the
    tree and the link count the filesystem reports.
    """
    inodes = {}
    if not os.path.lexists(path):
        return inodes
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = os.lstat(current)
        except OSError:
            continue
        key = (info.st_dev, info.st_ino)
        row = inodes.get(key)
        if row is None:
            row = {"blocks": info.st_blocks * BLOCK_SIZE, "nlink": info.st_nlink,
                   "links": 0, "dir": os.path.isdir(current) and not os.path.islink(current)}
            inodes[key] = row
        row["links"] += 1
        if not row["dir"]:
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            stack.append(entry.path)
    return inodes


def released_bytes(path, open_inodes=None, inodes=None):
    """The bytes the filesystem really gives back when ``path`` is removed.

    The blocks of a file are only released when the last link to it goes
    away: a file that is hard linked outside the workspace keeps its blocks,
    and a file that a process still holds open keeps them until the process
    closes it.  The blocks of the directories themselves are freed with the
    tree, so they count.

    ``open_inodes`` is the table of the inodes that a process holds open (see
    ``open_file_inodes``); the blocks of those inodes are not released yet.
    """
    open_inodes = open_inodes or {}
    inodes = tree_inodes(path) if inodes is None else inodes
    total = 0
    for key, row in inodes.items():
        if row["dir"]:
            # The directory goes away with the tree: its blocks are freed.
            total += row["blocks"]
            continue
        # Every link to the inode must be inside the tree, otherwise the
        # blocks stay allocated for the other link.
        if row["nlink"] > row["links"]:
            continue
        if open_inodes.get(key):
            continue
        total += row["blocks"]
    return total


def open_file_inodes(proc_root="/proc", wanted=None):
    """The inodes that a process holds open, with the pids that hold them.

    A file that a process still has open keeps its blocks after the name is
    unlinked, so the cleanup must not claim those bytes as released.

    ``wanted`` is the set of ``(device, inode)`` keys the caller cares about:
    the table then only holds the files of that set, so the sweep can ask
    for the open files of one workspace without keeping the whole table of
    the host.  The table is read again for every target that is deleted: a
    file that a process opened after the scan must not be counted as freed.
    """
    table = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        fd_dir = os.path.join(proc_root, name, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            try:
                info = os.stat(link)
            except OSError:
                continue
            key = (info.st_dev, info.st_ino)
            if wanted is not None and key not in wanted:
                continue
            table.setdefault(key, set()).add(int(name))
    return table


def fs_free_bytes(path, statvfs=os.statvfs):
    """The free bytes of the filesystem that holds ``path``."""
    sample = statvfs(path)
    return sample.f_bavail * sample.f_frsize


def _git(argv, timeout=60):
    """Run a git command; the output is never needed, only the exit code."""
    try:
        return subprocess.run(["git", *argv], capture_output=True, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def worktree_gitdir(path):
    """The git directory a worktree points at, read from its ``.git`` file.

    The pointer is only a hint: it may name any absolute path, so the caller
    must confirm that it really is the worktree metadata of a repository
    before it removes anything.
    """
    pointer = os.path.join(path, ".git")
    try:
        with open(pointer, encoding="utf-8") as stream:
            data = stream.read().strip()
    except OSError:
        return None
    if not data.startswith("gitdir:"):
        return None
    gitdir = data.split(":", 1)[1].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.abspath(os.path.join(path, gitdir))
    return gitdir


def repo_of_gitdir(gitdir):
    """The repository a worktree metadata directory belongs to.

    The metadata of a worktree lives in ``<repo>/.git/worktrees/<name>``, so
    the repository is three levels up.  ``None`` when there is no such
    directory.
    """
    if not gitdir:
        return None
    repo = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
    return repo if os.path.isdir(repo) else None


def worktree_repo(path):
    """The base repository of a worktree, or None when it cannot be found."""
    return repo_of_gitdir(worktree_gitdir(path))


def owned_worktree_gitdir(path):
    """The git metadata directory of a worktree, when it really belongs to it.

    A ``.git`` file may point anywhere, so the metadata is only trusted when
    it sits in the ``worktrees`` directory of a repository and names this
    worktree back: that is the shape git itself writes
    (``<repo>/.git/worktrees/<name>/gitdir`` holds the worktree's ``.git``
    file).  Anything else is never removed.
    """
    gitdir = worktree_gitdir(path)
    if not gitdir:
        return None
    gitdir = os.path.abspath(gitdir)
    worktrees = os.path.dirname(gitdir)
    if os.path.basename(worktrees) != "worktrees":
        return None
    repo_git = os.path.dirname(worktrees)
    if os.path.basename(repo_git) != ".git":
        return None
    repo = os.path.dirname(repo_git)
    if not os.path.isdir(repo):
        return None
    back = os.path.join(gitdir, "gitdir")
    try:
        with open(back, encoding="utf-8") as stream:
            target = stream.read().strip()
    except OSError:
        return None
    if os.path.abspath(target) != os.path.abspath(os.path.join(path, ".git")):
        return None
    return gitdir


def remove_worktree(path):
    """Remove a worktree directory and the git metadata that points at it.

    The worktree is removed with git when its repository can be found, so the
    repository does not keep a dangling worktree entry; otherwise the
    directory is removed directly.  The metadata directory is only removed
    when it is confirmed to be the worktree metadata of that repository: a
    ``.git`` file may point anywhere, and following it would delete a
    directory that has nothing to do with the workspace.  The repository
    itself is only taken from a confirmed metadata directory, so a pointer to
    a foreign path never makes the cleanup act on another repository.
    """
    gitdir = owned_worktree_gitdir(path)
    # The repository is only taken from a metadata directory that was
    # confirmed to belong to this worktree: a ``.git`` file that points
    # anywhere must never make the cleanup act on another repository.
    repo = repo_of_gitdir(gitdir)
    if repo and _git(["-C", repo, "worktree", "remove", "--force", path]) == 0:
        _git(["-C", repo, "worktree", "prune"])
        return not os.path.lexists(path)
    shutil.rmtree(path, ignore_errors=True)
    if gitdir:
        shutil.rmtree(gitdir, ignore_errors=True)
        if repo:
            _git(["-C", repo, "worktree", "prune"])
    return not os.path.lexists(path)


def remove_path(path, kind=None):
    """Remove a file, a symlink or a directory tree."""
    if kind == "worktree" and os.path.isdir(path) and not os.path.islink(path):
        remove_worktree(path)
        return
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def reverify(candidate, target, alive=None, holders=None):
    """Re-check the run and its processes right before a deletion.

    The plan was made a moment ago; the run may have started again, or a
    process may still be working in the workspace.  A deletion that cannot be
    confirmed is not performed.  An empty string means the deletion is safe.

    ``holders`` is a probe that the caller refreshes, so the check sees the
    processes that appeared after the scan: both the run directory and the
    target are looked at.
    """
    run_dir = candidate.get("run_dir") or candidate.get("path")
    status = _jload(os.path.join(str(run_dir or ""), "status.json"))
    state = str(status.get("state") or "")
    finished = _number(status.get("finished"))
    if state in ACTIVE_STATES and finished is None:
        return "run is in flight again"
    probe = alive or pid_alive
    runner_pid = status.get("runner_pid")
    if runner_pid:
        for pid in [runner_pid] + descendant_pids(runner_pid):
            if probe(pid):
                return f"process {pid} of the run is alive"
    if holders is not None:
        for path in (target.get("path"), run_dir):
            if not path:
                continue
            held = holders(path)
            if held is None:
                return "the process table could not be read"
            for pid in held:
                if probe(pid):
                    return f"process {pid} holds the workspace"
    return ""


def append_audit(audit_path, entry):
    """Append one audit line.  The log is JSON lines."""
    parent = os.path.dirname(audit_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def audit_entries(audit_path):
    """The audit lines of a retention log, skipping the unreadable ones."""
    entries = []
    try:
        with open(audit_path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return entries


def expire_workspaces(candidates, roots, audit_path=None, now=None, alive=None, holders=None):
    """Delete the planned workspaces and write the audit log.

    A candidate outside the allowed roots is refused: the cleanup never
    leaves the executor workspace roots.  The state of the run and the
    processes that work in it are re-checked before every deletion, so a run
    that became active again is kept.  The re-check reads the process table
    again: the scan and the deletion do not share one snapshot, so a process
    that entered the workspace after the scan is seen.  The files that a
    process holds open are read again for the same reason, so the audit does
    not claim bytes that are still allocated.

    The audit line of a deletion carries the run id, the age of the workspace
    and the bytes the filesystem really gave back.
    """
    now = time.time() if now is None else float(now)
    if isinstance(roots, str):
        roots = {"home": os.path.abspath(roots), "siblings": []}
    roots = roots or {"home": "", "siblings": []}
    audit_path = audit_path or os.path.join(str(roots.get("home") or ""), AUDIT_NAME)
    if holders is None:
        # Without a probe the check of the processes that work in the
        # workspace would be skipped: the deletion would then not know about
        # the processes that appeared after the scan.
        holders = holder_probe()
    deleted, refused, skipped = [], [], []
    freed = 0
    measured = 0
    for candidate in candidates or []:
        run_id = candidate.get("run_id")
        targets = list(candidate.get("targets") or [])
        # The run directory is removed last: it holds the record the
        # re-verification of the worktree needs.
        targets.sort(key=lambda target: 1 if target.get("kind") == "run_dir" else 0)
        for target in targets:
            path = os.path.abspath(str(target.get("path") or ""))
            kind = target.get("kind") or "run_dir"
            if not path or not allows_deletion(path, roots, run_id, kind=kind):
                refused.append({"run_id": run_id, "path": path, "kind": kind,
                                "reason": "outside the workspace roots"})
                continue
            if not os.path.lexists(path):
                continue
            # Fresh process information for this target: the snapshot of the
            # scan may be stale, so the holder probe reads /proc again.
            refresh = getattr(holders, "refresh", None)
            if callable(refresh):
                refresh()
            reason = reverify(candidate, target, alive=alive, holders=holders)
            if reason:
                skipped.append({"run_id": run_id, "path": path, "kind": kind, "reason": reason})
                continue
            before = fs_free_bytes(os.path.dirname(path) or path)
            # The bytes the filesystem really gives back: the blocks of an
            # externally hard linked or still open file stay allocated.  The
            # open files are read again for this target, so a file that a
            # process opened after the scan is not counted as freed.
            inodes = tree_inodes(path)
            open_inodes = open_file_inodes(wanted=set(inodes))
            size = released_bytes(path, open_inodes, inodes=inodes)
            try:
                remove_path(path, kind)
            except OSError as error:
                refused.append({"run_id": run_id, "path": path, "kind": kind,
                                "reason": f"not removed: {error}"})
                continue
            after = fs_free_bytes(os.path.dirname(path) or path)
            entry = {"ts": now, "run_id": run_id, "path": path, "kind": kind,
                     "tier": candidate.get("tier"), "status": candidate.get("status"),
                     "age_seconds": candidate.get("age_seconds"), "freed_bytes": size,
                     "measured_freed_bytes": max(0, after - before)}
            append_audit(audit_path, entry)
            deleted.append(entry)
            freed += size
            measured += entry["measured_freed_bytes"]
    return {"deleted": deleted, "refused": refused, "skipped": skipped,
            "freed_bytes": freed, "measured_freed_bytes": measured, "audit_path": audit_path}


# ---------------------------------------------------------------- health

def probe_module():
    """The disk probe (lib/resources/probe.py), loaded by path."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
    spec = importlib.util.spec_from_file_location("openswarm_resources_probe", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the disk probe at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def disk_watermark(path, usage=None):
    """The used percentage of the filesystem that holds ``path``."""
    if usage is None:
        usage = probe_module().fs_usage
    value = usage(path).get("disk_used_pct")
    number = _number(value)
    return number


def health_report(records, config=None, disk_used_pct=None, now=None):
    """The retention health of the executor host.

    The report carries the disk watermark, the alert, the cleanup priority
    and how many workspaces are expired (and how many bytes they hold).
    """
    config = config or DEFAULT_RETENTION
    now = time.time() if now is None else float(now)
    disk = None if disk_used_pct is None else float(disk_used_pct)
    counts = {TIER_SUCCESS: 0, TIER_FAILURE: 0, TIER_RUNNING: 0}
    expired = {TIER_SUCCESS: 0, TIER_FAILURE: 0}
    expired_bytes = 0
    for record in records:
        tier = record.get("tier") or classify_run(record, config)
        counts[tier] = counts.get(tier, 0) + 1
        hours = tier_hours(tier, config)
        if hours is None:
            continue
        age = max(0.0, now - float(record.get("reference") or now))
        if age > hours * 3600:
            expired[tier] = expired.get(tier, 0) + 1
            expired_bytes += int(record.get("size_bytes") or 0)
    threshold = float(config["disk_threshold_pct"])
    alert_pct = float(config["disk_alert_pct"])
    alert = disk is not None and disk >= alert_pct
    report = {
        "disk_used_pct": disk,
        "threshold_pct": threshold,
        "alert_pct": alert_pct,
        "alert": alert,
        "priority": "high" if alert else "normal",
        "counts": counts,
        "expired": expired,
        "expired_bytes": expired_bytes,
        "success_hours": config["success_hours"],
        "failure_hours": config["failure_hours"],
    }
    report["message"] = health_message(report)
    return report


def health_message(report):
    """One line that says what the host should do about its workspaces."""
    disk = report.get("disk_used_pct")
    used = "disk watermark unknown" if disk is None else f"disk {disk:.1f}% used"
    expired = report.get("expired") or {}
    total = sum(expired.values())
    if report.get("alert"):
        return (f"{used} >= {report['alert_pct']:g}%: cleanup priority high, "
                f"{total} expired workspace(s) hold {report['expired_bytes']} bytes")
    if disk is not None and disk >= report["threshold_pct"]:
        return (f"{used} >= {report['threshold_pct']:g}%: {total} expired workspace(s) "
                f"hold {report['expired_bytes']} bytes")
    return (f"{used} below {report['threshold_pct']:g}%: workspaces are kept, "
            f"{total} expired workspace(s) wait for the threshold")
