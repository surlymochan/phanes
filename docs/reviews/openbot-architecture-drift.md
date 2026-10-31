# OpenBot 架构文档与实现差异清单（#108 子任务，Refs #18）

- 审计对象：`openbot/docs/**`（`ARCHITECTURE.md`、`architecture/*.md`）、`openbot/README.md`、`openbot/AGENTS.md`、`openbot/src/**/AGENTS.md`、`openbot/packages/*/AGENTS.md`、`openbot/apps/*/AGENTS.md`、`openbot/remote/api/AGENTS.md`
- 实现基准：`openbot/src/`、`openbot/packages/`、`openbot/apps/`、`openbot/remote/`、`openbot/scripts/`、`openbot/biome.json`、`openbot/package.json`、`openbot/bun.lock`
- 基线提交：`61f0b694d69c0a7f1b47e7cfb362159899ae1a0c`（2026-10-31）
- 审计日期：2026-10-31
- 性质：**只读调研**。本报告只给建议，未修改任何被审计的 OpenBot 文档。

## 1. 方法与判定口径

从 OpenBot 的架构类文档中逐条抽出**可核对断言**（模块/目录/入口/进程/数据流、阈值与数量、协议版本、
迁移编号、文件路径），再到实现中定位对应代码，记录文件路径 + 行号作为证据。

判定口径：

- **一致**：断言与当前实现一致。
- **已过时**：断言与当前实现不一致，或文档内部编号口径互相矛盾。
- **无法核实**：依赖本仓库之外的运行环境（真实 Stripe/D1/生产 Worker、宿主机器、第三方 CLI 版本），
  仓库内既不能证实也不能证伪。

本报告的"建议修改"一节只提出改法，**不直接改原文档**；改动应由对应模块的负责人按
`openbot/AGENTS.md` 的规则单独提交。

## 2. 结论摘要

共核对 **30** 条断言：**一致 21 条、已过时 9 条**。另有 4 组断言依赖仓库外环境，无法核实，列在第 5 节。

