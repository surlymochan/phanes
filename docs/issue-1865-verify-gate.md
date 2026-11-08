# #1865 fix 推送前 verify 门禁（设计页）

状态：**已按统筹第 2 轮裁定实现**。设计页先于 AD 改动提交（PR #1874：`b52f104600` 设计页 →
`00ec25fa2b` AD 改动）；统筹第 2 轮 lane review（CX4 / codex/gpt-6.1-terra）对 PR 头 `00ec25fa2b`
给出 CHANGES，五条 BLOCKER（B1–B5）即对本设计页的裁定，本轮按裁定实现，逐条映射见 §8。

## 0. 落点变更（重基到 main 后的冲突解决）

原设计的落点是 `openswarm/lib/dispatch/engine.py`（旧 dispatcher）。该文件已随 #1894
（`Replace vendored openswarm with swrm as the single source`）从 main 删除：旧 dispatcher 退役，
仓内只剩 box 上的 AD 镜像 `tools/lane-autodispatch/autodispatch.py`（见
`tools/lane-autodispatch/README.md` 的「旧 dispatcher 已退役」）。按「保留 main 上已合并的改动」，
本卡把门禁落在存活的 AD 镜像上，不复活已退役的 dispatcher，也不改评审标准与测试标准。

镜像与旧 dispatcher 的对应关系：

| 旧 dispatcher（已退役） | 存活的 AD 镜像（本卡落点） |
|---|---|
| `engine.py` 收割 `finish()` 推送前 | `autodispatch.py` `finish()` 推送前（`git push` 之前） |
| `engine.py` 卡面 `验收:` 行 → `--verify` | `autodispatch.py` `verify_of()` → `--verify`（既有） |
| `engine.py` 重排 `gh issue edit --add-label lane-ready` | `autodispatch.py` 重排 `--remove-label in-progress --add-label lane-ready`（既有 quick-fix 口径） |
| `engine.py` `reviews.json` 评审轮次 | 镜像没有轮次簿：门禁不写 `lane-failed`、不消耗任何失败统计，只消耗卡上的本地重试预算 |

## 1. 问题

AD 收割 lane run 时只检查「diff 非空」，`verify_rc` 只写进 PR 正文/评论：推出去的改动没有
「通过了验收」的证据。最近 3 个 fix run 中两个 verify 为 null（卡面没有 `验收:` 行），门禁无从判断。

## 2. 隔离方式

| 项 | 内容 |
|---|---|
| 改动文件 | `tools/lane-autodispatch/autodispatch.py`（门禁 + 卡面兜底）、`tools/tests/test_lane_pr_validation.py`（单测与 tick 集成测试） |
| 不改 | 评审标准（PR 提交前校验 `validate_lane_pr`）、测试标准（`HARVEST_EXCLUDE_PATTERNS`）、`swrm/` 的执行器与 lane 运行时 |
| 生效路径 | 只有「AD 收割 lane run 并推送」这一条路径（推分支 + 开 PR）；派发顺序、队列份额、工作类上限不变 |
| 不同步 | box 运行副本 `~/phanes-ctl/bin/autodispatch.py` 由控制面同步（本仓是镜像源，同步步骤见 `tools/autodispatch-backend-fallback.md`） |

## 3. 卡面要求

卡面必须有一行 `验收:`，行内含反引号命令：

```
验收: `python -m unittest tools.tests.test_lane_pr_validation`
```

- 卡面已有 `验收:` 行 → 原样保留（不覆盖人写的验收），镜像既有的 `verify_of()` 把它作为 `--verify` 传给 lane。
- 卡面没有 → AD 用**被改文件的既有测试命令**兜底（见 §4），把这一行写回卡面，并用兜底命令作为本次 lane 的 `--verify`。

## 4. 兜底规则

1. 兜底命令 = 被改文件对应的既有测试：
   - 改的是测试文件本身 → 该测试文件自己的模块（`tests/test_x.py` → `python -m unittest <pkg>.tests.test_x`）；
   - 改的是带 `package.json` 的组件目录下的文件 → 该组件自己的检查命令（`volto/apps/<app>/**` → `cd <app> && bun run typecheck`）；
   - 其余模块 → 同目录的测试文件，或名字带该模块名的测试文件（`autodispatch.py` → `test_autodispatch_state_home.py` → `python -m unittest tools.tests.test_autodispatch_state_home`）；多个命令用 `&&` 连接，最多 4 个。
2. 找不到测试（只有文档/配置改动）→ 不写 `验收:` 行，门禁按「无验收证据」处理（不拦推送，只记日志）。
3. 兜底只算一次：卡面里的 `验收:` 行是唯一事实来源（派发时若仍缺，再用兜底补一次，命令写回卡面）。

## 5. 推送前门禁与重试上限

收割 lane run 时、**推送之前**读 `verify_rc`：

| `verify_rc` | 行为 |
|---|---|
| `0` | 正常推送 |
| `null` | 不拦推送，记日志 `no acceptance evidence`（没有证据 ≠ 失败） |
| 非 0，第 1、2 次 | **不推送、不消耗失败统计**；失败输出写进卡面，卡重排回 `lane-ready`（同一 fix 重试） |
| 非 0，第 3 次（重试上限 2 次用完） | 仍然推送，PR 评论标红 🔴 并写明 verify 失败 |

