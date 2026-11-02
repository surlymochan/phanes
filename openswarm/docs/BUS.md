# openswarm GitHub issue bus

Use the issue bus only for executors outside the controlled fleet, such as another person's agent, a cloud agent, or an agent on an unmanaged host. For hosts you control, use SSH fleet lanes so execution stays on the known machines.

## Setup

Create a dedicated GitHub repository for task cards. The planner host and every worker need GitHub CLI (`gh`) authentication with permission to read and comment on bus issues. Workers also need permission to clone the target repository and push result branches there.

Initialize labels, an issue template, and this protocol README:

```sh
openswarm bus init <bus-owner>/<bus-repo>
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
    "poll_seconds": 60,
    "poll_retries": 5,
    "max_backoff_seconds": 60,
    "claim_lease_seconds": 300,
    "labels_prefix": "openswarm",
    "open_pr": false
  }
}
```

`allowed_executors` is the allowlist of GitHub logins. Workers can be started on an executor host with:

```sh
openswarm bus-worker --bus <bus-owner>/<bus-repo> --name <executor-name> --backend codex --once
```

Pass `--repos owner/repo[,owner/repo]` to set a worker-local target repository allowlist, or configure `bus.worker_repos`; with neither configured the worker refuses every task. Use `--once` to claim at most one queued issue and exit; omit it to keep the worker in its resident polling loop. In resident mode the worker stops gracefully on SIGTERM or SIGINT: it finishes the task it is running, writes that task's result back to the bus and exits without claiming another card. Polling continues with capped exponential backoff on transient GitHub 429/5xx errors and fails with an explicit error after `bus.poll_retries` consecutive failures (default 5); the backoff never waits longer than `bus.max_backoff_seconds` (default 60). `--backend` may be `codex`, `ocgo`, `deepseek`, `cursor`, `agy`, or `pi`; `--model` is optional. A task's `verify` command runs only when it exactly matches `worker_verify_allow` or the worker's own `--verify`; other task commands are ignored and reported in the result followups. Set `open_pr` to `true` to have the worker open a draft PR after pushing its branch.

Dispatch a task from a target repository whose `origin` is its GitHub remote. The selected base commit must already be advertised by a remote branch or tag; push it first if necessary:

```sh
openswarm run --backend github-issue --cwd <target-repo> --base <commit> \
  --budget-minutes 40 --verify '<acceptance-command>' --task-file task.md
```

Use `--detach` for a background planner run and `openswarm watch <run-id>` to follow it. `openswarm bus status` shows open issue state, claimant, and age.

## Task card protocol

An issue is one task card. Its title is `[openswarm] <run title>`. The task description is followed by a fenced `openswarm-task` JSON block:

```json
{
  "run_id": "<run-id>",
  "repo": "<target-owner>/<target-repo>",
  "base": "<base-commit-sha>",
  "branch_hint": "openswarm/<run-id>",
  "verify": "<optional acceptance command>",
  "budget_minutes": 40,
  "report_schema": {"type": "object"}
}
```

The state labels are `openswarm:queued`, `openswarm:claimed`, `openswarm:done`, and `openswarm:failed`; they are display labels and do not authorize planner state transitions. The planner acts on `openswarm:cancelled` only as a stop signal. An optional `executor:<name>` label routes a card to one worker name. A worker claims by adding `openswarm:claimed` and commenting `/claim <executor-name>`. The active claim is a lease (300 seconds by default): the same worker name may renew an unexpired lease after restarting, while another worker can take over only after it expires (expired claims do not permanently lock / 过期 claim 不永久锁死). The earliest active `/claim` comment authored by an allowlisted GitHub account wins. The planner posts an `openswarm-task-digest: sha256:<hex>` first comment over the canonical task JSON; retrying that publication with the same digest is idempotent, and duplicate copies do not alter claim arbitration.

The worker checks out the requested base, runs openswarm's regular local run machinery with a hard budget and optional verify command, pushes `openswarm/<run-id>` to the target repository, then posts a fenced `openswarm-result` JSON comment:

After claiming, the worker reads the issue again before checkout. If the task JSON or base changed, it posts `/unclaim <executor-name>`, writes a failed result with `error_class` `task_changed` (any other task JSON change), `base_changed` (a different base commit), `digest_mismatch` (the planner digest no longer matches the task JSON), or `task_removed` (the task block is gone), and does not checkout or push. The failed result carries the card's `run_id`, so the planner reads it as a terminal failure of that round instead of polling until the budget expires. An unchanged card keeps its original digest and follows the normal claim, run, push, and result path.

