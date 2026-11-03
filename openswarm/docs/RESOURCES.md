# Resource pressure and cleanup planning

## Inode watermarks

The inode policy uses hysteresis: at **95% used**, stop dispatching new work;
recover when usage falls to **92% or below**. **90% used** is an earlier cleanup
trigger so an operator can act before dispatch stops. These are inode thresholds,
not byte-usage percentages. The dispatcher probes local hosts with `statvfs` and
remote hosts with `df -Pi <path>`, and keeps each host's pause state in memory.
`df -Pi <path>` reports the filesystem that holds `<path>`, so a host whose work
lives on a non-root mount (`/home`, `/data`, …) is guarded as well. Configure
the defaults under `resources.inode.pause_pct` (95), `resume_pct` (92), and
`cleanup_pct` (90); `path` and `probe_interval_s` optionally select the filesystem
and probe cadence.

The guard needs no marker file: a host without free inodes cannot create one, so
the decision is made in memory. A failing log or ledger write never breaks the
decision — the host stays paused even when the disk cannot take the report.
State transitions are logged and written to the dispatch ledger
(`ledger_file`, by default `<state_dir>/dispatch-ledger.jsonl`) as
`host_inode_pause` and `host_inode_resume` events.

## Cleanup hook

At `cleanup_pct` (90% used) the dispatcher runs the cleanup hook once per
pressure episode; the hook runs again after the usage falls below the watermark
and rises again. A hook that fails (it cannot start, it times out, it exits
non-zero) never breaks the dispatch decision: the failure is recorded and the
hook is retried after `retry_s` seconds (300 by default), so one broken hook
does not silence the cleanup for the whole episode. The hook is off by default
and configured under `resources.inode.cleanup_hook`:

```json
{"resources": {"inode": {"cleanup_hook": {
  "enabled": true,
  "command": [],
  "roots": {"completed_run_dir": ["~/.claude/openswarm/runs"],
            "closed_worktree": ["~/.claude/openswarm-executor-candidate/lanes"],
            "pkg_cache": ["~/.bun/install/cache"]},
  "rules": {}}}}}
```

* `command` replaces the built-in cleanup with an external command; the
  dispatcher runs it and records its exit code in the ledger.
* `roots` are the allowlisted container directories of the built-in cleanup.
  Nothing outside them is ever touched.
* `rules` overrides the per-kind thresholds of the table below.
* `timeout_s` (300) bounds the external command, `retry_s` (300) is the delay
  before a failed hook is tried again.

The built-in cleanup removes completed run directories (a run whose recorded
state is terminal), closed worktrees, and package manager caches. A worktree is
closed when it carries the `.openswarm-closed` marker, when its lane is no
longer configured (the lane was shut down), or when its branch is already
merged into `base_branch` — the work was delivered, so the checkout is
disposable. A lane directory holds its worktree in `wt`
(`<home>/lanes/<lane>/wt`), and the scan resolves that layout.

The worktree of a busy lane is never removed. The dispatcher collects the busy
paths from the real lane paths of the running tick (`<home>/lanes/<lane>/wt`
plus the run directory and the shared checkout), collects them again when the
hook runs, and re-checks them, together with the allowlist, immediately before
every removal. A lane that the dispatcher believes to be idle but whose own
state file (`<home>/lanes/<lane>/state.json`) records a run in flight is busy
as well: the lane writes `current_run` there while it works.

The built-in cleanup only reaches the filesystem of the host it runs on; clean
a remote host with the `command` hook. A dry-run tick (`--dry-run`) never
deletes a directory and never writes the ledger: it records the paths it would
remove and the ledger event in the dispatch plan.

## Default cleanup rules

| Kind | Eligible when | Rule override |
| --- | --- | --- |
| `run_dir` | mtime age exceeds 3 days | `run_dir` in seconds |
| `worktree_node_modules` | mtime age exceeds 12 hours | `worktree_node_modules` in seconds |
| `pkg_cache` | immediate entry count exceeds 20,000 | `pkg_cache` as an entry count |
| `tmp_dir` | mtime age exceeds 48 hours | `tmp_dir` in seconds |
| `completed_run_dir` | mtime age exceeds 3 days, run state is terminal | `completed_run_dir` in seconds |
| `closed_worktree` | mtime age exceeds 12 hours, worktree is closed | `closed_worktree` in seconds |

Thresholds are strict: equality does not qualify. Directory mtime is an age proxy,
not proof that a worktree is idle. Always supply the complete current busy-path
snapshot. Every busy path, ancestor and descendant is protected, regardless of
age or size. `rules["allow_roots"]` is required to permit any path. Containment is
checked on directory boundaries, both before and after resolving symlinks; a
lookalike prefix or a symlink escaping its allow root is excluded. Busy aliases
are protected too. Revalidate busy state and filesystem paths before any separate
consumer acts on a plan; this is a point-in-time plan, not a deletion authorization.

## API and CLI

Load `lib/resources/cleanup.py` by path, without an `__init__.py`.
`plan_cleanup(candidates, busy_paths, now, rules)` accepts epoch timestamps,
returns `{path, kind, reason}` records, and never changes the candidates or rules.
Override the keys in the table alongside `allow_roots` to customize thresholds.

`scan(roots, kinds)` considers immediate child directories of container roots.
Supply one kind for all roots, or a list of kinds aligned with roots. For
`worktree_node_modules`, each container root is an individual worktree and only
its immediate `node_modules` child is considered. The scanner skips symlink
children and plain files. Entry counts include just one level; nested directory
contents are not traversed. Inaccessible or disappearing entries cause an error
instead of silently returning a partial snapshot.

This module is the cleanup hook boundary: callers provide allowlisted roots,
candidate kinds, and a current busy-path snapshot, then consume the dry-run plan.
Every busy worktree and any overlapping candidate is excluded. The CLI itself
does not remove files.

`scan_hook(roots, lanes, repo, base)` scans the roots of the built-in hook and
returns the candidates of `completed_run_dir`, `closed_worktree`,
`worktree_node_modules`, `pkg_cache`, and `tmp_dir`. `lanes` is the set of the
lane names that are still configured, `repo` and `base` locate the repository
whose `base` branch decides whether a worktree's branch is merged.
`run_state(path)`, `run_completed(path)`, `worktree_closed(path, lanes, repo,
base)`, `worktree_branch(path)`, `branch_merged(path, repo, base)` and
`worktree_of(directory)` are the state checks the scan relies on.
`execute_cleanup(plan, allow_roots, busy_paths, repo)` is the only function
here that removes anything: it re-checks the allowlist and the busy paths
before every removal, prunes the removed worktrees from the repository, and
returns the removed paths.

```sh
python3 openswarm/lib/resources/cleanup.py --roots /srv/runs,/srv/old-tmp --busy /srv/runs/active --kinds run_dir,tmp_dir --json
python3 openswarm/lib/resources/cleanup.py --roots /srv/worktree --busy /srv/worktree --kinds worktree_node_modules
```

The CLI defaults to `run_dir`, uses its roots as the allowlist, and only prints a
dry-run plan (JSON with `--json`). It has no delete option. Successful planning
exits 0, including an empty plan; input/scan failures exit 2. The CLI is not
wired to the dispatcher: the dispatcher calls `scan_hook`, `plan_cleanup`, and
`execute_cleanup` itself, as described in the cleanup hook section above.
