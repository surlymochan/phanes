# External lanes (GitHub-only executors)

An external lane is an executor that can read and write the repository on GitHub but cannot be reached over SSH. It
pulls work itself, while fleet lanes have work pushed to them. The issue-label contract and the agent SDK live in
[`sdk/external-lane`](../sdk/external-lane/README.md). This page covers the dispatcher side.

## Configure

`policy.local.json`:

```json
{"external_backends": {"chatgpt-agent": {
  "repo": "owner/name", "max_inflight": 2, "claim_timeout_s": 21600, "max_requeues": 2,
  "auto_assign": true, "assign_from_backends": ["codex", "pi"],
  "assign_exclude_hot": ["openswarm/bin/openswarm"], "branch_prefixes": ["fleet/chatgpt-"]}}}
```

| Key | Meaning |
|-----|---------|
| `max_inflight` | Upper bound on cards labelled `backend:<name>` that are `lane-ready` or `lane-claimed`. Cards at the PR stage do not count. |
| `auto_assign` | Refill from `lane-ready` cards that are unpinned (or pinned to one of `assign_from_backends`), have an acceptance line, no `依赖`/`Depends on`, no rework marker, and no file in `assign_exclude_hot`. |
| `claim_timeout_s` | A `lane-claimed` card with no linked PR activity and no issue activity for this long goes back to `lane-ready`. After `max_requeues` requeues it becomes `lane-failed` + `needs-human` instead, so it does not flap. |
| `branch_prefixes` | Extra head-ref prefixes recognised as this backend's PRs. `fleet/<name>-` is always included. |

## Run

```bash
openswarm external init                # create backend:<name> and lane-claimed labels
openswarm external tick [--dry-run]    # one pass; run it from cron or a supervisor every few minutes
openswarm external status
openswarm external reserved            # {"skip_labels": [...]}: fleet dispatchers must not take these cards
```

Each pass tracks the backend's PRs by number. A PR is linked to an issue by `Closes #N`, or by
`fleet/<name>-issue<N>-…` when the body has no such line. The pass relabels the issue `lane-claimed` → `lane-pr` and posts a
commit status `external-lane/contract` on the head: `failure` for a missing link or a thin diff (fewer than 30
effective lines, or mostly docs), otherwise `success`. Review and merge stay with the normal lane review and merge
gate. A failing status keeps the PR out of review and merge until a new head fixes it.

Fleet dispatchers must treat every label from `reserved` as off-limits: never dispatch or cross-backend-fallback those
cards, and treat `lane-claimed` as in flight.