```json
{
  "status": "done",
  "run_id": "<run-id>",
  "error_class": "none",
  "summary": "<summary>",
  "files_changed": ["<path>"],
  "tests": "<commands and outcomes>",
  "followups": [],
  "branch": "openswarm/<run-id>",
  "commit": "<commit-sha>",
  "pr_url": null
}
```

Successful results use `status: "done"` and include a non-empty `run_id`. Failed results use a machine-readable failure status (`"partial"` or `"failed"`) and include a non-empty `error_class` so they are actionable without parsing prose. Card drift uses `task_changed`, `base_changed`, `digest_mismatch`, or `task_removed`.

When the worker ran an allowed verify command, the result comment also carries a `verify` object with the `cmd`, its exit code `rc`, and the captured `tail` (stdout/stderr summary), and the same evidence is appended to `tests`. A non-zero exit makes the result `failed` with `error_class: "verify_failed"`; the pushed `branch`, `commit`, and verify evidence stay in the comment, and the local run result and ledger row are aligned with that terminal status. A zero exit keeps `status: "done"`.

The result comment is a machine-readable contract: it must contain `status=<done|partial|failed>` and a stable `error_class` enum (JSON keys are `"status"` and `"error_class"`). An optional `resets_at` is an ISO8601 UTC timestamp ending in `Z`, allowed only with `error_class: "rate_limit"`; other error classes or malformed timestamps are rejected. After a worker writes a `rate_limit` result, it cools that backend for `bus.rate_limit_cooldown_s` seconds (default 300) and records the resulting instant as `resets_at`. During the cooldown the worker does not claim on that backend, including later issues in the same batch; `--once` returns `cooling` (printed as `{"status": "cooling"}`) without claiming. The enum is `none` for successful results, or one of `auth`, `base_unavailable`, `budget_exceeded`, `budget_exceeded_daily`, `bus_error`, `claim_task_mutated`, `compare_runner_failed`, `exec_error`, `executor_failed`, `executor_io_lost`, `model_unavailable`, `network`, `non-fast-forward`, `no_report`, `prepare_failed`, `protocol_error`, `rate_limit`, `repo-missing`, `risk_blocked`, `stopped`, `upstream_forbidden`, `verify_failed`, `worker_crashed`, `worktree_unavailable`, `task_changed`, `base_changed`, `digest_mismatch`, and `task_removed` for failures. Both success and failure comments must include `error_class`; missing fields or values outside this enum are rejected. `status`, `summary`, `files_changed`, `tests`, and `followups` follow openswarm's regular result schema. The bus result adds `branch`, `commit`, and optional `pr_url`. Each result also carries `claim_round` and an `idempotency_key` derived from the issue number, winning `/claim` comment id, and run id. The planner accepts a result only when both fields match the current winning claim; a delayed result from an expired or earlier claim round is ignored, so a prior round cannot override a takeover. Repeating the same result with the same key is idempotent. The planner takes the terminal status from a valid `openswarm-result` comment authored by the winning claimer, regardless of done or failed labels. It fetches the reported result branch, verifies it locally when requested, writes the usual run result and ledger entry, and never merges or pushes to the target repository. The worker always pushes `openswarm/<run_id>`; `branch_hint` is informational and ignored.

After claiming, the worker snapshots the canonical task JSON and its planner digest on every poll. If the issue task JSON or planner digest changes, the worker aborts the child run, does not push a branch, and posts a failed result with `error_class: "claim_task_mutated"`.

### Worker 崩溃与受控重启 / Worker crash and controlled restart

常驻 worker 异常退出或崩溃后，受控重启必须保留未完成的 claim 状态，且不丢 in-flight claim 标记：
- **In-flight claim marker / 运行中认领标记**: 任务认领与执行期间，worker 在本地 claim 目录（`DATA/bus-worker/<run_id>/claim.json`）维护 in-flight 状态。重启过程不得丢弃或丢失此 in-flight 标记。
- **未完成 claim 处理 / Unfinished claim reconciliation**: 常驻 worker 重启后应扫描识别未完成的 in-flight claim。依据策略写 error（发表包含 `error_class: "worker_crashed"` 的失败结果评论并标记 failed）或续跑（复用未过期 lease 继续执行，避免丢弃已有工作进度）。
- **不得双开同 claim / Prevent duplicate claim execution**: 重启或重入时严格禁止对同一 in-flight claim 并发拉起两个运行进程。未完成 claim 必须在受控恢复（写 error 或续跑）确认前拒绝重复认领与双开。
- **过期 claim 不永久锁死 / Expired claim lease takeover**: Claim 具有有效租约（默认 300 秒）。若持有 claim 的 worker 崩溃或未续约导致租约过期，过期 claim 不永久锁死，其它白名单 worker 判定原租约失效后可发表新的 `/claim <worker>` 接管任务。

