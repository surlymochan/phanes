# Issue #18 审计：OpenSwarm 文档与实现漂移

- 审计对象：`openswarm/README.md`、`openswarm/docs/*.md`、`openswarm/SKILL.md`
- 实现基准：`openswarm/bin/openswarm`、`openswarm/bin/fleet-capacity`、`openswarm/policy.json`、`openswarm/bin/pi-minimax-broker.mjs`
- 基线提交：`df02c552d0b3d2b16d71b60c0b18dd39b71ea2ad`（2026-10-31）
- 审计日期：2026-10-31

## 1. 方法与判定口径

逐条抽取 README / docs 中的**可核对声明**（默认值、数量、开关名、命令与标志、阈值、标签、后端集合、路径、协议行为），
再到 `bin/openswarm`、`bin/fleet-capacity`、`policy.json` 中定位对应实现，记录行号作为证据。

判定口径：

- **错误**：声明与当前实现不一致（含“穷举式”列表遗漏了现网已有的项）。
- **正确**：与实现一致。
- **不完整但不错误**：只描述了实现的一部分，未构成事实错误；本次**不修改**，仅在第 5 节列出建议。
- **仓库外不可验证**：依赖本仓库之外的运行环境（Pi/Antigravity 版本、远端主机配置等），只记录、不判错。

本次只修改“错误”的声明，保持改动最小且可追溯；被改动的均为**枚举/默认值/行为描述**，没有改写叙述性内容。

## 2. 结论摘要

| # | 文档位置 | 声明 | 判定 | 处理 |
|---|---|---|---|---|
| 1 | `openswarm/README.md:115` | `concurrency` 默认值 `{"codex": 4, "minimax-cn": 6, "total": 10}` | 错误（漏 `agy: 2`） | 已修复 |
| 2 | `openswarm/README.md:703`、`openswarm/SKILL.md:184-185` | `risk.enforce` 默认 5 项 | 错误（漏 `computer use`） | 已修复 |
| 3 | `openswarm/README.md:605-607`（架构图） | “Cursor CLI is a fourth executor” | 错误（现有 8 个执行器） | 已修复 |
| 4 | `openswarm/README.md:114` | 一次性 Pi/Codex/Cursor 均可在同一会话内重试 429 | 错误（隔离 Pi 无法续会话，故不重试） | 已修复 |
| 5 | `openswarm/README.md:167` | `--quiet` 只保留 8 类事件 | 错误（漏 `RATELIMIT`） | 已修复 |
| 6 | `openswarm/README.md:63` | OmO 进程内路由延迟中位数 0.026 ms | 错误（现值 0.0292 ms） | 已修复 |
| 7 | `openswarm/docs/BUS.md:38` | `bus-worker --backend` 可选 `codex, ocgo, deepseek, cursor, pi` | 错误（漏 `agy`） | 已修复 |
| 8 | `openswarm/docs/executor-recovery.md:15` | 每个 broker 进程最多接受 64 次推理请求 | 错误（实为最多 4 个并发请求） | 已修复 |
| 9 | `openswarm/docs/executor-recovery.md:31` | Pi 只读 review / consult 一律被拒绝 | 错误（经隔离启动器时可用） | 已修复 |

## 3. 已修复的漂移（逐条证据）

### 3.1 `concurrency` 默认值漏 `agy`

- 文档原文：`openswarm/README.md:115` — “defaults are `{"codex": 4, "minimax-cn": 6, "total": 10}`”
- 实现：`openswarm/policy.json:258-263` — `"concurrency": {"codex": 4, "minimax-cn": 6, "agy": 2, "total": 10}`；
  读取逻辑 `openswarm/bin/openswarm:3308-3316`（`provider_concurrency_limits`），键集合
  `openswarm/bin/openswarm:3292-3294`（provider → backend → `total`）。
- 判定：错误。该声明是穷举式 JSON，默认值缺少 `agy: 2`。
- 修复：README 改为 `{"codex": 4, "minimax-cn": 6, "agy": 2, "total": 10}`。

### 3.2 `risk.enforce` 默认值漏 `computer use`

