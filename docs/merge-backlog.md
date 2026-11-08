# 合并积压（merge backlog）：mergeable 却长期未合并的 PR 清单与原因

> Refs #1696。本文档描述健康检查的**合并积压**分段：把「开了却一直不动」的
> 合并队列显形——逐条写清**卡在哪一步**、**停了多久**。

## 背景（2026-11-02 晚实测）

当晚多个 PR 已 APPROVE 或 CI 绿却长时间停留在 open（external lane 的 #1632
等待复审与薄卡豁免裁决、多个舰队 PR 等 review），合并管线没有「卡在哪一步」的
统一视图：是等审查、等 CI、等豁免裁决还是合并循环没扫到，全靠人工逐个查。积压
不显形就会被误认为还在推进。

## 入队口径

- `lane-pr` 标签是打在**关联卡（issue）**上的（`tools/lane-autodispatch/autodispatch.py`），
  不是打在 PR 上。清单用 PR 正文 `Closes #N` 或分支 `fleet/<lane>-issue<N>-…` 找到
  关联卡，卡上带 `lane-pr` 才算入队。
- 已合并 / 已关闭 / 草稿的 PR 不在清单里。
- 同一个仓库里 issue 与 PR 共用一个编号空间，所以 PR #1632 的关联卡是 **#1626**，
  不是 #1632。

## 阶段判定

按优先级判定：

| 阶段 | 判据 |
| --- | --- |
| 等人工裁决 | PR 带 `needs-human` 标签（`external-lane/contract` 状态红时原因是**薄卡豁免**） |
| 审查 CHANGES 待修复 | PR 带 `needs-changes` 标签，或最近一次 CHANGES 判决针对当前 head |
| CI 未绿 | 有红灯检查或检查还在跑 |
| 已批待合并 | 已批准（`lane-reviewed` 标签或账本里的 APPROVE 判决）**且** CI 绿 |
| 等复审 | 有判决记录，但判决对应的 sha 与当前 head 不一致（head 变了） |
| 等首审 | 没有判决记录 |
| unknown | 判定不了（缺读数） |

**判定不了就 unknown**：缺 review 记录、缺 CI rollup 时不臆测，阶段标 `unknown`
并把已有信号（`CI 状态缺失`、`review 记录缺失`、`人工裁决标记时间缺失`、红灯检查名
等）列出来。**空检查列表不是绿灯**：rollup 为空、或只有 `lane-review` 一条时没有 CI
成功证据，已批准的 PR 也标 `unknown`（信号 `CI 无检查项`）。

## 停留时长

停留时长从**进入当前阶段的那一刻**起算，不从 PR 创建时间起算，每条都带
`dwell_source` 说明数字的来源：

| 阶段 | 起点 |
| --- | --- |
| 等首审 | PR 创建时间 |
| 等复审 | 头部移动（PR 最后变动时间，须不早于判决时间） |
| 审查 CHANGES 待修复 | CHANGES 判决时间（账本里的 `at`） |
| CI 未绿 | 失败检查的完成时间 |
| 已批待合并 | **批准时间与 CI 转绿时间里较晚的那个** |
| 等人工裁决 | `needs-human` 标签贴上去的时刻（账本里的 `needs_human_at`） |

- **已批待合并**要「批准」和「CI 绿」同时成立，所以从**较晚**的那一刻起算：批准
  3 小时后 CI 才转绿的 PR，进入这个阶段只有几分钟，不会被当成「已批待合并三小时」。
- **等人工裁决**从**贴标签**那一刻起算，不是从 CHANGES 判决那一刻：判决可能比贴
  标签早几个小时（复审一直失败）。取不到这个时刻时停留时长记为未知，**不回落**成
  任意 `updatedAt`。
- 取不到起点时停留时长为未知（`dwell_s = null`），并在信号里说明缺哪样读数。

## 点名规则

**已批待合并**停留超 **2 小时**、或任一阶段停留超 **24 小时**的条目，在脑日志里
单独成行点名：

```
合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）
合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）
```

**只显形**：不动合并循环、不代审、不代合。

## 用法

```bash
# 单看积压清单（有超阈值的条目时退出码为 1，可直接当健康检查用）
python3 tools/merge-backlog.py \
  --prs /tmp/gh-lane-prs.json \
  --issues /tmp/gh-lane-issues.json \
  --reviews ~/.claude/openswarm-dispatch/reviews.json \
  --now 2026-11-02T21:05:00+08:00 --tz +08:00

# 直接从 GitHub 读，并把点名写进脑日志
python3 tools/merge-backlog.py --gh-live --tz +08:00 \
  --brain-log ~/phanes-ctl/brain/fleet-status.log
```

`--prs` 是 open PR 的 JSON 数组（`gh pr list --state open --json number,title,url,
labels,body,headRefName,headRefOid,isDraft,mergeable,reviewDecision,
statusCheckRollup,createdAt,updatedAt`），`--issues` 是带 `lane-pr` 标签的卡的 JSON
数组（`gh issue list --label lane-pr --state open --json number,title,state,labels,
body,updatedAt`），`--reviews` 是审查账本（可选）。

