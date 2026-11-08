# Issue #1872: writable Git metadata in executor worktrees

## Evidence

The regression probe in `swrm/tests/test_workspace.py` exercises `git add` and
`git update-ref` in the checked-out worktree. The reported host reproduction
failed both operations with `Read-only file system`: `git add` attempted to
create `index.lock` under `/root/work/phanes.git/worktrees/<name>/`, and
`git update-ref` could not create a ref under `/root/work/phanes.git/refs/`.
This confirms the worktree's source directory being writable is insufficient;
its `.git` file resolves to metadata outside the sandbox's writable roots.

In the isolated Pi wrapper, the worktree is bind-mounted writable, but the
Git common directory was explicitly mounted read-only. That common directory
contains both `worktrees/<name>/index` and shared refs/objects. The wrapper now
bind-mounts that directory writable; its existing namespace, credential, and
network isolation remain in place. `test_linked_git_worktree_and_node_remain_available`
now also verifies staging and ref updates from within the isolated Pi process.

The same path layout is used by other executor lanes, but their effective
sandbox mounts are backend-specific. The current-worktree regression test is
the host acceptance probe for those lanes. Compare successful and failed run
mounts/environments before claiming those backends are fixed; this change does
not alter Codex or other backend sandbox configuration. No review or test
acceptance criteria were changed.
