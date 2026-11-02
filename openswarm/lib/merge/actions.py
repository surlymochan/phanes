"""The merge-gate actions around the merge decision (Refs #1432, part of #1355).

The box side merge loop (``merge-loop.sh``) does three things next to the
"reviewed and green -> merge" decision of #1355:

* the union merge of the append-only documentation: a pull request that is
  dirty only because of a documentation file (``docs/ctl-boot.md``) gets the
  base branch union-merged into its branch and is pushed back; every other
  conflict is left to the rebase queue,
* the rerun of a failed CI job: every failed job of a pull request is rerun
  once (``gh run rerun <run> --job <job>``) and never again; a job of an
  ignored platform (the macOS jobs: the fleet has no macOS runner) is left
  alone, also inside a mixed workflow,
* the post-merge hook: when ``main`` moved, a project command runs once (the
  phanes deployment of the demo); a failed run keeps the old version and is
  retried in the next round.

This module owns the *decisions* of those three actions, not the wiring: the
merge gate calls into it (#1355).  Every call that leaves the process (git,
gh, the hook command) goes through an injectable ``runner``, so the module can
be tested offline: the tests use a fake runner and disposable git
repositories.

The three actions:

``union_candidates(prs, merge_tree_files, union_files)``
    A pure selection: the pull requests whose conflicts are all union-mergeable.

``union_merge(repo_dir, pr, branch, union_files, runner)``
    The action: union-merge the base branch into the branch of the pull
    request, in a disposable worktree, and push the result back.  The base is
    the newest one the remote knows: it is fetched before the merge, so a
    stale local base branch neither hides the conflict with the current base
    nor fakes a "nothing to do".

``rerun_once(pr, runs, seen_path, runner)``
    The action: rerun the failed jobs of a pull request, once per job.  The
    jobs of a run are read with ``gh run view --json jobs``, so the ignore
    pattern applies to the job and not only to the name of the workflow.

``post_merge_hook(cmd, last_sha_path, main_sha, runner)``
    The action: run a command once per new sha of the base branch.

``openswarm/lib/merge/`` is a namespace package without an ``__init__.py``, so
this module is loaded by path (see ``openswarm/tests/test_merge_actions.py``).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

# The name pattern of the jobs that are never rerun: a failing macOS job is a
# known local problem (the fleet has no macOS runner).
DEFAULT_IGNORE_CHECKS_RE = r"(?i)macos"

# The conclusions of `gh run list` that mean "the CI of the pull request failed".
FAILURE_CONCLUSIONS = ("failure", "timed_out", "startup_failure")

# The author of the merge commit of a union merge.
MERGE_USER_NAME = "OpenSwarm"
MERGE_USER_EMAIL = "openswarm@example.invalid"

# The status of the union merge.
STATUS_MERGED = "merged"    # the branch got a new merge commit
STATUS_NOOP = "noop"        # the base was already merged: the head did not move
STATUS_MIXED = "mixed"      # a conflict outside the union files: the merge was aborted
STATUS_ERROR = "error"      # git (or the push) failed


# --------------------------------------------------------------------- utils


def _returncode(result: Any) -> int:
    """The exit code of a runner result; a broken result counts as a failure."""
    code = getattr(result, "returncode", 0)
    if isinstance(code, bool) or not isinstance(code, int):
        return 1
    return code


def _text(value: Any) -> str:
    """The output of a runner result as text (bytes are decoded)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _stdout(result: Any) -> str:
    """The standard output of a runner result."""
    return _text(getattr(result, "stdout", ""))


def _stderr(result: Any) -> str:
    """The error output of a runner result."""
    return _text(getattr(result, "stderr", "")).strip()


def _run(runner: Any, cwd: str, args: Sequence[str], text: bool = True) -> Any:
    """Run one git command in `cwd` and return the result of the runner."""
    command = ["git"] + [str(arg) for arg in args]
    return runner(command, cwd=cwd, capture_output=True, text=text)


def _rev_parse(runner: Any, cwd: str, ref: str) -> str:
    """The sha a ref points at; an empty text when the ref is unknown."""
    result = _run(runner, cwd, ["rev-parse", ref])
    if _returncode(result):
        return ""
    return _stdout(result).strip()


