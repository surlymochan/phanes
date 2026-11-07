#!/usr/bin/env python3
"""Check that every Codex lane on this host can write its own git metadata inside the sandbox.

A Codex ``workspace-write`` sandbox mounts a worktree's git metadata read-only, so a lane can only
commit when the sandbox's writable roots contain that lane's *exact* git admin dir
(``.git/worktrees/<name>``). Two lanes whose worktree directories share a basename get different
admin dirs (``wt`` and ``wt1``), so a root pinned to one name covers one lane only: the other lane
fails every git write with ``Read-only file system`` (Refs #1832).

This tool reads the host state only (lanes.json, the lane worktrees, ``~/.codex/config.toml`` and
the deployed openswarm release) and reports, per Codex lane, whether its admin dir is writable in
the sandbox. It never writes to the host.

Usage:
    tools/check-codex-sandbox-gitdir.py [--home DIR] [--lanes FILE] [--codex-home DIR]
                                       [--openswarm-bin FILE] [--json]
"""
import argparse
import json
import os
import re
import subprocess
import sys

DEFAULT_HOME = os.environ.get("OPENSWARM_HOME") or os.path.expanduser("~/.claude/openswarm-executor-candidate")


def run(args, cwd=None):
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git(cwd, *args):
    return run(["git", *args], cwd=cwd)