| # | 文档位置 | 断言摘要 | 判定 | 实现证据（文件:行） |
| --- | --- | --- | --- | --- |
| 1 | `openbot/docs/architecture/change-rules.md:5` | "the catalog-pinned `effect` **4.0.0** package" | **已过时** | `openbot/package.json:73` → `"effect": "4.0.6"`；`openbot/bun.lock:2033` → `effect@4.0.6` |
| 2 | `openbot/AGENTS.md:52` | "Use Effect **4.0.0** for migrated server and desktop domain services" | **已过时** | 同 #1 |
| 3 | `openbot/docs/architecture/events.md:17` | `workflow_trigger_webhooks` 表是 "schema **v46**" | **已过时** | 表由 `WORKFLOW_SCHEMA_SQL` 建立：`openbot/src/backend/workflows/workflow-schema.ts:107`；该 SQL 挂在**迁移 47**：`openbot/src/backend/openbot-database-schema.ts:869-873`；v46 是 `browser_profiles`（同文件 `:866-869`）。`openbot/docs/architecture/database-migrations.md:36` 亦记 fork 版本为 47 |
| 4 | `openbot/docs/architecture/team-api.md:5` | "Team API protocol v8 over **three** ordered WebRTC DataChannels: `rpc`, `events`, and `files`" | **已过时** | 实际建 **4** 条：`openbot/src/renderer/src/features/team/team-webrtc.ts:458-461`（`rpc`/`events`/`files`/`desktop`）；`openbot/packages/team-client/src/remote-peer.ts:347` → `const CHANNELS: ChannelKind[] = ["rpc", "events", "files", "desktop"]`；通道类型 `openbot/src/main/team-webrtc-bridge.ts:12` |
| 5 | `openbot/README.md:568` | "WebRTC **protocol v3** carries RPC, events, and binary files to desktop and mobile clients" | **已过时** | 当前 Team API 协议为 **v8**：`openbot/packages/contracts/src/team-protocol/v8.ts:11` → `export const TEAM_PROTOCOL_V8 = 8 as const;`；`openbot/docs/architecture/team-api.md:5` 亦写 v8 |
| 6 | `openbot/README.md:511` | `packages/i18n` 的界面文字只有 "English, French, Japanese, Brazilian Portuguese and Turkish"（5 种） | **已过时** | 实际 **13** 个语种目录：`openbot/packages/i18n/src/messages/` → `de en es fr it ja ko nl pl pt ru tr zh`；可选语言 14 项（`system` + 13）：`openbot/packages/contracts/src/app-language.ts:10-25`、`openbot/packages/i18n/src/languages.ts:24-39` |
| 7 | `openbot/README.md:498-501` | 架构图里 "Cloudflare Workers └── TanStack Start + Solid 2 account API"（只有一个 Worker） | **已过时** | 有 **两个** Worker：`openbot/apps/auth-api/wrangler.jsonc`（`openbot-auth-api`）与 `openbot/apps/site-router/wrangler.jsonc`（`openbot-site-router`）；`openbot/docs/ARCHITECTURE.md:3` 已写 "two Cloudflare Workers"；`openbot/docs/architecture/structure.md:10` 也列出 `site-router/` |
| 8 | `openbot/docs/architecture/browser.md:186` | "Named profiles are rows of the `browser_profiles` table (**migration 45**)" | **已过时** | fork 的迁移 **46** 才建该表：`openbot/src/backend/openbot-database-schema.ts:866-869`；v45 建的是 `routine_backoff`（同文件 `:860-864`）。`openbot/docs/architecture/database-migrations.md:35,70` 明确 "46 → Creates `browser_profiles`" |
| 9 | `openbot/docs/architecture/integrations.md:27` | 浏览器端拿授权时用 `web-messaging-install.ts` | **已过时**（指错文件） | `web-messaging-install.ts` 是 **contracts 类型模块**：`openbot/packages/contracts/src/web-messaging-install.ts`；浏览器端实现在 `openbot/src/renderer/src/features/web-client/web-messaging-connector.ts`（`openbot/src/renderer/src/features/web-client/web-messaging-connector.ts:4,17`） |
| 10 | `openbot/docs/architecture/structure.md:3-30` | Workspace map：`apps/`、`packages/`、`remote/`、`src/`、`scripts/`、`tools/`、`.agents/skills/` 各自归属 | 一致 | 目录均存在；`packages/` 下 8 个包 `brand contracts i18n logging team-client telemetry ui user-errors`；`remote/api`、`remote/scripts`、`tools/` 下的 `biome`、`ui-foundation`、`vitest`、`typescript`、`agent-context` 齐备 |
| 11 | `openbot/docs/architecture/structure.md:40-63` | 依赖方向 + `biome.json` 的 `noRestrictedImports` / `noNodejsModules` 覆盖 | 一致 | `openbot/biome.json:148-171`（renderer+UI → main/backend/preload）、`:173-192`（main/backend/preload → renderer）、`:193-228`（contracts → Electron/SolidJS/provider/Cloudflare/app）、`:229-257`（account server → desktop）、`:302-322`（shared UI → 应用/平台代码）、`:103-115`（`noNodejsModules` 覆盖 contracts + team-client）；实际无越界 import（`grep` renderer→main/backend/preload 与 main/backend/preload→renderer 均为 0 命中）；`openbot/packages/contracts/package.json` 依赖仅 `emoji-regex`/`fflate`/`marked` |
| 12 | `openbot/docs/ARCHITECTURE.md:3` | "desktop, browser, and mobile applications, **two Cloudflare Workers**, a self-hosted Signal service, and shared packages" | 一致 | `openbot/apps/auth-api/wrangler.jsonc`、`openbot/apps/site-router/wrangler.jsonc`；Signal 服务 `openbot/remote/api/`（`openbot/remote/api/src/signal-service.ts`） |
| 13 | `openbot/docs/architecture/events.md:5` | 一次 webhook 触发器 "at most **128** JSON Pointer filters" | 一致 | `openbot/src/backend/webhook-trigger.ts:8` → `export const WEBHOOK_FILTERS_LIMIT = 128;`（校验同文件 `:14`） |
| 14 | `openbot/docs/architecture/events.md:46` | "A host has at most `WEBHOOK_ROUTES_LIMIT` (**64**) routes"，main 与 account service 双查 | 一致 | `openbot/packages/contracts/src/signal-protocol/webhook-route.ts:31` → `= 64`；main 侧 `openbot/src/main/host-events-service.ts:510,538`；account service 侧 `openbot/remote/api/src/tokens.ts:93` |
| 15 | `openbot/docs/architecture/events.md:43` | secret 形如 "`whsec_` + **32 随机字节** base64url" | 一致 | `openbot/src/main/host-events-service.ts:98` → `return \`whsec_${randomBytes(32).toString("base64url")}\`;` |
| 16 | `openbot/docs/architecture/events.md:106` | 时间戳窗口 **5 分钟**；delivery ID 匹配 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$` | 一致 | `openbot/packages/contracts/src/signal-protocol/webhook-route.ts:16` → `WEBHOOK_MAX_CLOCK_SKEW_SECONDS = 5 * 60`、`:22` → delivery ID 正则；使用点 `openbot/src/main/webhook-security.ts:11,63-64` |
| 17 | `openbot/docs/architecture/events.md:88` | 每主机 pending 预算 "**16 requests, 128 KiB**" | 一致 | `openbot/remote/api/src/signal-service.ts:247-248` → `maximumPendingPerHost: 16`、`maximumPendingBytesPerHost: 128 * 1024` |
| 18 | `openbot/docs/architecture/events.md:87` | Signal 等待主机答案的上限 **2.5 秒** | 一致 | `openbot/remote/api/src/signal-service.ts:246` → `timeoutMilliseconds: 2_500` |
| 19 | `openbot/docs/architecture/events.md:51-52` | 路由同步失败后 **30 秒**重试、翻倍到 **15 分钟**封顶 | 一致 | `openbot/src/main/host-events-runtime.ts:5-6` → `RETRY_FIRST_MS = 30_000`、`RETRY_MAX_MS = 15 * 60_000`（翻倍逻辑 `:93-95`） |
| 20 | `openbot/docs/architecture/events.md:110,124` | receipt 保留 **7 天**；workflow body 超过 **64,000** 字符返回 413 | 一致 | `openbot/src/backend/webhook-route-store.ts:16` → `RECEIPT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000`；`openbot/packages/contracts/src/input-limits.ts:59` → `workflowWebhookPayload: 64_000`（使用点 `openbot/src/backend/workflows/workflows.ts:870`） |
| 21 | `openbot/docs/architecture/events.md:78` | webhook ingress socket "backoff of **2 seconds** that doubles up to **5 minutes**" | 一致 | `openbot/src/main/signal-ingress.ts:50-51` → `BACKOFF_START_MS = 2_000`、`BACKOFF_LIMIT_MS = 5 * 60_000`（翻倍 `:678`） |
| 22 | `openbot/docs/architecture/database-migrations.md:15-19,35-38,67-73,187` | 基线 **v8**；fork 追加 45–49；最新版本 **49**；"five reserved schema parts" | 一致 | `openbot/src/backend/openbot-database-schema.ts:25` → `BASELINE_SCHEMA_VERSION = 8`、`:860-888`（45–49）、`:892` → `LATEST_SCHEMA_VERSION = 49`；保留部分 5 组：同文件 `:1059-1063`（backoff / profiles / workflows / threads / tasks）；历史文件 `openbot/src/backend/openbot-database-schema-history.json`、`openbot/src/backend/openbot-database-upstream-schema-history.json` 均存在 |
| 23 | `openbot/docs/architecture/structure.md:112-116` | `browser-tabs.json` 版本化（`v1` 无 `BrowserEnvironment`、`v2` 带），总是按当前版本重写 | 一致 | `openbot/src/backend/browser-state.ts:282` → `version: 2`、`:291` → 只接受 1 或 2、`:303,307` 读 v1 时补默认环境 |
| 24 | `openbot/docs/architecture/structure.md:96-101` | 审批自动化文件 `openbot-approval-automation-v3.json`，无 v3 时回退 `-v2`/`-v1` | 一致 | `openbot/src/main/application-services.ts:331-334` → v3 / v2 / v1 三个文件名常量 |
| 25 | `openbot/docs/architecture/change-rules.md:52-63`（规则 7） | renderer 状态放在 `src/renderer/src/features/<domain>/<domain>-context.tsx` | 一致（3 处命名例外见第 6 节建议 10） | 25 个上下文文件均落在 `src/renderer/src/features/<domain>/` 下且以 `-context.tsx` 结尾；其中 3 处文件名与所在目录名不一致（`conversation-controller-context.tsx`、`provider-detection-context.tsx`、`whats-new-context.tsx`） |
| 26 | `openbot/docs/architecture/change-rules.md:72-79`（规则 10） | 用 `@openbot/logging`（ts-log）而非 `console.*`；`noConsole` 覆盖 `src`/`scripts`/`packages`；remote-desktop 配方脚本豁免 | 一致 | `openbot/packages/logging/package.json:14` → `"ts-log": "3.0.3"`；`openbot/biome.json:112-127`（`noConsole` + 5 个 `!scripts/...` 豁免）；豁免脚本清单与 `openbot/.github/workflows/remote-desktop-runtime.yml:85` 一致（该步骤另外还列了 `scripts/mac-runtime-dylibs.ts`，该文件无 `console.` 调用，见第 6 节建议 5） |
| 27 | `openbot/docs/architecture/skills-and-plugins.md:12-25,27` | 工作区技能目录共 **7** 个；被限制的 agent 不能写这些目录 | 一致 | 去重后的 7 个：`openbot/packages/contracts/src/agent-providers.ts:192,213,234,257,280,303,326` → `.agents/skills`、`.claude/skills`、`.opencode/skills`、`.gemini/skills`、`.cursor/skills`、`.cline/skills`、`.clinerules/skills`；保护清单 `openbot/src/backend/process-confinement.ts:284-303`（`PROJECT_SETTINGS`） |
| 28 | `openbot/docs/architecture/skills-and-plugins.md:44-52` | `~/.agents/skills` 最多 **200** 个；写进开发者指令的技能最多 **100** 个、描述取前 **300** 字符 | 一致 | `openbot/src/backend/home-skills.ts:10` → `MAX_HOME_SKILLS = 200`；`openbot/src/backend/agent/developer-instructions.ts:322-323` → `MAX_HOME_SKILLS_IN_INSTRUCTIONS = 100`、`MAX_HOME_SKILL_DESCRIPTION = 300` |
| 29 | `openbot/docs/architecture/servers.md:51` | `siteLimitForPlan`：none **1**、Starter **3**、Standard **10**、Pro **50** | 一致 | `openbot/packages/contracts/src/billing.ts:44-48`（`siteLimit: 3/10/50`）、`:56-58`（无套餐回落 1） |
| 30 | `openbot/docs/architecture/servers.md:638` | `licenses` D1 表在迁移 **`0038`** | 一致 | `openbot/apps/auth-api/migrations/0038_licenses.sql` 存在；`openbot/apps/auth-api/src/server/license-service.ts` 持有该表 |

## 3. 已过时的声明（逐条证据）

### 3.1 Effect 版本：文档写 4.0.0，目录锁定 4.0.6

- 文档原文：`openbot/docs/architecture/change-rules.md:5` — "Service workflows use the catalog-pinned `effect` 4.0.0 package."
  `openbot/AGENTS.md:52` — "Use Effect 4.0.0 for migrated server and desktop domain services, schemas, typed errors,"
- 实现：`openbot/package.json:73` — catalog 中 `"effect": "4.0.6"`；`openbot/bun.lock:2033` —
  `"effect": ["effect@4.0.6", ...]`。各工作区统一引用 `catalog:`（`openbot/package.json:394` 等）。
- 判定：**已过时**。文档把版本号写死在两处，且与"catalog-pinned"的措辞自相矛盾——既然是 catalog 锁定，
  版本号就不该在正文里复述。
- 建议：删除具体版本号，改为"catalog 锁定的 `effect` 4.x 包"；若要保留版本号，两处一起改成 4.0.6。

### 3.2 `workflow_trigger_webhooks` 的迁移号：文档写 v46，实现是 v47

- 文档原文：`openbot/docs/architecture/events.md:17` — "| Workflow trigger route ID, encrypted secret, URL | Host SQLite |
  `workflow_trigger_webhooks` (schema v46) |"
- 实现：表 DDL 在 `openbot/src/backend/workflows/workflow-schema.ts:107`（`CREATE TABLE IF NOT EXISTS workflow_trigger_webhooks`），
  由 `WORKFLOW_SCHEMA_SQL` 建立；该 SQL 挂在**迁移 47**（`openbot/src/backend/openbot-database-schema.ts:869-873`）。
  迁移 46 建的是 `browser_profiles`（同文件 `:866-869`），迁移 45 建的是 `routine_backoff`（`:860-864`）。
  同一文档体系里的 `openbot/docs/architecture/database-migrations.md:36` 已经写明
  "Workflow tables | 46 | **47**"（upstream 46 / fork 47）。
- 判定：**已过时**。同一仓库的两份文档对同一个迁移号给出不同答案。
- 建议：`events.md:17` 的括号改为 "(schema v47)"，或直接去掉括号、改为链接
  `database-migrations.md#what-the-fork-appends`，避免两处再各写一次编号。

