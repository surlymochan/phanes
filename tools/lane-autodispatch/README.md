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
python3 tools/check-ad-idle-redispatch.py
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

## 证据审查风险分级（Refs #1988）

验证类卡派发前，AD 用纯函数分类器（`tester_risk.py`）判定这张卡的证据要读多深，输出
`full_read`（全读）或 `sampled`（按固定种子抽检），并把决策与理由写进卡状态：

- 输入：目标 PR 的 diff 统计（改动文件数、增删行数）、历史 `needs-human` 标记（含已摘掉的
  标签）、可配置的抽检散列（seed + 抽检率）；
- 规则：大 diff（≥ 20 个文件或 ≥ 1000 行变更）或任一 `needs-human` 前科 → 恒为 `full_read`；
  常规卡按 `SHA-256(seed, PR 号)` 稳定抽检（同一 PR 每次结果相同），被抽中的那部分按全读处理；
  数据缺失或畸形 → 保守 `full_read`；
- 写入：决策与理由进派发卡正文（车道读到的卡）、车道状态 `state[lane]["tester_risk"]`，以及
  按卡保留的 `state["_tester_risk_cards"][<issue>]`（车道释放后仍可查）。

### 配置项

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `AD_TESTER_RISK_SAMPLE_RATE` | `0.2` | 常规卡中被稳定抽中（按全读处理）的比例（0 = 常规卡一律抽检，1 = 常规卡一律全读） |
| `AD_TESTER_RISK_SEED` | `risk-v1` | 抽检散列种子，同一 seed 下同一 PR 的结果稳定 |

### 部署

分类器与调度器同目录，部署时一起复制；缺少分类器不会中断派单，只是所有卡都按全读处理：

```bash
cp /workspace/phanes-repo/tools/lane-autodispatch/autodispatch.py ~/phanes-ctl/bin/autodispatch.py
cp /workspace/phanes-repo/tools/lane-autodispatch/tester_risk.py ~/phanes-ctl/bin/tester_risk.py
```

离线验证：`python3 -m pytest -q tools/lane-autodispatch/test_tester_risk.py`。

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

## 派发简报模板（#1851）

实现卡要求入口、调用链、数据结构来源和 tick/集成测试。修复轮额外注入 F1–F6 清单；
PR 号取自卡内容，未指定时保留 `<N>`。非实现类别不注入修复清单。

## 收割前的越界产物过滤（Refs #1835）

收割 fix/rebase/impl 的结果、推送回 PR 分支之前，AD 对待推送分支的变更文件做机械过滤：
匹配到共享对象文件（`.so`，如 `swrm/tests/.dde4*-00000000.so`）或
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

## 车道空闲间隙（Refs #1688）

run 结束到下一张卡之间天然存在空窗：池子不空、容量不卡时，这段空窗纯属调度延迟，直接拉低
车道的并行利用率。本轮把空窗收窄，并把它变成可观测、可归因的指标。

**即时重派发**：收割/结束一条 run 后，`tick()` 立即对同一车道再尝试一次派发，再收割下一条
车道；不等其他车道的慢收割或下一个 tick。队列与 label 权威读在首次派发时加载，同轮共享；
队列读取失败时继续收割其余车道，同轮不重复读取。即时重派发与普通派发走同一条 acquire
路径（`give_card()`）：同一套车道暂停检查（`PAUSE-<lane>`）、同一套主机可用性检查、同一次
`dispatch()`、同一张卡的选择与 label 权威读。拿不到卡或拿不到槽位时静默失败：移除 in-progress
标签、不重试、不占位，也不给整台主机退避（容量耗尽和宿主仍报 busy 都不计为宿主故障），留给
下一 tick；真实 SSH 或 base fetch 故障仍按原有逻辑退避。被暂停的车道不派发。

**空闲时长指标**：每条车道的空闲窗口记在 state 的 `_idle`（`since`/`last_seen` 与各原因的秒数），
关闭后写入 `_idle_last`。`lanes-status.json` 新增：

