# 车道 review 度量：首轮通过率与平均轮次（Refs #1733）

车道审查循环的健康度由两个数说清：**首轮通过率**（被审 PR 在第一个正式审查轮就通过的比例）
与**平均 review 轮次**（被审 PR 平均需要的正式审查轮数）。本卡把这两个数按**车道**与**后端**
统计出来，写进状态文件，并在每日简报里呈现。

## 口径（measurement rules）

口径写在指标文件的文件头（`definitions`），脚本与简报读的是同一份定义。

| 指标 | 口径 |
|---|---|
| 正式轮次（formal round） | 一个 PR head 上的一次审查判定。dispatcher 每写一条记录都写 `review_rounds`（该 PR 的判定次数）。**rebase 复审不是正式轮次**：代码没变、只换了基线，这类复审计入 `rebase_reviews`，不进轮次。 |
| 首轮通过（first-round pass） | 该 PR 的**首个**正式判定是 `APPROVE`。dispatcher 把首个判定单独存进 `first_verdict` 且**永不覆盖**：后来的判定（新 head、复审）不能把首轮失败的 PR 变成首轮通过。仍在 `CHANGES` 的 PR 记为首轮失败（第一轮已经发生）。 |
| 历史记录（legacy） | 旧记录只有 `rounds`（返工轮次）：`CHANGES` 记录的轮次已在计数内，`APPROVE` 记录是 `rounds + 1`。没有轮次、只有 `APPROVE` 的旧记录按「首轮通过」读入（`legacy-inferred`，样本数见 `legacy_samples`）。**缺轮次且无法判断的记录跳过**（计入 `skipped`），不报错、不进任何比率。 |
| BLOCKER / FOLLOW-UP 分布 | 取审查结论里的 `[BLOCKER]` / `[FOLLOW-UP]` 条目（车道审查真正用的格式）；结论没有条目时才回退到 `BLOCKERS: n` / `FOLLOW-UPS: n` 汇总行。条目优先于汇总行。分桶为 `0 / 1 / 2 / 3+ / unknown`，缺计数保持 `unknown`，不从散文里猜。 |
| follow-up 开单量 | 审查结论里每条 `[FOLLOW-UP]` 对应**一个真实 issue**（dispatcher 真的开出来的单，编号在 `followup_issues`）。**返工任务不是 follow-up 卡**：`CHANGES` 的返工任务留在原 issue 上（issue 正文追加一段），单独计在 `rework_tasks`。 |
| 周 / 垫底车道 | 周 = 记录 `at` 的 ISO 周。某周的垫底车道 = 首轮通过率最低者（并列时平均轮次更高者，再并列按车道名）。连续两周垫底的车道列在 `bottom_lanes`（作者视角另列 `bottom_author_lanes`）。 |
| 车道 | `lanes` 是**审查方**车道（写下判定的那条车道）；`author_lanes` 是**被审方**车道（PR 所属车道），后者才是调度降派要看的视角；`backends` 是审查方后端。 |

## 目标线

| 目标线 | 值 | 落点 |
|---|---|---|
| 首轮通过率 | ≥ 60% | `targets.first_round_pass_rate`，逐组对照 `meets_first_round_target` |
| 平均轮次 | ≤ 2 | `targets.avg_review_rounds`，逐组对照 `meets_rounds_target` |

## 接入点

| Where | What |
|---|---|
| `lib/dispatch/review_metrics.py` | 口径与统计（纯函数：读记录与 PR 数据，不读文件、不跑命令、不碰时钟） |
| `lib/dispatch/engine.py` | 写记录（轮次、首个判定、发现数、follow-up 开单结果）与写状态文件 |
| `bin/openswarm` | 每日简报（文本 + HTML）：车道表、首轮通过率、平均轮次、follow-up 开单量、目标线对照、连续两周垫底清单 |
| `tools/lane-review-metrics.py` | 离线脚本：对 `reviews.json` 跑出同一份指标文件 |

状态文件与 `lanes-status.json` 同级（dispatch 的 `state_dir`）：

```
<state_dir>/lanes-status.json
<state_dir>/lane-review-metrics.json     # 本卡的输出（Refs #1733）
<state_dir>/reviews.json                  # 审查记录（输入）
```

每日简报读的就是这个目录（`dispatch_state_dir()`），所以简报里的数字与状态文件里的数字同源。

## 用法

```sh
# 离线：对现有 reviews.json 跑一遍，写出指标文件并打印一屏摘要
python3 tools/lane-review-metrics.py --reviews <state_dir>/reviews.json

# 带上真实 PR 数据（补齐旧记录缺的作者车道 / 后端）
python3 tools/lane-review-metrics.py --reviews <state_dir>/reviews.json \
    --prs <prs.json> --json

# 每日简报（含车道 review 度量一节）
python3 bin/openswarm report --daily --html /tmp/daily.html
```

## 连续两周垫底车道

`bottom_lanes` / `bottom_author_lanes` 只作**调度降派的参考**：本卡**不实现**自动降级，
派单策略不变。

## 上轮审查逐项交付映射（第 2 轮审查，head `a32ba53e1f`）