### 3.3 WebRTC DataChannel 数量：文档写三条，实现是四条

- 文档原文：`openbot/docs/architecture/team-api.md:5` — "Current remote connections use Team API protocol v8
  over three ordered WebRTC DataChannels: `rpc`, `events`, and `files`."
- 实现：桌面端建 4 条通道——`openbot/src/renderer/src/features/team/team-webrtc.ts:458-461`
  （`createDataChannel(..., "rpc" / "events" / "files" / "desktop")`）；共享客户端同样是 4 条——
  `openbot/packages/team-client/src/remote-peer.ts:347`
  `const CHANNELS: ChannelKind[] = ["rpc", "events", "files", "desktop"]`；通道联合类型见
  `openbot/src/main/team-webrtc-bridge.ts:12`、`openbot/packages/team-client/src/remote-peer.ts:286`。
  `desktop` 通道承载 Remote Desktop 控制帧（`openbot/src/main/team-webrtc-host-peer.ts:967-993`）。
- 判定：**已过时**。漏掉 `desktop` 通道，而 `docs/architecture/browser.md:259` 又说 Remote Desktop
  "reuses the Remote Desktop websocket tunnel rather than adding a WebRTC channel"——两处口径互相打架。
- 建议：`team-api.md:5` 改为 "four ordered WebRTC DataChannels: `rpc`, `events`, `files`, and `desktop`"；
  同时核对 `browser.md` 那句"不新增 WebRTC 通道"是否也要改。

