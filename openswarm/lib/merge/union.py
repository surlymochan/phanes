"""Union-merge selected documentation conflicts without publishing changes."""

import fnmatch
import os
import subprocess
import tempfile


def _is_union_file(path, patterns):
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def union_candidates(prs, conflict_files_by_pr, union_files):
    """Return conflicting PRs whose complete conflict set is union-mergeable."""
    candidates = []
    for pr in prs:
        if pr.get("mergeable") != "CONFLICTING":
            continue
        number = pr["number"]
        conflicts = conflict_files_by_pr.get(number, [])
        if conflicts and all(_is_union_file(path, union_files) for path in conflicts):
            candidates.append(pr)
    return sorted(candidates, key=lambda pr: pr["number"])


def _merge_pending(runner, worktree):
    """Report whether the worktree holds a merge that still needs a commit."""
    pending = runner(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                     cwd=worktree, capture_output=True, text=True)
    return pending.returncode == 0


def union_merge(repo_dir, branch, base_ref, union_files, runner=subprocess.run):
    """Merge ``base_ref`` into ``branch`` in a disposable detached worktree.

    The caller is responsible for updating a remote ref with an appropriate
    lease after inspecting the returned commit SHA.
    """
    repo_dir = os.path.abspath(repo_dir)
    with tempfile.TemporaryDirectory(prefix="openswarm-union-", dir=repo_dir) as temp_root:
        worktree = os.path.join(temp_root, "worktree")
        added = False
        try:
            result = runner(["git", "worktree", "add", "--detach", worktree, branch],
                            cwd=repo_dir, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
            added = True
            original = runner(["git", "rev-parse", "HEAD"], cwd=worktree,
                              capture_output=True, text=True).stdout.strip()
            merge = runner(["git", "merge", "--no-commit", base_ref], cwd=worktree,
                           capture_output=True, text=True)
            unmerged = runner(["git", "diff", "--name-only", "--diff-filter=U"],
                              cwd=worktree, capture_output=True, text=True)
            conflicts = sorted(path for path in unmerged.stdout.splitlines() if path)
            head = runner(["git", "rev-parse", "HEAD"], cwd=worktree,
                          capture_output=True, text=True).stdout.strip()
            if merge.returncode != 0 and not conflicts:
                # The merge failed for a reason other than a content conflict
                # (unknown base ref, unrelated histories, ...). Report the real
                # cause instead of pretending the result was a mixed conflict.
                message = (merge.stderr or merge.stdout).strip()
                return {"status": "error", "error": message or "git merge failed"}
            if not conflicts:
                if head != original:
                    # Fast-forward: the merge already moved HEAD onto the base
                    # commit, so there is no merge commit left to create.
                    return {"status": "merged", "sha": head}
                if not _merge_pending(runner, worktree):
                    # Base is already an ancestor of the branch: nothing to merge.
                    return {"status": "noop"}
            non_union = [path for path in conflicts if not _is_union_file(path, union_files)]
            if non_union:
                runner(["git", "merge", "--abort"], cwd=worktree,
                       capture_output=True, text=True)
                return {"status": "mixed", "files": non_union}

            for path in conflicts:
                stages = []
                for stage in (1, 2, 3):
                    stage_result = runner(["git", "show", ":{}:{}".format(stage, path)],
                                          cwd=worktree, capture_output=True, text=True)
                    if stage_result.returncode:
                        runner(["git", "merge", "--abort"], cwd=worktree,
                               capture_output=True, text=True)
                        return {"status": "mixed", "files": [path]}
                    stages.append(stage_result.stdout)
                with tempfile.TemporaryDirectory(prefix="union-file-", dir=temp_root) as scratch:
                    names = []
                    for index, content in enumerate(stages):
                        name = os.path.join(scratch, str(index))
                        with open(name, "w", encoding="utf-8", newline="") as stream:
                            stream.write(content)
                        names.append(name)
                    # git merge-file expects current, ancestor, other (2, 1, 3).
                    merge_files = [names[1], names[0], names[2]]
                    combined = runner(["git", "merge-file", "--union", "-p"] + merge_files,
                                      cwd=worktree, capture_output=True, text=True)
                if combined.returncode not in (0, 1):
                    runner(["git", "merge", "--abort"], cwd=worktree,
                           capture_output=True, text=True)
                    return {"status": "mixed", "files": [path]}
                destination = os.path.join(worktree, path)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "w", encoding="utf-8", newline="") as stream:
                    stream.write(combined.stdout)
                staged = runner(["git", "add", "--", path], cwd=worktree,
                                capture_output=True, text=True)
                if staged.returncode:
                    runner(["git", "merge", "--abort"], cwd=worktree,
                           capture_output=True, text=True)
                    return {"status": "mixed", "files": [path]}

            committed = runner(["git", "-c", "user.name=OpenSwarm", "-c",
                                "user.email=openswarm@example.invalid", "commit", "--no-edit"],
                               cwd=worktree, capture_output=True, text=True)
            if committed.returncode:
                runner(["git", "merge", "--abort"], cwd=worktree,
                       capture_output=True, text=True)
                return {"status": "mixed", "files": conflicts}
            sha = runner(["git", "rev-parse", "HEAD"], cwd=worktree,
                         capture_output=True, text=True).stdout.strip()
            return {"status": "merged", "sha": sha}
        finally:
            if added:
                runner(["git", "worktree", "remove", "--force", worktree],
                       cwd=repo_dir, capture_output=True, text=True)
