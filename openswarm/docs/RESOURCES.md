# Resource pressure and cleanup planning

## Inode watermarks

The inode policy uses hysteresis: at **95% used**, stop dispatching new work;
recover when usage falls to **92% or below**. **90% used** is an earlier cleanup
trigger so an operator can act before dispatch stops. These are inode thresholds,
not byte-usage percentages. This planner does not dispatch, monitor pressure, or
perform cleanup; dispatch policy and execution are separate responsibilities.

## Default cleanup rules

| Kind | Eligible when | Rule override |
| --- | --- | --- |
| `run_dir` | mtime age exceeds 3 days | `run_dir` in seconds |
| `worktree_node_modules` | mtime age exceeds 12 hours | `worktree_node_modules` in seconds |
| `pkg_cache` | immediate entry count exceeds 20,000 | `pkg_cache` as an entry count |
| `tmp_dir` | mtime age exceeds 48 hours | `tmp_dir` in seconds |

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

```sh
python3 openswarm/lib/resources/cleanup.py --roots /srv/runs,/srv/old-tmp --busy /srv/runs/active --kinds run_dir,tmp_dir --json
python3 openswarm/lib/resources/cleanup.py --roots /srv/worktree --busy /srv/worktree --kinds worktree_node_modules
```

The CLI defaults to `run_dir`, uses its roots as the allowlist, and only prints a
dry-run plan (JSON with `--json`). It has no delete option. Successful planning
exits 0, including an empty plan; input/scan failures exit 2. No CLI wiring or
dispatch-engine changes are included in this module.
