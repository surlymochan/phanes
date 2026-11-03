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
python3 -m unittest openswarm.tests.test_lane_delivery_map
```

## 交付说明映射表预检（Refs #1849，rubric A7/F3）

首轮与 fix 轮的**交付说明**须含一张「验收条目→改动文件→测试」映射表：缺行时评审必报流程性
BLOCKER，白烧一轮。AD 在**推送前**检查这张表，缺表或缺行就把卡**退回作者**：不推送、不建 PR，
卡上写明缺什么并重新标记 `lane-ready`（作者再跑一次）。不进评审，也就不计评审轮次。

判定的两份载体（任一命中即可）：

- head 提交的正文（PR 带着它，评审在 PR 上读得到）；
- 分支里的交付说明文件 `.lane-review/PR-<n>.md`。

映射表的写法：表头要能看出「验收」列与「测试」列，每行三列都要写满（验收条目、改动文件、测试）：

```markdown
| 验收 | 改动文件 | 测试 |
| --- | --- | --- |
| `python3 -m unittest tools.tests` | `tools/lane-autodispatch/autodispatch.py` | `python3 -m unittest tools.tests` |
```

卡的每条验收句子都要有一行对应（`验收：`/`Verify：` 行，或验收段里的每条 bullet）。验收句子是
一条命令时，测试列写上同一条命令也算对应。

这项预检与 #1704 的 PR 必备字段校验（`Closes #N`、验收段、标题带号）共用同一个开关：
`AD_PR_VALIDATION=strict`（默认）缺项即退回，`warn` 只记录不退回（灰度用）。映射表的退回按
可快速修复类（`delivery_map`）处理，不计入 lane-failed 统计，也不计评审轮次。

## 队列优先级类与保底份额（Refs #1794）

队列分两类：**验证类**（`verify-intent` 标签 + `## Verify Intent` 段，即 P1-1 的 verify 卡）与
**工作类**（其余卡）。AD 派发时验证类在保底车道内优先派发（保底份额），工作类的并发上限
因此是 `total - reserve`（保底份额先满足）；两类都不会被对方锁死。

份额按**空闲车道与在途构成**算，不是全局静态数（车道暂停/故障/在途都计入）：

```
total        = 当前可参与派发的车道数（健康 + 未暂停 + 空闲或在途）
review       = 其中 review 车道数（CX2/CX4/CX5）
reserve      = min(review, total - 1, max(AD_VERIFY_MIN, round(total * AD_VERIFY_SHARE)))
verify_slots = reserve - verify_busy     # 验证类还剩的保底车道
work_slots   = total - reserve - work_busy # 工作类还剩的车道
```

`reserve` 至少留一个车道给另一类（`total - 1`），所以**车道不足时不会锁死全部派发**：保底
车道上没有对应类别的卡时，另一类照常派发。

### 优先级合成顺序（与既有排序规则的合成）

1. **类别准入**：验证类在保底车道（`verify_slots > 0` 且是 review 车道）上先于工作类；
2. **pin 类别**：own pin → unpinned → cross-pin（[`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md)）；
3. **同类别内**：`prio`（p0 < p1 < p2 < 无）→ issue 号；同一 pin 类别与 prio 下工作类先于验证类（`rank()`）。

类别准入只改写“验证类 vs 工作类”的先后，pin / prio / 号 的相对次序不变（稳定排序）。

### 配置项与部署启用方式

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `AD_VERIFY_SHARE_ENABLED` | `0`（关闭） | 保底份额策略总开关；关闭时派发行为与本策略之前完全一致 |
| `AD_VERIFY_SHARE` | `0.2` | 保底比例：保底车道数 `max(AD_VERIFY_MIN, round(total * 比例))` |
| `AD_VERIFY_MIN` | `1` | 保底车道数下限 |

数值配置写错时回退默认值（`env_number()`），不会让 AD 在导入时崩掉。

默认**关闭**（`AD_VERIFY_SHARE_ENABLED=0`），要启用必须显式设置：

```bash
# 1) 同步仓库镜像到 box 运行时
cp /workspace/phanes-repo/tools/lane-autodispatch/autodispatch.py ~/phanes-ctl/bin/autodispatch.py
# 2) 在 AD 的启动环境里导出开关（start-control-plane 启动的 lane-autodispatch 也要拿到它）
export AD_VERIFY_SHARE_ENABLED=1        # 可选：export AD_VERIFY_SHARE=0.2 AD_VERIFY_MIN=1
# 3) 重启 AD 循环后生效
~/phanes-ctl/bin/start-control-plane
```

生效与让行都写日志（`~/phanes-ctl/autodispatch/autodispatch.log`）：

- `queue share: <lane> prefers verify|work (verify deficit=…, work slots=…); other class yields` —— 份额生效、另一类让行；
- `queue share: dispatched <class> #<n> on <lane>` —— 本次派发的类别。

离线验证：`python3 tools/lane-autodispatch/test_can_take.py`（含工作洪峰 / 验证洪峰两个场景）。

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