### 3.4 README 里的协议版本 v3

- 文档原文：`openbot/README.md:568` — "WebRTC protocol v3 carries RPC, events, and binary files to desktop and
  mobile clients."
- 实现：当前 Team API 协议为 v8——`openbot/packages/contracts/src/team-protocol/v8.ts:11`
  `export const TEAM_PROTOCOL_V8 = 8 as const;`；版本协商与能力表见
  `openbot/packages/contracts/src/team-protocol/current.ts`（`teamCapabilitiesForProtocol`）与
  `openbot/docs/architecture/team-api.md:5,258`。
- 判定：**已过时**。README 停留在 v3 时代。
- 建议：改为"Team API protocol v8"；若不想在 README 追版本号，改为"the current Team API protocol"。

### 3.5 README 里 i18n 的语种清单

- 文档原文：`openbot/README.md:511` — "`packages/i18n` contains the interface text in English, French,
  Japanese, Brazilian Portuguese and Turkish for desktop, web and mobile."
- 实现：`openbot/packages/i18n/src/messages/` 下有 **13** 个语种目录 `de en es fr it ja ko nl pl pt ru tr zh`；
  语言选择器读 `openbot/packages/contracts/src/app-language.ts:10-25`（`system` + 13），
  显示名在 `openbot/packages/i18n/src/languages.ts:24-39`。`docs/i18n.md:154-171` 只描述"新增语言"的步骤，
  未列具体语种，所以这处过期只发生在 README。
