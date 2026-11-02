# Review gate

## Offline branch-protection check

`lib/review/protection.py` checks a saved GitHub branch-protection JSON response.
It verifies that the configured review status (default `lane-review`) is required,
using both legacy `required_status_checks.contexts` and modern `checks[].context`,
and that `enforce_admins.enabled` is true. Missing protection, a missing context,
or administrators being exempt produces a warning and exit code 1. A compliant
snapshot exits 0; invalid input exits 2. This is an offline snapshot check, not a
live query or a check of rulesets and other GitHub policies.

```sh
# Fetch a snapshot separately, then inspect it offline.
gh api repos/OWNER/REPO/branches/main/protection > protection.json
python3 openswarm/lib/review/protection.py --json protection.json --repo OWNER/REPO --branch main

# If the branch has no protection, explicitly supply null.
echo null | python3 openswarm/lib/review/protection.py --json -
```

`--context NAME` selects a different required status. On warnings the CLI prints a
suggested `gh api -X PUT ... --input -` command, but never executes it. Supply the
actual `--repo` and `--branch` before using the suggestion; the default repository
is the placeholder `OWNER/REPO`. The JSON adds the context and enforces admins,
preserving existing check contexts/app bindings, review requirements, push actor
restrictions and supported boolean protection settings. Read-only API response
fields are omitted from the PUT payload. Review the command against a fresh
snapshot before applying it. The checker is independent of doctor wiring.

## A new head requires a new review

**新 head = 未审。** Any operation that changes a PR head, including rebase, a CI
fix, or union-merge, produces a new commit that needs another review. A successful
review status on the previous SHA does not approve the new SHA. Keep the PR out
of merge until the review and required checks apply to its current head.

## 审查车道按积压分配（Refs #1701）

2026-11-02 晚实测：审查只落在 CX2/CX4/CX5 三条 terra 车道上，派发时审查任务常堆在同一条车道、另两条空闲，
PR 排队等审、修复轮次被拖长。审查是合并管线的瓶颈，均衡能直接缩短 PR 从完成到合并的耗时。

现在派发审查时按**积压**分配：

- **积压（backlog）**：一条车道还欠着的审查数 = 正在审的 PR 数 + 审查失败、等待重试的 PR 数。
  欠账记在 PR 的审查记录里（`reviews.json` 的 `pending_lane`），审查出结论或 PR 离开审查管线时释放。
- **选车道**：在候选（空闲且允许审查的车道）里取积压最少者；积压相同时轮询——上一次审查等得最久的车道先上。
  审查轮转单独记在 `review-lru.json`，不再与任务派发共用 `lane-lru.json`：共用时，只做审查的车道计数最旧，
  会被反复选中，这正是当晚单车道连审的成因。
- **在途上限**：`review.lane_max`（默认 1）是一条车道同时可持有的审查数。到达上限的车道被跳过，
  PR 宁可排队等空闲车道；若所有候选车道都在上限上，则由积压最少的那条兜底（会写日志），避免管线卡死。
- **日志**：每次审查派发写一行，含 PR、所选车道和当时各审查车道的积压快照，事后可复核：

  ```
  review dispatched PR #1610 (head 1a2b3c4d, author CX4) → CX2 (govpn2/codex) run r-1610 [backlog CX2=0 CX4=1 CX5=0]
  ```

配置（dispatch 配置的 `review` 段）：

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `lane_max` | 1 | 单车道同时可持有的审查数（0 = 不设上限） |
| `lanes` | `[]` | 允许审查的车道（空 = 所有空闲车道都可以） |

实现：`openswarm/lib/dispatch/review_balance.py`（纯模块：积压统计、最少积压排序、上限判断、欠账释放）
与 `openswarm/lib/dispatch/engine.py` 的 `pick_reviewer` / `dispatch_reviews` / `finish_review`；
测试：`openswarm/tests/test_dispatch_review_balance.py`（含当晚审查序列回放）；
离线回放：`python3 tools/check-review-balance.py`（对比旧/新选车道结果，输出每条车道的审查数）。