def _merge_pending(runner: Any, cwd: str) -> bool:
    """Whether the worktree holds a merge that still needs a commit."""
    result = _run(runner, cwd, ["rev-parse", "-q", "--verify", "MERGE_HEAD"])
    return _returncode(result) == 0


def _is_ancestor(runner: Any, repo_dir: str, older: str, newer: str) -> bool:
    """Whether the commit `older` is an ancestor of the commit `newer`."""
    result = _run(runner, repo_dir, ["merge-base", "--is-ancestor", older, newer])
    return _returncode(result) == 0


def _unmerged(runner: Any, cwd: str) -> List[str]:
    """The files that are still in conflict, sorted."""
    result = _run(runner, cwd, ["diff", "--name-only", "--diff-filter=U"])
    if _returncode(result):
        return []
    return sorted(path for path in _stdout(result).splitlines() if path.strip())


def _is_union_file(path: str, patterns: Iterable[str]) -> bool:
    """Whether a conflicting file may be union-merged."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns or ())


def _pr_number(pr: Any) -> Optional[int]:
    """The number of a pull request (a dict or the number itself)."""
    value = pr.get("number") if isinstance(pr, dict) else pr
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pr_field(pr: Any, *names: str) -> str:
    """The first non-empty field of a pull request, as text."""
    if not isinstance(pr, dict):
        return ""
    for name in names:
        value = pr.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _paths(value: Any) -> List[str]:
    """The conflicting files of one pull request, as a list of paths."""
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _conflict_table(merge_tree_files: Any) -> Dict[int, List[str]]:
    """The conflicting files of every pull request, keyed by the PR number.

    `merge_tree_files` is the output of `git merge-tree`: the number of the
    pull request maps to the files that conflict with the base branch.  The
    numbers may be text or numbers, the files a list or the raw text of the
    merge tree.
    """
    table: Dict[int, List[str]] = {}
    if not isinstance(merge_tree_files, dict):
        return table
    for key, value in merge_tree_files.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        table[number] = _paths(value)
    return table


# --------------------------------------------------------------------- union


def union_candidates(prs: Any, merge_tree_files: Any, union_files: Any) -> List[Dict[str, Any]]:
    """The pull requests whose conflicts are all union-mergeable.

    `prs` are the rows of ``gh pr list --json number,headRefName,mergeable``,
    `merge_tree_files` maps the number of a pull request to the files that
    conflict with the base branch (the output of ``git merge-tree``) and
    `union_files` are the glob patterns of the files that may be union-merged
    (``docs/*.md``).

    A pull request is a candidate when it is dirty and *every* one of its
    conflicting files matches one of the patterns: a pull request with a
    conflict in a code file is left to the rebase queue.  A pull request
    without conflicts is not a candidate either: there is nothing to merge.

    The function is pure: it reads nothing and writes nothing.  The result is
    sorted by the number of the pull request, so the caller always sees the
    same order.
    """
    conflicts_of = _conflict_table(merge_tree_files)
    candidates: List[Dict[str, Any]] = []
    for pr in prs or []:
        if not isinstance(pr, dict):
            continue
        # A pull request that GitHub reports as mergeable needs no union merge;
        # a pull request without that field is judged by its conflicts alone.
        mergeable = _pr_field(pr, "mergeable").strip().upper()
        if mergeable and mergeable not in ("CONFLICTING", "DIRTY"):
            continue
        conflicts = conflicts_of.get(_pr_number(pr), [])
        if not conflicts:
            continue
        if all(_is_union_file(path, union_files) for path in conflicts):
            candidates.append(pr)
    return sorted(candidates, key=lambda pr: _pr_number(pr) or 0)


def _union_resolve(runner: Any, worktree: str, path: str, scratch: str) -> bool:
    """Resolve one conflicting file with ``git merge-file --union``.

    The three stages of the conflict (the ancestor, ours and theirs) are
    union-merged, so both sides survive; the result is staged.  False means
    the conflict could not be resolved: the caller aborts the merge then.
    """
    stages: List[bytes] = []
    for stage in (1, 2, 3):
        blob = _run(runner, worktree, ["show", ":{}:{}".format(stage, path)], text=False)
        if _returncode(blob):
            return False
        content = getattr(blob, "stdout", b"")
        stages.append(content if isinstance(content, bytes) else str(content).encode("utf-8"))
    os.makedirs(scratch, exist_ok=True)
    names = []
    for index, content in enumerate(stages):
        name = os.path.join(scratch, "{}-{}".format(index, os.path.basename(path)))
        with open(name, "wb") as stream:
            stream.write(content)
        names.append(name)
    # git merge-file wants the current file, the ancestor and the other file.
    combined = _run(runner, worktree,
                    ["merge-file", "--union", "-p", names[1], names[0], names[2]], text=False)
    if _returncode(combined) not in (0, 1):
        return False
    merged = getattr(combined, "stdout", b"")
    destination = os.path.join(worktree, path)
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(destination, "wb") as stream:
        stream.write(merged if isinstance(merged, bytes) else str(merged).encode("utf-8"))
    staged = _run(runner, worktree, ["add", "--", path])
    return _returncode(staged) == 0


def _temp_root(repo_dir: str) -> tempfile.TemporaryDirectory:
    """A temporary directory for the worktree of a union merge.

    It lives inside the repository when that is possible (same filesystem, so
    the worktree is cheap to create and to remove), and in the temporary
    directory of the system otherwise.
    """
    try:
        return tempfile.TemporaryDirectory(prefix="openswarm-union-", dir=repo_dir)
    except OSError:
        return tempfile.TemporaryDirectory(prefix="openswarm-union-")


def union_merge(
    repo_dir: Any,
    pr: Any,
    branch: Any,
    union_files: Any,
    runner: Any = subprocess.run,
    base_ref: Any = None,
    remote: Any = None,
    base_sha: Any = None,
) -> Dict[str, Any]:
    """Union-merge the base branch into the branch of a pull request.

    The merge happens in a disposable worktree, so the working copy of the
    repository is never touched.  A conflict in a file of `union_files` is
    resolved with ``git merge-file --union`` (both sides survive); a conflict
    in any other file aborts the merge and leaves the branch alone -- that
    pull request belongs to the rebase queue.

    The base branch is *fetched* before the merge: the base of the box is
    often behind the base of GitHub, and merging a stale base would neither
    resolve the conflict with the current base nor notice that the branch is
    already up to date.  The newest base that is known is merged: the sha that
    was fetched from the remote and the sha the caller knows are compared and
    the descendant of the two wins.

    The result of the merge is pushed back to `branch` of the remote, but only
    when the head of the branch really moved: a base branch that was already
    merged changes nothing, and nothing is pushed then.

    `repo_dir` is the repository, `pr` the pull request (a dict of the
    ``gh pr view`` payload, or its number), `branch` the branch of the pull
    request, `union_files` the glob patterns of the files that may be
    union-merged and `runner` the callable that runs the git commands (it is
    called as ``runner(command, cwd=..., capture_output=True, text=...)``).

    `base_ref` is the name of the base branch (the base of the pull request by
    default), `base_sha` the sha of the base branch as the caller knows it and
    `remote` the name of the remote (the ``origin`` of the repository by
    default).

    The returned dict has a ``status``:

    ``merged``
        The branch got a new merge commit; ``sha`` is its sha, ``pushed`` tells
        whether the push went through.
    ``noop``
        The base branch was already merged: the head did not move, nothing was
        pushed.
    ``mixed``
        A conflict outside the union files: the merge was aborted, ``files``
        holds the conflicting files.
    ``error``
        Git (or the push) failed; ``error`` holds the message.

    Every result also holds the merged base (``base``) and where it came from
    (``base_source``: ``remote``, ``caller``, ``local``, ``pr`` or ``none``).
    """
    repo_dir = os.path.abspath(str(repo_dir))
    branch = str(branch or "").strip()
    if not branch:
        raise ValueError("branch must not be empty")
    remote_name = str(remote) if remote else (_pr_field(pr, "remote") or "origin")
    resolved = _resolve_base(runner, repo_dir, pr, base_ref, base_sha, remote_name)
    base = resolved["sha"]
    head_ref = _pr_field(pr, "headRefOid") or branch
    if not _rev_parse(runner, repo_dir, head_ref):
        head_ref = branch
    head_before = _rev_parse(runner, repo_dir, head_ref)
    if not head_before:
        return {"status": STATUS_ERROR, "error": "unknown branch {}".format(branch),
                "branch": branch, "pushed": False, "base": base,
                "base_source": resolved["source"]}

    with _temp_root(repo_dir) as temp_root:
        worktree = os.path.join(temp_root, "worktree")
        added = False
        try:
            added_result = _run(runner, repo_dir, ["worktree", "add", "--detach", worktree, head_ref])
            if _returncode(added_result):
                return {"status": STATUS_ERROR, "error": _stderr(added_result) or "git worktree add failed",
                        "branch": branch, "pushed": False, "base": base,
                        "base_source": resolved["source"]}
            added = True
            merge = _run(runner, worktree, ["merge", "--no-commit", "--no-ff", base])
            conflicts = _unmerged(runner, worktree)
            if _returncode(merge) and not conflicts:
                # The merge failed for another reason than a content conflict
                # (unknown base ref, unrelated histories, ...): report it.
                _run(runner, worktree, ["merge", "--abort"])
                message = _stderr(merge) or _stdout(merge).strip() or "git merge failed"
                return {"status": STATUS_ERROR, "error": message, "branch": branch,
                        "pushed": False, "base": base, "base_source": resolved["source"]}
            if not conflicts and not _merge_pending(runner, worktree):
                # "Already up to date": the base branch is already merged.
                return {"status": STATUS_NOOP, "sha": head_before, "branch": branch,
                        "pushed": False, "base": base, "base_source": resolved["source"]}
            outside = [path for path in conflicts if not _is_union_file(path, union_files)]
            if outside:
                _run(runner, worktree, ["merge", "--abort"])
                return {"status": STATUS_MIXED, "files": outside, "branch": branch,
                        "pushed": False, "base": base, "base_source": resolved["source"]}
            scratch = os.path.join(temp_root, "stages")
            for path in conflicts:
                if not _union_resolve(runner, worktree, path, scratch):
                    _run(runner, worktree, ["merge", "--abort"])
                    return {"status": STATUS_MIXED, "files": [path], "branch": branch,
                            "pushed": False, "base": base, "base_source": resolved["source"]}
            committed = _run(runner, worktree,
                             ["-c", "user.name=" + MERGE_USER_NAME,
                              "-c", "user.email=" + MERGE_USER_EMAIL,
                              "-c", "core.editor=true", "commit", "--no-edit"])
            if _returncode(committed):
                _run(runner, worktree, ["merge", "--abort"])
                message = _stderr(committed) or "git commit failed"
                return {"status": STATUS_ERROR, "error": message, "branch": branch,
                        "pushed": False, "base": base, "base_source": resolved["source"]}
            sha = _rev_parse(runner, worktree, "HEAD")
            if not sha or sha == head_before:
                # The head did not move: there is nothing to push back.
                return {"status": STATUS_NOOP, "sha": sha or head_before, "branch": branch,
                        "pushed": False, "base": base, "base_source": resolved["source"]}
            pushed = _run(runner, worktree, ["push", remote_name, "HEAD:refs/heads/" + branch])
            if _returncode(pushed):
                message = _stderr(pushed) or "git push failed"
                return {"status": STATUS_ERROR, "error": message, "sha": sha,
                        "branch": branch, "pushed": False, "base": base,
                        "base_source": resolved["source"]}
            return {"status": STATUS_MERGED, "sha": sha, "branch": branch, "pushed": True,
                    "files": conflicts, "base": base, "base_source": resolved["source"]}
        finally:
            if added:
                _run(runner, repo_dir, ["worktree", "remove", "--force", worktree])


def _fetch_base(runner: Any, repo_dir: str, remote: str, base_branch: str) -> str:
    """The sha of the base branch on the remote, fetched into the repository.

    The base branch of the box is often behind the base branch of GitHub (the
    repository was cloned long ago and nobody fetched it), so the base is
    fetched before the merge.  An empty text means "the remote base is
    unknown": the fetch failed (no network, no credentials, no such branch).
    The caller then falls back to what the repository knows.
    """
    result = _run(runner, repo_dir, ["fetch", "--no-tags", "--quiet", remote, base_branch])
    if _returncode(result):
        return ""
    # FETCH_HEAD holds the sha that was just fetched.  It is only read when
    # the fetch went through, so a failed fetch never leaves a stale sha
    # behind.
    return _rev_parse(runner, repo_dir, "FETCH_HEAD")


def _newest_base(runner: Any, repo_dir: str, candidates: Sequence[str]) -> str:
    """The newest of the known base commits; the descendant of the others wins."""
    known = [sha for sha in candidates if sha and _rev_parse(runner, repo_dir, sha)]
    for candidate in known:
        older = any(other != candidate and _is_ancestor(runner, repo_dir, candidate, other)
                    for other in known)
        if not older:
            return candidate
    return known[0] if known else ""


def _resolve_base(
    runner: Any,
    repo_dir: str,
    pr: Any,
    base_ref: Any,
    base_sha: Any,
    remote: str,
) -> Dict[str, str]:
    """The commit of the base branch that is merged into the pull request.

    The newest known base wins: the sha that was fetched from the remote and
    the sha the caller knows are compared, and the descendant of the two is
    merged.  That covers a stale local base branch (the remote moved on) and a
    stale caller (the remote moved on after the caller looked).

    The sha of the base branch of the pull request (``baseRefOid``) is only a
    last resort: it is the base the pull request was opened against, so it is
    behind the base of the remote by definition.

    The returned dict holds the sha (``sha``) and where it came from
    (``source``: ``remote``, ``caller``, ``local``, ``pr`` or ``none``).
    """
    branch = str(base_ref or "").strip() or _pr_field(pr, "baseRefName", "base") or "main"
    fetched = _fetch_base(runner, repo_dir, remote, branch)
    caller = str(base_sha or "").strip()
    newest = _newest_base(runner, repo_dir, [fetched, caller])
    if newest:
        return {"sha": newest, "source": "remote" if newest == fetched else "caller"}
    if _rev_parse(runner, repo_dir, branch):
        # The remote could not be reached: the local base branch is the best
        # that is left.
        return {"sha": branch, "source": "local"}
    oid = _pr_field(pr, "baseRefOid")
    if oid and _rev_parse(runner, repo_dir, oid):
        return {"sha": oid, "source": "pr"}
    return {"sha": branch, "source": "none"}


# --------------------------------------------------------------------- rerun


def _matcher(ignore_re: Any):
    """The compiled ignore pattern; None disables the filter."""
    if ignore_re is None:
        return None
    if hasattr(ignore_re, "search"):
        return ignore_re
    try:
        return re.compile(ignore_re)
    except re.error as error:
        raise ValueError("invalid ignore pattern {!r}: {}".format(ignore_re, error)) from error


def _seen_runs(seen_path: Any) -> Dict[str, Any]:
    """The runs and jobs that were already rerun, read from the state file.

    The key of a job is the id of its run and the id of the job
    (``"1234567:7654321"``), the key of a whole run is its id alone.

    A missing or damaged file is not an error: it then reads as empty, which
    means "nothing was rerun yet".
    """
    try:
        with open(str(seen_path), encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    runs = payload.get("runs")
    if not isinstance(runs, dict):
        return {}
    return {str(key): value for key, value in runs.items()}


def _write_seen_runs(seen_path: Any, runs: Dict[str, Any]) -> None:
    """Write the state file atomically (temporary file + ``os.replace``)."""
    target = os.path.abspath(str(seen_path))
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory or None,
        prefix=os.path.basename(target) + ".", suffix=".tmp", delete=False)
    temporary = handle.name
    try:
        with handle:
            json.dump({"runs": runs}, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _execute(runner: Any, command: Sequence[str]) -> tuple:
    """Run a command; (True, "") when it went through, else (False, why)."""
    try:
        result = runner(list(command), check=False, capture_output=True, text=True)
    except Exception as error:  # the runner itself blew up
        return False, "{}: {}".format(type(error).__name__, error)
    if _returncode(result) == 0:
        return True, ""
    detail = _stderr(result)
    return False, "{} exited with {}{}".format(
        " ".join(str(part) for part in command), _returncode(result),
        ": " + detail if detail else "")


def _capture(runner: Any, command: Sequence[str]) -> str:
    """The output of a command ("" when it failed)."""
    try:
        result = runner(list(command), check=False, capture_output=True, text=True)
    except Exception:
        return ""
    if _returncode(result):
        return ""
    return _stdout(result)


def _job_rows(value: Any) -> List[Dict[str, Any]]:
    """The jobs of a run, from a list or from the JSON of ``gh run view``."""
    if isinstance(value, (bytes, str)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        try:
            value = json.loads(text)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get("jobs")
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, dict)]


def _job_id(job: Any) -> str:
    """The id of a job (the number ``gh run rerun --job`` wants)."""
    for name in ("databaseId", "id"):
        value = job.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _failed_jobs(jobs: Any) -> List[Dict[str, Any]]:
    """The failed jobs of a run."""
    failed: List[Dict[str, Any]] = []
    for job in jobs:
        conclusion = str(job.get("conclusion") or "").strip().lower()
        status = str(job.get("status") or "").strip().lower()
        if conclusion in FAILURE_CONCLUSIONS or (not conclusion and status == "completed"):
            failed.append(job)
    return failed


def _run_jobs(runner: Any, run_id: str) -> List[Dict[str, Any]]:
    """The jobs of a run, read with ``gh run view`` (empty when unknown)."""
    payload = _capture(runner, ["gh", "run", "view", str(run_id), "--json", "jobs"])
    return _job_rows(payload)


def rerun_once(
    pr: Any,
    runs: Any,
    seen_path: Any,
    runner: Any = subprocess.run,
    ignore_checks_re: Any = DEFAULT_IGNORE_CHECKS_RE,
    now: Any = None,
) -> List[Dict[str, Any]]:
    """Rerun the failed jobs of a pull request, once per job.

    `runs` are the rows of ``gh run list --json databaseId,name,conclusion``:
    a run is looked at when its conclusion is a failure (``failure``,
    ``timed_out`` or ``startup_failure``) and when its name does not match
    `ignore_checks_re` (:data:`DEFAULT_IGNORE_CHECKS_RE` by default, so a
    failing macOS workflow is left alone).

    The *jobs* of such a run decide what really happens: they are read with
    ``gh run view <id> --json jobs`` (or taken from the ``jobs`` key of the
    row, when the caller already knows them).  Every failed job whose name
    does not match the ignore pattern is rerun on its own
    (``gh run rerun <run> --job <job>``), so a mixed workflow does not rerun
    its macOS job together with its Linux job.

    Every job is rerun at most once: the jobs that were rerun are remembered
    in the JSON file `seen_path`::

        {"runs": {"1234567:7654321": {"pr": 1445, "at": 1730000000.0}}}

    The key of a job is the id of its run and the id of the job; a run that was
    rerun as a whole is remembered under its own id, so its jobs are left
    alone as well.  The file is written atomically (temporary file +
    ``os.replace``), so a crashed writer never leaves a half written file
    behind.  A job is only remembered when the command really went through,
    so a command that failed is tried again by the next round.

    When the failed jobs of a run are unknown, the run is left alone: rerun-
    ning the whole run (``gh run rerun --failed``) would rerun the ignored
    jobs too.

    `runner` is the callable that runs the ``gh`` commands (it is called as
    ``runner(command, check=False, capture_output=True, text=True)``) and `now`
    is the timestamp written into the state file (the wall clock when it is
    None).

    Every row of the result is ``{"run_id": ..., "job_id": ..., "action":
    "rerun"|"skip", "reason": ...}``: a run or a job that was left alone is
    reported as skipped with the reason it was left alone.
    """
    matcher = _matcher(ignore_checks_re)
    number = _pr_number(pr)
    moment = time.time() if now is None else float(now)
    seen = _seen_runs(seen_path)
    actions: List[Dict[str, Any]] = []
    for row in runs or []:
        if not isinstance(row, dict):
            continue
        run_id = row.get("databaseId")
        if run_id in (None, ""):
            run_id = row.get("id")
        key = str(run_id).strip() if run_id is not None else ""
        if not key:
            continue
        name = str(row.get("name") or row.get("workflowName") or "")
        conclusion = str(row.get("conclusion") or "").strip().lower()
        if conclusion not in FAILURE_CONCLUSIONS:
            actions.append({"run_id": run_id, "job_id": None, "action": "skip",
                            "reason": "conclusion '{}' is not a failure".format(conclusion or "none")})
            continue
        if matcher is not None and matcher.search(name):
            actions.append({"run_id": run_id, "job_id": None, "action": "skip",
                            "reason": "name '{}' matches the ignore pattern".format(name)})
            continue
        jobs = _failed_jobs(_job_rows(row.get("jobs")))
        if not jobs:
            jobs = _failed_jobs(_run_jobs(runner, key))
        if not jobs:
            actions.append({"run_id": run_id, "job_id": None, "action": "skip",
                            "reason": "the failed jobs of the run are unknown"})
            continue
        for job in jobs:
            job_id = _job_id(job)
            job_name = str(job.get("name") or "")
            if matcher is not None and matcher.search(job_name):
                actions.append({"run_id": run_id, "job_id": job_id, "action": "skip",
                                "reason": "job '{}' matches the ignore pattern".format(job_name)})
                continue
            if not job_id:
                actions.append({"run_id": run_id, "job_id": None, "action": "skip",
                                "reason": "the job has no id"})
                continue
            job_key = "{}:{}".format(key, job_id)
            if job_key in seen or key in seen:
                actions.append({"run_id": run_id, "job_id": job_id, "action": "skip",
                                "reason": "run {} was already rerun".format(key)})
                continue
            ok, detail = _execute(runner, ["gh", "run", "rerun", key, "--job", job_id])
            if not ok:
                actions.append({"run_id": run_id, "job_id": job_id, "action": "skip",
                                "reason": "rerun failed: " + detail})
                continue
            seen[job_key] = {"pr": number, "at": moment}
            _write_seen_runs(seen_path, seen)
            actions.append({"run_id": run_id, "job_id": job_id, "action": "rerun",
                            "reason": "rerun of the failed job"})
    return actions


# -------------------------------------------------------------- post-merge hook


def _argv(cmd: Any) -> List[str]:
    """The command of the hook as an argv list (no shell is used)."""
    if isinstance(cmd, (str, bytes)):
        parts = shlex.split(cmd.decode("utf-8") if isinstance(cmd, bytes) else cmd)
    elif isinstance(cmd, (list, tuple)):
        parts = [str(part) for part in cmd]
    else:
        raise ValueError("cmd must be an argv list or a command line")
    if not parts:
        raise ValueError("cmd must not be empty")
    return parts


def _read_sha(path: Any) -> str:
    """The sha of the last successful run of the hook (empty when there is none)."""
    try:
        with open(str(path), encoding="utf-8") as stream:
            return stream.read().strip()
    except OSError:
        return ""


def _write_sha(path: Any, sha: str) -> None:
    """Write the sha of the last successful run of the hook, atomically."""
    target = os.path.abspath(str(path))
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory or None,
        prefix=os.path.basename(target) + ".", suffix=".tmp", delete=False)
    temporary = handle.name
    try:
        with handle:
            handle.write(sha + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def post_merge_hook(
    cmd: Any,
    last_sha_path: Any,
    main_sha: Any,
    runner: Any = subprocess.run,
) -> Dict[str, Any]:
    """Run a command once per new sha of the base branch.

    The hook only runs when the base branch moved: the sha of the last
    successful run is remembered in the file `last_sha_path`.  A failed run is
    *not* remembered, so the version that worked stays in place and the next
    round tries again.

    `cmd` is the command of the hook (an argv list, or a command line that is
    split without a shell), `last_sha_path` the file that remembers the sha of
    the last successful run and `main_sha` the sha of the base branch.  The
    runner is called as ``runner(command, check=False, capture_output=True,
    text=True)``.

    The returned dict has a ``status``: ``ok`` (the command ran and succeeded),
    ``skipped`` (the sha did not move since the last successful run) or
    ``failed`` (the command ran and failed; ``error`` holds the message).
    """
    command = _argv(cmd)
    sha = str(main_sha or "").strip()
    if not sha:
        raise ValueError("main_sha must not be empty")
    if _read_sha(last_sha_path) == sha:
        return {"status": "skipped", "sha": sha}
    ok, detail = _execute(runner, command)
    if not ok:
        return {"status": "failed", "sha": sha, "error": detail}
    _write_sha(last_sha_path, sha)
    return {"status": "ok", "sha": sha}
