# 收割塌缩护栏（Refs #1833）

fix/rebase 轮若不在 PR 当前 head 上续改、只产出增量补丁，AD 把该提交推回 PR 分支就会把整个 PR
替换成一个补丁。2026-11-03 实测（head 前后体量，文件数/改动行数）：

| PR | fix 前 | fix 后 | 结果 |
| --- | --- | --- | --- |
| #1807 | 8 / 1780 | 1 / 11 | 被 builder 强推还原 |
| #1804 | 6 / 1401 | 3 / 39 | 被 builder 强推还原 |
| #1715 | 8 / 868 | 2 / 23 | 被 builder 强推还原 |
| #1755 | 8 / 746 | 1 / 19 | 被 builder 强推还原 |
| #1653 | 6 / 200 | 2 / 19 | 被 builder 强推还原 |
| #1824 | 9 / 835 | 1 / 17 | 被 builder 强推还原 |

还原清单在盒子上：`gocc:~/phanes-ctl/autodispatch/bak/restore-collapsed-heads-20261103.json`
（前后 sha）。护栏做的是把这类事故挡在推送之前。

## 规则

收割推送前（`tools/lane-autodispatch/autodispatch.py` 的 `finish()`）对“fix 后 head”做两道检查：

1. **祖先关系**：新 head 必须以 fix 前 PR head 为祖先
   （`git merge-base --is-ancestor <fix 前 head> <fix 后 head>`）。不成立即视作整体替换，拒绝。
2. **体量不得塌缩**：新 head 相对 base 的**变更文件数**或**总改动行数**（`git diff --numstat`），
   **任一项**相对 fix 前 head 下降超过阈值即拒绝（默认 50%，不是两项都超才拒绝）。

阈值：`AD_HARVEST_COLLAPSE_RATIO`（环境变量，默认 `0.5`；`0.2` = 下降 20% 即拒绝）。
比较用的 base：优先用派发时的 base commit，本地没有时退回本地 `origin/main`。

**fail closed**：祖先判定或体量统计的 git 命令失败时一律不放行（记因、不推送）。
读不到 fix 前 head（远端 `ls-remote` 失败、目标 PR 的 head 取不回来）同样拒绝。

**豁免**：卡正文显式声明“整体重写”（含 `整体重写` / `full-rewrite` / `rewrite-allowed` 标记）
的卡跳过本护栏。确实要改写历史的轮次（例如明确要求重基到最新 main）必须在卡里写明。

## fix 前 head 从哪来

- 卡正文声明了目标 PR（`Rebase-PR: #N <branch>` / `重基PR: #N <branch>`，fix/rebase 轮的写法）：
  用该 PR 当前的 head（`gh pr view N --json headRefOid`；本地没有这个对象时先
  `git fetch origin <sha>` 取回）。
- 没有声明目标 PR：取即将推送的分支在远端的 head（`git ls-remote origin refs/heads/<branch>`）；
  分支还不存在（新建 PR）时没有可替换的 head，放行。

## 拒绝之后

- **不推送**：PR 分支保持 fix 前 head，不建 PR、不改分支。
- **不消耗 review 轮次**：PR 的 head 与审查计数都保持原样。
- 标 `lane-failed`，并在 issue 上留一条线索（PR 号、前后 sha、前后体量、判定原因）。
- brain 日志写一行，含 PR 号、前后 sha、前后体量与判定原因。
- 失败台账：`state.json` 的 `_harvest_guard` 追加一条记录（分类 `harvest_collapse`）。

## 部署对应关系

仓内 `tools/lane-autodispatch/autodispatch.py` 是部署版 `~/phanes-ctl/bin/autodispatch.py` 的镜像
（见 [`../tools/lane-autodispatch/README.md`](../tools/lane-autodispatch/README.md)）：
**本 PR 只改仓内镜像，部署版需要另行同步**（同步步骤见
[`../tools/autodispatch-backend-fallback.md`](../tools/autodispatch-backend-fallback.md)）。
同步后护栏在盒子上的 AD 里生效。

## 验收

上轮审查逐条对应：

- B1：卡正文读取或解析失败时拒绝收割，不再将失败当作空正文；
  `HarvestPushGuardTests.test_unreadable_card_with_missing_branch_fails_closed`
  覆盖目标 PR 卡读取失败且收割分支不存在，测试通过。
- B2：真实 SHA 回放尚未完成。当前 `CollapsedPrReplayTests.test_every_collapsed_pr_is_rejected`
  只覆盖合成历史，测试通过，但不能代替清单中真实 SHA 和 base 的回放。

```bash
python3 -m unittest openswarm.tests.test_harvest_collapse_guard -v
```

回放（2026-11-03 被还原的 6 个 PR；变体 A = fix 后 head 不在 fix 前 head 上续改，
变体 B = 续改了但把大部分改动删掉）：

```
PR      变体    前 文件/行        后 文件/行        判定
#1807   A     8/1780        1/11          新 head 不是 fix 前 head 的祖先（视作整体替换）
#1807   B     8/1780        1/11          变更文件数塌缩 8 → 1（阈值 50%）
#1804   A     6/1401        3/39          新 head 不是 fix 前 head 的祖先（视作整体替换）
#1804   B     6/1401        3/39          总改动行数塌缩 1401 → 39（阈值 50%）
#1715   A     8/868         2/23          新 head 不是 fix 前 head 的祖先（视作整体替换）
#1715   B     8/868         2/23          变更文件数塌缩 8 → 2（阈值 50%）
#1755   A     8/746         1/19          新 head 不是 fix 前 head 的祖先（视作整体替换）
#1755   B     8/746         1/19          变更文件数塌缩 8 → 1（阈值 50%）
#1653   A     6/200         2/19          新 head 不是 fix 前 head 的祖先（视作整体替换）
#1653   B     6/200         2/19          变更文件数塌缩 6 → 2（阈值 50%）
#1824   A     9/835         1/17          新 head 不是 fix 前 head 的祖先（视作整体替换）
#1824   B     9/835         1/17          变更文件数塌缩 9 → 1（阈值 50%）
```

以上是按体量合成的测试历史，12 条全部命中拒绝；这不能证明真实提交的祖先关系或体量。
真实 SHA 回放仍待取得盒子上的清单和对应 git 对象后执行。