## Trust and cancellation

Issue content and comments are untrusted input. The worker accepts a card only when its issue author and first digest comment are both from `bus.allowed_planners` and the digest matches the task JSON in the current body. Workers require a target repository in their worker-local `--repos` or `bus.worker_repos` allowlist. The planner accepts claims and results only from `bus.allowed_executors`; only the earliest allowed claimer's result is used. The planner treats comments as data, never runs comment text, validates the result fields and branch name, and checks the fetched commit against the reported SHA. Workers must stop when an issue is labelled `openswarm:cancelled`.

Do not put secrets in issue bodies or comments. Do not grant bus workers more target repository access than needed to push their task branch. The planner never merges PRs and never pushes to the target repository itself. Revoke executor access when a worker is retired.

## 标签速查 / State label quick reference

| 状态标签 / State label | 说明 / Description | Planner 责任 / Planner responsibility | Worker 责任 / Worker responsibility |
|---|---|---|---|
| `openswarm:queued` | **排队中 / Queued**<br>任务卡已创建，等待执行端认领。<br>Task card created, waiting for an executor. | 创建 Issue 并贴上初始标签；发表 `openswarm-task-digest` 任务摘要首评。<br>Creates the issue with this initial label; posts the `openswarm-task-digest` first comment. | 轮询可认领任务；校验 Planner 作者身份与任务摘要有效性。<br>Polls for available tasks; verifies planner authorship and task digest validity before claiming. |
| `openswarm:claimed` | **已认领 / Claimed**<br>执行端已认领任务，正在准备或执行中。<br>Claimed by a worker; execution underway. | 监听 `/claim <name>` 评论，裁定最早发表有效认领的白名单执行者胜出；租约过期后允许其它 worker 接管（过期 claim 不永久锁死）。<br>Monitors `/claim` comments; arbitrates earliest allowlisted claimant as the winner; allows other workers to take over after lease expiry without deadlock. | 移除 `queued` 并添加 `claimed`，发表 `/claim` 评论，确认胜出后拉取基线启动执行；未过期时重启可续约，租约过期后可被其它 worker 接管；claim 后卡片被改动时发表 `/unclaim <executor-name>` 撤销认领。<br>Replaces `queued` with `claimed`, posts `/claim`, and begins local run upon winning the claim; unexpired lease can be renewed on restart, other workers can take over when expired, and a worker whose card changed after the claim posts `/unclaim <executor-name>` to release it. |
| `openswarm:done` | **已完成 / Done**<br>执行成功并推送结果分支。<br>Worker completed successfully and pushed result branch. | 状态展示标签；以 Worker 发表的 `openswarm-result` 评论为准拉取分支、进行本地验证并记录账本。<br>Display label only; reads terminal status from `openswarm-result` comment, fetches branch, verifies locally, and writes ledger. | 运行成功并推送分支（及可选 PR）后，发表结果评论，移除 `queued`/`claimed` 并添加 `done`。<br>Pushes result branch (and optional PR), posts `openswarm-result` comment, and replaces active labels with `done`. |
| `openswarm:failed` | **失败 / Failed**<br>任务执行失败、发生异常或推送失败。<br>Task execution, verification, or push failed. | 状态展示标签；依据 `openswarm-result` 评论记录失败状态与错误摘要至运行日志与账本。<br>Display label only; records failure status and error summary from `openswarm-result` comment into run logs and ledger. | 发生未捕获异常、测试失败或分支推送失败时，发表失败结果评论，移除 `queued`/`claimed` 并添加 `failed`；claim 后卡片被改动时先用 `/unclaim <executor-name>` 撤销认领，再发表带 `error_class` 的失败结果且不 checkout/push。<br>On run error, uncaught exception, or push failure, posts `openswarm-result` comment and marks issue with `failed`; when the card changed after the claim the worker first posts `/unclaim <executor-name>`, then a failed result with a classifiable `error_class`, without checkout or push. |
| `openswarm:cancelled` | **已取消 / Cancelled**<br>任务已被终止。<br>Run cancelled or aborted. | 停止信号；附上此标签，移除其他状态标签，并评论取消原因。<br>Stop signal; attaches `cancelled`, removes active state labels, and comments cancellation reason. | 轮询检测到此标签时必须立即终止正在运行的任务子进程并退出。<br>Polls for this label during execution; must immediately terminate running subprocesses and abort when detected. |