- 文档原文：`openswarm/README.md:703`、`openswarm/SKILL.md:184-185`
- 实现：`openswarm/bin/openswarm:3938` —
  `ENFORCED_RISKS = ["push", "publish", "secret print", "rm outside worktree", "sudo", "computer use"]`；
  生效路径 `openswarm/bin/openswarm:4211-4218`（`enforced_risk`，其中 `"computer use"` 触发
  `computer_use_risk`，`openswarm/bin/openswarm:4148-4166`，拦截 Codex Computer Use 工具调用）。
- 判定：错误。两个文档都把默认值写成 5 项，实际默认 6 项。
- 修复：README 与 SKILL.md 均补上 `computer use`。

### 3.3 架构图称 Cursor 是“第四个执行器”

- 文档原文：`openswarm/README.md:605-607` — “Cursor CLI is a fourth executor, opt-in only (`--backend cursor`).”
- 实现：执行器共 8 个 —— `openswarm/bin/openswarm:4750-4751`（`EXECUTORS`：codex / ocgo / deepseek / cursor /
  agy / pi / hermes / copilot），路由器同集合 `openswarm/bin/openswarm:3167-3168`（`PLANNERS`）；
  `openswarm/bin/openswarm:11418` 的 `--backend` 取值同集合；`bin/openswarm` 文件头
  （`openswarm/bin/openswarm:2-6`）已说明 agy、pi 为 opt-in。
- 判定：错误。“第四个”这一数量描述已过期，且遗漏 agy / pi / hermes / copilot。
- 修复：架构图改为列出 Cursor、Antigravity、Pi、Copilot、Hermes 均为 opt-in，并说明除非加入
  `routing.default_order` 否则不进入自动路由顺序（`openswarm/policy.json:74-76`，当前仅 `codex`）。

### 3.4 一次性 Pi 的 429 重试描述

- 文档原文：`openswarm/README.md:114` — “One-shot Pi, Codex and Cursor runs retry visible 429s in the same session”
- 实现：`openswarm/bin/openswarm:3762-3763` — 重试前提是 `can_resume`，其中
  `(meta or {}).get("resume_supported") is not False`；隔离 Pi 明确返回
  `resume_supported: False`（`openswarm/bin/openswarm:4678`、`4700`，提示 “isolated Pi cannot resume a session”），
  而 `pi_bin()` 默认优先返回隔离启动器 `bin/pi-isolated`（`openswarm/bin/openswarm:3075-3093`）。
- 判定：错误。默认的一次性 Pi 走隔离启动器，不能续会话，因此不会重试；Codex（有 `thread_id`）与
  Cursor（`--continue`，`openswarm/bin/openswarm:4449-4452`）才会重试。
- 修复：README 明确“一次性 Pi 仅在会话可续时重试；隔离启动器不能续会话，故不重试”。
  该事实与 `openswarm/docs/executor-recovery.md:32`（“One-shot isolated Pi cannot resume a rate-limited session”）
  保持一致。

### 3.5 `watch --quiet` 事件清单漏 `RATELIMIT`

- 文档原文：`openswarm/README.md:167` — “`--quiet` keeps only `DONE`, `EXITED`, `RISK`, `STALL`, `ERROR`,
  `BUDGET`, `ATTEMPT`, and `HEARTBEAT`.”
- 实现：`openswarm/bin/openswarm:9964-9966` — 静默模式保留集合为
  `DONE, EXITED, RISK, STALL, ERROR, BUDGET, ATTEMPT, HEARTBEAT, RATELIMIT`；限流重试事件在
  `openswarm/bin/openswarm:9992-9996`、`:10030-10034` 产生。
- 判定：错误。声明使用“only”，实际还保留 `RATELIMIT`。
- 修复：README 补上 `RATELIMIT`。

### 3.6 OmO 路由延迟中位数

- 文档原文：`openswarm/README.md:63` — “Routing decision latency, in-process median | **0.008 ms** | 0.026 ms | – |”
- 实现/数据：`openswarm/bench/results/latency_omo.json` — `in_process.median = 0.0292`（n=1120, p95=0.1027），
  与 `openswarm/bench/results/summary.md:144`、`openswarm/bench/README.md:299` 一致；
  openswarm 侧 `openswarm/bench/results/latency_openswarm.json` 的 `in_process.median = 0.0075`（≈0.008 ms，与原文一致）。
- 判定：错误。OmO 一侧数值与仓库内基准数据不符。
- 修复：改为 `0.029 ms`。（本条不在“架构/lanes/fleet capacity”主线内，但同属 README 与实现/数据的漂移，一并更正。）

