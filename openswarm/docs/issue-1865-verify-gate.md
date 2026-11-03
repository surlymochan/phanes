# #1865 fix 推送前 verify 门禁（设计页）

状态：**待统筹审阅**。本设计页先于 AD 改动单独提交，审阅通过后才改 `lib/dispatch/engine.py`。

## 1. 问题

AD 收割 fix run 时只检查「diff 非空」，`verify_rc` 只写进 PR 评论：推出去的改动没有「通过了验收」的证据。
最近 3 个 fix run 里 2 个 `verify_rc` 为 null（卡面没有 `验收:` 行），门禁无从判断。

## 2. 隔离方式

| 项 | 内容 |
|---|---|
| 改动文件 | 只有 `openswarm/lib/dispatch/engine.py`（收割门禁 + fix 卡面生成） |
| 不改 | 评审标准（`review.rounds` / `max_fails`）、测试标准（`thin_pr` / `harvest_exclude`）、lane 运行时 `bin/openswarm` |
| 生效路径 | 只有「lane 把改动推回已有 PR 分支」这条路径（fix / rebase 卡）；新开 PR 的路径不变 |
| 不同步 | `tools/lane-autodispatch/autodispatch.py`（box 镜像）没有推回 PR 分支的路径（只新开 PR），不在本次范围；`验收:` 行由 AD 写进卡面，镜像读卡即得 |

## 3. 卡面要求

fix 卡（review CHANGES / CI 红灯 / 冲突重基）必须有一行 `验收:`，行内含反引号命令：

```
验收: `python -m unittest openswarm.tests.test_dispatch_engine`
```

- 卡面已有 `验收:` 行 → 原样保留（不覆盖人写的验收）。
- 卡面没有 → AD 用**被改文件的既有测试命令**兜底（见 §4），并把这一行写回卡面。

## 4. 兜底规则

1. 兜底命令 = 每个被改模块的既有测试：`openswarm/lib/dispatch/engine.py` →
   `openswarm/tests/test_dispatch_engine.py` → `python -m unittest openswarm.tests.test_dispatch_engine`
   （多个测试文件用 `&&` 连接，最多 4 个）。
2. 找不到测试文件（只有文档/配置改动）→ 不写 `验收:` 行，门禁按「无验收证据」处理（不拦推送，只记日志）。
3. 兜底只算一次：卡面里的 `验收:` 行是唯一事实来源（派发时若仍缺，再用兜底补一次）。

## 5. 推送前门禁与重试上限

收割 fix run 时、推送之前读 `verify_rc`：

| `verify_rc` | 行为 |
|---|---|
| `0` | 正常推送 |
| `null` | 不拦推送，记日志 `no acceptance evidence`（没有证据 ≠ 失败） |
| 非 0，第 1、2 次 | **不推送、不消耗评审轮次**；失败输出写进卡面，卡重排回 `lane-ready`（同一 fix 重试） |
| 非 0，第 3 次（重试上限 2 次用完） | 仍然推送，PR 评论标红 🔴 并写明 verify 失败 |

- 本地重试上限 **2 次**（配置 `verify_gate.max_retries`）。
- 重排幂等：失败输出带 `<!-- lane-verify-fail pr=<n> run=<run> -->` 标记；同一个 run 再次收割时看到标记就不再追加、不再计次。
- 重排一次调用完成（`gh issue edit --body … --add-label lane-ready --remove-label in-progress`）：调用失败则保留收割状态，下个 tick 重试，不消耗重试预算。
- 新的 fix 卡生成时清掉上一轮的失败段（计数按当前 fix 轮次）。

## 6. 度量口径

| 指标 | 口径 |
|---|---|
| 门禁拦下 | brain log 行 `verify rc <n> → not pushed, the card is requeued (k/2)` 的条数（按 run 去重） |
| 验收缺失 | brain log 行 `no acceptance evidence` 的条数 / fix run 总数 |
| 重试耗尽 | PR 评论中出现 🔴 的次数 |

## 7. 验收

- 单测：`verify_rc=0` 推送；非 0 不推送、不计轮次、排队一次重试；两次本地重试后仍失败则推送并标红评论。
- 单测：卡面缺 `验收:` 时用既有测试命令兜底。
- tick 级集成测试：失败重排 → 再次派发 → 重试耗尽后推送并评论。
- CI：OpenSwarm tests 全通过；本卡有效改动 ≤ 250 行。
