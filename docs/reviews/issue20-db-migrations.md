# Issue #20 子任务：数据库迁移清单与兼容性测试现状

- 关联：Refs #111 / #20。
- 调研日期：2026-10-31。
- 范围：仅本仓库（Phanes）工作树 `openswarm/20261031-175244-b3b7e1`（HEAD `61f0b694d`）中 `openbot/` 子树内的数据库迁移资产与相关测试。
- 性质：**只读调研**。本次未修改任何产品代码、未新增任何迁移、未连接任何数据库（含本地 D1 / SQLite 实例）。
- 引用格式：`路径:行号`，行号对应上述提交。

---

## 0. 方法与边界

- 盘点方式：文件系统遍历 + 文本检索（`find` / `grep` / `read`），辅以 `git log`/`git status` 确认工作树状态。
- **未能运行的测试**：本工作树没有 `openbot/node_modules`，机器上也没有 `bun`/`vitest`/`npx`（只有 Node v22.23.3），因此 `bun run test:desktop`（vitest）与 `bun run test:api` 两套迁移测试**在本沙箱内无法执行**。本文件的覆盖判定全部来自对测试源码的逐条阅读；第 5 节给出的是可在有依赖的机器上执行的命令。
- **未做的事**：没有对 D1 迁移链做实跑验证。上一轮同 Issue 的审计（`docs/audit/issue-20-upstream-sync.md:241, 267`）曾用 `node:sqlite` 实跑确认 D1 链 41/41 可按文件名顺序应用、`foreign_key_check` 为空、`integrity_check` 为 `ok`；该结论在本文件里按"引用既有核验"处理，不重复声明为本次实测。
- 与既有文档的关系：`docs/audit/issue-20-upstream-sync.md` 面向"同步上游时的风险与检查计划"；本文件面向"迁移资产清单 + 测试覆盖矩阵 + 补测建议"，两者互补、不重复。

---

## 1. 结论摘要

1. `openbot/` 里有 **两条互不相干的迁移链**，规则完全相反：
   - **桌面端 `openbot.db`**：TypeScript 注册表式，版本 v8 → v49 连续 42 条（`openbot/src/backend/openbot-database-schema.ts:660-891`），**不可逆、无备份**（`openbot/src/backend/AGENTS.md:7-10`）。
   - **账号服务 Cloudflare D1**：`openbot/apps/auth-api/migrations/` 下 41 个 `.sql`，编号 `0001`–`0040`（**`0034` 重号**），**可逆但与部署竞态**（`openbot/apps/auth-api/AGENTS.md:14-24`）。
2. 桌面端有 **一条"谱系分叉"**：fork 侧 45 与上游侧 45 含义不同（`routine_backoff` vs `browser_profiles`），fork 把上游 45/46/47 追加到自己的 46/47/48，并在 49 补 `routine_backoff`（`openbot/docs/architecture/database-migrations.md:29-46` 与 `:65-78`）。
3. 桌面端兼容性测试**覆盖充分**：空库对拍、非空库数据保全、fork/imported 双谱系、失败回滚与重试、缺版本拒绝、外键违规回滚、完整性检查均有源码级证据。
4. **D1 侧存在两处明确缺口**：
   - **整链"空库跑完"没有一条独立断言**——vitest 层的 `migratedDatabase()` 在 `apps/auth-api/test/` 的 **10 处调用全部带 `last` 上界**（最大到 `0039_remote_host_key_handovers.sql`），无参调用不存在；`0040` 只被单独 exec。整链只被 `apps/auth-api/scripts/` 下的 8 个 e2e 脚本顺带应用，而这些脚本既不在 `bun run test:api`（vitest）里、也不在 `openbot/.github/workflows/ci.yml` 里，且**没有一个断言 `foreign_key_check` / `integrity_check`**。
   - **编号唯一性无守护**——`0034` 重号存在，但没有任何测试扫描 `migrations/` 目录校验编号。
5. D1 的**数据型迁移（`0036`、`0037`）没有专门的测试**，而它们直接改写生产行（`UPDATE`），是整条链里最需要"旧 Worker 兼容 + 失败回滚"证据的两条。
6. 补测建议见第 4 节，共 8 条可独立实现的小 issue，每条给出验收命令。

---

## 2. 迁移清单

### 2.1 桌面端 `openbot.db`（主链，注册表式，无独立迁移文件）

桌面端**没有"一个版本一个文件"的结构**：全部迁移写在一个 TypeScript 注册表数组里，schema DDL 分散在 15 个分片文件中。

**注册表**：`openbot/src/backend/openbot-database-schema.ts:660-891`（`MIGRATIONS`），`:892` 由末元素推导 `LATEST_SCHEMA_VERSION = 49`；`:980-990` 的 `validateMigrationRegistry` 强制版本从基线 8 起连续、无重复、无空洞。

