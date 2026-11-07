#!/usr/bin/env python3
"""Check that every Codex lane on this host can write its own git metadata inside the sandbox.

A Codex ``workspace-write`` sandbox mounts a worktree's git metadata read-only, so a lane can only
commit when the sandbox's writable roots contain that lane's *exact* git admin dir
(``.git/worktrees/<name>``). Every lane uses a worktree directory named ``wt``, so git numbers the
admin dirs of the second lane (``wt``, ``wt1``); a root pinned to one name covers one lane only and
the other lane fails every git write with ``Read-only file system`` (Refs #1832).

This tool reads the host state only (lanes.json, the lane worktrees, ``~/.codex/config.toml`` and
the deployed runtime) and reports, per Codex lane, whether its admin dir is writable in the sandbox:

* the admin dir is read from the worktree itself (``git rev-parse --absolute-git-dir``),
* the writable roots are the ones the *deployed runtime* computes for that worktree, so the verdict
  reflects the runtime that actually runs the lane,
* the roots pinned in ``~/.codex/config.toml`` are reported as well, because a pinned root that
  names one admin dir covers that lane only.

It never writes to the host (the optional ``--probe`` only reports whether the host can write into
the admin dir; it does not write anything).

Usage:
    tools/check-codex-sandbox-gitdir.py [--home DIR] [--lanes FILE] [--codex-home DIR]
                                       [--openswarm-bin FILE] [--lane NAME] [--probe] [--json]
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys

DEFAULT_HOMES = ("~/.claude/openswarm-executor-candidate", "~/.claude/swrm")


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
    """The worktree directories of a lane.

    The runtime keeps the worktree of a lane in ``<data dir>/lanes/<name>/wt``; a lane that was
    rebuilt keeps the same path. Sibling directories that carry a worktree ``.git`` pointer and the
    workspace path of the lane state are added as well, so a lane whose worktree moved is still
    found.
    """
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
    """The deployed runtime of this host (systemd unit, release dir or PATH)."""
    units = ["openswarm-executor-{}.service".format(name) for name in lane_names]
    _, listed, _ = run(["systemctl", "list-units", "--type=service", "--all", "openswarm*", "--no-legend"])
    units += [line.split()[0] for line in (listed or "").splitlines() if line.strip()]
    for unit in units:
        _, text, _ = run(["systemctl", "cat", unit])
        match = re.search(r"ExecStart=(\S*(?:openswarm|swrm)(?:-fleet)?)\b", text or "")
        if match and os.path.isfile(match.group(1)):
            return match.group(1)
    roots = ["/root/tools/openswarm-executor-candidate/releases",
             os.path.expanduser("~/tools/openswarm-executor-candidate/releases"),
             os.path.join(os.path.dirname(home.rstrip("/")), "tools", "openswarm-executor-candidate", "releases")]
    for releases in roots:
        if not os.path.isdir(releases):
            continue
        candidates = []
        for entry in sorted(os.listdir(releases)):
            for name in ("openswarm", "swrm"):
                candidates.append(os.path.join(releases, entry, name, "bin", name))
        for candidate in reversed(candidates):
            if os.path.isfile(candidate):
                return candidate
    for name in ("openswarm", "swrm"):
        _, path, _ = run(["sh", "-c", "command -v {}".format(name)])
        if path and os.path.isfile(path):
            return path
    return None


def load_runtime(binary):
    """Load the deployed runtime as a module, so its own root computation decides the verdict."""
    if not binary or not os.path.isfile(binary):
        return None
    try:
        loader = importlib.machinery.SourceFileLoader("deployed_runtime", binary)
        spec = importlib.util.spec_from_loader("deployed_runtime", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


def local_gitdir_roots(worktree):
    """The roots of a runtime without the gitdir support: worktree root and git dir only."""
    rc, out, _ = git(worktree, "rev-parse", "--git-dir", "--git-common-dir")
    lines = [x for x in out.splitlines() if x.strip()]
    if rc or len(lines) != 2:
        return []
    gitdir, common = (os.path.realpath(os.path.join(worktree, x.strip())) for x in lines)
    roots = [os.path.realpath(worktree), gitdir]
    if common != gitdir:
        roots += [os.path.join(common, sub) for sub in ("objects", "refs", "logs")
                  if os.path.isdir(os.path.join(common, sub))]
    return roots


def runtime_roots(runtime, worktree):
    """The writable roots the deployed runtime computes for ``worktree`` and where they come from.

    Returns ``(roots, source, runtime_computes_roots)``: a runtime without the gitdir support
    cannot hand the roots to the sandbox, so its verdict is not "writable" even when the roots
    would cover the admin dir.
    """
    if runtime is not None:
        # ``codex_writable_roots`` is the current runtime, ``codex_gitdir_writable_roots`` the
        # older one; both hand the worktree's git metadata to the sandbox.
        for name in ("codex_writable_roots", "codex_gitdir_writable_roots"):
            getter = getattr(runtime, name, None)
            if not callable(getter):
                continue
            try:
                policy = runtime.load_policy()
            except Exception:  # noqa: BLE001
                policy = {"codex": {"sandbox": "workspace-write"}}
            try:
                return list(getter(worktree, policy)), "deployed runtime ({})".format(name), True
            except Exception:  # noqa: BLE001
                pass
    return local_gitdir_roots(worktree), "local fallback (the deployed runtime does not compute the roots)", False


def host_can_write(path):
    """Whether the host (not the sandbox) can write into ``path``."""
    if not path or not os.path.isdir(path):
        return None
    return os.access(path, os.W_OK)


def lane_rows(lanes, home, codex_home, binary, probe=False):
    pinned = config_writable_roots(codex_home)
    runtime = load_runtime(binary)
    rows = []
    for lane in lanes:
        if (lane.get("backend") or "").lower() != "codex":
            continue
        name = lane.get("name") or "?"
        row = {"lane": name, "worktree": None, "gitdir": None, "admin_name": None,
               "pinned_root": False, "runtime_roots": None, "roots_source": None,
               "host_writable": None, "verdict": "unknown", "detail": ""}
        for worktree in lane_worktrees(home, name):
            rc, gitdir, _ = git(worktree, "rev-parse", "--absolute-git-dir")
            if rc or not gitdir:
                continue
            row["worktree"] = worktree
            row["gitdir"] = os.path.realpath(gitdir)
            row["admin_name"] = os.path.basename(row["gitdir"])
            row["pinned_root"] = row["gitdir"] in pinned
            break
        if not row["gitdir"]:
            row["verdict"] = "missing-worktree"
            row["detail"] = "no lane worktree with a git admin dir was found"
            rows.append(row)
            continue
        roots, source, computes = runtime_roots(runtime, row["worktree"])
        row["runtime_roots"] = roots
        row["roots_source"] = source
        if probe:
            row["host_writable"] = host_can_write(row["gitdir"])
        if computes and row["gitdir"] in roots:
            row["verdict"] = "writable"
            row["detail"] = "the git admin dir is one of the sandbox writable roots ({})".format(source)
        elif computes:
            row["verdict"] = "read-only-index"
            row["detail"] = ("the git admin dir is not a sandbox writable root ({}): every git write "
                             "inside the sandbox fails with 'Read-only file system'".format(source))
        else:
            row["verdict"] = "read-only-index"
            row["detail"] = ("the deployed runtime does not hand the git admin dir to the sandbox, so "
                             "every git write inside the sandbox fails with 'Read-only file system'")
        rows.append(row)
    return rows, pinned


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", default=None, help="runtime data dir of this host")
    parser.add_argument("--lanes", default=None, help="lanes.json path (default: <home>/lanes.json)")
    parser.add_argument("--codex-home", default=os.path.expanduser("~/.codex"), help="codex home")
    parser.add_argument("--openswarm-bin", default=None, help="deployed runtime binary")
    parser.add_argument("--lane", default=None, help="only report this lane")
    parser.add_argument("--probe", action="store_true", help="also report whether the host can write the admin dir")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args()

    homes = []
    if args.home:
        homes.append(args.home)
    env_home = os.environ.get("OPENSWARM_HOME")
    if env_home:
        homes.append(env_home)
    homes += [os.path.expanduser(path) for path in DEFAULT_HOMES]
    home = next((path for path in homes if os.path.isfile(os.path.join(path, "lanes.json"))), homes[0])
    lanes_path = args.lanes or os.path.join(home, "lanes.json")
    try:
        with open(lanes_path, encoding="utf-8") as handle:
            lanes = (json.load(handle) or {}).get("lanes") or []
    except (OSError, ValueError) as error:
        print("cannot read lanes.json ({}): {}".format(lanes_path, error), file=sys.stderr)
        return 2
    if args.lane:
        lanes = [lane for lane in lanes if (lane.get("name") or "") == args.lane]

    binary = args.openswarm_bin or detect_openswarm_bin(
        home, [lane.get("name") for lane in lanes if lane.get("name")])
    rows, pinned = lane_rows(lanes, home, args.codex_home, binary, probe=args.probe)
    names = [row["admin_name"] for row in rows if row["admin_name"]]
    shared = sorted({name for name in names if names.count(name) > 1})

    report = {"lanes_file": lanes_path, "home": home, "openswarm_bin": binary, "pinned_roots": pinned,
              "admin_dirs": names, "shared_admin_dirs": shared, "lanes": rows}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("runtime data dir: {}".format(home))
        print("deployed runtime: {}".format(binary or "not found"))
        print("pinned sandbox roots: {}".format(", ".join(pinned) or "none"))
        print("git admin dirs: {}".format(", ".join(names) or "none"))
        if shared:
            print("shared admin dirs: {}".format(", ".join(shared)))
        for row in rows:
            print("{}: {} gitdir={} pinned={} verdict={} ({})".format(
                row["lane"], row["worktree"] or "-", row["gitdir"] or "-",
                row["pinned_root"], row["verdict"], row["detail"]))
            if row["host_writable"] is not None:
                print("    host can write the admin dir: {}".format(row["host_writable"]))
    return 0 if rows and all(row["verdict"] == "writable" for row in rows) and not shared else 1


if __name__ == "__main__":
    sys.exit(main())
