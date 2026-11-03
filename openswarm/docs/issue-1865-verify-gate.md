# #1865 fix 推送前 verify 门禁（设计页）

状态：**待统筹审阅**。本设计页先于 AD 改动单独提交，审阅通过后才改 `lib/dispatch/engine.py`。

## 1. 问题

AD 收割 fix run 时只检查「diff 非空」，`verify_rc` 只写进 PR 评论：推出去的改动没有「通过了验收」的证据；最近 3 个 fix run 里 2 个 `verify_rc` 为 null（卡面没有 `验收:` 行），门禁无从判断。

## 2. 隔离方式

- 改动文件：只有 `openswarm/lib/dispatch/engine.py`（收割门禁 + fix 卡面生成）。
- 不改：评审标准（`review.rounds` / `max_fails`）、测试标准（`thin_pr` / `harvest_exclude`）、lane 运行时 `bin/openswarm`。
- 生效路径：只有「lane 把改动推回已有 PR 分支」这条路径（fix / rebase 卡）；新开 PR 的路径不变。
- 不同步：`tools/lane-autodispatch/autodispatch.py`（box 镜像）没有推回 PR 分支的路径（只新开 PR）；`验收:` 行由 AD 写进卡面，镜像读卡即得。

## 3. 卡面要求与兜底

fix 卡（review CHANGES / CI 红灯 / 冲突重基）必须有一行 `验收:`，行内含反引号命令
（`验收: `python -m unittest openswarm.tests.test_dispatch_engine``）。

- 卡面已有 `验收:` 行 → 原样保留（不覆盖人写的验收）。
- 卡面没有 → AD 用**被改文件的既有测试命令**兜底（`engine.py` → `tests/test_dispatch_engine.py` →
  `python -m unittest openswarm.tests.test_dispatch_engine`，多个测试文件用 `&&` 连接，最多 4 个），并写回卡面。
- 找不到测试文件（只有文档/配置改动）→ 不写 `验收:` 行，按「无验收证据」处理（不拦推送，只记日志）。
- 兜底只算一次：卡面里的 `验收:` 行是唯一事实来源（派发时仍缺，再用兜底补一次）。

## 4. 推送前门禁与重试上限

收割 fix run 时、推送之前读 `verify_rc`：`0` 正常推送；`null` 不拦推送（没有证据 ≠ 失败，记日志
`no acceptance evidence`）；非 0 则**不推送、不消耗评审轮次**，把失败输出写进卡面并把卡重排回
`lane-ready`（同一 fix 重试）。本地重试上限 **2 次**（配置 `verify_gate.max_retries`）：第 3 次仍失败就推送，
并在 PR 评论标红 🔴 写明 verify 失败。

- 重排幂等：失败输出带 `<!-- lane-verify-fail pr=<n> run=<run> -->` 标记；同一个 run 再次收割时看到标记就不再追加、不再计次。
- 重排一次调用完成（`gh issue edit --body … --add-label lane-ready --remove-label in-progress`）：调用失败则保留收割状态，下个 tick 重试，不消耗重试预算。
- 新的 fix 卡生成时清掉上一轮的失败段（计数按当前 fix 轮次）。

## 5. 度量口径

- 门禁拦下：brain log 行 `verify rc <n> → not pushed, the card is requeued (k/2)` 的条数（按 run 去重）。
- 验收缺失：brain log 行 `no acceptance evidence` 的条数 / fix run 总数；重试耗尽：PR 评论中出现 🔴 的次数。