| 版本 | 注册表行 | 执行体 | 作用（一句话） |
| --- | --- | --- | --- |
| 8（冻结基线） | `:662-666` | `migrateToBaselineV8` `:935-971` | 建立 v8 全量基线 schema，并回填读取基线/直读、压缩会话与信箱历史、迁移 provider session 与 reactions。 |
| 9 | `:668-671` | `refreshProviderSessionsForDynamicTools` `:972-978` | 把所有 active 的 provider session 置为 `inactive`（动态工具集变化后旧会话失效）。 |
| 10 | `:672-675` | 同上 | 同上。 |
| 11 | `:676-679` | 同上 | 同上。 |
| 12 | `:680-683` | `migrateReactionsForAgentActors` `:1526-…` | 重建 `projection_reactions`，把 actor 判别从 `bot` 改为 `agent`（列进入主键，故须重建）。 |
| 13 | `:684-689` | `rewriteGeneratedAgentIds` `:1591-1631` | 重写"应用自己生成的" agent id（`bot-<uuid>` → `agent-<uuid>`），用户自取 id 不动。 |
| 14 | `:690-692` | `refreshProviderSessionsForDynamicTools` | 再次失效所有 active provider session。 |
| 15 | `:693` | `ANALYTICS_SCHEMA_SQL` `:512-548` | 新增用量统计表 `agent_usage_records` / `agent_usage_checkpoints` / `agent_usage_activity`。 |
| 16 | `:694` | `ANALYTICS_DATE_INDEX_SQL` `:556-559` | 新增以 `occurred_at` 打头的两个统计索引（宿主级报表用）。 |
| 17 | `:696-703` | `migrateProviderSessionsForOpencode` `:1344-1349` | 重建 `projection_provider_sessions`，放宽 provider CHECK（加入 `opencode`）。 |
| 18 | `:704-707` | `CHANNEL_SCHEMA_SQL` | 新增 11 张 `projection_channel_*` 投影表（频道消息、任务、作业、上下文、摘要、读取、记忆、例程等）。 |
| 19 | `:708-716` | `migrateChannelSettings` `:1518-1521` | 修复未发布分支遗留的 provider session 约束，并新增 channel settings 投影。 |
| 20 | `:717-721` | `MCP_SERVERS_SCHEMA_SQL` | 新增 `projection_mcp_servers` 投影表。 |
| 21 | `:722-727` | `freeComputerUseServerName` `:1485-1517` | 把内置 "Computer Use" MCP server 改名成一个空闲名（只改数据，不改 schema）。 |
| 22 | `:728-734` | `migrateProviderSessionsForAntigravity` `:1350-1354` | 重建 provider session 表，放宽 CHECK（加入 `antigravity`）。 |
| 23 | `:735-740` | `migrateProviderSessionsForCustomAgents` `:1355-1359` | 重建 provider session 表，放宽 CHECK（加入 `acp`）。 |
| 24 | `:741-746` | `migrateProviderSessionsForCursor` `:1360-1364` | 重建 provider session 表，放宽 CHECK（加入 `cursor`）。 |
| 25 | `:747-751` | `MESSAGING_SCHEMA_SQL` | 新增 `projection_messaging_connections` / `_threads` / `_memories` / `_allowed_roles`。 |
| 26 | `:752-757` | `migrateProviderSessionsForCline` `:1365-1369` | 重建 provider session 表，放宽 CHECK（加入 `cline`）。 |
| 27 | `:758-763` | `addRoutineLimitPolicy` `:1370-1379` | 给 routine 与 run 加 `limit_policy` 列（默认 `wait`），既有例程保持等待语义。 |
| 28 | `:764-769` | `removeProviderSessionsCheck` `:1424-1429` | 重建 provider session 表，**移除** provider CHECK 约束。 |
| 29 | `:770-775` | `PROVIDER_HISTORY_SCHEMA_SQL` | 新增 provider history 导入状态/回合/暂存三张表。 |
| 30 | `:776-780` | `WEBHOOK_SCHEMA_SQL` `:28-…` | 新增 webhook 触发与投递表。 |
| 31 | `:781-786` | `ROUTINE_FLOW_SCHEMA_SQL` | 新增 `routine_flow_links` / `_positions` / `_steps`。 |
| 32 | `:787-790` | `migrateAgentMemorySelection` | 新增 `agent_memory_selections` 表并为既有记忆补默认选择行与索引。 |
| 33 | `:791-797` | `addRoutineMissedRuns` `:1380-1398` | 给 routine 与 run 加 `missed_policy` / 漏跑计数列。 |
| 34 | `:798-802` | `AGENT_WAIT_SCHEMA_SQL` | 新增 `agent_waits` 表。 |
| 35 | `:803-808` | `addRoutineRunSource` `:1399-1408` | 给 routine run 加 `run_source` 列。 |
| 36 | `:809-813` | `MESSAGING_MEMORY_SCHEMA_SQL` | 新增 messaging memory 投影表。 |
| 37 | `:814-819` | `ROUTINE_RESULT_SCHEMA_SQL` | 新增 `routine_result_destinations` / `_deliveries`。 |
| 38 | `:820-825` | `AGENT_TASK_SCHEMA_SQL` | 新增 `projection_agent_tasks` 表。 |
| 39 | `:826-831` | `deleteRepeatedMailboxReconciliations` `:1905-1934` | 删除重复的 mailbox reconcile 事件行（提交后 VACUUM，`:920-930`）。 |
| 40 | `:832-838` | `addThreadMessageOrder` `:1409-1423` | 给 thread message 加排序键列与索引，并按既有分页顺序回填键值。 |
| 41 | `:839-843` | `V41_ATTACHMENT_OWNER_INDEX_SQL` `:469-475` | 新增 attachment owner 索引。 |
| 42 | `:844-848` | `V42_ROUTINE_FLOW_SWEEP_INDEX_SQL` `:476-485` | 新增 routine flow sweep 索引。 |
| 43 | `:849-853` | `V43_THREAD_EVENT_TYPE_INDEX_SQL` `:486-490` | 新增 thread event type 索引。 |
| 44 | `:854-859` | `MESSAGING_ALLOWED_ROLES_SCHEMA_SQL` | 新增 messaging allowed roles 表（谁可以 @ OpenBot）。 |
| 45（fork） | `:860-865` | `ROUTINE_BACKOFF_SCHEMA_SQL` | **fork 谱系**：新增 `routine_backoff` 表（`CREATE TABLE IF NOT EXISTS`，已有行保留）。 |
| 46（fork = 上游 45） | `:866-871` | `BROWSER_PROFILE_SCHEMA_SQL`，`hasTable` 存在则跳过 | **上游谱系**：新增 `browser_profiles` 表。 |
| 47（fork = 上游 46） | `:872-877` | `WORKFLOW_SCHEMA_SQL`，存在则跳过 | 新增 `workflows` 及 10 张相关表。 |
| 48（fork = 上游 47） | `:878-883` | `AGENT_THREAD_MIGRATION_SQL`，存在则跳过 | 新增 `projection_agent_threads`，把 `projection_agent_tasks` 的行搬进去后 DROP 旧表。 |
| 49 | `:884-889` | `ROUTINE_BACKOFF_SCHEMA_SQL`，存在则跳过 | 给 **imported（上游来源）** 库补 `routine_backoff` 表。 |

