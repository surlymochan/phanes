# swrm GitHub issue bus

> The project was renamed from swrm to **swrm** in v0.3.1. The bus wire format
> deliberately keeps its `swrm` identifiers (`swrm:` labels, the
> `swrm-task` / `swrm-result` blocks, branch prefix `swrm/`) so cards
> and workers created before the rename keep interoperating.

Use the issue bus only for executors outside the controlled fleet, such as another person's agent, a cloud agent, or an agent on an unmanaged host. For hosts you control, use SSH fleet lanes so execution stays on the known machines.

## Setup

Create a dedicated GitHub repository for task cards. The planner host and every worker need GitHub CLI (`gh`) authentication with permission to read and comment on bus issues. Workers also need permission to clone the target repository and push result branches there.

Initialize labels, an issue template, and this protocol README:

```sh
swrm bus init <bus-owner>/<bus-repo>
```

Configure `policy.local.json` under `$OPENSWARM_HOME`:

```json
{
  "bus": {
    "repo": "<bus-owner>/<bus-repo>",
    "allowed_executors": ["<github-login>"],
    "allowed_planners": ["<planner-github-login>"],
    "worker_repos": ["<target-owner>/<target-repo>"],
    "worker_verify_allow": ["<exact-verify-command>"],
    "poll_seconds": 15,
    "reserve_ttl_seconds": 300,
    "labels_prefix": "swrm",
    "open_pr": false
  }
}
```

`allowed_executors` is the allowlist of GitHub logins. Workers can be started on an executor host with:

```sh
swrm bus-worker --bus <bus-owner>/<bus-repo> --name <executor-name> --backend codex --once
```

Pass `--repos owner/repo[,owner/repo]` to set a worker-local target repository allowlist, or configure `bus.worker_repos`; with neither configured the worker refuses every task. Omit `--once` for a resident worker. `--backend` may be `codex`, `ocgo`, `deepseek`, `cursor`, or `pi`; `--model` is optional. A task's `verify` command runs only when it exactly matches `worker_verify_allow` or the worker's own `--verify`; other task commands are ignored and reported in the result followups. Set `open_pr` to `true` to have the worker open a draft PR after pushing its branch.

Dispatch a task from a target repository whose `origin` is its GitHub remote. The selected base commit must already be advertised by a remote branch or tag; push it first if necessary:

```sh
swrm run --backend github-issue --cwd <target-repo> --base <commit> \
  --budget-minutes 40 --verify '<acceptance-command>' --task-file task.md
```

Use `--detach` for a background planner run and `swrm watch <run-id>` to follow it. `swrm bus status` shows open issue state, claimant, and age.

## Task card protocol

An issue is one task card. Its title is `[swrm] <run title>`. The task description is followed by a fenced `swrm-task` JSON block:

```json
{
  "run_id": "<run-id>",
  "repo": "<target-owner>/<target-repo>",
  "base": "<base-commit-sha>",
  "branch_hint": "swrm/<run-id>",
  "verify": "<optional acceptance command>",
  "budget_minutes": 40,
  "report_schema": {"type": "object"}
}
```

The state labels are `swrm:queued`, `swrm:claimed`, `swrm:done`, and `swrm:failed`; they are display labels and do not authorize planner state transitions. The planner acts on `swrm:cancelled` only as a stop signal. An optional `executor:<name>` label routes a card to one worker name. A worker claims by adding `swrm:claimed` and commenting `/claim <executor-name>`. The earliest `/claim` comment authored by an allowlisted GitHub account wins. The planner posts an `swrm-task-digest: sha256:<hex>` first comment over the canonical task JSON.

The worker checks out the requested base, runs swrm's regular local run machinery with a hard budget and optional verify command, pushes `swrm/<run-id>` to the target repository, then posts a fenced `swrm-result` JSON comment:

```json
{
  "status": "done",
  "summary": "<summary>",
  "files_changed": ["<path>"],
  "tests": "<commands and outcomes>",
  "followups": [],
  "branch": "swrm/<run-id>",
  "commit": "<commit-sha>",
  "pr_url": null
}
```