- 判定：**已过时**（枚举式清单漏了 8 种）。
- 建议：README 不再逐个列举，改为"13 个语种的目录（见 `packages/i18n/src/messages/`）"，或与
  `APP_LANGUAGES` 保持单一来源。

### 3.6 README 架构图里的 Cloudflare Worker 数量

- 文档原文：`openbot/README.md:498-501` — 架构图只有
  "Cloudflare Workers └── TanStack Start + Solid 2 account API"（一个 Worker）。
- 实现：仓库里有两个 Worker 配置——`openbot/apps/auth-api/wrangler.jsonc`（`name: openbot-auth-api`，
  `main: src/worker-entry.ts`）与 `openbot/apps/site-router/wrangler.jsonc`（`name: openbot-site-router`，
  `main: src/index.ts`，从私有 R2 提供已发布站点）。`openbot/docs/ARCHITECTURE.md:3` 写的是
  "two Cloudflare Workers"；`openbot/docs/architecture/structure.md:10` 也列了 `site-router/`。
- 判定：**已过时**。README 的图比实现少一个 Worker。
- 建议：在 README 架构图补第二条 Worker 分支（site-router），或把图改成"Cloudflare Workers (2)"并说明各自职责。

### 3.7 `browser_profiles` 的迁移号：文档写 45，fork 实际是 46

- 文档原文：`openbot/docs/architecture/browser.md:186` — "Named profiles are rows of the `browser_profiles`
  table (migration 45)."
- 实现：`openbot/src/backend/openbot-database-schema.ts:866-869` — 迁移 **46** 执行
  `BROWSER_PROFILE_SCHEMA_SQL`；迁移 45 执行 `ROUTINE_BACKOFF_SCHEMA_SQL`（`:860-864`）。
  `openbot/docs/architecture/database-migrations.md:35`（"browser_profiles | 45 | 46"）与 `:70`
  （fork 的 46 行）也是 46。45 是 upstream 版本号。
- 判定：**已过时**（用了 upstream 的编号，而不是本仓库 fork 的编号）。
- 建议：`browser.md:186` 改为 "(migration 46)"，或改为指向 `database-migrations.md` 的锚点，避免再写死编号。

### 3.8 浏览器端授权交接的文件名指向错误模块

- 文档原文：`openbot/docs/architecture/integrations.md:27` — "The browser client gets the grant from a return
  page of its own origin (`web-messaging-install.ts`)."
- 实现：`web-messaging-install.ts` 实际是 **contracts 里的类型模块**
  （`openbot/packages/contracts/src/web-messaging-install.ts`），被渲染器与 Worker 共同引用
  （`openbot/src/renderer/src/features/web-client/web-messaging-connector.ts:17`、
  `openbot/apps/auth-api/src/components/landing/web-messaging-handoff.ts:7`）。
  浏览器端真正做交接的模块是
  `openbot/src/renderer/src/features/web-client/web-messaging-connector.ts`。
- 判定：**已过时**（文件名指向了错误的模块类型）。文档本节开头承诺"Each section has a working example
  with real file paths"，因此这里尤其需要指对。
- 建议：改为 `web-messaging-connector.ts`（渲染器侧），并补一句 contracts 类型模块的位置。

## 4. 已核对一致的断言（抽样，均未修改）