**配套的 schema 分片**（`LATEST_SCHEMA_SQL` 与迁移共用，导入见 `openbot/src/backend/openbot-database-schema.ts:7-23`）：

| 分片文件 | 内容 |
| --- | --- |
| `openbot/src/backend/agent-memory-selection.ts:7` | `agent_memory_selections` |
| `openbot/src/backend/agent-tasks/agent-task-schema.ts:15` | `projection_agent_tasks` |
| `openbot/src/backend/agent-threads/agent-thread-schema.ts:17,42` | `projection_agent_threads` + 搬迁 SQL |
| `openbot/src/backend/agent-waits/agent-wait-schema.ts:18` | `agent_waits` |
| `openbot/src/backend/browser-profiles/browser-profile-schema.ts:9` | `browser_profiles` |
| `openbot/src/backend/channel-schema.ts:5,62` | `projection_channel_*` + channel settings |
| `openbot/src/backend/database/provider-history-schema.ts:8` | `provider_history_*` |
| `openbot/src/backend/mcp-schema.ts:14` | `projection_mcp_servers` |
| `openbot/src/backend/messaging/messaging-schema.ts:15,55,77` | `projection_messaging_*` |
| `openbot/src/backend/routine-backoff/routine-backoff-schema.ts:8` | `routine_backoff` |
| `openbot/src/backend/routine-flows/routine-flow-schema.ts:23` | `routine_flow_*` |
| `openbot/src/backend/routine-results/routine-result-schema.ts:21` | `routine_result_*` |
| `openbot/src/backend/workflows/workflow-schema.ts:42` | `workflows` 及 10 张相关表 |
| `openbot/src/backend/openbot-database-schema.ts:28` `WEBHOOK_SCHEMA_SQL` | webhook 触发/投递 |
| `openbot/src/backend/openbot-database-schema.ts:512` `ANALYTICS_SCHEMA_SQL`、`:556` `ANALYTICS_DATE_INDEX_SQL` | 用量统计表与索引 |

**历史与证据文件**（不是迁移本体，但是迁移的不可变约束）：

| 路径 | 说明 |
| --- | --- |
| `openbot/src/backend/openbot-database-schema-history.json` | fork 谱系 8..49 共 **42** 条，记录每版新增/修改/删除的对象与 DDL 原文（已核验：42 条、无缺号）。 |
| `openbot/src/backend/openbot-database-upstream-schema-history.json` | 上游谱系仅 45/46/47 三条。 |
| `openbot/src/backend/released-schemas/v0.1.12.sql`、`v0.1.15.sql`、`v0.1.18.sql`、`v0.2.1.sql` | 注册表之前的历史发布快照（标记 3/4/6），文件头注明"不要修改"，用作升级夹具。 |

### 2.2 账号服务 Cloudflare D1（次链，SQL 文件式）

目录：`openbot/apps/auth-api/migrations/`，**41 个 `.sql`**，编号 `0001`–`0040`，其中 **`0034` 重号**（`0034_account_attribution.sql` 与 `0034_server_claims.sql`）。

