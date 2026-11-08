# lane-autodispatch (repo mirror)

Runtime copy lives on the box at `~/phanes-ctl/bin/autodispatch.py`.
Backend starvation rules: [`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md).

Two versions, one source: this file is the repo mirror, `~/phanes-ctl/bin/autodispatch.py` on the
box is the deployed runtime the control plane syncs from it (sync steps:
[`../autodispatch-backend-fallback.md`](../autodispatch-backend-fallback.md)); a change here is
only live on the box after that sync.

**旧 dispatcher 已退役**：上游已删除 `lib/dispatch/engine.py`；本轮一并移除无生产调用方的
phanes_only dispatch 扩展及其测试。当前自动派发使用 swrm 原生 batch/fleet。
本目录仍保留独立的旧控制面镜像和离线测试，不包含旧 engine 的审查派发实现。
`tools/check-ad-review-pause-uncounted.py` 的运行副本契约不适用于本镜像。

Base-commit validation: the base commit must exist in the target repo before a lane is assigned
(`git -C <repo> cat-file -e <base>^{commit}`). A missing base is fetched into the target repo and
the check is repeated (Refs #1810); a base that is still missing is reported and backed off as
before. A failing fetch aborts the dispatch right away: a later successful check (e.g. a concurrent
tick that fetched the object in the meantime) must never mask it.

```bash
python3 tools/lane-autodispatch/test_can_take.py
python3 tools/lane-autodispatch/test_harvest_filter.py
python3 tools/check-ad-base-fetch-on-miss.py
python3 -m unittest tools.tests.test_lane_pr_validation
```

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

`reserve` 至少留一个车道给另一类（`total - 1`），所以**车道不足时不会锁死全部派发**：工作类
的上限至少留一条车道，验证类的保底份额先满足；保底车道上没有验证类卡时，该车道照常派工作卡
（工作类未达上限时）。

### 工作类上限是硬上限（Refs #1864）

`work_slots == 0`（工作类已占满 `total - reserve` 条车道）时**不派发工作卡**：工作卡等待车道
释放，直到有车道空出来。上限是限流（cap），不是排序偏好——只把工作卡排到前面（`preferred=false`）
时工作卡仍会被派发，上限没有落实。验证类不受影响：保底份额内的 review 车道照常派发验证卡。

### 优先级合成顺序（与既有排序规则的合成）

1. **类别准入**：验证类在保底车道（`verify_slots > 0` 且是 review 车道）上先于工作类；工作类达
   上限（`work_slots == 0`）时不派发工作卡；
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

- `queue share: <lane> prefers verify (verify deficit=…, work slots=…); other class yields` —— 份额生效、另一类让行；
- `queue share: <lane> work class at its cap (work slots=0, verify deficit=…); N work card(s) wait` —— 工作类达上限，工作卡等待；
- `queue share: dispatched <class> #<n> on <lane>` —— 本次派发的类别。

离线验证：`python3 tools/lane-autodispatch/test_can_take.py`（含工作洪峰 / 验证洪峰 / 工作类上限三个场景）。

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

## 派发简报模板：实现卡三行与 fix 轮 F1–F6（Refs #1839 / #1851）

AD 写给 lane 的任务书（task book）按卡的类别注入不同的硬性要求，常量在
`autodispatch.py` 里，注入点在 `dispatch()`：

| 卡 | 常量 | 强制的要求 |
| --- | --- | --- |
| 实现卡（默认类别） | `IMPL_CARD_TASK_BOOK` | 交付说明逐个新增函数列出**入口→调用链→数据结构来源**；至少一个 **tick/集成级测试**（从真实入口驱动到可观测结果，只测函数加 mock 不算） |
| 修复轮 | `FIX_ROUND_TASK_BOOK` | rubric **F1–F6** 六条清单 |

修复轮的六条（`FIX_ROUND_TASK_BOOK`）：

| 条 | 要求 |
| --- | --- |
| F1 | 从 `rebase-pr-<N>` 续改，先做祖先校验（`git merge-base --is-ancestor <base> rebase-pr-<N>`）；校验不过就停下报告，不另起分支重写 |
| F2 | 先枚举同类点（同样的写法、同样的调用点），再逐个修 |
| F3 | 上一轮每条 BLOCKER 逐条勾选（`- [x]`）并附证据（文件:行号 + 测试名或命令输出） |
| F4 | 先写能复现问题的失败测试并跑一次确认是红的，再改实现让它转绿 |
| F5 | 不删既有集成测试（要改断言就改断言并写明理由） |
| F6 | 修完复跑 A4/A2 两项验收（同一条命令、同一个环境） |

**修复轮卡的标记是显式的**，不靠标题猜：fix 标签（`fix` / `fix-round` / `review-fix` /
`rework`，含 `card:` / `type:` 前缀）、正文里的 `## Fix Round` 段，或标题前缀
（`fix round:` / `修复轮：` 等）。普通工作卡（只是修一个 bug）不带清单；planner /
follow-up / ops / upstream / verify-intent 这些类别的卡也不要清单（它们不交代码）。
修复轮仍是实现卡，所以三行交付要求照样注入。

`{pr}` 占位符替换成卡里指出的 PR 号（`rebase-pr-<N>` 分支名或 `目标 PR：#N` 字段，
标题里的 `PR #N` 也算）；卡没写 PR 号时保留 `<N>`。

离线验证：

```bash
python3 -m unittest tools.tests.test_dispatch_brief_templates -v   # 模板渲染
python3 -m unittest tools.tests.test_fix_round_brief -v            # 注入接线
python3 -m unittest tools.tests.test_impl_card_task_book -v         # 实现卡三行
```

## 收割前的越界产物过滤（Refs #1835）

收割 fix/rebase/impl 的结果、推送回 PR 分支之前，AD 对待推送分支的变更文件做机械过滤：
匹配到隐藏二进制（以 `.` 开头的 `.so`，如 `swrm/tests/.dde4*-00000000.so`）或
`.lane-review/` 目录下的文件（评审产物）时，把这些路径从提交中剔除（保留其余改动、
重新提交），剔除清单写 brain 日志一行。剔除后若有效改动为空（比较最终树与基线树，
不是比较 head），按空结果处理、不推送；过滤本身失败（git 命令报错）时 fail closed、
不推送并记因。规则是列表常量（`HARVEST_EXCLUDE_PATTERNS`），可配置。

旧 dispatcher 及其离线回放工具已删除。仓内 AD 的过滤由
`test_harvest_filter.py` 独立验证；不再与已删除的模块同步。

## 开关来源审计（Refs #1827）

`PAUSE*` / `EXCLUDE*` 文件是派发开关：今天它们被不明来源写入，事后追不到创建者，只能
人读 brain 日志。AD **每轮 tick** 比对 autodispatch 目录（`~/phanes-ctl/autodispatch`）下的
开关集合（文件名 + mtime + 内容），有变化就往 `switch-audit.jsonl` 追加一行：

| 字段 | 含义 |
| --- | --- |
| `at` | 审计时间（`%F %T`） |
| `file` | 开关文件名 |
| `action` | `created` / `deleted` / `changed`（mtime 或内容变了就是 `changed`） |
| `before` / `after` | 前后内容摘要（首行，截断到 120 字符） |
| `owner` | 属主（uid / gid / 用户名） |
| `mtime` | 文件 mtime（epoch 秒；删除时是删除前的 mtime） |
| `unattributed` | 是否缺授权条目 |
| `authorization` | 命中的授权条目（brain 日志尾行或开关自身的 `auto:`） |

基线（上一轮的快照）写在 `switch-audit-state.json`；基线损坏时只重建基线、不写行（不拿
损坏的基线伪造一次变化）。

**读不到 ≠ 不存在**：开关目录列不出来、或某个开关读不了（权限/IO 错误）时，这一轮的
快照是不完整的，审计**跳过比较、也不更新基线**（只记一行日志）。读不到的文件不算被删除，
恢复后仍与上一轮完好基线比较，不产生假的 `deleted` / `created`；这一轮里真实发生的删除，
会在恢复后的那一轮按真实的 before/after 记一次。

每个开关每轮**只 stat 一次**：`switch_stats()` 用这一次 `os.stat()` 的 `st_mode` 判断是不是
普通文件，`switch_entry()` 复用同一个 stat 结果。不能再用第二次 stat（`os.path.isfile()`
会自己吞掉 OSError）：第二次 stat 失败会把**仍存活**的开关踢出集合，快照却仍被标成完整，
于是写出假的 `deleted`、改坏基线，恢复后再写出假的 `created`。

授权判定：`brain/log.md` 最近 30 行命中 `用户.*令|user order|auto:` 就算有授权；开关自身
内容以 `auto:` 开头也算有授权（health-loop 自己写的开关，见 `docs/ctl-boot.md`）。两条都
没有的变化行标 `unattributed`，并往 `~/phanes-ctl/autodispatch/health.alert` 追加一条
`level=hard source=switch-audit` 告警。

**只审计、只告警：不自动删除、不自动恢复开关。** 审计文件损坏、开关目录不可读时，审计
跳过后记一行日志即可，派发主循环不受影响（`audit_switches()` 内部吞掉所有异常）。

离线验证：

```bash
python3 tools/check-ad-switch-audit.py                    # 临时目录里 touch 一个 PAUSE-DISPATCH，下一轮 tick 出审计行
cd swrm && python3 -m unittest tests.test_autodispatch.SwitchAuditTests -v
```

### 修复交付对照（review 意见 → 改动 → 测试）

测试都在 `swrm/tests/test_autodispatch.py::SwitchAuditTests`。

| 轮次 | 意见（BLOCKER） | 对应改动 | 测试 |
| --- | --- | --- | --- |
| 第 1 轮 B1 | 单个开关 stat/读取失败被当成删除（`switch_entry` 返回 None → 快照省略它 → 假的 `deleted` + 告警、基线被写成缺文件的、恢复后再假 `created`） | `switch_entry()` 读失败返回 None；`switch_snapshot()` 快照不完整时返回 `complete=False`；`_audit_switches()` 快照不完整时**跳过比较、不更新基线**，派发循环照常跑 | `test_a_read_failure_does_not_fake_a_deletion`、`test_the_recovery_after_a_read_failure_does_not_fake_a_creation`、`test_a_deletion_during_a_read_failure_is_audited_after_the_recovery` |
| 第 1 轮 B1 | 目录枚举失败被当成空开关集（`switch_stats()` 返回空集合） | `switch_stats()` 目录列不出来时返回 `complete=False`，同样跳过比较与基线更新 | `test_an_unreadable_directory_does_not_fake_a_deletion`、`test_an_unreadable_switch_directory_is_survivable` |
| 第 2 轮 B1 | `os.path.isfile()` 的第二次 stat 吞掉 OSError：仍存活的开关被漏掉、快照被标成完整 → 假 `deleted` + 基线被改写，恢复后假 `created` | `switch_stats()` 复用第一次 `os.stat()` 的 `st_mode`（`stat.S_ISREG`）判断文件类型，不再第二次 stat；`switch_entry()` 复用同一个 stat 结果（每轮每开关只 stat 一次） | `test_a_stat_failure_does_not_fake_a_deletion`、`test_the_recovery_after_a_stat_failure_does_not_fake_a_creation`、`test_a_switch_is_stat_ed_only_once_per_tick`、`test_a_living_switch_is_audited_when_a_second_stat_would_fail` |
| 第 2 轮 B2 | 交付说明没有逐条列出上轮 B1、对应改动和具体测试 | 本表（README）与提交信息里的同一张对照表 | — |