### 3.7 `bus-worker --backend` 可选值漏 `agy`

- 文档原文：`openswarm/docs/BUS.md:38` — “`--backend` may be `codex`, `ocgo`, `deepseek`, `cursor`, or `pi`”
- 实现：`openswarm/bin/openswarm:5923` — `BUS_EXECUTOR_BACKENDS = ("codex", "ocgo", "deepseek", "cursor", "agy", "pi")`；
  CLI 绑定处 `openswarm/bin/openswarm:11639-11640`。
- 判定：错误。枚举漏 `agy`。
- 修复：BUS.md 补上 `agy`。

### 3.8 Pi broker 的请求上限

- 文档原文：`openswarm/docs/executor-recovery.md:15-16` — “Each broker process accepts at most 64 inference
  requests; restart the bridge when that limit is reached.”
- 实现：`openswarm/bin/pi-minimax-broker.mjs:20-27` — `const maxInFlight = 4;`，
  `if (inFlight >= maxInFlight) return fail(503, 'broker temporarily unavailable');`（并发上限，超出返回 HTTP 503）。
  全仓库无“64 次请求”限制（`grep -rn "\b64\b" bin/` 仅命中 SHA 正则、socket 长度等无关项）。
- 判定：错误。限制是**并发 4**，不是累计 64 次，且无需重启桥接。
- 修复：改为“最多 4 个并发推理请求，第 5 个返回 HTTP 503，直到其中一个结束”。

### 3.9 Pi 只读 review / consult 的可用性

- 文档原文：`openswarm/docs/executor-recovery.md:31-32` — “Pi read-only review and consult are deliberately
  rejected until their own sandbox contract is verified.”
- 实现：`openswarm/bin/openswarm:5573-5575` — 未使用隔离启动器时抛出
  `ValueError("read-only Pi review requires the isolated launcher")`；`openswarm/bin/openswarm_consult.py:97-101` —
  同样的校验，错误信息 “read-only Pi consultation requires the isolated launcher”；
  `openswarm/bin/openswarm_consult.py:28` — `SUPPORTED_BACKENDS = {"codex", "agy", "pi", "copilot"}`（Pi 在列）。
- 判定：错误。隔离启动器 `bin/pi-isolated` 已落地，沙箱契约已具备；Pi 的只读 review / consult 现在**通过隔离启动器可用**，
  仅在缺少隔离启动器时被拒绝。
- 修复：改为“Pi 只读 review 与 consult 仅通过隔离启动器运行；缺少时会被拒绝”。

## 4. 已核对且正确的声明（抽样，未修改）