| 编号 | 文件 | 作用（一句话） |
| --- | --- | --- |
| 0001 | `0001_google_auth.sql` | 建 `users` / `oauth_flows` / `auth_exchange_codes` / `auth_sessions` 及索引。 |
| 0002 | `0002_email_login_codes.sql` | 建 `email_login_challenges` / `auth_rate_limits`（邮箱验证码登录 + 限流）。 |
| 0003 | `0003_remove_google_oauth.sql` | `users.google_subject` 改名为 `identity_key`，删除 oauth 两表。 |
| 0004 | `0004_team_auth_tickets.sql` | 建 `team_auth_tickets`（团队设备票据）。 |
| 0005 | `0005_team_tunnels.sql` | 建 `team_tunnels`（隧道归属与状态）。 |
| 0006 | `0006_one_team_server_per_user.sql` | 把 `team_tunnels.user_id` 索引改为 UNIQUE（一人一服务器）。 |
| 0007 | `0007_team_host_machine_tokens.sql` | 给 `team_tunnels` 加 `machine_token_hash` 与唯一部分索引。 |
| 0008 | `0008_remove_team_tunnel_vnc.sql` | 重建 `team_tunnels`，移除 VNC 相关列。 |
| 0009 | `0009_skills_marketplace.sql` | 建技能市场三表（`marketplace_skills` / `_versions` / `_install_receipts`）。 |
| 0010 | `0010_agents_marketplace.sql` | 建 Agent 市场三表。 |
| 0011 | `0011_marketplace_catalog_indexes.sql` | 为两个市场加目录排序部分索引。 |
| 0012 | `0012_remote_control_plane.sql` | 建远程控制面：`remote_hosts` / `_memberships` / `_invites` / `_sessions`。 |
| 0013 | `0013_remote_session_lifecycle.sql` | 建 `remote_auth_events`（重试发件箱）与远程会话生命周期索引/触发器。 |
| 0014 | `0014_mobile_auth_sessions.sql` | 建 `mobile_auth_sessions`（手机端会话）。 |
| 0015 | `0015_email_challenge_delivery_state.sql` | 给 `email_login_challenges` 加 `delivery_state` 列（pending/sent/failed）。 |
| 0016 | `0016_hosted_sites.sql` | 建 `hosted_sites` / `site_deployments`（托管站点控制面）。 |
| 0017 | `0017_mobile_session_security.sql` | 给票据加移动端主机列、永久化会话有效期、撤销远程会话触发器。 |
| 0018 | `0018_remote_device_sessions.sql` | 给 `remote_sessions` 加 `auth_session_hash`，唯一索引改为"每设备一活跃会话"。 |
| 0019 | `0019_marketplace_presentation.sql` | 给市场版本加 `category`、给市场项加 `show_creator_avatar`。 |
| 0020 | `0020_permanent_invites.sql` | 给 `remote_invites` 加 `max_uses` / `use_count`（永久邀请链接）。 |
| 0021 | `0021_agent_templates.sql` | 建 `agent_templates`（Agent 模板）。 |
| 0022 | `0022_billing.sql` | 建 `billing_customers` / `_subscriptions` / `_webhook_events`。 |
| 0023 | `0023_hosted_servers.sql` | 建 `hosted_servers` / `hosting_webhook_deliveries`。 |
| 0024 | `0024_hosted_site_servers.sql` | 给 `hosted_sites` 加 `server_id` 与索引（站点归属服务器）。 |
| 0025 | `0025_slack_workspace_routes.sql` | 建 `slack_workspace_routes`（Slack 工作区 → 主机路由）。 |
| 0026 | `0026_discord_guild_routes.sql` | 建 `discord_guild_routes`。 |
| 0027 | `0027_webhook_routes.sql` | 建 `webhook_routes`（公开路由 ID → 主机/账号归属）。 |
| 0028 | `0028_hosted_server_lifecycle.sql` | 给 `hosted_servers` 加删除排期/订阅/租约列与索引。 |
| 0029 | `0029_telegram_chat_routes.sql` | 建 `telegram_chat_routes` / `telegram_link_codes`。 |
| 0030 | `0030_sales_analytics.sql` | 建 `billing_analytics_events` / `billing_fx_rates` 与触发器。 |
| 0031 | `0031_billing_sales.sql` | 建 `billing_sales_checkouts` / `billing_sales_facts`（结账与支付事实）。 |
| 0032 | `0032_mobile_connect_links.sql` | 建 `mobile_connect_links`（可复用 Mobile Connect 链接，仅存哈希）。 |
| 0033 | `0033_mobile_session_connect_link.sql` | 给 `mobile_auth_sessions` 加 `connect_link_host_id` 与索引。 |
| **0034** | `0034_account_attribution.sql` | 建 `account_attribution`（首次获客来源，**重号**）。 |
| **0034** | `0034_server_claims.sql` | 建 `server_claims`（服务器一次性认领，**重号**）。 |
| 0035 | `0035_billing_sales_reversals.sql` | 重建 `billing_sales_facts` 以容纳退款/拒付事实类型。 |
| 0036 | `0036_membership_removed_at.sql` | 给 `remote_memberships` 加 `removed_at`（**UPDATE 之外的数据型迁移**）。 |
| 0037 | `0037_close_invites_of_former_admins.sql` | 关闭已离职/降权 admin 留下的未用邀请（**纯 UPDATE 数据迁移**）。 |
| 0038 | `0038_licenses.sql` | 建 `licenses`（商业许可订阅）。 |
| 0039 | `0039_remote_host_key_handovers.sql` | 建 `remote_host_key_handovers`（主机密钥交接链）。 |
| 0040 | `0040_server_member_limits.sql` | 建 `server_member_limits`（单服务器成员数上限）。 |

### 2.3 迁移执行入口

**桌面端 `openbot.db`**（自上而下的唯一调用链）：

```
OpenBotDatabase.initialize()            openbot/src/backend/openbot-database.ts:103-105
  └─ DatabaseCore.initialize()          openbot/src/backend/database/database-core.ts:61-97
       ├─ 打开前读 -wal 是否存在 → closedCleanly   :65
       ├─ PRAGMA journal_mode=WAL / foreign_keys=ON / busy_timeout=5000   :70-73
       └─ DatabaseCore.#migrate(closedCleanly)      :81
            └─ migrateOpenBotDatabase(connection, { closedCleanly })   :213-215
                 ├─ validateMigrationRegistry()            openbot-database-schema.ts:980
                 ├─ 无既有 schema → createLatestDatabase()  :898-901, :1190-1202
                 │     （exec LATEST_SCHEMA_SQL :561 + 把 42 个标记全部写成已应用）
                 ├─ readAppliedVersions / validateAppliedVersions   :1204 / :1220
                 ├─ validateReservedSchema(db, currentVersion)     :1000-1087
                 └─ 逐条 runMigration()                    :1243-1259
                       （BEGIN IMMEDIATE → up() → 写 schema_migrations 标记
                         → assertForeignKeys → COMMIT；失败 ROLLBACK）
```

- 单条迁移的原子性、`disableForeignKeys` 的 `finally` 复位、持写锁时的保留 schema 复检：`openbot-database-schema.ts:1243-1259`。
- 迁移后 `PRAGMA quick_check(20)`：`:1268-1277`（仅在"有迁移发生"或"上次未干净关闭"时跑，`:931-933`）。
- 版本高于注册表末版 / 历史有洞 → 拒绝启动：`:1220-1236`。

**账号服务 D1**：

- 应用命令：`openbot/apps/auth-api/package.json:19-20`
  - `wrangler d1 migrations apply openbot-auth --local`
  - `wrangler d1 migrations apply openbot-auth --remote`
