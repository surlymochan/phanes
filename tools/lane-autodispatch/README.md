# lane-autodispatch (repo mirror)

Runtime copy lives on the box at `~/phanes-ctl/bin/autodispatch.py`.
Backend starvation rules: [`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md).

Two versions, one source: this file is the repo mirror, `~/phanes-ctl/bin/autodispatch.py` on the
box is the deployed runtime the control plane syncs from it (sync steps:
[`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md)); a change here is
only live on the box after that sync.

Base-commit validation: the base commit must exist in the target repo before a lane is assigned
(`git -C <repo> cat-file -e <base>^{commit}`). A missing base is fetched into the target repo and
the check is repeated (Refs #1810); a base that is still missing is reported and backed off as
before. A failing fetch aborts the dispatch right away: a later successful check (e.g. a concurrent
tick that fetched the object in the meantime) must never mask it.

```bash
python3 tools/lane-autodispatch/test_can_take.py
python3 tools/check-ad-base-fetch-on-miss.py
python3 -m unittest tools.tests.test_lane_pr_validation
python3 -m unittest openswarm.tests.test_harvest_collapse_guard
```

## PR 提交前校验（Refs #1704）

收割完成、真正要提 PR 时，AD 用 lane 自己的产物（commit 标题、commit 正文、执行器
summary/tests、host verify rc）拼出 PR 标题与正文，再逐项校验：

- 正文含 `Closes/Fixes/Resolves #N`（N = 任务 issue）：关闭引用只能来自 lane 自己写的
  关闭关键字（commit 正文或执行器 summary），标题里的 issue 号不算关闭指令，AD 不代写；
- 正文含验收段或 Verify 段，且内容是真实证据（host verify rc、lane 自己的验收句或
  lane 报告的测试命令；缺失的测试结果保持为空，`verify rc` 缺失时不再伪造验收行）；
- 标题含 issue 号。

缺项逐条返回给 runner；严格模式（默认，`AD_PR_VALIDATION=strict`）不创建 PR，把 issue
退回 `lane-ready`，并按可快速修复类（`pr_validation` / `quick_fix`）记录，不打
`lane-failed`，避免污染失败统计。告警模式（`AD_PR_VALIDATION=warn`）只记录缺项照常提 PR。
校验结果写入 run 记录（lane 主机 `runs/<run>/pr-validation.json` 与 `pr-validation.log`）
和 AD 侧 `~/phanes-ctl/autodispatch/runs/<run>.log`，供阻塞诊断引用。

## 收割塌缩护栏（Refs #1833）

收割推送前核对“fix 后 head”：它必须以 fix 前 PR head 为祖先
（`git merge-base --is-ancestor`），且相对 base 的变更文件数**或**总改动行数任一项相对
fix 前 head 下降不超过阈值（默认 50%，`AD_HARVEST_COLLAPSE_RATIO`）。不满足即拒绝推送：
PR 分支保持 fix 前 head、不建 PR、不消耗 review 轮次，issue 标 `lane-failed`，brain 日志写
一行（PR 号、前后 sha、前后体量、判定原因），`state.json` 的 `_harvest_guard` 追加台账记录。
git 命令失败一律 fail closed；卡正文声明“整体重写”可豁免。

规则、fix 前 head 的来源与 2026-11-03 六个被还原 PR 的回放：
[`../../docs/harvest-collapse-guard.md`](../../docs/harvest-collapse-guard.md)。