- 本地重试上限 **2 次**（`AD_VERIFY_GATE_RETRIES`，默认 2）。
- 重排幂等：失败输出带 `<!-- lane-verify-fail run=<run> -->` 标记；同一个 run 再次收割时看到标记就不再追加、不再计次。
- 重排一次调用完成（`gh issue edit --body … --add-label lane-ready --remove-label in-progress`）：调用失败则**保留收割状态**（`done=False`），下个 tick 重试，且不消耗重试预算（重试次数只从卡面里已写入的标记数出来，写成功才算一次）。
- 卡面读不出来（`gh` 失败或 JSON 无效）→ 不改卡面、不推送、保留收割状态，下个 tick 重试。
- 重试计数按当前 fix 轮次：镜像没有「新的 fix 卡」生成路径（评审派发随旧 dispatcher 退役），失败段
  只由本门禁写入，卡面上已有的失败标记数就是本轮已用的重试次数。

## 6. 度量口径

| 指标 | 口径 |
|---|---|
| 门禁拦下 | brain log 行 `verify rc <n> → not pushed, the card is requeued (k/2)` 的条数（按 run 去重） |
| 验收缺失 | brain log 行 `no acceptance evidence` 的条数 / 收割 run 总数 |
| 重试耗尽 | PR 评论中出现 🔴 的次数 |

## 7. 验收

- 单测：`verify_rc=0` 推送；非 0 不推送、不计失败统计、排队一次重试；两次本地重试后仍失败则推送并标红评论。
- 单测：卡面缺 `验收:` 时用既有测试命令兜底（测试文件自身、组件命令、模块名匹配三种）。
- tick 级集成测试：失败重排 → 再次派发 → 重排写失败时保留状态 → 重试耗尽后推送并评论。
- CI：`bash tools/ci-openswarm.sh` 全通过（#1894 起该脚本名为 `tools/ci-swrm.sh`，旧入口保留为兼容壳）；本卡有效改动 ≤ 250 行。

## 8. 本轮裁定（B1–B5）逐条映射

编号沿用统筹第 2 轮评审的固定编号。落点由 `openswarm/lib/dispatch/engine.py`（已随 #1894 退役）
改为存活的 AD 镜像 `tools/lane-autodispatch/autodispatch.py`，见 §0。

| 编号 | 意见 | 改动文件:行 | 测试名 | 结果 |
|---|---|---|---|---|
| B1 | 卡面读取失败/JSON 无效时不得置空原正文、不得覆盖；写失败与标签结果要检查；重复收割幂等 | `tools/lane-autodispatch/autodispatch.py:792`（`read_card`：读失败或 JSON 无效返回 `(False, "")`，调用方不改卡面）、`:800`（`write_acceptance`：写结果检查）、`:1197`（`verify_gate`：读失败→`retry`，写失败→`retry`，同 run 的标记已存在→`requeue` 不计次）、`:1260`（`finish`：读/写失败保留收割状态 `done=False`） | `tools/tests/test_lane_pr_validation.py` `VerifyGateTests.test_an_unreadable_or_unwritable_card_keeps_the_harvest_state` | 通过（读失败不覆盖卡面、写失败不消耗重试预算、再次收割仍是第一次重试） |
| B2 | 重排失败后保留状态、重试耗尽后的推送与标红评论要有 tick/集成证据 | `tools/lane-autodispatch/autodispatch.py:1260`（门禁在 `git push` 之前）、`:1285`（推送后 PR 标红评论）、`tools/tests/test_lane_pr_validation.py` `VerifyGateTests.test_the_tick_requeues_the_failed_run_and_pushes_it_after_the_budget` | 同左 | 通过（tick 级：写失败保留车道占用→重排→再次派发→两次重试后推送并 🔴 评论） |
| B3 | 设计页须先于 AD 改动提交，并有统筹审阅通过的可核验证据 | `docs/issue-1865-verify-gate.md`（状态行记录第 2 轮 CHANGES 裁定与 PR 头 `00ec25fa2b`）；提交顺序：设计页 → AD 改动 | 无（文档交付） | 通过（设计页在 AD 改动之前提交；裁定记录在 §0 与本表） |
| B4 | 恢复「改动的测试文件自身」与「非 Python 组件命令」的兜底 | `tools/lane-autodispatch/autodispatch.py:760`（`verify_fallback`：测试文件自身、同目录/同名测试文件、带 `package.json` 的组件 → `bun run typecheck`）、`:977`（派发时兜底并写回卡面） | `tools/tests/test_lane_pr_validation.py` `VerifyGateTests.test_a_card_without_the_acceptance_line_falls_back_to_the_tests` | 通过（模块→既有测试、测试文件自身、组件 typecheck 三种兜底 + 写回卡面） |
| B5 | 逐条给出 B 编号 → 改动文件 → 测试名 → 结果的映射 | 本表（§8） | — | 通过 |

有效改动（代码行，文档行按仓内惯例不计入）：`tools/lane-autodispatch/autodispatch.py` +112/-2、
`tools/tests/test_lane_pr_validation.py` +135/-4、`tools/ci-openswarm.sh` +2，合计 249 行。