- 绑定与目录：`openbot/apps/auth-api/wrangler.jsonc:72`（`d1_databases[0].migrations_dir = "migrations"`；另有 test 环境两处同名绑定）。
- 仓库根快捷命令：`openbot/package.json:96-97`（`api:migrate:local` / `api:migrate:remote`）。
- CI 顺序（先迁移后部署，竞态窗口由此产生）：`openbot/.github/workflows/ci.yml:1102-1107`。

---

## 3. 兼容性测试现状

### 3.1 桌面端测试文件清单

`openbot/src/backend/` 下共 25 个相关测试文件（23 个 `*-migration.test.ts` + `openbot-database-schema-parity.test.ts` + `openbot-database.test.ts`）：

`agent-memory-selection-` / `agent-task-` / `agent-thread-` / `agent-usage-` / `agent-wait-` / `agent-workspace-root-` / `attachment-owner-index-` / `browser-profile-` / `computer-use-name-` / `conversation-read-` / `event-type-index-` / `mailbox-reconcile-` / `messaging-allowed-roles-` / `messaging-memory-` / `migration-guard-` / `openbot-database-events-` / `pre-registry-schema-` / `routine-backoff-` / `routine-flow-sweep-index-` / `routine-result-` / `routine-run-columns-` / `thread-message-order-` / `workflow-migration.test.ts`

### 3.2 D1 测试文件清单

`openbot/apps/auth-api/test/` 下 12 个 `*-migration.test.ts` + 1 个夹具 `sqlite-d1.ts`（另有与迁移无关的 `dev-network-access.test.ts`）：

`billing-` / `billing-sales-` / `email-challenge-delivery-` / `hosted-servers-` / `hosted-site-servers-` / `license-` / `marketplace-` / `mobile-auth-` / `remote-control-plane-` / `remote-host-key-handovers-` / `sales-analytics-` / `server-member-limits-migration.test.ts`

夹具 `sqlite-d1.ts:5-13` 的 `migratedDatabase(last?)`：按**文件名排序**逐个 `exec` 迁移文件；传 `last` 时在文件名大于 `last` 处停止。

### 3.3 覆盖矩阵

#### 维度 A — 空库

| 对象 | 判定 | 证据 |
| --- | --- | --- |
| 桌面端：全新安装 | **已覆盖** | `openbot-database-schema-parity.test.ts:36` `newInstallDatabase()`（空 `:memory:` 库 → `createLatestDatabase`，`:184-188`）；`:43` 比较归一化声明；`:50` 比较已应用版本集合；`:178-182` 说明该分支就是 `hasExistingSchema` 为假时走的路径。 |
| 桌面端：空库 vs 升级库形状一致 | **已覆盖** | `openbot-database-schema-parity.test.ts:36-52`（表/列/索引/外键形状 + 归一化 DDL + 标记三组断言）。 |
| 桌面端：空库跑完整链 | **已覆盖** | `openbot-database-schema-parity.test.ts:197-201` `upgradedInstallDatabase()`：只有一张空 `schema_migrations` 表，从 v8 基线一路跑到 v49。 |
| 桌面端：空库的 `schema_migrations` 标记内容（42 条、时间戳一致） | **已覆盖** | `openbot-database-schema-parity.test.ts:50` 比较两条路径的已应用版本；`openbot-database-schema.ts:1190-1202` 的 `createLatestDatabase` 写全部标记。 |
| D1：空库跑**整条链**（41 个文件按序） | **未覆盖（vitest 层）** | `sqlite-d1.ts:5` 的 `migratedDatabase()` 在 `apps/auth-api/test/` 的 **10 处调用全部带 `last` 上界**（`license-migration.test.ts:13`、`remote-host-key-handovers-migration.test.ts:13`、`billing-sales-migration.test.ts:13,38`、`hosted-servers-migration.test.ts:23`、`hosted-site-servers-migration.test.ts:23`、`server-member-limits-migration.test.ts:14`、`billing-migration.test.ts:13`、`sales-analytics-migration.test.ts:13,75`），**无参调用在全仓库不存在**；`0040` 只被单独 exec（`server-member-limits-migration.test.ts:30-32`）。<br>**旁证（e2e 层）**：`apps/auth-api/scripts/` 下 8 个脚本各自实现"按文件名排序应用全部 41 个迁移"（`remote-auth-events-e2e.ts:29-32`、`invite-acceptance-e2e.ts:31-34`、`host-key-handover-e2e.ts:36-39`、`cloud-authorization-e2e.ts:27-30`、`hosted-held-resume-e2e.ts:39`、`hosted-retention-wake-e2e.ts:35`、`hosted-start-wait-e2e.ts:41`、`billing-snapshot-e2e.ts:90-93`），所以"整链能应用"事实上被它们依赖；但这些脚本**不在 `bun run test:api`（vitest）里，也不在 `openbot/.github/workflows/ci.yml` 里**，且 `grep -l "integrity_check\|foreign_key_check" scripts/*.ts` **无匹配**——即没有一条断言说"整链跑完后库是干净的"。 |

#### 维度 B — 非空库（有用户数据）