| 声明 | 文档位置 | 实现证据 |
|---|---|---|
| `bin/openswarm` 子命令清单（probe…ledger） | `openswarm/README.md:612` | `openswarm/bin/openswarm:11379-11696`（argparse 子命令定义） |
| `watch` 窗口 28 分钟后退出、需 `--from-end` 续看 | `openswarm/README.md:157` | `openswarm/bin/openswarm:11547`（`--max-minutes` 默认 28）、`:10061-10064` |
| `verify-remote` 默认校验超时 600 秒 | `openswarm/README.md:526` | `openswarm/bin/openswarm:11626` |
| `lane systemd-unit` 输出 `MemoryMax=900M`/`CPUQuota=150%`/`Nice=10` | `openswarm/README.md:460` | `openswarm/bin/openswarm:8564-8565` |
| 路由阈值 85%/95%/1.25×/24h/60%/220k | `openswarm/README.md:576-580` | `openswarm/policy.json:43-53`（`thresholds`）、`:77`（`large_context_tokens`）；判定逻辑 `openswarm/bin/openswarm:2862-2874` |
| `rate_limit` 默认 `[30, 60, 120, 240]` / `4` | `openswarm/README.md:115` | `openswarm/policy.json:264-266`、`openswarm/bin/openswarm:3635-3646` |
| lessons 最多 12 条 / 3000 字符，patterns 最多 3 条 / 1500 字符 | `openswarm/README.md:311`、`openswarm/README.md:345` | `openswarm/bin/openswarm:213-214`、`:218-219` |
| patterns 退役需至少 8 个注入 + 8 个对照样本 | `openswarm/README.md:361` | `openswarm/bin/openswarm:869-874` |
| `review.auto_card_types` 默认 `feature, fix, review-fix, migration` | `openswarm/README.md:377` | `openswarm/policy.json:275` |
| 预算超限返回 `budget_exceeded_daily` 且退出码 3 | `openswarm/README.md:689` | `openswarm/bin/openswarm:2732-2734`、`:2782-2784`、`:9652-9653`（batch）、`:8497-8498`（lane assign） |
| 心跳含耗时、命令数、diff 摘要、末条消息截断 200 字符 | `openswarm/README.md:167-168` | `openswarm/bin/openswarm:10055-10059` |
| Cursor lane 默认模型 `composer-2.5` | `openswarm/README.md:450` | `openswarm/bin/openswarm:6740-6741`；ACP 精确/基名匹配与歧义失败 `openswarm/bin/openswarm:7491-7499` |
| Codex 以 `-s workspace-write` 运行；OpenCode `external_directory: deny` | `openswarm/README.md:673-674` | `openswarm/bin/openswarm:4359`、`openswarm/policy.json:121`、`openswarm/bin/openswarm:4384-4385` |
| Codex `ultra` 力度永不启用 | `openswarm/README.md:676` | `openswarm/policy.json:18-20`（`never_efforts`）、`openswarm/bin/openswarm:1768-1770` |
| “单文件、仅标准库” | `openswarm/README.md:123` | `openswarm/bin/openswarm:38-70`（导入均为标准库，含 `zoneinfo`） |
| Python 3.9+ / Linux 有 CI 覆盖 | `openswarm/README.md:127`、`:720` | `openswarm/.github/workflows/ci.yml`（矩阵 3.9/3.11/3.13 × ubuntu/macos） |
| `batch --per-host` 默认 1 | `openswarm/README.md:500` | `openswarm/bin/openswarm:11610` |
| bus 状态标签 `queued/claimed/done/failed/cancelled` | `openswarm/docs/BUS.md:65` | `openswarm/bin/openswarm:5922` |
| bus 双白名单默认拒绝 | `openswarm/docs/BUS.md:17-30` | `openswarm/policy.json:280-291`（`allowed_executors: []`、`allowed_planners: []`）、校验 `openswarm/bin/openswarm:5996-6003` |
| worker 仅在 verify 命中 `worker_verify_allow` 或自有 `--verify` 时执行校验，其余记入 followups | `openswarm/docs/BUS.md:38` | `openswarm/bin/openswarm:6592-6601` |
| 规划方先发 `openswarm-task-digest: sha256:<hex>` 摘要评论 | `openswarm/docs/BUS.md:65` | `openswarm/bin/openswarm:6119-6125`、`:6212` |
| worker 固定推送 `openswarm/<run_id>` | `openswarm/docs/BUS.md:82` | `openswarm/bin/openswarm:6616` |
| fleet-capacity：forced command 末位参数即“宿主”，写入 `status.fleet_host` 并在释放/TTL 恢复中复用 | `openswarm/README.md:692-698` | `openswarm/bin/openswarm:3355-3360`、`:3594`、`:3505-3517`、`:3444-3446` |
| fleet-capacity 允许的认证宿主为 `gocc/gores1/local`，支持 acquire/release/status/recover | `openswarm/README.md:693` | `openswarm/bin/fleet-capacity:15-16`、`:70-79` |
| `fleet status` 不连接暂停的 instabox，未知状态同样不连 | `openswarm/README.md:487` | `openswarm/bin/openswarm:9276-9280`、`:9363-9368` |
| 跨主机 provider 配额由规划方协调（`batch` 汇总各主机快照） | `openswarm/README.md:505` | `openswarm/bin/openswarm:9472-9520`（`batch_provider_slot_available`） |
| fleet-capacity 的后端白名单与上限（total 8、host:gocc 4、host:gores1 4、host:local 2、codex 1、pi 4、cursor 2、agy 2、copilot 2、minimax-cn 4） | 文档未写 | `openswarm/bin/fleet-capacity:12-14` |
| SWE-bench / 合成任务 / 路由配额基准表中的数值 | `openswarm/README.md:12-64` | `openswarm/bench/swebench/results/deepseek/tables.md`、`openswarm/bench/e2e/results/headline.json`、`openswarm/bench/results/summary.md:144` |
| `agy-via-local-proxy` 的 Keychain/RAM disk 行为描述 | `openswarm/docs/agy-macos-runtime.md` | `openswarm/bin/agy-via-local-proxy`（Keychain 账号 `openswarm`、服务 `openbot-agy-nick-oauth`、RAM 卷 `/Volumes/OpenBotAgyVolatile`、仅在 refresh token 变化时回写） |