| 断言 | 文档位置 | 实现证据 |
| --- | --- | --- |
| `src/main` 拥有 Electron 生命周期 / 窗口安全 / IPC 注册；`src/backend` 拥有 provider 适配与持久化；`src/preload` 只暴露 `window.openbot` | `openbot/README.md:504-507` | `openbot/src/main/index.ts`、`openbot/src/backend/agent-service.ts`、`openbot/src/preload/index.ts` |
| 账号服务是 TanStack Start + Solid 2 | `openbot/README.md:499,508` | `openbot/apps/auth-api/package.json` → `@tanstack/solid-start` `2.0.0-rc.9`、`solid-js` `2.0.0-rc.18` |
| 状态页在 `status.openbot.run`，由 GitHub Pages 托管 | `openbot/README.md:509` | `openbot/apps/status/scripts/notify.ts:56`、`openbot/apps/status/scripts/update.ts:72` |
| `packages/contracts` 无 Electron / SolidJS / provider / Cloudflare 依赖 | `openbot/docs/architecture/structure.md:50` | `openbot/packages/contracts/package.json`（依赖仅 `emoji-regex`/`fflate`/`marked`）；`openbot/biome.json:193-228` |
| 桌面应用留在仓库根目录，不迁到 `apps/desktop` | `openbot/docs/architecture/structure.md:33-36` | `openbot/package.json`（根即 Electron 应用）、`openbot/electron-builder.yml` |
| 共享 UI 由 `@openbot/ui` 拥有，特性走显式子路径，样式表在 `features/conversation/styles/` | `openbot/docs/architecture/structure.md:137-178` | `openbot/packages/ui/package.json`（`"./features/sidebar/Sidebar"`、`"./features/conversation/conversation.css"` 等导出）；`openbot/packages/ui/src/features/conversation/conversation.css:3-32` 的 `@import` 清单；`openbot/packages/ui/src/index.ts` 是唯一 barrel |
| 团队 API 当前协议为 v8（DataChannel 数量见 #4）；HTTP 头与 WebSocket 子协议名固定 | `openbot/docs/architecture/team-api.md:5,258` | `openbot/packages/contracts/src/team-protocol/v8.ts:11`；HTTP 头 `openbot/packages/contracts/src/team-protocol/provider-aware-codec.ts:45-46`；WebSocket 子协议 `openbot/packages/contracts/src/team-protocol/v1.ts:7` |
| 移动端 `QUIET_RESUME_MS` 为 5 秒 | `openbot/docs/architecture/team-api.md:41` | `openbot/apps/mobile/src/features/workspace/components/server-connection.tsx:24` → `const QUIET_RESUME_MS = 5_000;` |
| Signal `rate_limited` 后下次 socket 等 60 秒 | `openbot/docs/architecture/team-api.md:49` | `openbot/src/renderer/src/features/team/team-webrtc.ts:1006` → `signalRetryAt = Date.now() + 60_000` |
| Live Activity：事件最短 1 秒、同态 5 秒、10 分钟保活；优先级 10 / 5 | `openbot/docs/architecture/channels-and-messaging.md:12-16` | `openbot/src/main/live-activity-push.ts:28,30,34`、`:244`（`priority: ... ? 5 : 10`） |
| 主机 30 秒无输入时不发推送 | `openbot/docs/architecture/channels-and-messaging.md:76` | `openbot/src/main/push-notifications.ts:43` → `export const DESKTOP_USE_MS = 30 * 1000;` |
| 浏览器标签页空闲 30 分钟卸载页面（低内存 5 分钟） | `openbot/docs/architecture/browser.md:242` | `openbot/src/backend/browser-host.ts:232-233` → `IDLE_TAB_SLEEP_MS = 30 * 60_000`、`LOW_MEMORY_TAB_SLEEP_MS = 5 * 60_000` |
| 移动端分析：最多缓冲 100 条、30 分钟 | `openbot/docs/architecture/analytics.md:6-8` | `openbot/apps/mobile/src/features/analytics/analytics-core.ts:51-52` |
| 首次触点保存 90 天 | `openbot/docs/architecture/analytics.md:51-52` | `openbot/packages/contracts/src/first-touch.ts:48` → `FIRST_TOUCH_MAX_AGE_DAYS = 90` |
| MCP 回调端口 28417、路径 `/mcp-auth` | `openbot/docs/architecture/integrations.md:23` | `openbot/src/main/mcp-oauth-redirect-server.ts:28,31` |
| `secureOAuthFetch` 体积上限 1 MiB、超时 20 秒 | `openbot/docs/architecture/integrations.md:58-59` | `openbot/src/backend/mcp-oauth-provider.ts:1427,1433` |
| Skill 导入导出：格式 `openbot-import.json` / `openbot-transfer.json`，`isUnsafeArchivePath` 过滤 | `openbot/docs/architecture/agents.md:707,733-737` | `openbot/resources/agent-import/grok-bot/SKILL.md`、`openbot/src/main/agent-import-manifest.ts`、`openbot/src/main/instance-transfer-manifest.ts`、`openbot/src/main/skill-package.ts:104` |
| dispatch 运行记录落 `openbot-dispatch-v1.json` | `openbot/docs/architecture/agents.md:30` | `openbot/src/backend/dispatch/dispatch-service.ts:230,461` |
| 独立 agent 数据库进程：运行时只允许 `node:*` 与 `effect` | `openbot/src/backend/AGENTS.md:71-78` | `openbot/src/backend/agent-data/agent-database-host.ts:11-15`（仅 `node:fs`/`node:path`/`node:readline`/`node:sqlite`/`effect`）；其余引用为 `import type` |
| `runAsNode: false` fuse，宿主是 Electron `utilityProcess` | `openbot/src/backend/AGENTS.md:83-86` | `openbot/electron-builder.yml:13`、注释 `openbot/src/backend/agent-data/agent-database-host.ts:366`、`openbot/src/main/agent-database-host-process.ts:4` |
| Team API 路由按域分文件，每个路由以 `return "unmatched"` 收尾 | `openbot/src/main/AGENTS.md:109-124` | `openbot/src/main/team-api/route-team.ts`、`route-agents.ts`、`route-browser.ts`、`route-files.ts` 等；`openbot/src/main/team-api/http-error.ts` |
| IPC 端点声明集中在 `ipc-endpoints.ts`，`registerIpcGroups` 按组强类型 | `openbot/src/main/AGENTS.md:10-24`、`openbot/packages/contracts/AGENTS.md:29-48` | `openbot/packages/contracts/src/ipc-endpoints.ts:1767`（`IPC_GROUP_PATHS`）、`openbot/src/main/ipc/define-ipc-group.ts`、`openbot/src/main/ipc-channel-coverage.test.ts` |
| 八个 agent 组被展开进 `agent` 组 | `openbot/packages/contracts/AGENTS.md:77` | `openbot/src/preload/index.ts:1043,1052,1056,1066,1073,1081,1099,1123`（agentMemories / sharedTables / agentRoutines / channelMemories / channelRoutines / mcpServers / agentAdmin / agentAttachments） |
| 构建后的 preload 校验脚本与手写方法清单 | `openbot/src/preload/AGENTS.md:33-35` | `openbot/scripts/verify-preload-bundle.ts`；`HAND_WRITTEN_METHODS` 见该文件 |
| Signal 侧 Slack / Discord / Telegram 路径与 10 分钟内存队列 | `openbot/remote/api/AGENTS.md:3-24` | `openbot/packages/contracts/src/signal-protocol/slack-route.ts:15`、`telegram-route.ts:25`、`discord-route.ts:16`；`openbot/remote/api/src/ingress-queue.ts:50` → `ttlMilliseconds: 10 * 60_000` |
| provider CLI 更新来源：GitHub `releases/latest`（Codex、Pi）、npm `latest`（Claude、OpenCode、Cline）、`muse-stable` 渠道 | `openbot/docs/architecture/providers.md:125-131` | `openbot/src/main/provider-runtime-releases.ts:75,154`（GitHub）、`:191,213,330`（npm）、`:104`（`muse-stable`） |