| 对象 | 判定 | 证据 |
| --- | --- | --- |
| 桌面端：升级保留数据 | **已覆盖** | 逐条迁移都有"播种 → 升级 → 断言行不变"，如 `routine-backoff-migration.test.ts:60-92`（v8..v48 共 41 个版本各一条）、`agent-task-migration.test.ts:86-101`、`workflow-migration.test.ts:136-172`、`thread-message-order-migration.test.ts:169-194`。 |
| 桌面端：真实发布期快照升级 | **已覆盖** | `pre-registry-schema-migration.test.ts:24-34`（4 个发布 tag × 7 组标记组合）、`:251-345` 断言除"基线本来就会改的 6 张表"（`:241-248` `CHANGED_TABLES`）外**所有表的每一行都不变**，且升级后 schema 与新装库一致（`:265`）。 |
| 桌面端：整库升级与数据保全 | **已覆盖** | `openbot-database.test.ts` 25 条以上用例，含 v3 历史迁移（`:1399`）、v19→v20 MCP（`:1726`）、v24→v25 messaging（`:1771`）、v29→v30 webhook（`openbot-database-events-migration.test.ts:22`）。 |
| D1：升级保留数据 | **已覆盖** | 每个专项测试都播种数据 → 应用迁移 → 断言快照不变，如 `sales-analytics-migration.test.ts:12-70`、`server-member-limits-migration.test.ts:13-83`、`remote-host-key-handovers-migration.test.ts:12`、`license-migration.test.ts:12`。 |
| D1：迁移可重复执行（幂等） | **已覆盖** | `marketplace-migration.test.ts:24`（`0011` 连跑两次）、`billing-migration.test.ts:12`、`license-migration.test.ts:12`、`server-member-limits-migration.test.ts:31-34`（`0040` 连跑两次 + 快照不变）。 |
| 桌面端：**超大库 / 长事务 / 磁盘写满**下的升级 | **未覆盖** | 全仓库没有针对 `SQLITE_FULL`、只读文件、超大 `openbot.db` 的迁移用例。 |
| 桌面端：**两个进程同时打开同一库**时的升级 | **未覆盖** | 只有 `database-core.ts:72` 的 `busy_timeout = 5000` 与 `runMigration` 的 `BEGIN IMMEDIATE`（`openbot-database-schema.ts:1246`）；没有并发升级测试。 |

#### 维度 C — 不同来源库

| 对象 | 判定 | 证据 |
| --- | --- | --- |
| 桌面端：**fork 谱系**来源 | **已覆盖** | `routine-backoff-migration.test.ts:162-181`（fork 45 的 backoff 行在 46/47/48/49 失败后仍保留且标记时间戳不变）；`openbot-database.test.ts:4031-4064`。 |
| 桌面端：**imported（上游发布）谱系**来源 | **已覆盖** | `routine-backoff-migration.test.ts:129-144` `importedAt(version)` 构造上游 45/46/47 三种来源；`:183-218` 对 45/46/47 × 46/47/48/49 共 6 种组合断言"失败不改变来源数据、重试后数据不变、标记到 49、再次执行是空操作"。 |
| 桌面端：保留 schema 的**声明文本差异**（上游写法不同） | **已覆盖** | `routine-backoff-migration.test.ts:267-305`：接受上游原文与改写格式的同一张表（`:282-304`）；`:306` 接受大写表名；`:344`/`:357`/`:370` 拒绝列/字符串字面量/CHECK 不一致的表。判定逻辑在 `openbot-database-schema.ts:1000-1087`（状态机 `fork`/`upstream`/`imported48`/`earlier`，`:1058-1078`）。 |
| 桌面端：**注册表之前**的历史发布（标记 3/4/6） | **已覆盖** | `pre-registry-schema-migration.test.ts:24-34, 251-345`。 |
| 桌面端：保留表"有表无标记" / "有标记无表" | **已覆盖** | `routine-backoff-migration.test.ts:231-237`（拒绝"有表无标记"）、`:239-252`（拒绝矛盾结构）；`migration-guard-migration.test.ts:102-113`（缺任意版本一律拒绝且不改任何东西）。 |
| 桌面端：**更晚的上游版本**（上游 48 及以后） | **未覆盖（上游尚未发布，无样本）** | `openbot-database-upstream-schema-history.json` 只有 45/46/47；`openbot-database-schema-parity.test.ts:64-70` 把"上游版本 → fork 版本"硬编码为 `+1` 偏移。 |
| 桌面端：**标记行本身异常**（`applied_at` 为空/非法、重复行） | **未覆盖** | `openbot-database-schema.ts:1204-1218` 只读 `version`；`applied_at` 的 `NOT NULL` 由 SQLite 自身保证，但没有测试断言"标记内容被篡改时的行为"。 |
| D1：**旧 Worker 版本**（部署竞态窗口） | **部分覆盖** | 已覆盖：`0009/0010/0011/0012/0013/0014/0015/0017/0018/0019/0020/0021/0022/0023/0024/0030/0031/0034_server_claims/0035/0037/0038/0039/0040`（多数用例含"旧 Worker 仍能插入"断言，如 `marketplace-migration.test.ts:60-67`、`server-member-limits-migration.test.ts:36-42`）。**未覆盖**：`0001`–`0008`、`0016`、`0025`–`0029`、`0032`、`0033`、`0034_account_attribution`、`0036`。其中 `0036`/`0037` 是**直接改写生产行**的 `UPDATE` 迁移，风险最高。 |
| D1：**旧 Worker 的完整快照** | **未覆盖** | 现有夹具一律用"本仓库当前的迁移文件"重建旧库（`sqlite-d1.ts:8-10`），没有像桌面端 `released-schemas/*.sql` 那样的"某个已发布 Worker 的真实 DDL 快照"，因此"旧 Worker 兼容"只证明了"旧**代码**能写新 schema"，没有证明"旧**schema**能被新迁移正确升级"。 |

#### 维度 D — 失败回滚与重试