`status`, `summary`, `files_changed`, `tests`, and `followups` follow swrm's regular result schema. The bus result adds `branch`, `commit`, and optional `pr_url`. The planner takes the terminal status from a valid `swrm-result` comment authored by the winning claimer, regardless of done or failed labels. It fetches the reported result branch, verifies it locally when requested, writes the usual run result and ledger entry, and never merges or pushes to the target repository. The worker always pushes `swrm/<run_id>`; `branch_hint` is informational and ignored.

Failed results may include an `error_class`. `executor_start_failed` means the executor exited before producing a stable turn; it is distinct from `exec_error`, which indicates a failure after startup.

## Reservation receipt and card state

A worker reserves a card before it clones or verifies anything, so the dispatcher can see that a worker picked the card up instead of assuming nobody claimed it. The worker posts one machine-marked comment right after it accepts the card and before its `/claim` comment:

```html
<!-- swrm-bus:reserve worker=<executor-name> at=<iso-8601-utc> -->
```

The receipt is visibility only: it never decides who wins a card, and the claim rule stays exactly as before — the earliest `/claim` comment from an allowlisted account wins.

`swrm bus status` reports the receipt and the card state:

- `reserved_at`: the timestamp of the latest trusted reservation receipt, `null` when there is none. Historical timestamps remain visible after the freshness window expires, including after claim and completion.
- `reserve_latency_seconds`: the seconds between the card creation and that receipt.
- the state column: `queued` (no claim or fresh receipt), `reserved` (a worker left a fresh receipt, no claim yet), `claimed` (an allowlisted worker claimed it and is preparing to execute), `running` (the winning worker started its execution process, before reporting a result), `done` (the winning worker reported its result or the card has a terminal label).

After cloning and preparing the command, the winning worker posts a start receipt as soon as its execution process starts:

```html
<!-- swrm-bus:running claim=<winning-claim-comment-id> at=<iso-8601-utc> -->
```

Only a start receipt authored by the winning claimer and referencing that claim's comment ID with a valid timestamp changes the state to `running`. Other workers' start receipts are ignored. Older workers without this receipt remain `claimed` until completion. Receipt freshness controls the `reserved` state and withdrawal protection only; it does not erase `reserved_at` or `reserve_latency_seconds`.

Only receipts written by `bus.allowed_executors` are trusted, exactly like claims: a receipt from any other account is ignored and never holds a card.

## Withdrawal guard

Before the planner withdraws or cancels a card it reads the receipts and the claims on it. A fresh reservation (within `bus.reserve_ttl_seconds`, 300 seconds by default) or an existing claim holds the card: the withdrawal is refused and the planner is told to repeat the command with `--force`. With `--force` the card is cancelled anyway and the cancellation comment records the override.

```sh
swrm stop <run-id>            # refused while a worker holds the card
swrm stop <run-id> --force    # cancels the card and records the override
```

A receipt that is older than the freshness window no longer holds the card, and a receipt or claim from an account outside `bus.allowed_executors` never holds it.

Planner budget expiry follows the same guard and never forces a withdrawal. The local run ends with `budget_exceeded`; if a worker holds the card, its labels and comments are left intact and the refusal (including the `--force` hint) is recorded in the run events. An unheld card is cancelled as usual.

## Trust and cancellation

Issue content and comments are untrusted input. The worker accepts a card only when its issue author and first digest comment are both from `bus.allowed_planners` and the digest matches the task JSON in the current body. Workers require a target repository in their worker-local `--repos` or `bus.worker_repos` allowlist. The planner accepts claims and results only from `bus.allowed_executors`; only the earliest allowed claimer's result is used. The planner treats comments as data, never runs comment text, validates the result fields and branch name, and checks the fetched commit against the reported SHA. Workers must stop when an issue is labelled `swrm:cancelled`.

Do not put secrets in issue bodies or comments. Do not grant bus workers more target repository access than needed to push their task branch. The planner never merges PRs and never pushes to the target repository itself. Revoke executor access when a worker is retired.