| 字段 | 含义 |
| --- | --- |
| `_idle_s` | 每条车道**最近一次**空闲时长（上次 run 结束 → 本次派发，秒） |
| `_idle_reason` | 该间隙的原因 |
| `_idle_open_s` | 仍在等待的车道当前已经空闲的秒数 |
| `_idle_worst_s` | 当前最大的空窗 |

**原因归因**：空闲超过 5 分钟（`AD_IDLE_LOG_S`，默认 300s）的派发在 brain log 写一行
`<lane> idle <n>s before dispatch (reason: <reason>)`。原因是各时间桶里最长的一桶：
`dispatcher not running`（派发器停机）、`no card`（无卡）、`no capacity`（无容量）、
`lane paused`（车道暂停）、`dispatcher not dispatching`（收割/派发延迟）。每个空闲窗口的桶
从零开始：上一个窗口的停机时长不会解释下一个窗口的间隙。
两次观察之间的在线等待按上次观察结果累计；本轮结果只用于之后的等待，避免把此前的
“无卡”时长记给本轮刚出现的“无容量”（反向切换同理）。

**结束时间的采集**：空闲窗口从 run 的**真实结束时间**起算，不是从收割时刻。远端状态片段
（`REMOTE_STATUS`）读取 `runs/<run>/result.json` 与 `runs/<run>/status.json` 的结束时间戳
（`finished_at`/`ended_at`/`finished`/`updated_at`；布尔标志不是时间，不参与选取），没有时退回
result.json 的 mtime。收割延迟、或派发器停机期间结束的 run 因此不会被算成“刚刚空闲”。

**停机时长**：派发器的心跳（`$D/heartbeat`，tick 之间每 5s 刷新一次）是它活着的证据；心跳与
当前 tick 的间隔就是派发器自己的停机时长（`HEARTBEAT_GRACE_S` 吸收心跳抖动）。关闭窗口前按
**旧心跳**累计截至 tick 开始时的停机时长；tick 内收割与派发耗时计入 `lag_s`，不会被算成停机。
每条车道成功派发时立即关闭自己的窗口，后续车道派发耗时不延长它的指标，也不会产生虚假的长空闲日志。
tick 开始前的其余时间才落到之前的原因桶，所以“无卡 → 派发器停机 15 分钟
→ 恢复当轮就派发成功”的间隙归因是“派发器未运行”，不是“无卡”。

离线验证：

```bash
python3 tools/check-ad-idle-redispatch.py                 # 26 个 tick 级 / 集成用例
python3 tools/lane-autodispatch/test_idle_redispatch.py   # 9 个复现用例（基线全红）
```

### 修复交付对照（review 意见 → 改动 → 测试）

测试在 `tools/check-ad-idle-redispatch.py`（tick 级 / 集成）与
`tools/lane-autodispatch/test_idle_redispatch.py`（复现用例）。