| 对象 | 判定 | 证据 |
| --- | --- | --- |
| 桌面端：单条迁移失败 → 回滚（表 + 标记） | **已覆盖** | 每个 `*-migration.test.ts` 都有 "rolls back … then retries"：用 `CREATE TEMP TRIGGER fail_marker BEFORE INSERT ON schema_migrations WHEN new.version = N BEGIN SELECT RAISE(ABORT, 'late failure'); END;` 让标记写入失败（`routine-backoff-migration.test.ts:168-169`、`agent-thread-migration.test.ts:232-250`、`thread-message-order-migration.test.ts:196-216`、`mailbox-reconcile-migration.test.ts:143-157` 等 20+ 处）。 |
| 桌面端：失败后**重试**成功且数据不变 | **已覆盖** | 同一批用例在去掉触发器后再次 `migrateOpenBotDatabase(db)` 并断言数据与标记。 |
| 桌面端：外键违规 → 回滚 → 重试 | **已覆盖** | `migration-guard-migration.test.ts:69-101`：对 v28 与 v43–v49 逐个注入"孤儿"消息/回合，断言整库 schema 与数据零变化、`PRAGMA foreign_keys` 仍为 1、再次启动仍然失败、删掉孤儿后升级完成且 `foreign_key_check` 空、`integrity_check` ok。 |
| 桌面端：基线迁移失败 → 回滚 → 重试 | **已覆盖** | `pre-registry-schema-migration.test.ts:347-373`。 |
| 桌面端：完整性检查（`quick_check`）在非干净关闭后触发 | **已覆盖** | `openbot-database.test.ts:3984-3998`（非干净关闭时检查）、`:3999-4010`（干净关闭时不读全文件）、`:4011-4028`（迁移后检查）。 |
| 桌面端：VACUUM 失败只告警不致命 | **未覆盖** | `openbot-database-schema.ts:920-930` 的 `warn` 分支没有测试。 |
| D1：迁移失败 → 回滚 | **部分覆盖** | 仅 `sales-analytics-migration.test.ts:74-90` 一条：手工 `BEGIN` → 迁移后再插一个同名表触发失败 → `ROLLBACK` → 断言新表全部不存在。**注意：这是测试自己写的事务，不是 D1 执行器的行为**；wrangler 逐文件事务化与失败重试没有任何测试。 |
| D1：迁移失败后**由部署流程重试** | **未覆盖** | 没有测试模拟"迁移文件被改动后重跑"或"D1 迁移表记账"。 |
| D1：**数据型迁移**（`0036`、`0037`）的失败回滚 | **未覆盖** | `0036`/`0037` 没有专项测试文件；`scripts/` 下的 e2e 引用 `0036`/`0037` 但不在 vitest 套件内。 |

---

## 4. 补测建议（可独立实现的小 issue）

每条都可单独开 issue、单独实现、单独验收。命令均以仓库根为工作目录；`openbot/` 的依赖需先 `bun install --frozen-lockfile`。

### 建议 1 — D1 整链空库冒烟（D1 最高优先级缺口）

- **内容**：新增 `openbot/apps/auth-api/test/migration-chain.test.ts`，用 `migratedDatabase()`（**不传 `last`**）把 41 个迁移文件按序应用到内存库，断言：`PRAGMA foreign_key_check` 为空、`PRAGMA integrity_check` 为 `ok`、`sqlite_master` 中每个 `CREATE TABLE` 的表都存在、迁移文件数与目录文件数一致（防止新增文件漏测）。同时断言 `0034` 重号不会导致漏应用（两个文件都必须在结果里）。
- **验收命令**：
  ```bash
  cd openbot/apps/auth-api && bun run test -- test/migration-chain.test.ts
  ```
- **说明**：当前 `sqlite-d1.ts:5` 的 `migratedDatabase()` 已支持无参调用，只需补测试文件即可。现状是：整链"能应用"只被 `apps/auth-api/scripts/` 下的 8 个 e2e 脚本隐式依赖，而它们不在 vitest 套件、不在 CI、也不做完整性断言——这条测试把那个隐式依赖变成一条显式的、每次都跑的断言。

### 建议 2 — D1 迁移编号唯一性守护

- **内容**：新增 `openbot/apps/auth-api/test/migration-numbering.test.ts`，扫描 `migrations/` 目录，断言文件名前缀（4 位序号）唯一且从 `0001` 连续。**当前会以 `0034` 重号失败**——所以这条 issue 必须与"把 `0034_server_claims.sql` 重命名为 `0034b`/`0035` 之后的连续编号"一起做，或先标 `skip` 并留 TODO 注释说明。
- **验收命令**：
  ```bash
  cd openbot/apps/auth-api && bun run test -- test/migration-numbering.test.ts
  ```
- **注意**：重命名 D1 迁移文件会改变 D1 迁移表里的迁移名，**只能在所有环境都还没应用过该文件之前做**；否则需要走"扩展 → 部署 → 收缩"（`openbot/apps/auth-api/AGENTS.md:22-24`）。

### 建议 3 — D1 数据型迁移 `0036` 专项测试

- **内容**：新增 `openbot/apps/auth-api/test/membership-removed-at-migration.test.ts`：构造 `remote_memberships` 三种状态（主动离开 → `removed_at = 0`；被移除 → `removed_at` 为毫秒时间戳；仍活跃 → `NULL`；迁移前就被撤销 → 迁移后 `removed_at` 取 `updated_at`），应用 `0036` 后逐行断言；再断言旧 Worker 写入的一行 `removed_at` 为 `NULL` 时仍被算作移除。
- **验收命令**：
  ```bash
  cd openbot/apps/auth-api && bun run test -- test/membership-removed-at-migration.test.ts
  ```

### 建议 4 — D1 数据型迁移 `0037` 专项测试（含回滚/重试）

- **内容**：新增 `openbot/apps/auth-api/test/close-invites-migration.test.ts`：构造"创建者仍是 owner/admin"、"创建者已离职/被移除/被降级"、"邀请已被使用"、"邀请已撤销"四种组合，断言 `0037` 只关闭第三种之外应关闭的那些、且不动已使用/已撤销的行；再在 `BEGIN` 中让迁移后的一步失败并 `ROLLBACK`，断言 `remote_invites` 完全回到迁移前。
- **验收命令**：
  ```bash
  cd openbot/apps/auth-api && bun run test -- test/close-invites-migration.test.ts
  ```