`--ci-policy` 传合并门禁同一套忽略规则，否则任何红灯都算红灯（保守方向）；本仓库
的常规写法：

```json
{"ignore_red_re": "macos", "advisory_checks": ["Check upstream drift"]}
```

## 回放（2026-11-02 夜班）

回放数据在 `tools/tests/fixtures/merge-backlog-2026-11-02/`（来源说明见同目录的
`PROVENANCE.md`；`--now 2026-11-02T21:05:00+08:00`）：

```
PR     STAGE                    DWELL     CARD  SIGNALS
#1632  等人工裁决（薄卡豁免）    26h02m    #1626  CI 失败: external-lane/contract; external-lane/contract 失败（薄卡/契约门）
#1633  已批待合并               3h00m     #1627  reviewDecision=APPROVED
#1634  等首审                   30h05m    #1628
#1637  unknown                 -         #1631  CI 状态缺失
#1638  CI 未绿                  4h18m     #1625  CI 失败: Phanes components
#1639  已批待合并               5m00s     #1624  reviewDecision=APPROVED
合并积压点名 #1632 等人工裁决（薄卡豁免） 停留26h02m（阈值 24h00m）
合并积压点名 #1633 已批待合并 停留3h00m（阈值 2h00m）
合并积压点名 #1634 等首审 停留30h05m（阈值 24h00m）
```

- **#1632**（当晚的 external lane PR）落在**等人工裁决（薄卡豁免）**：关联卡 #1626 带
  `lane-pr`、`external-lane/contract` 状态红（薄卡）、PR 带 `needs-human` 标签；停留
  时长从**贴上 `needs-human` 标签的那一刻**（账本里的 `needs_human_at`）起算，不是从
  更早的 CHANGES 判决起算，26h02m 超过 24h 阈值，被点名。
- **#1633** 已批准且 CI 绿，停留时长从 **APPROVE 判决时间**与 **CI 转绿时间**里较晚
  的那个起算（不是 PR 创建时间），3h00m 超过 2h 阈值，被点名。
- **#1639** 批准了 3 小时，但 CI 5 分钟前才转绿 → 进入「已批待合并」只有 5 分钟，
  **不被点名**；这正是「按阶段起点计时」与「按批准时间计时」的区别。
- **#1635**（当晚已合并）不出现在清单里；**#1636**（卡还没进 `lane-pr`）也不在清单里。
- **#1637** 没有 CI rollup 也没有判决记录 → 阶段 `unknown`，信号 `CI 状态缺失`，
  停留时长未知，不臆测。
- 快照里每条 PR 的 `headRefOid` 都是本仓库**已发布历史**里的真实提交（`git cat-file
  -e <sha>^{commit}` 且 `git merge-base --is-ancestor <sha> HEAD` 两条都能过），所以
  任何一份历史的 clone（含 CI 的 `fetch-depth: 0` checkout）都能逐条核对。

## 实现位置

- 判定与计时（纯函数，不读文件、不碰时钟）：[`tools/merge_backlog.py`](../tools/merge_backlog.py)
- 采集与点名（命令行，可当健康检查用）：[`tools/merge-backlog.py`](../tools/merge-backlog.py)
- 测试：[`tools/tests/test_merge_backlog.py`](../tools/tests/test_merge_backlog.py)、[`tools/tests/test_merge_backlog_cli.py`](../tools/tests/test_merge_backlog_cli.py)
- 回放数据：[`tools/tests/fixtures/merge-backlog-2026-11-02/`](../tools/tests/fixtures/merge-backlog-2026-11-02/PROVENANCE.md)

## 本轮（重基到 main）BLOCKER 关账映射