| 编号 | 阻塞项 | 本轮修复 | 对应测试 |
|---|---|---|---|
| B1 | 返工被误计为开单、真实 follow-up 分布遗漏：CHANGES 只改原 issue 并把计数加一，没有开 follow-up issue；统计只读 `FOLLOW-UPS:` 汇总行，而实际审查格式用 `[FOLLOW-UP]` 条目 | `review_metrics.parse_counts` / `follow_up_entries` 以 `[BLOCKER]` / `[FOLLOW-UP]` 条目为准（无条目才回退汇总行）；`engine.Dispatcher.open_follow_up_cards` 为每条 `[FOLLOW-UP]` **真的开一个 issue**（先建单、后打标签：仓库没有 `follow-up` 标签时单照样开出来），开单编号写进记录 `followup_issues`；返工任务单独计 `rework_tasks`，两者不混 | `test_the_finding_entries_of_the_review_format_are_counted`、`test_a_follow_up_finding_opens_a_real_follow_up_card`、`test_the_card_is_created_even_when_the_label_does_not_exist`、`test_a_follow_up_card_that_could_not_be_created_is_not_counted`、`test_the_same_head_does_not_open_the_follow_up_cards_twice`、`test_a_changes_round_queues_a_rework_task_and_no_follow_up_card`、`test_the_rework_task_of_a_changes_round_is_not_a_follow_up_card` |
| B2 | rebase 复审覆盖原 verdict，首轮通过被错记 | 首个正式判定单独存 `first_verdict` 且永不覆盖；rebase 复审不增加正式轮次（`rebase_reviews`），也不改首个判定。**重基到 main 后补一处**：main 的产物过滤（#1835）会重建被推的分支，被推的 head 不再是 `refs/fleet/<lane>`，因此记的是**真正被推的 head**，rebase 复审才认得出 | `test_the_first_verdict_is_kept_when_a_later_verdict_arrives`、`test_a_rebase_re_review_is_not_a_formal_round`、`test_a_rebase_re_review_of_a_filtered_head_is_not_a_formal_round`、`test_a_rebase_re_review_does_not_turn_a_failed_first_round_into_a_pass`、`test_the_rebase_harvest_remembers_the_head_it_moved`、`test_the_rebase_harvest_remembers_the_head_the_filter_rebuilt` |
| B3 | 上一 head 的测试集成点（rebase 排除、空数据）在上一轮消失 | 恢复并适配 `openswarm/tests/test_lane_review_metrics.py`：`RebaseExclusion`（rebase 复审不是正式轮次、不改首轮结果）、`EmptyData`（空数据/缺文件路径不是错误）、`DailyBrief`（简报呈现）三组都在，另补「简报默认读 dispatch 状态目录」一条 | `tests/test_lane_review_metrics.py` 全部（`RebaseExclusion` / `EmptyData` / `DailyBrief`） |
| B4 | 交付说明只有标题，缺 B1–B4 的「阻塞项 → 修改 → 对应测试」映射 | 本文件即交付说明：上表逐条列出 B1、B2、B3、B4 的阻塞项、修复与对应测试，并附下面的验证命令与实际输出 | 本文件「验证」一节 |

## 本轮（自动重基到 main）

head `353c813623` 重基到 main 时只有一处冲突：`openswarm/lib/dispatch/engine.py` 的
**rebase 收割块**（推完 rebase 之后记 head 的那一段）。

- main 侧（#1842）：记 `rebase_sha` + `rebase_base_sha`，表示「这个 head 已经在这一版 main 上
  重基过」，供 `review_queue()` 判断还要不要再派 rebase 任务。
- 本卡侧（#1733）：记 `rebased_sha`，表示「这个 head 是 dispatcher 自己推上去的」，复审时
  认出它是 rebase 复审（不算正式轮次、不改首个判定）。

两边记的是**同一个被推的 head**，语义不冲突，所以冲突解成一处：一次 `merge_review()` 同时写
`rebase_sha` / `rebase_base_sha` / `rebased_sha`。取 head 的方式沿用本卡的一版：产物过滤
（#1835）重建过分支时 `head` 本身就是 40 位 sha，否则 `git rev-parse` 取。

补了两条断言锁住这个合并后的行为（`tests/test_dispatch_review_metrics.py`）：
`test_the_rebase_harvest_remembers_the_head_it_moved` 与
`test_the_rebase_harvest_remembers_the_head_the_filter_rebuilt` 现在同时验
`rebased_sha`（本卡）与 `rebase_sha` / `rebase_base_sha`（main 的 #1842）。

## 验证

```sh
cd openswarm
python3 -m unittest tests.test_review_metrics tests.test_dispatch_review_metrics tests.test_lane_review_metrics -v
```

本工作树的实际输出（48 个用例全绿）：

```text
................................................
----------------------------------------------------------------------
Ran 48 tests in 0.090s

OK
```

脚本对一份按 dispatcher 真实写入格式构造的 `reviews.json`（含一条旧格式记录）的输出。这份样本
在仓库里（`openswarm/tests/fixtures/reviews-lane-metrics.json` 与同目录的
`prs-lane-metrics.json`，后者是 PR 数据：旧记录缺作者车道时由它补齐），所以下面这条命令
可以离线复现：

```sh
python3 tools/lane-review-metrics.py \
    --reviews openswarm/tests/fixtures/reviews-lane-metrics.json \
    --prs openswarm/tests/fixtures/prs-lane-metrics.json \
    --out /tmp/lane-review-metrics.json
```

```text
lane review metrics: 3 review(s), first-round pass 33.3% (target >= 60%), average rounds 1.33 (target <= 2.00)
  lane         reviews   1st-pass  avg-rounds follow-ups   rework target
  CX4                2      0.0%        1.50          1        1 below target
  M5                 1    100.0%        1.00          0        0 ok
  bottom two weeks (reviewing lanes): CX4 (2026-W45)
  bottom two weeks (author lanes): M5 (2026-W45)
metrics file: /tmp/lane-review-metrics.json
```

指标文件的头部（`definitions` + `targets`）就是上面的口径表与目标线对照。