## 5. 不完整但不错误（未修改，建议后续处理）

1. `openswarm/README.md:89-94`（“Why” 执行器列表）、`:127`（Quickstart 前置条件）、`:422`（Resident lanes 开头）只列举了
   Codex / OpenCode Go / DeepSeek / Cursor，未提及 agy、pi、hermes、copilot。属于叙述不完整而非事实错误；
   若要更新，建议统一在 `openswarm/SKILL.md:7-9` 的措辞基础上补齐（SKILL.md 的描述已较新）。
2. `openswarm/README.md:106`（依赖目录）只写 `node_modules`、`.venv`，实际默认还链接 `venv`
   （`openswarm/bin/openswarm:4781-4783`）；`openswarm/SKILL.md:52` 已写全。
3. `openswarm/README.md:528-529`（`verify_remote` 字段）只列 `{host, rc, failing, seconds}`，实际还含
   `prepare`、`workspace_note`、可选 `error_class`（`openswarm/bin/openswarm:9747-9749`）。
4. `openswarm/README.md:618`（`$OPENSWARM_HOME` 运行时数据清单）未列 `quota-claude.json`、`worktrees/`；
   环境变量表也未列 `OPENSWARM_FLEET_GUARD_CONFIG`（`openswarm/bin/openswarm:3327`）、
   `OPENSWARM_CODEX_APP_SERVER_BIN`（`:1651`）、`OPENSWARM_PI_RPC_BIN`（`:6943`）、
   `OPENSWARM_COPILOT_BIN`（`:6962`）、`OPENSWARM_PI_BROKER_SOCKET` / `OPENSWARM_PI_RUNTIME`（`bin/pi-isolated`）。
5. `openswarm/README.md:505` 提到 `concurrency_url` 这一在 openswarm 中并不存在的键名；该句本身是否定式表述
   （“不使用 concurrency_url”），不构成事实错误，但建议改写以免读者检索不到对应配置。

## 6. 仓库外不可验证的声明（仅记录）

- `openswarm/docs/executor-recovery.md:11`：“Pi 0.73-compatible installation”——仓库内没有 Pi 版本断言
  （`bin/pi-isolated` 只检查 `node_modules/.bin/pi` 是否存在），无法在仓库内证实或证伪。
- `openswarm/docs/executor-recovery.md:6-7`（Cursor ACP 默认暴露 Composer 2.5 fast）、`:37-40`（macOS Keychain、
  `COPILOT_HOME` 明文回退）依赖 Cursor / Copilot CLI 与 macOS 外部环境。
- `openswarm/README.md:692-698`（SSH forced-command 端点、`/root/.ssh/openswarm-fleet/config` 示例）依赖部署侧
  SSH 配置；仓库内只能核对 `bin/openswarm` 与 `bin/fleet-capacity` 的协议行为（见第 4 节相关行）。

## 7. 复现方式与测试

核对手段（只读）：

```bash
cd openswarm
python3 bin/openswarm --help                 # 子命令与标志
grep -n "ENFORCED_RISKS\|can_resume\|BUS_EXECUTOR_BACKENDS\|maxInFlight" bin/openswarm bin/pi-minimax-broker.mjs
python3 -m json.tool policy.json | head -80  # 默认值
```

测试：`python3 -m unittest discover -s tests`（openswarm，400 项）→ **3 项失败，均为沙箱环境所致，与本次文档改动无关**：

- `tests.test_lane.LaneTests.test_cursor_systemd_unit_is_printed_not_installed`：`pwd.getpwuid()` 在当前容器
  找不到 uid 1000（`openswarm/bin/openswarm:8555`）。
- `tests.test_pi_isolation.PiIsolationTests.test_host_secrets_and_network_are_not_visible`、
  `...test_linked_git_worktree_and_node_remain_available`：`bwrap: Can't mount proc on /proc: Operation not permitted`。

本次改动全部为 Markdown 文本，不涉及可执行代码，未新增/修改任何测试。