## 5. 无法核实 / 依赖仓库外环境的断言

- `openbot/docs/architecture/servers.md:574-618`（计费）：`STRIPE_SECRET_KEY`、Price 金额、Customer Portal、
  webhook 签名校验依赖真实 Stripe 账户与生产 Worker；仓库内只能核对路由与键名
  （`openbot/packages/contracts/src/billing.ts`、`openbot/apps/auth-api/src/server/billing-service.ts`）。
- `openbot/docs/architecture/servers.md:620-660`（商业许可）：`LICENSE_PRICES` 的 EUR 基准与 `currency_options`
  来自 Stripe 产品配置（`openbot/packages/contracts/src/license.ts` 只有结构，无金额）。
- `openbot/docs/architecture/providers.md:127-131` 中 "x.ai 发布无哈希，Grok 仅靠 TLS" 与 ACP registry 条目
  （`antigravity-acp`、`cursor`）：依赖上游 x.ai 与 ACP registry 的实时响应，仓库内只能确认代码路径存在
  （`openbot/src/main/provider-runtime-releases.ts:61,295,299`）。
- `openbot/src/renderer/AGENTS.md:12` 提到的 `node_modules/solid-js/CHEATSHEET.md`：本工作树未安装
  `node_modules`，无法核对文件内容；`package.json` 的版本号（`solid-js` `2.0.0-rc.18`、
  `@kobalte/core` `2.0.0-alpha.0`）与断言一致。

## 6. 建议修改（只建议，未改原文档）

按影响面排序。每条只给出改法与依据，由对应模块负责人单独提交。

1. **Effect 版本**：`openbot/docs/architecture/change-rules.md:5` 与 `openbot/AGENTS.md:52` 去掉写死的
   "4.0.0"，改成"catalog 锁定的 `effect` 4.x"。若必须写版本，两处一起改成 `4.0.6`
   （依据 `openbot/package.json:73`、`openbot/bun.lock:2033`）。
2. **`workflow_trigger_webhooks` 迁移号**：`openbot/docs/architecture/events.md:17` 改为 "(schema v47)"
   或改为链接 `database-migrations.md#what-the-fork-appends`（依据 `openbot/src/backend/openbot-database-schema.ts:869-873`）。
3. **WebRTC DataChannel 数量**：`openbot/docs/architecture/team-api.md:5` 改为四条并补上 `desktop`；
   同时复核 `openbot/docs/architecture/browser.md:259` 关于 Remote Desktop "不新增 WebRTC 通道" 的表述
   （依据 `openbot/src/renderer/src/features/team/team-webrtc.ts:458-461`、
   `openbot/packages/team-client/src/remote-peer.ts:347`）。
4. **README 协议版本**：`openbot/README.md:568` 的 "protocol v3" 改为 v8 或改写为"当前 Team API 协议"
   （依据 `openbot/packages/contracts/src/team-protocol/v8.ts:11`）。
5. **README i18n 语种**：`openbot/README.md:511` 不再逐个列举，或与
   `openbot/packages/contracts/src/app-language.ts:10-25` 对齐为 13 种（+ `system`）。
6. **README 架构图**：`openbot/README.md:498-501` 补上 `site-router` Worker 分支，与
   `openbot/docs/ARCHITECTURE.md:3` 的 "two Cloudflare Workers" 保持一致。
7. **`browser_profiles` 迁移号**：`openbot/docs/architecture/browser.md:186` 改为 "(migration 46)"，
   或统一改为引用 `database-migrations.md`。
8. **浏览器授权交接文件**：`openbot/docs/architecture/integrations.md:27` 的 `web-messaging-install.ts`
   改为 `web-messaging-connector.ts`（渲染器侧），必要时补 contracts 类型模块的位置。