| BLOCKER | 改动文件:行 | 测试名 | 结果 |
| --- | --- | --- | --- |
| **B1** 真实样本回放（PR #1632 ↔ 卡 #1626、已合并不入清单、head SHA 可核验） | `tools/merge_backlog.py:350`（`linked_issue` 读正文/分支两条真实信号）、`tools/tests/fixtures/merge-backlog-2026-11-02/gh-lane-prs.json`、`gh-lane-issues.json`、`reviews.json`、`PROVENANCE.md` | `tools/tests/test_merge_backlog.py::FixtureShapeTests::test_a_pull_request_and_its_card_have_different_numbers`、`::FixtureShapeTests::test_the_body_and_the_branch_point_at_the_same_card`、`::FixtureShapeTests::test_the_head_shas_are_real_commits_of_this_repository`、`::NightReplayTests::test_the_external_lane_card_waits_for_a_human`、`::NightReplayTests::test_the_merged_pull_request_is_not_in_the_queue`、`tools/tests/test_merge_backlog_cli.py::MergeBacklogCliTests::test_the_merged_pull_request_is_not_in_the_queue` | 全绿（58 项） |
| **B2** 按当前阶段起点计时（批准后 CI 才转绿、人工裁决从贴标签起算、取不到即未知） | `tools/merge_backlog.py:639`（`_approved_start`：批准与 CI 转绿取**较晚**者）、`tools/merge_backlog.py:623`（`_needs_human_start`：只认 `needs_human_at`，不回落 `updatedAt`） | `tools/tests/test_merge_backlog.py::StageTransitionTests::test_the_checks_that_turn_green_after_the_approval_start_the_merge_stage`、`::DwellTests::test_the_dwell_of_the_human_stage_starts_at_the_label`、`::DwellTests::test_without_the_label_time_the_human_stage_has_no_dwell`、`::ThresholdTests::test_a_pull_request_that_just_turned_green_is_not_named`、`::NightReplayTests::test_the_pull_request_whose_checks_turned_green_late_is_not_named` | 全绿（58 项） |
| **B3** 空 CI 列表 ≠ 绿灯（缺证据即 unknown 并附信号） | `tools/merge_backlog.py:542`（`ci_reading`：缺 rollup → unknown，空 rollup → none）、`tools/merge_backlog.py:783`（`classify`：只有 `CI_GREEN` 才判「已批待合并」） | `tools/tests/test_merge_backlog.py::StageTests::test_an_approved_pull_request_without_any_check_is_unknown`、`::StageTests::test_a_rollup_with_only_the_lane_verdict_is_not_green`、`::StageTests::test_an_approved_pull_request_without_ci_data_is_unknown`、`::NightReplayTests::test_the_pull_request_without_readings_is_unknown` | 全绿（58 项） |

## 本轮（修 CI red）BLOCKER 关账映射

CI 的 `Swrm tests` 变红的原因：回放快照里有三个 `headRefOid` 取自**只存在于某一台
机器**的提交（调度器的车道分支、AD 的本地 `rebase-pr-*` 辅助分支），而 CI 的 clone
（`fetch-depth: 0`）只看得到**已发布分支**带来的提交，于是
`FixtureShapeTests::test_the_head_shas_are_real_commits_of_this_repository` 在 CI 上
报 `a3a196e576… is not a commit of this repository`。修法是换数据 + 加断言，不是
删测试、不放宽断言。

| BLOCKER | 改动文件:行 | 测试名 | 结果 |
| --- | --- | --- | --- |
| **CI-1** 回放数据的 head SHA 必须是已发布历史里的提交（CI 的 clone 核得出） | `tools/tests/fixtures/merge-backlog-2026-11-02/gh-lane-prs.json`（#1633/#1638/#1639 的 `headRefOid`）、`tools/tests/fixtures/merge-backlog-2026-11-02/reviews.json`（同三条的 `sha`）、`tools/tests/fixtures/merge-backlog-2026-11-02/PROVENANCE.md`（SHA 表 + 「为什么必须是已发布历史里的提交」）、`tools/tests/test_merge_backlog.py:506`（`test_the_head_shas_are_real_commits_of_this_repository`：提交存在 **且** 是 `HEAD` 的祖先）、`tools/tests/test_merge_backlog.py:532`（`test_the_head_shas_are_published_commits_of_this_repository`） | `tools/tests/test_merge_backlog.py::FixtureShapeTests::test_the_head_shas_are_real_commits_of_this_repository`、`::FixtureShapeTests::test_the_head_shas_are_published_commits_of_this_repository`、`::FixtureShapeTests::test_the_ledger_knows_the_head_of_every_verdict`、`::NightReplayTests::test_the_queue_of_that_night` | 全绿（58 项；已发布历史的 clone 里复现过红→绿） |
| **CI-2** CLI 自检不能依赖宿主机时区（车队宿主机是 UTC+8，CI 是 UTC） | `tools/merge-backlog.py:151`（`self_test()`：账本的墙钟按 UTC 读，`offset=0`） | `tools/tests/test_merge_backlog_cli.py::MergeBacklogCliTests::test_the_self_test`、`::MergeBacklogCliTests::test_the_self_test_does_not_depend_on_the_host_timezone`（`TZ=Asia/Shanghai`） | 全绿（58 项） |

## 重基说明（#1894 之后）

本 PR 原先把判定逻辑放在 `openswarm/lib/dispatch/merge_backlog.py`，并挂在
`tools/fleet-status-page.py` 的健康检查页上。main 上随 #1894（swrm 成为唯一源）删掉了
整棵 vendored `openswarm/` 包与 `tools/fleet-status-page.py`，所以重基后：

- 判定逻辑搬到 `tools/merge_backlog.py`（纯函数，只依赖标准库）；共享的 CI 分类器
  `openswarm/lib/ci/checks.py` 也随 #1894 删除，本模块自带一份保守副本（advisory
  检查永远不门禁、被忽略的红灯不算红灯、没有有效检查项是 `none` 而不是绿灯）。
- 健康检查的落点是 `tools/merge-backlog.py`：清单照旧打印，超阈值的条目在脑日志里
  单独成行点名（`--brain-log`），退出码 1 可以直接当检查用。
- 审查门禁（写 `reviews.json`、贴 `needs-human` 标签的那部分）随 #1894 一起离开本
  仓库：没有账本时判决时刻读不出来，条目按规则标 `unknown` 并附信号，不臆测。