### 建议 5 — D1 "旧 Worker 兼容矩阵"测试

- **内容**：新增 `openbot/apps/auth-api/test/legacy-worker-compat.test.ts`，为目前没有专项测试的迁移（`0001`–`0008`、`0016`、`0025`–`0029`、`0032`、`0033`、`0034_account_attribution`）各写一条最小断言：迁移后旧 Worker 仍能按旧列集合 `INSERT`/`SELECT` 成功，且 `PRAGMA foreign_key_check` 为空。
- **验收命令**：
  ```bash
  cd openbot/apps/auth-api && bun run test -- test/legacy-worker-compat.test.ts
  ```

### 建议 6 — 桌面端：迁移期间并发打开同一库

- **内容**：新增 `openbot/src/backend/database-concurrent-migration.test.ts`：用两个 `DatabaseSync` 打开同一个临时文件，第二个连接在第一个持 `BEGIN IMMEDIATE` 写锁时执行 `migrateOpenBotDatabase`，断言（a）在 `busy_timeout` 内失败或成功，**绝不出现"迁移标记已写但 DDL 未生效"的中间态**；（b）失败后数据库仍可被第三个连接正常升级到 v49。
- **验收命令**：
  ```bash
  cd openbot && bun run test:desktop -- src/backend/database-concurrent-migration.test.ts
  ```

### 建议 7 — 桌面端：迁移标记行异常时的行为

- **内容**：新增 `openbot/src/backend/migration-marker-integrity.test.ts`：对"标记的 `applied_at` 为空串 / 非 ISO 串 / 早于基线时间"的库，断言 `migrateOpenBotDatabase` 的既定行为（当前实现应**接受**并保留原样，因为只读 `version`）；同时断言"标记里出现重复 version"被 `validateAppliedVersions` 拒绝且零写入。
- **验收命令**：
  ```bash
  cd openbot && bun run test:desktop -- src/backend/migration-marker-integrity.test.ts
  ```
- **说明**：这条先把现状**固化成契约**，不预设期望行为；若后续决定加严，实现与测试一起改。

### 建议 8 — 把桌面端迁移测试接入仓库 CI

- **内容**：在 `.github/workflows/phanes.yml` 的 openbot job 中增加 `bun run test:desktop -- src/backend/openbot-database-schema-parity.test.ts src/backend/migration-guard-migration.test.ts src/backend/routine-backoff-migration.test.ts src/backend/pre-registry-schema-migration.test.ts`，与 `openbot/.github/workflows/ci.yml:225` 的做法对齐（当前 Phanes 侧 CI 不跑 openbot 的 vitest，见 `docs/audit/issue-20-upstream-sync.md:115-117`）。
- **验收命令**：
  ```bash
  grep -n "openbot-database-schema-parity" .github/workflows/phanes.yml
  ```
  ```bash
  cd openbot && bun run test:desktop -- src/backend/openbot-database-schema-parity.test.ts src/backend/migration-guard-migration.test.ts src/backend/routine-backoff-migration.test.ts src/backend/pre-registry-schema-migration.test.ts
  ```

> 附加（可选，同属"让测试能被跑到"）：扩展 `openbot/.githooks/pre-commit:102` 的 `schema_pattern`，把 15 个 schema 分片与 `openbot-database-upstream-schema-history.json` 纳入触发面。验收命令：
> ```bash
> grep -n "workflow-schema\|browser-profile-schema" openbot/.githooks/pre-commit
> ```

---

## 5. 本次已核验 / 未能核验

**已核验（静态阅读 + 文件系统盘点）**

- 桌面端 `MIGRATIONS` 注册表版本连续为 8..49、共 42 条，无重复无空洞（`openbot-database-schema.ts:660-891` 逐条读取；`:980-990` 的 `validateMigrationRegistry` 是运行期保证）。
- `openbot-database-schema-history.json` 实测 42 条、覆盖 8..49、无缺号；`openbot-database-upstream-schema-history.json` 仅 45/46/47 三条。
- D1 目录实测 41 个 `.sql`，`0001`–`0040`，`0034` 重号两文件（`0034_account_attribution.sql`、`0034_server_claims.sql`）。
- `migratedDatabase()` 在 `apps/auth-api/test/` 的 **10 处调用全部带 `last` 上界**，无参调用不存在 ⇒ "整链空库"在 vitest 层无断言（e2e 脚本层隐式依赖，见 3.3 维度 A）。
- 桌面端 25 个相关测试文件、D1 12 个 `*-migration.test.ts` + 1 个夹具，逐个读取了 `describe`/`it` 列表。
- 桌面端与 D1 两条链的迁移执行入口调用链（第 2.3 节）逐行确认。

**未能核验**

- **所有 vitest 用例均未执行**：本工作树无 `openbot/node_modules`，机器无 `bun`/`vitest`/`npx`（仅 Node v22.23.3）。本文件关于"已覆盖/未覆盖"的判定是对**测试源码**的判定，不是对**运行结果**的判定。
- **未连接任何数据库**：未执行 `wrangler d1 migrations apply`，未打开任何真实 SQLite/D1 实例，未执行 `openbot.db` 的升级路径。D1 整链可应用性的实测结论沿用 `docs/audit/issue-20-upstream-sync.md:241, 267`（上一轮用 `node:sqlite` 实跑）。
- **上游已发布版本的真实标记集合**：仓库内无 tag、无网络，无法独立复核；相关说法以 `openbot/docs/architecture/database-migrations.md:29-46` 与 `openbot-database-upstream-schema-history.json` 为准。
- `openswarm/` 侧的 `dispatch.sqlite`（`openswarm/bin/hub-openswarm-bridge`）不在本次范围内（任务限定 `openbot/`），仅在上一轮审计中记录。