def config_writable_roots(codex_home):
    """``sandbox_workspace_write.writable_roots`` from ``~/.codex/config.toml``."""
    path = os.path.join(codex_home, "config.toml")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    table = re.search(r"^\[sandbox_workspace_write\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    body = table.group(1) if table else text
    array = re.search(r"^\s*writable_roots\s*=\s*\[(.*?)\]", body, re.S | re.M)
    if not array:
        return []
    return [double or single for double, single in
            re.findall(r'"([^"]*)"|\'([^\']*)\'', array.group(1))]


def lane_worktrees(home, name):
    """Candidate worktree directories of a lane: the fixed ``lanes/<name>/wt`` and any sibling
    directory that carries a worktree ``.git`` pointer."""
    found = []
    lane_dir = os.path.join(home, "lanes", name)
    fixed = os.path.join(lane_dir, "wt")
    if os.path.isdir(fixed):
        found.append(fixed)
    if os.path.isdir(lane_dir):
        for entry in sorted(os.listdir(lane_dir)):
            path = os.path.join(lane_dir, entry)
            if path in found or not os.path.isdir(path):
                continue
            if os.path.isfile(os.path.join(path, ".git")):
                found.append(path)
    state = os.path.join(lane_dir, "state.json")
    try:
        with open(state, encoding="utf-8") as handle:
            workspace = (json.load(handle) or {}).get("workspace") or {}
        path = workspace.get("path") or workspace.get("root")
        if path and os.path.isdir(path) and path not in found:
            found.append(path)
    except (OSError, ValueError):
        pass
    return found


def detect_openswarm_bin(home, lane_names=()):
    """The deployed openswarm binary of this host (systemd unit, release dir or PATH)."""
    units = ["openswarm-executor-{}.service".format(name) for name in lane_names]
    _, listed, _ = run(["systemctl", "list-units", "--type=service", "--all", "openswarm*", "--no-legend"])
    units += [line.split()[0] for line in (listed or "").splitlines() if line.strip()]
    for unit in units:
        _, text, _ = run(["systemctl", "cat", unit])
        match = re.search(r"ExecStart=(\S*openswarm(?:-fleet)?)\b", text or "")
        if match and os.path.isfile(match.group(1)):
            return match.group(1)
    roots = ["/root/tools/openswarm-executor-candidate/releases",
             os.path.expanduser("~/tools/openswarm-executor-candidate/releases"),
             os.path.join(os.path.dirname(home.rstrip("/")), "tools", "openswarm-executor-candidate", "releases")]
    for releases in roots:
        if not os.path.isdir(releases):
            continue
        candidates = sorted(os.path.join(releases, entry, "openswarm", "bin", "openswarm")
                            for entry in os.listdir(releases))
        for candidate in reversed(candidates):
            if os.path.isfile(candidate):
                return candidate
    _, path, _ = run(["sh", "-c", "command -v openswarm"])
    return path or None


def runtime_computes_roots(binary):
    """Whether the deployed openswarm computes the sandbox roots per thread (Refs #1832)."""
    if not binary or not os.path.isfile(binary):
        return None
    try:
        with open(binary, encoding="utf-8", errors="replace") as handle:
            return "codex_gitdir_writable_roots" in handle.read()
    except OSError:
        return None


def lane_rows(lanes, home, codex_home, binary):
    pinned = config_writable_roots(codex_home)
    rows = []
    for lane in lanes:
        if (lane.get("backend") or "").lower() != "codex":
            continue
        name = lane.get("name") or "?"
        row = {"lane": name, "worktree": None, "gitdir": None, "admin_name": None,
               "pinned_root": False, "runtime_roots": None, "verdict": "unknown", "detail": ""}
        for worktree in lane_worktrees(home, name):
            rc, gitdir, _ = git(worktree, "rev-parse", "--absolute-git-dir")
            if rc or not gitdir:
                continue
            row["worktree"] = worktree
            row["gitdir"] = gitdir
            row["admin_name"] = os.path.basename(gitdir)
            row["pinned_root"] = gitdir in pinned
            break
        if not row["gitdir"]:
            row["verdict"] = "missing-worktree"
            row["detail"] = "no lane worktree with a git admin dir was found"
            rows.append(row)
            continue
        if row["pinned_root"]:
            row["verdict"] = "writable"
            row["detail"] = "git admin dir is a pinned sandbox root"
        elif runtime_computes_roots(binary):
            row["verdict"] = "writable"
            row["detail"] = "deployed openswarm computes the git metadata roots per thread"
        else:
            row["verdict"] = "read-only-index"
            row["detail"] = ("git admin dir is not a pinned sandbox root and the deployed openswarm "
                             "does not compute the roots: git writes fail with 'Read-only file system'")
        row["runtime_roots"] = runtime_computes_roots(binary)
        rows.append(row)
    return rows, pinned


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", default=DEFAULT_HOME, help="openswarm home of this host")
    parser.add_argument("--lanes", default=None, help="lanes.json path (default: <home>/lanes.json)")
    parser.add_argument("--codex-home", default=os.path.expanduser("~/.codex"), help="codex home")
    parser.add_argument("--openswarm-bin", default=None, help="deployed openswarm binary")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()

    lanes_path = args.lanes or os.path.join(args.home, "lanes.json")
    try:
        with open(lanes_path, encoding="utf-8") as handle:
            lanes = (json.load(handle) or {}).get("lanes") or []
    except (OSError, ValueError) as error:
        print("cannot read lanes.json ({}): {}".format(lanes_path, error), file=sys.stderr)
        return 2

    binary = args.openswarm_bin or detect_openswarm_bin(
        args.home, [lane.get("name") for lane in lanes if lane.get("name")])
    rows, pinned = lane_rows(lanes, args.home, args.codex_home, binary)
    names = [row["admin_name"] for row in rows if row["admin_name"]]
    shared = sorted({name for name in names if names.count(name) > 1})

    report = {"lanes_file": lanes_path, "openswarm_bin": binary, "pinned_roots": pinned,
              "admin_dirs": names, "shared_admin_dirs": shared, "lanes": rows}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("openswarm binary: {}".format(binary or "not found"))
        print("pinned sandbox roots: {}".format(", ".join(pinned) or "none"))
        print("git admin dirs: {}".format(", ".join(names) or "none"))
        if shared:
            print("shared admin dirs: {}".format(", ".join(shared)))
        for row in rows:
            print("{}: {} gitdir={} pinned={} verdict={} ({})".format(
                row["lane"], row["worktree"] or "-", row["gitdir"] or "-",
                row["pinned_root"], row["verdict"], row["detail"]))
    return 0 if rows and all(row["verdict"] == "writable" for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