9. **`noConsole` 豁免清单与 CI 步骤的口径**：`openbot/docs/architecture/change-rules.md:72-79` 说 CI 步骤
   "lists them"，但 `openbot/.github/workflows/remote-desktop-runtime.yml:85` 比 `openbot/biome.json:123-127`
   多列了 `scripts/mac-runtime-dylibs.ts`（该文件无 `console.` 调用，所以不需要豁免）。建议把句子改成
   "该步骤覆盖这些脚本以及一个额外的打包脚本"，避免读者以为两个清单相等。
10. **renderer 上下文命名的三处例外**：`openbot/docs/architecture/change-rules.md:52-63` 的规则 7 要求
    `<domain>-context.tsx`，但 `openbot/src/renderer/src/features/conversation/conversation-controller-context.tsx`、
    `openbot/src/renderer/src/features/custom-providers/provider-detection-context.tsx`、
    `openbot/src/renderer/src/features/updates/whats-new-context.tsx` 三处文件名与所在目录名不一致。
    建议在规则里补一句"子域上下文用 `<sub-domain>-context.tsx`"，或在文件里写明例外。
11. **workspace map 覆盖范围**：`openbot/docs/architecture/structure.md:3-30` 的 map 未列 `examples/`、
    `marketplace/`、`resources/`、`deploy/`、`vendor/`。这些目录确实存在（`openbot/examples/org-health-local`、
    `openbot/marketplace/production-catalog`、`openbot/resources/managed-skills`、`openbot/deploy/self-host`、
    `openbot/vendor/remote-desktop`）。建议在 map 末尾补两行概括，避免读者以为它们是遗留目录。
12. **D1 迁移编号重复（代码侧观察，非文档漂移）**：`openbot/apps/auth-api/migrations/` 下同时存在
    `0034_account_attribution.sql` 与 `0034_server_claims.sql`。两个迁移都会被应用（wrangler 按文件名排序），
    两张表也都在用（`openbot/apps/auth-api/src/server/d1-auth-repository.ts:1004`、
    `openbot/apps/auth-api/src/server/auth-data-retention.ts:77`），但编号重复会让"迁移号唯一"的直觉失效。
    建议在后续迁移中补一个说明，或把其中一个重编号（需与部署流程确认）。
13. **`docs/architecture/integrations.md` 的"每个小节都有可运行的真实路径"承诺**：核对第 3 节（新增 provider）
    与第 6 节（新增 Team API 能力）时，未在本轮逐条展开；若这两节也含版本号或数量型断言，
    建议按本报告的格式补一次抽查。

## 7. 复现方式与测试

核对手段（全部只读）：

```bash
cd openbot
# 依赖版本
grep -n '"effect"' package.json bun.lock
# 迁移号
grep -n "version: 4[5-9]" -A4 src/backend/openbot-database-schema.ts
grep -n "workflow_trigger_webhooks" src/backend/workflows/workflow-schema.ts
# WebRTC 通道
grep -n "createDataChannel" src/renderer/src/features/team/team-webrtc.ts
grep -n "CHANNELS" packages/team-client/src/remote-peer.ts
# 语种
ls packages/i18n/src/messages/
# Worker 数量
ls apps/*/wrangler.jsonc
# 文档内部链接与锚点
python3 - <<'PY'
import os,re,glob
# 扫描 openbot/docs/**/*.md 的相对链接，验证目标文件与锚点是否存在
PY
```

- 链接与锚点扫描：`openbot/docs/**/*.md`（含 `openbot/AGENTS.md` 与各级 `AGENTS.md`）的相对链接全部命中，
  唯一例外是 `openbot/docs/RELEASING.md:684` 指向 `remote-session-deployment.md#deployment-procedure--requires-separate-approval`，
  该锚点对应标题 `## Deployment procedure — requires separate approval`（`openbot/docs/remote-session-deployment.md:30`），
  按 GitHub 的锚点规则（em dash 被删除、两侧空格各转一个连字符）实际可达，**不是断链**。
- 反向引用扫描：`openbot/docs/**` 与各级 `AGENTS.md` 中反引号包裹的**文件路径**共 521 条，逐条在仓库中查找，
  全部命中或属于运行期产物（`openbot-approval-automation-v3.json`、`.openbot-build/**`、`openbot-install.json`
  等运行时文件，仓库内本就不存在，属正常）。
- 反引号包裹的**标识符**（类名 / 函数名，189 条）在 `src`、`packages`、`apps`、`remote`、`scripts`、`tools`
  中逐条查找，除 `ExecStop`（systemd 单元指令，见 `openbot/scripts/hosting/openbot.service:23`）与
  `NativeTabs`（Expo Router API 的**建议用法**，`openbot/apps/mobile/AGENTS.md:20`，当前移动端代码未使用）
  外全部命中。
- 测试：本次为只读调研，未新增/修改任何代码或测试文件，因此**没有可运行的测试**。
  本工作树未安装 `node_modules`，`bun install` 与 `bun run test:*` 未执行。
  验收命令 `test -s docs/reviews/openbot-architecture-drift.md && grep -c '|' docs/reviews/openbot-architecture-drift.md`
  可在本机直接运行。
