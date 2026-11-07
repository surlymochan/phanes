"""The artifact filter of a harvested lane branch (Refs #1835).

A lane branch can carry artifacts that must never reach a PR: the hidden build
leftovers of the test suite (``openswarm/tests/.dde4*-00000000.so``) and the
review products (``.lane-review/PR-N.md``).  The dispatcher rebuilds the branch
without those paths before it pushes it, so a PR only holds the real change.

The rules are a list constant (one more artifact kind is one more pattern) and
the filter never guesses: when a git command fails it raises ``FilterError`` and
the caller fails closed (nothing is pushed).
"""

import os
import re
import subprocess
import tempfile

# The hidden shared objects: the native library the agent runtime unpacks into the
# working directory (Refs #1773).  A named rule, so the repository check and the
# harvest use the same one.
HIDDEN_SHARED_OBJECT = re.compile(r"(^|/)\.[^/]*\.so$")

# The artifacts that never enter a PR.  A list, so the rule set stays configurable.
HARVEST_EXCLUDE_PATTERNS = [
    HIDDEN_SHARED_OBJECT,                   # hidden shared objects (build leftovers)
    re.compile(r"(^|/)\.lane-review/"),      # the review products of the lane
]


class FilterError(RuntimeError):
    """The filter could not decide: the caller must fail closed (push nothing)."""


def compile_patterns(patterns=None):
    """The rule set: the default constant, or the configured patterns."""
    if patterns is None:
        return HARVEST_EXCLUDE_PATTERNS
    return [p if hasattr(p, "search") else re.compile(p) for p in patterns]


def excluded_paths(paths, patterns=None):
    """The paths of `paths` that match a rule (sorted, without duplicates)."""
    rules = compile_patterns(patterns)
    return sorted({path for path in paths if path and any(rule.search(path) for rule in rules)})


def _git(repo, *args, env=None, inp=None):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, env=env, input=inp)
    if proc.returncode:
        raise FilterError(proc.stderr.decode("utf-8", "replace").strip() or ("git %s failed" % args[0]))
    return proc.stdout


def tracked_artifacts(repo, patterns=None):
    """The artifacts among the files the repository tracks.

    The filter cleans a lane branch before it is pushed, but the repository itself
    must not carry the artifacts either: a tracked one is swept into every change
    that runs ``git add -A`` (Refs #1773).
    """
    paths = [os.fsdecode(path) for path in _git(repo, "ls-files", "-z").split(b"\0") if path]
    return excluded_paths(paths, patterns)


def _tree(repo, ref):
    return _git(repo, "rev-parse", "%s^{tree}" % ref).strip().decode()


def _commits(repo, base, ref):
    """The commits of the branch over the base (linear: a merge commit cannot be rebuilt)."""
    _git(repo, "merge-base", "--is-ancestor", base, ref)
    commits = _git(repo, "rev-list", "--reverse", "--topo-order", "--parents", "%s..%s" % (base, ref)).splitlines()
    if any(len(line.split()) != 2 for line in commits):
        raise FilterError("cannot filter merge commits")
    return commits


def _changed(repo, base, ref, commits):
    """The paths the branch touches: the diff over the base and every single commit."""
    paths = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, ref).split(b"\0")
    for line in commits:
        commit = line.split()[0].decode()
        paths += _git(repo, "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z", commit).split(b"\0")
    return [os.fsdecode(path) for path in paths if path]


def excluded_paths_of(repo, ref, base, patterns=None):
    """The excluded paths of the branch `ref` over the base (the branch is not rebuilt).

    The report is the same one the filter writes to the brain log; it needs no write
    access, so a PR head can be replayed offline.
    """
    if not base:
        raise FilterError("missing dispatch base")
    commits = _commits(repo, base, ref)
    return excluded_paths(_changed(repo, base, ref, commits), patterns)


def filter_branch(repo, ref, base, patterns=None):
    """Rebuild `ref` without the excluded paths.

    Returns ``(head, removed, empty)``: the head of the rebuilt branch (the input
    ref when nothing was excluded), the excluded paths, and whether the rebuilt
    tree is the tree of ``base`` (no effective change is left).  The emptiness is a
    tree comparison, not a head comparison: commits that cancel each other out
    leave the tree of the base, whatever the head is.
    """
    if not base:
        raise FilterError("missing dispatch base")
    commits = _commits(repo, base, ref)
    removed = excluded_paths(_changed(repo, base, ref, commits), patterns)
    if not removed:
        return ref, [], _tree(repo, ref) == _tree(repo, base)
    parent = base
    with tempfile.TemporaryDirectory(prefix=".harvest-", dir=repo) as directory:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(directory, "index"), "GIT_LITERAL_PATHSPECS": "1"}
        for line in commits:
            commit = line.split()[0].decode()
            _git(repo, "read-tree", commit, env=env)
            _git(repo, "update-index", "--force-remove", "--", *removed, env=env)
            present = [path for path in removed if _git(repo, "ls-tree", "-z", base, "--", path)]
            if present:
                _git(repo, "restore", "--source", base, "--staged", "--", *present, env=env)
            tree = _git(repo, "write-tree", env=env).strip().decode()
            if tree == _tree(repo, "%s^{tree}" % parent):
                continue                      # nothing is left of this commit
            author = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI", commit).rstrip(b"\n").split(b"\0")
            commit_env = {**env, "GIT_AUTHOR_NAME": os.fsdecode(author[0]),
                          "GIT_AUTHOR_EMAIL": os.fsdecode(author[1]), "GIT_AUTHOR_DATE": os.fsdecode(author[2]),
                          "GIT_COMMITTER_NAME": os.fsdecode(author[0]),
                          "GIT_COMMITTER_EMAIL": os.fsdecode(author[1]),
                          "GIT_COMMITTER_DATE": os.fsdecode(author[2])}
            message = _git(repo, "show", "-s", "--format=%B", commit)
            parent = _git(repo, "commit-tree", tree, "-p", parent, env=commit_env, inp=message).strip().decode()
    return parent, removed, _tree(repo, parent) == _tree(repo, base)
