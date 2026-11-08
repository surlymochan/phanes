# Issue #1872: writable Git metadata in executor worktrees

## Confirmed evidence

The retained regression probe in `swrm/tests/test_workspace.py` exercises
`git add` and `git update-ref` in the checkout running the test, using the same
worktree and executor environment for both operations. It always prints the
return code, worktree, resolved index/ref path and stderr, including on success.
The reported failed reproduction returned 128 for both operations:
`index.lock` was under `/root/work/phanes.git/worktrees/<name>/`, and the ref
was under `/root/work/phanes.git/refs/`. Both errors reported a read-only file
system. A writable source directory alone does not make its external Git
metadata writable.

In `swrm/bin/pi-isolated`, the worktree is mounted with `--bind`, while its
Git common directory is mounted with `--ro-bind`. A linked worktree's index is
inside that common directory under `worktrees/<name>/`; refs and objects are
also in the common directory. The isolated Pi integration probe checks that
Git reads still work and both metadata writes fail under this existing policy.
This change preserves that policy; it does not grant additional writable roots.

The revision probe in the current executor returned `git add rc=128` with
`Read-only file system` at
`/root/work/phanes/.git/worktrees/-swrm-wt-phanes-20261108-205520-4a492a/index.lock`,
while `git update-ref rc=0` at
`/root/work/phanes/.git/refs/l2-1872-probe/<pid>`.
This confirms that index and shared-ref permissions can differ even within one
run; a successful ref update does not establish writable worktree metadata.

## Pending evidence: successful versus failed runs

The supplied failed-run evidence identifies M20 run `20261104-003334-8ffc6a`
and M17 run `20261104-012635-00f292`, but supplies no effective mount manifests
or successful run with a nonempty `diff.patch`. No archived run artifacts were
found in this checkout. A historical mount/environment comparison therefore
remains unconfirmed, including the effective Codex writable roots. Do not infer
error counts from prompt/card text matches or claim all lanes share a proven
cause.

For a host comparison, retain a successful run's nonempty `diff.patch` and a
failed run's actual Git stderr. Run the acceptance probe inside each lane's
original sandbox and worktree; capture its output together with the lane/run ID,
resolved `--absolute-git-dir` and `--git-common-dir`, bwrap arguments or Codex
writable-root configuration, and `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`
and `GIT_WORK_TREE` if set. Compare whether each resolved metadata path falls
under an effective writable mount. A host-only passing probe confirms host
permissions, not the permissions of the original lane sandbox.

## Proposed design (not implemented here)

Prefer control-plane Git finalization outside the executor sandbox. Keep the
executor's source edits in its writable worktree, collect its diff, then have
the controller stage, verify and commit in that same worktree after the executor
exits. Serialize index/ref operations per worktree and check the expected base
before finalization. Record Git return codes and resolved paths in run artifacts;
propagate failures without reporting a commit. Existing verification and review
gates still apply before a run can succeed.

An alternative is to create the worktree Git metadata inside an already writable
root. Moving only `worktrees/<name>/index` is insufficient: commits also write
objects and shared refs. That alternative needs a separately reviewed metadata
layout/lifecycle design, including cleanup and concurrent worktrees, before any
implementation or sandbox policy change.

## Acceptance

Run from the checkout, with temporary files kept inside the workspace:

```sh
mkdir -p .swrm-tmp
TMPDIR="$PWD/.swrm-tmp" PYTHONPATH=swrm/tests python3 -m unittest test_workspace.WorkspaceTest.test_current_worktree_git_metadata_is_writable
TMPDIR="$PWD/.swrm-tmp" PYTHONPATH=swrm/tests python3 -m unittest test_workspace test_pi_isolation
git diff --check
```

The current-worktree probe must pass on a host with writable Git metadata; it
remains a failing diagnostic in an executor whose metadata is read-only. Run the
Pi tests on a Linux host with bubblewrap, socat and namespace/socket support.
The legacy `openswarm/bin/pi-isolated` maps to `swrm/bin/pi-isolated`; workspace
behavior from `openswarm/bin/openswarm` is covered by `swrm/tests/test_workspace.py`.
