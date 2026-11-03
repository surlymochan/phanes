#!/usr/bin/env python3
"""Replay the harvest artifact filter on the local head of a PR (Refs #1835).

The dispatcher drops the artifacts of a lane branch before it pushes it: the hidden
build leftovers (``openswarm/tests/.dde4*-00000000.so``) and the review products
(``.lane-review/PR-N.md``).  This tool reports what the filter would drop on a head,
so the head of a PR can be replayed offline; it only reads, no write access to the
repository is needed.

    python3 tools/check-harvest-filter.py 1768 1677          # the local heads rebase-pr-<n>
    python3 tools/check-harvest-filter.py refs/heads/x --base <sha>
"""
import argparse
import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("harvest_filter",
                                               os.path.join(ROOT, "openswarm", "lib", "dispatch", "harvest_filter.py"))
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def resolve(repo, name):
    """The local head of a PR: `rebase-pr-<n>` (what the dispatcher fetched) or a ref."""
    if name.isdigit():
        for candidate in ("refs/heads/rebase-pr-%s" % name, "refs/remotes/origin/rebase-pr-%s" % name):
            rc, _, _ = git(repo, "rev-parse", "--verify", "--quiet", candidate)
            if rc == 0:
                return candidate
        return "rebase-pr-%s" % name
    return name


BASE_BRANCHES = ("main", "refs/heads/main", "refs/ad/base", "master", "refs/heads/master", "origin/main")


def base_branch_of(repo, name):
    """The base branch of the replay: the given one, or the first candidate that exists.

    The worktree of the dispatcher keeps the base under `refs/ad/base` (there is no
    local `main` there), so the replay must not depend on one name only."""
    for candidate in (name,) + BASE_BRANCHES:
        if candidate and git(repo, "rev-parse", "--verify", "--quiet", candidate)[0] == 0:
            return candidate
    return name


def base_of(repo, ref, base, base_branch):
    if base:
        return base
    rc, out, _ = git(repo, "merge-base", base_branch, ref)
    return out if rc == 0 else ""


def sizes(repo, ref):
    """The size of every blob of the head (the excluded artifacts are megabytes)."""
    rc, out, _ = git(repo, "ls-tree", "-r", "-l", ref)
    if rc:
        return {}
    table = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 4 and parts[3].isdigit():
            table[path] = int(parts[3])
    return table


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("refs", nargs="+", help="PR numbers (rebase-pr-<n>) or refs")
    parser.add_argument("--repo", default=ROOT, help="the repository (default: the repo root)")
    parser.add_argument("--base", default="", help="the base commit (default: the merge base with --base-branch)")
    parser.add_argument("--base-branch", default="main", help="the base branch (default: main)")
    args = parser.parse_args(argv)

    hits = 0
    missed = 0
    base_branch = base_branch_of(args.repo, args.base_branch)
    for name in args.refs:
        ref = resolve(args.repo, name)
        rc, _, err = git(args.repo, "rev-parse", "--verify", "--quiet", ref)
        if rc:
            print("%s: not a local ref (%s)" % (name, err or "unknown ref"))
            missed += 1
            continue
        base = base_of(args.repo, ref, args.base, base_branch)
        try:
            removed = H.excluded_paths_of(args.repo, ref, base)
        except H.FilterError as error:
            print("%s: the filter cannot decide (%s)" % (name, error))
            missed += 1
            continue
        table = sizes(args.repo, ref)
        print("%s (base %s): %d excluded path(s)" % (ref, base[:8] or "?", len(removed)))
        for path in removed:
            size = table.get(path)
            print("  %s%s" % (path, " (%d bytes)" % size if size else ""))
        hits += len(removed)
    print("%d excluded path(s) in total" % hits)
    if missed:
        print("%d ref(s) could not be replayed" % missed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