| 轮次 | 意见（BLOCKER） | 对应改动 | 测试 |
| --- | --- | --- | --- |
| 批次复审 B1 | 容量预留开启时，刚收割但远端快照仍为 busy 的车道被漏算，工作槽位误为 0 | 本轮已释放车道纳入同一 `queue_class_limits()` 可用容量计算，仍检查暂停、主机与工作类上限 | `test_verify_share_counts_a_just_harvested_lane_as_available`、`test_verify_share_still_reserves_capacity_after_harvest`、`test_verify_share_uses_a_free_work_slot_only_once` |
| 第 1 轮 B1 | 即时重派发绕过车道暂停：运行中的车道被暂停后，run 一结束仍收到新任务 | 即时重派发与普通派发合并到 `give_card()`：同一套 `lane_paused()` / `host_ok()` 检查，暂停车道不派发 | `test_a_paused_lane_gets_no_card_after_its_run_ended`（两个文件各一份） |
| 第 1 轮 B2 | 远端结果没有结束时间，空闲时长从收割时刻起算；收割重试、push、建 PR 与停机恢复都会少算 | 远端状态片段采集 run 的结束时间（`ended_at`），空闲窗口从真实结束时间起算 | `test_the_idle_metric_starts_at_the_real_end_of_the_run`、`test_the_remote_status_reports_the_end_time_of_the_run` |
| 第 1 轮 B3 | `setdefault` 保留上一个窗口的 `down_s`，一次停机之后所有长空闲都被归因为派发器未运行 | 每个空闲窗口的桶从零开始（`idle_open()` 新建窗口），窗口关闭后被丢弃 | `test_the_next_idle_window_does_not_inherit_the_downtime_of_the_previous_one` |
| 第 2 轮 B1 | 缺 run 结束后暂停车道不派发的 tick 级测试 | 补 tick 级用例（暂停车道 + 收割同一轮） | `test_a_paused_lane_gets_no_card_after_its_run_ended` |
| 第 2 轮 B2 | 缺延迟收割与派发器停机恢复的 tick / 集成测试 | 补两个用例：停机 900s 后恢复当轮派发、窗口跨停机后从零开始 | `test_the_downtime_of_the_dispatcher_is_the_reason_of_a_long_gap`、`test_the_next_idle_window_does_not_inherit_the_downtime_of_the_previous_one` |
| 第 2 轮 B3 | `_idle_down_s` 归零但没有停机时长累计，非零长间隙无法归因为派发器未运行 | 按心跳与上次观察的间隔累计停机时长（`dispatcher_downtime()`） | 同上 |
| 第 3 轮 B1 | 已开启的空闲窗口在恢复后当轮派发时直接 `idle_close`，跳过停机累计：上次观察以来的全部时长都记给了上次结果，没有用旧心跳算停机（无卡后停机 15 分钟、恢复有卡，日志仍归因为无卡） | `idle_close()` 关闭窗口前用**旧心跳**（`dispatcher_last_alive()`）累计该窗口的停机时长，其余时间才落到上次观察的原因桶；窗口随关闭被丢弃，下一个窗口从零开始 | `test_the_downtime_of_an_open_window_is_accounted_when_the_dispatcher_returns`、`test_the_window_after_a_dispatcher_outage_starts_empty` |
| 第 3 轮 B2 | `tools/check-ad-idle-redispatch.py` 把 `remote` 整体替换为假实现并直接注入 `ended_at`，延迟收割与停机恢复没有执行真实的结果采集 | 远端状态片段用 `stamp()` 只挑真正的时间戳（布尔 `finished` 不算时间）；新增集成用例：真实片段读假执行器 home → tick → 指标 → brain log | `test_the_real_remote_status_drives_the_idle_metric`（另见 `test_the_remote_status_reports_the_end_time_of_the_run`） |
| 第 3 轮 B3 | 交付说明只概述实现与测试，没有按 B1/B2/B3 逐条映射 | 本表（README）与提交信息里的同一张对照表 | — |
| 第 4 轮 B1 | 先串行收割所有车道、再统一重派发，一条车道被另一条车道的慢收割阻塞 | 每条车道释放后立即调用现有 `give_card()`，再处理下一条车道；队列首次派发时加载、同轮共享，读取失败也继续收割且不重试 | `test_a_finished_lane_is_redispatched_before_another_lanes_slow_harvest`、`test_a_queue_outage_does_not_stop_harvesting_or_retry_in_the_same_tick` |
| 第 4 轮 B2 | 容量不足仍调用 `host_fail()`：记成主机故障并退避，挡住同主机的其他车道 | `dispatch_failed()` 识别容量耗尽，移除 claim 后静默返回；真实主机故障仍退避 | `test_a_capacity_failure_is_the_reason_of_the_gap`、`test_a_capacity_failure_does_not_block_another_lane_on_the_same_host`、`test_a_transport_failure_still_backs_the_host_off` |
| 第 4 轮 B3 | 所有车道派发结束后统一关闭窗口，后续车道的耗时虚增前一车道的指标与长空闲日志 | `give_card()` 在本车道派发成功后立即关闭窗口并记录日志 | `test_each_lane_gap_ends_at_its_own_dispatch` |
| 第 4 轮 B4 | 本轮失败原因被用于上一观察区间（600 秒无卡后才出现的无容量覆盖了已知原因） | `idle_account()` 与 `idle_close()` 一致：按窗口保存的上次结果累计已过时间，再保存本轮结果供下一区间使用 | `test_a_new_capacity_failure_does_not_replace_the_previous_no_card_wait`、`test_an_empty_pool_does_not_replace_the_previous_capacity_wait` |
