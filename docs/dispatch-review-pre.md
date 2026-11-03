# 预审层：pi 产出先过 codex 卫生预审，再进 terra 正式审

Refs #1731（承接监督者方案第二部分第 3 条）。

正式审（terra）之前加一层**预审**：pi 后端产出的 PR 先派给 codex luna 车道做卫生预审，
预审挑出的问题由生产车道修复后重提，预审通过（或降级）之后 PR 才进入正式审。

实现位置：`openswarm/lib/dispatch/engine.py`（dispatch 引擎的 review 段）。

## 1. 谁走预审

* 只有 **pi** 后端产出的 PR 走预审（PR 分支 `fleet/<lane>-issue<n>-...` 的作者车道后端为 pi）。
* codex 产出**跳过**预审，直接进正式审。

## 2. 预审车道怎么选（lanes.json）

预审车道由 dispatch 的 `lanes.json`（默认 `<state_dir>/lanes.json`，形如
`{"<lane>": {"host": ..., "backend": ..., "model": ...}}`）里的 `review_pre` 标记选出：

```json
{"C9": {"host": "box", "backend": "codex", "model": "gpt-6-luna", "review_pre": true}}
```

规则：

* `review_pre` 为真值的车道才是预审车道；没标记的车道不会被派预审。
* `review.pre.backend`（默认 `codex`）与 `review.pre.model_re`（默认 `luna`）再筛一遍：
  预审只跑在 codex luna 车道上。车道没写 `model` 时按标记放行（标记本身就是显式意图）。
* **并发**由标记出来的车道池决定：每个车道一次只跑一个任务，所以池子多大就最多同时几个预审；
  `review.pre.max_concurrency`（默认 `0` = 不额外限制）可以再压一个上限。

## 3. 预审做什么

预审任务是正式审任务的窄化版本：只看主路径 bug、二进制/生成物卫生、验收映射是否完整、
有没有明显越界；不看风格。车道把结论写进 `.lane-review/PRE-PR-<pr>.md`：

```
VERDICT: APPROVE   (or VERDICT: CHANGES)
SUMMARY: one line
## Findings
- file:line findings
## Required changes
- only for CHANGES
```

* `APPROVE` → 下一 tick 该 PR 进入正式审。
* `CHANGES` → 把结论写进 issue 的 `## Lane pre-review requested changes` 段并贴 `lane-ready`，
  生产车道修完重推；新 head 会重新走一遍预审。

预审任务里的 diff 与正式审同源：`gh pr diff` 优先，GitHub 不可达时用 dispatcher 本地的对象重建
（PR 头 `refs/ad/pr-<n>` / `refs/heads/rebase-pr-<n>` / PR 列表里的 head sha，对 base 分支做三方
diff）。重建与失败都在 dispatch 日志里留痕，审查车道不会再拿到一个空 diff。

## 4. 预审不是正式审轮次

* 预审**不写** `reviews.json`：不写 `rounds`、不写 `fails`、不写 `verdict`，也不发 commit status、不贴标签。
* 预审自己的记录在 `<state_dir>/pre-reviews.json`（一个 PR 一条），字段：
  `sha` / `status` / `lane` / `run` / `round` / `fails` / `wait_since` / `changes_since`。
  `status` 取值：`waiting`（等车道）、`dispatched`（预审在跑）、`approved`、`changes`、
  `degraded`（超时降级）、`superseded`（正式审接管）。
* 正式审的 `reviews.json` 轮次计数完全不受预审影响（对照验证：预审跑完一轮后，
  `reviews.json` 里该 PR 仍没有 `rounds`）。

## 5. 正式审走哪条车道

正式审（terra）只跑在**声明了正式审模型的车道**上（`review.formal.model_re`，默认 `terra`，
按 `lanes.json` 里的 `model` 匹配）：

* 标记了 `review_pre` 的预审车道**永远不接**正式审；
* 车队里声明了 terra 车道时，正式审**只**在这些车道里选：terra 全忙就**等**它们，
  不会退到 pi 或其他非 terra 车道（dispatch 日志：`review PR #N: waiting for a formal review lane
  (terra); none of them is idle`）；
* 车队里**一条**符合配置的车道都没有时（老的 lanes.json 没有审车道路），正式审退回到
  “不是预审车道的空闲车道”，免得整条审阅流水线永远卡住（dispatch 日志：
  `review: no lane of the fleet states the formal review model (terra); the formal review falls back
  to the lanes that are not pre-review lanes`）。把 `review.formal.required` 设成 `true`
  可以关掉这个退路（那时车队必须自己声明审车道）。

## 6. 失败模式

