# PR acceptance mapping (Refs #1730)

A card says what has to hold; a pull request has to say what proves it.  The two are
connected by the **acceptance mapping**: a numbered table in the PR body where every
acceptance item of the card gets exactly one line, and every line carries the evidence
that proves it (a test name plus its result, or the deciding command output).

## What runs where

| Where | What |
|---|---|
| `lib/review/acceptance_map.py` | the pure part: parse the card items, parse the mapping entries, compare them, render the review block |
| `lib/dispatch/engine.py` (`Dispatcher.task_text`, `Dispatcher.finish`, `Dispatcher.review_candidates`, `Dispatcher.review_item`) | the wiring: ask the lane for the mapping, write it into the PR body, send a PR without it back, hand the table to the reviewer |

The card is the authority: the numbering of the mapping never replaces the numbering of
the card.  A mapping that renumbers the items or points them at something else leaves
the card item uncovered instead of redefining it.

## The mapping

The PR body carries one section:

```markdown
## 验收映射

<!-- lane-acceptance-map -->

1. 样例 PR 缺映射段被打回且不消耗 review 轮次，有日志证据 证据：`tests/test_acceptance_map.py::test_a_pr_without_the_mapping_section_is_only_hinted_at` PASS
2. 样例 PR 带映射段正常进 review，review 产出引用验收编号 证据：`tests/test_acceptance_map.py::test_a_pr_with_the_mapping_goes_to_review_with_the_table` PASS
```

The lane writes the same table into `.lane-acceptance.md` at the root of its branch; the
harvest copies it into the PR body and the dispatch task asks for it.

## The gate in front of the review

- A PR whose section is missing, or whose entries carry no evidence, is **sent back to
  its author lane before the review is dispatched**.  That send-back is **not a review
  round**: the PR has not been reviewed at all, so `rounds` does not grow.
- The send-back happens **once per head sha**.  When the write of the task fails, the
  head sha is *not* recorded: the next tick tries again.
- The review prompt carries the mapping table, the card items with their numbers and
  the items the mapping does not cover.  The reviewer has to verify every item by
  number (`[A1] file:line ...`), and an item the mapping does not cover is a
  **BLOCKER**: an approval that ignores the gap is downgraded to `CHANGES`.

## Old PR and new PR: the boundary is the go-live moment

A PR without the section is not a broken PR, it is an **old** one: it takes the
compatibility path (the reviewer is only told about the gap, never sent back).  "Old"
means **created before the mapping gate went live**.

The boundary is:

- `review.acceptance_map.enabled_since` when the deployment states it (ISO-8601 UTC);
- otherwise the first moment this dispatcher ran with the gate enabled, recorded once in
  the state dir (`acceptance-map-enabled-at`) and reused after a restart.

It is **never a date written into the code**: the code that implements the gate is
newer than any date one could hard-code, so a hard-coded date would send back the old
PRs that were created between that date and the go-live moment - exactly the PRs the
compatibility path exists for.

The decision uses the **creation time of the PR** and the go-live moment of the gate,
never the body of the PR: the body is written by the author and can be edited (or
emptied) at any time, so a deleted mapping section must never turn a new PR into an
old one.

## The proof

### 上轮审查逐项交付映射

| 验收项 | 本轮改动 | 对应测试（`openswarm/tests/test_acceptance_map.py`） | 结果 |
|---|---|---|---|
| B1：样例 PR 缺映射段被打回且不消耗 review 轮次，有日志证据 | `Dispatcher.review_candidates` 在审查派发前判 `empty` 并调用 `queue_mapping_fix`；`Dispatcher.queue_fix` 只有在 issue 写入成功后才记录 `map_sha`，失败时记错误并下轮重试。 | `test_a_pr_without_evidence_is_sent_back_before_the_review`、`test_missing_section_on_new_pr_is_sent_back_and_failed_write_retries`、`test_the_send_back_happens_once_per_head` | PASS |
| B2：样例 PR 带映射段正常进 review，review 产出引用验收编号 | `Dispatcher.review_item` 把卡面验收项、PR 映射表和未覆盖项注入 review prompt，模板要求 findings 引用 `[A1]` 编号。 | `test_a_pr_with_the_mapping_goes_to_review_with_the_table`、`test_a_mapping_gap_is_named_in_the_review_task`、`test_the_task_asks_the_lane_for_the_mapping` | PASS |
| B3：映射漏项样例被 reviewer 标 BLOCKER | `acceptance_map.coverage` 以卡面为准算漏项；prompt 明确要求漏项标 BLOCKER；`Dispatcher.finish_review` 把带漏项的 APPROVE 降级为 CHANGES 并写明 BLOCKER。 | `test_the_gap_of_the_issue_card_is_reported`、`test_an_approval_over_a_mapping_gap_is_downgraded`、`test_a_blocker_verdict_is_published_and_counts_as_a_round` | PASS |

本轮（第 3 轮）按上轮意见修改的两处：

| 上轮意见 | 本轮改动 | 对应测试 | 结果 |
|---|---|---|---|
| [BLOCKER] `engine.py:210` 固定以 2026-11-03 00:00 UTC 判断旧 PR；该时刻早于修复提交本身，会把上线前创建的老 PR 当成新 PR 打回 | 去掉写死的 `legacy_before`；边界改为「映射门禁实际上线时刻」：配置 `review.acceptance_map.enabled_since`，缺省时取 dispatcher 首次带门禁运行的时刻并记进状态目录（`acceptance-map-enabled-at`）；判定只看 PR 创建时间与该边界，不看可被作者删除的正文段 | `test_a_pr_created_before_the_gate_went_live_takes_the_compatibility_path`、`test_a_pr_created_after_the_gate_went_live_is_sent_back`、`test_a_pr_created_at_the_gate_moment_is_a_new_pr`、`test_a_pr_created_after_the_fix_but_before_the_gate_went_live_stays_old`、`test_the_boundary_is_the_go_live_moment_and_not_a_date_in_the_code`、`test_a_new_pr_whose_mapping_section_was_deleted_is_still_sent_back` | PASS |
| [BLOCKER] 未给出 B1/B2/B3 → 改动 → 测试的逐条交付说明 | 本文件「上轮审查逐项交付映射」表 | — | PASS |

```sh
cd openswarm
python3 -m unittest tests.test_acceptance_map -v
python3 -m unittest tests.test_dispatch_engine tests.test_dispatch_review_fails -v
```

本轮固定编号交付映射（合并 main 后验证）：

- B1 → `openswarm/lib/dispatch/engine.py:1280`（上线时刻持久化及兼容判定）；`openswarm/tests/test_acceptance_map.py:408`（上线前、上线后及边界测试）→ `test_a_pr_created_before_the_gate_went_live_takes_the_compatibility_path`、`test_a_pr_created_after_the_gate_went_live_is_sent_back`、`test_a_pr_created_at_the_gate_moment_is_a_new_pr`、`test_a_pr_created_after_the_fix_but_before_the_gate_went_live_stays_old` → PASS。
- B2 → `openswarm/docs/acceptance-mapping.md:70`（上轮 B1/B2/B3 的改动、测试及结果映射）及本节 → 人工核对三条映射及测试名，并运行 `tests.test_acceptance_map` → PASS（人工核对及单元测试）。