| 失败模式 | 行为 | 留痕 |
|---|---|---|
| 预审车道全忙 | PR 等待，超时（`review.pre.timeout_s`，默认 900s）后**降级直进正式审** | dispatch 日志 `pre-review PR #N: ... → degraded to the formal review` + PR 评论 `lane-pre-review-degraded` + `pre-reviews.json` 的 `degraded_reason` |
| 预审跑了很久没结论 | 同一个超时口径（从 `wait_since` 起算），超时后降级 | 同上 |
| 预审提了 CHANGES 但生产车道一直不修 | `review.pre.fix_timeout_s`（默认 3600s）后降级 | 同上 |
| 预审与正式审意见冲突 | **以正式审为准**：正式审结论落 `reviews.json`、发 commit status、贴标签；预审记录置 `superseded` | dispatch 日志 `pre-review PR #N: superseded by the formal review of <lane>` |
| 迟到的预审结论（已降级/已被正式审接管） | 只记日志，不改状态、不排队修复任务 | dispatch 日志 `... arrived after the formal review took over → the formal verdict decides` |
| 预审车道跑挂（没有结论文件） | 按预审自己的 `fails` 计；达到 `review.max_fails` 后降级 | dispatch 日志 `pre-review PR #N by <lane>: no verdict file ...` |
| 预审的 run 变成孤儿（车道跑了别的活，runner 已死） | 预审记录回到 `waiting`，PR 重新排一次预审（不等超时） | dispatch 日志 `pre-review PR #N: the pre-review run was orphaned → queued again` + `pre-reviews.json` 的 `reset_reason` |
| 审查/预审任务拿不到 diff（GitHub 不可达，`gh pr diff` 为空） | 用 dispatcher 本地的对象重建 diff（PR 头对 base 分支的三方 diff）；本地也没有 PR 头时任务里 diff 为空并留痕 | dispatch 日志 `review: PR #N: GitHub gave no diff; rebuilt it from the local objects (<ref>)` / `no diff at all` |

## 7. 全链路日志

预审每一步都写 dispatch 日志（`<state_dir>/dispatch.log`，可用 `brain_log` 同步到 brain 日志）：

```
pre-review dispatched PR #42 (head a1b2c3d4, author M5) → C9 (box/codex) run 20261102-...
pre-review PR #42 by C9 (codex/gpt-6-luna): APPROVE → the formal review may start
pre-review PR #42 by C9 (codex/gpt-6-luna): CHANGES → fix task queued on #420 (pre-round 1; no review round counted)
pre-review PR #42: no free pre-review lane; waiting (timeout 900s)
pre-review PR #42: the pre-review run was orphaned → queued again
pre-review PR #42: no pre-review verdict in time → degraded to the formal review (the pre-review counts no review round)
pre-review PR #42: superseded by the formal review of T1 → the formal verdict decides
review PR #42: waiting for a formal review lane (terra); none of them is idle
review dispatched PR #42 (head a1b2c3d4, author M5) → T1 (box/codex) run 20261102-...
```

排查命令：

```bash
grep 'pre-review PR' <state_dir>/dispatch.log          # 一个 PR 的全链路
jq '."42"' <state_dir>/pre-reviews.json                  # 预审记录
jq '."42"' <state_dir>/reviews.json                      # 正式审记录（rounds 只在这里）
```

## 8. 配置项（dispatch 配置 `review.pre`）

| 键 | 默认 | 含义 |
|---|---|---|
| `enabled` | `true` | 预审层总开关 |
| `marker` | `review_pre` | lanes.json 里标记预审车道的键 |
| `backend` | `codex` | 预审车道的后端 |
| `model_re` | `luna` | 预审车道的模型正则（空 = 不限） |
| `author_backend` | `pi` | 哪些后端产出的 PR 走预审 |
| `max_concurrency` | `0` | 并发上限（0 = 由标记车道池决定） |
| `timeout_s` | `900` | 等预审结论/预审在跑的超时，超时降级 |
| `fix_timeout_s` | `3600` | CHANGES 后等生产车道修复的超时 |
| `rounds` | `3` | 预审修复轮数上限，超了降级 |
| `budget_minutes` | `10` | 预审任务给车道的预算（分钟） |

正式审车道（dispatch 配置 `review.formal`）：

| 键 | 默认 | 含义 |
|---|---|---|
| `backend` | `""` | 正式审车道的后端（空 = 不限） |
| `model_re` | `terra` | 正式审车道的模型正则（空 = 不限） |
| `required` | `false` | `true`：车队没有符合配置的车道时正式审就等（关掉退路） |

## 9. 测试

```bash
cd openswarm && python3 -m unittest tests.test_dispatch_review_pre -v
```

覆盖：pi 产出先过预审、codex 产出跳过预审、车道标记与并发上限、预审通过后进正式审、
预审 CHANGES 排队修复且**不增加 `reviews.json` rounds**、预审车道全忙超时降级并留痕、
正式审压过预审（冲突以正式审为准）、预审 run 变孤儿后重新排队、
正式审只走 terra 车道（terra 忙时等待，不退到 pi/预审车道）、
以及一条完整链路（预审 → 修复 → 新的 head 复审 → 进正式审）的日志留痕（tick 级）。

审查/预审任务的 diff 来源（GitHub 不可达时用本地对象重建）另有单测：

```bash
cd openswarm && python3 -m unittest tests.test_dispatch_review_diff -v
```
