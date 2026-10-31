# Issue #20 审计：OpenBot 上游同步点与数据库迁移兼容性

审计日期：2026-10-31。范围：本仓库（Phanes）当前工作树 `openswarm/20261031-154039-014c16`（HEAD `df02c552d`）。

本文件只做只读盘点与计划，不改动任何产品代码。文中引用格式为 `路径:行号`，行号对应本仓库当前工作树。

## 0. 方法与本次核验的边界

- 盘点方式：文件系统遍历 + `git log`/`git diff`/`git cat-file` 的对象库比对 + Node `node:sqlite`（本机 Node v22.23.3）实跑。
- **未能核验的部分**：本沙箱无网络（`git ls-remote origin` 报 `Could not resolve host: github.com`），仓库内也没有任何 tag，因此"上游已发布版本的迁移标记集合"只能引用仓库自带文档 `openbot/docs/architecture/database-migrations.md` 的说法，无法独立复核。
- **未能运行的测试**：本工作树没有 `openbot/node_modules`，机器上也没有 `bun`，所以 `bun run test:desktop`（vitest）整套迁移测试无法在此执行。第 5 节给出的是可在有依赖的机器上执行的命令。

## 1. 结论摘要

1. 本仓库涉及数据库/schema 的资产有三处，全部集中在 `openbot/` 子树内；`openswarm/` 没有 SQL 迁移。
2. 桌面端 `openbot.db` 有一条 **v8 → v49、连续 42 条**的迁移链（`openbot/src/backend/openbot-database-schema.ts:25`、`:660`、`:892`），并且**存在两条编号谱系**：OpenBot fork 谱系与"上游发布"谱系，两者的 45/46/47 指向不同的操作（`openbot/docs/architecture/database-migrations.md:29-46`）。
3. 账号服务另有一条 **Cloudflare D1 迁移链**（`openbot/apps/auth-api/migrations/`，41 个文件），规则与桌面端相反：可回滚，但**被部署竞态**（`openbot/apps/auth-api/AGENTS.md:14-27`）。
4. 上游同步点有两个：`openbot/` 与 `openswarm/` 两个 git subtree，加上 `integrations/` 的符号链接。**最近一次 openbot 上游同步（`596e0a6ab`）对全部迁移/schema 文件零改动**（本次用 `git diff` 逐路径比对确认）。
5. 真正的风险不是"上游改了表结构"，而是 **上游新增迁移版本会打破 fork 侧硬编码的版本号与偏移假设**，进而逼迫改动已发布的迁移 —— 而桌面端迁移不可回滚、无备份。

## 2. 迁移链与 schema 文件清单

### 2.1 桌面端 `openbot.db`（主链，OpenBot 子树内）

| 资产 | 路径 | 说明 |
| --- | --- | --- |
| 迁移注册表 | `openbot/src/backend/openbot-database-schema.ts:660-891` | `MIGRATIONS` 数组，版本 8..49；`:892` 由末元素推导 `LATEST_SCHEMA_VERSION` |
| 冻结基线 v8 | `openbot/src/backend/openbot-database-schema.ts:74`（`BASELINE_V8_SCHEMA_SQL`） | 注释 `:72-73` 明确"不得修改，只能追加迁移" |
| 新装库 schema | `openbot/src/backend/openbot-database-schema.ts:561-608`（`LATEST_SCHEMA_SQL`） | 由基线 + 13 个 schema 分片拼接；`createLatestDatabase`（`:1190`）直接 exec 它并把 42 个标记全部写成已应用 |
| 迁移执行器 | `openbot/src/backend/openbot-database-schema.ts:894-933` | 入口 `migrateOpenBotDatabase`；生产调用点 `openbot/src/backend/database/database-core.ts:214` |
| 保留 schema 校验 | `openbot/src/backend/openbot-database-schema.ts:1000-1080` | `validateReservedSchema`，按标记版本判定 fork/imported 形状 |
| 单条迁移执行 | `openbot/src/backend/openbot-database-schema.ts:1243-1259` | 每条迁移一个事务 + 一个 `schema_migrations` 标记；`:1248` 在持写锁时复检保留 schema |
| 版本历史（fork） | `openbot/src/backend/openbot-database-schema-history.json` | 42 个版本条目（8..49），记录每个版本新增/修改/删除的对象 |
| 版本历史（上游） | `openbot/src/backend/openbot-database-upstream-schema-history.json` | 只有上游谱系 45/46/47 三条 |
| 发布期 schema 快照 | `openbot/src/backend/released-schemas/v0.1.12.sql`、`v0.1.15.sql`、`v0.1.18.sql`、`v0.2.1.sql` | v8 迁移注册表之前的旧发布（标记 3/4/6），文件头注明"不要修改" |
| 迁移总说明 | `openbot/docs/architecture/database-migrations.md` | 两条谱系、保留 schema 证据、失败/重试语义 |

`LATEST_SCHEMA_SQL` 拼装的 13 个 schema 分片及其出处（`openbot/src/backend/openbot-database-schema.ts:7-23`）：

`agent-memory-selection.ts`、`agent-tasks/agent-task-schema.ts`、`agent-threads/agent-thread-schema.ts`、`agent-waits/agent-wait-schema.ts`、`browser-profiles/browser-profile-schema.ts`、`channel-schema.ts`、`database/provider-history-schema.ts`、`mcp-schema.ts`、`messaging/messaging-schema.ts`、`routine-backoff/routine-backoff-schema.ts`、`routine-flows/routine-flow-schema.ts`、`routine-results/routine-result-schema.ts`、`workflows/workflow-schema.ts`；另有两个定义在主文件内的分片：`WEBHOOK_SCHEMA_SQL`（`:28`）与 `ANALYTICS_SCHEMA_SQL`（`:512`）。

按 `openbot-database-schema-history.json` 统计，当前 schema 覆盖 76 张表、65 个索引、3 个触发器。

### 2.2 账号服务 D1（次链，`openbot/apps/auth-api/`）

- 迁移目录 `openbot/apps/auth-api/migrations/`：41 个 `.sql`，编号 `0001`–`0040`，其中 **`0034` 重号**（`0034_account_attribution.sql` 与 `0034_server_claims.sql`）。
- 应用命令：`openbot/apps/auth-api/package.json:19-20`（`wrangler d1 migrations apply openbot-auth --local/--remote`）；配置 `openbot/apps/auth-api/wrangler.jsonc:72`（`migrations_dir: "migrations"`）。
- 生产 CI 顺序：`openbot/.github/workflows/ci.yml:1102-1107` 先 `wrangler d1 migrations apply DB --remote`，`:1113` 才 `wrangler deploy`。
- 测试夹具：`openbot/apps/auth-api/test/sqlite-d1.ts:5-13` 的 `migratedDatabase()` 按文件名排序逐个应用；13 个 `*-migration.test.ts` 覆盖单条迁移。

### 2.3 OpenSwarm 侧与连接层

- `openswarm/` **没有 SQL 迁移**。运行态是 JSON/JSONL：`state.json`、`ledger.jsonl`、`breakers.json`、`lanes.json`、`fleet.json`（`openswarm/README.md:615-616`）。schema 变更靠读写代码本身，没有版本化迁移。
- 唯一的例外是连接入口 `openswarm/bin/hub-openswarm-bridge:134-143`：内嵌一个 SQLite `~/.claude/openswarm-hub-bridge/dispatch.sqlite`，表 `handoffs`，并用一次性 `ALTER TABLE handoffs ADD COLUMN route TEXT` 做幂等升级。`integrations/` 下的四个入口都是指向 `openswarm/bin/` 的符号链接（`integrations/README.md`）。

### 2.4 迁移相关测试（覆盖面）

桌面端 `openbot/src/backend/` 下：`openbot-database-schema-parity.test.ts`（两条构建路径对拍 + 两份历史文件校验）、`pre-registry-schema-migration.test.ts`（v0.1.12–v0.2.1 发布期快照升级）、以及 `agent-thread-migration.test.ts`、`workflow-migration.test.ts`、`browser-profile-migration.test.ts`、`routine-backoff-migration.test.ts`、`openbot-database.test.ts` 等 20 余个 `*-migration.test.ts`。

## 3. 上游同步点

### 3.1 来源与历史（均已用 `git log` 核对）

| 目录 | 来源 | 导入提交 | 后续上游合并 |
| --- | --- | --- | --- |
| `openbot/` | git subtree，`git-subtree-split: afcc320667907bdda4b57ac876cc90885b1ffc7a` | `db82b6adc` | `596e0a6ab`（合并 `2f02a65b1bebebae8640ddf971f1719f33139f53`，即 OpenBot main） |
| `openswarm/` | git subtree，`git-subtree-split: eb0ea4c1aa31111166db5984e3f6d701e1103bf7` | `a55d20b61` | `d86cd4779`（`6d5c3c555c2b2275924dacf9dd35c5e3c770b58a`）、`2d0759cc8`（`a56daa0b06f5292d8a57687d1535a9b405665c0e`） |
| `integrations/` | Phanes 自有 | — | 符号链接指向 `openswarm/bin/` |

上游仓库标识有两处不一致，同步前必须确认：`openbot/package.json:12-15` 写的是 `git+https://github.com/nightly-labs/openbot.git`，而仓库根 `README.md:16` 与 `README.md:63` 仍写 `nibiru-labs/openbot`。

**最近一次同步对迁移链的影响：零。** 用对象库比对 `afcc3206:src/backend/openbot-database-schema.ts` 与 `2f02a65b:src/backend/openbot-database-schema.ts`、`afcc3206:apps/auth-api/migrations` 与 `2f02a65b:apps/auth-api/migrations`、`…/openbot-database-schema-history.json`、`…/openbot-database-upstream-schema-history.json`、`…/released-schemas` 五个路径，`git diff --stat` 全部为空。

### 3.2 上游自己的合并先例（最重要的参考）

OpenBot 上游历史里的 `fd5719fc7`（"Merge nightly-labs/openbot main into the fork (#42)"，2026-10-28）带进了 429 个上游提交，其提交说明原文：

> "Migration 45 keeps the shipped fork routine_backoff; upstream browser profiles, workflows and agent threads are 46-48, and 49 adds routine_backoff to imported upstream histories."

这次合并就是本仓库 `openbot/` 现状的来源，它同时带进了新的 D1 迁移 `0038_licenses.sql`、`0039_remote_host_key_handovers.sql`、`0040_server_member_limits.sql`（`git show --stat fd5719fc7`）。也就是说：**"上游新增迁移版本 → fork 侧重新编号"这个场景已经真实发生过一次**，本审计的风险清单以它为基准。

### 3.3 门禁位置

| 门禁 | 位置 | 是否覆盖迁移测试 |
| --- | --- | --- |
| 仓库根 pre-commit | `.githooks/pre-commit:5` → 委托 `openbot/.githooks/pre-commit` | 间接，见下 |
| OpenBot pre-commit | `openbot/.githooks/pre-commit:95-99` | 仅当暂存路径命中 `schema_pattern` 时跑 `bun run test:desktop -- src/backend/openbot-database-schema-parity.test.ts` |
| OpenBot 自身 CI | `openbot/.github/workflows/ci.yml:224-225`（`bun run test:desktop`）、`:1102-1107`（D1 迁移） | 是，但**该文件不在仓库根 `.github/workflows/`，GitHub 不会执行** |
| Phanes CI | `.github/workflows/phanes.yml:34-48` | **否**：openbot job 只跑 `bun run typecheck:node`、`bun run i18n:check`、`bun run build` |

### 3.4 本地改动与上游的交集

- Phanes 本地改过 `openbot/` 的 7 个提交：`8b31861da`、`351ff9709`、`11a19474f`、`3332ada7a`、`a123f92dc`、`90a1c536b`、`72f517ebf`。逐个 `git show --stat` 核对后确认：**全部集中在 `openbot/scripts/`、`openbot/examples/`、`openbot/changelog.d/` 与 `.githooks/`，没有任何一个触及 schema 或迁移文件**。因此当前 openbot 侧的合并冲突面与迁移链无关。
- `openswarm/` 侧自上次上游合并（`2d0759cc8`）以来有 54 个本地提交；`openswarm/bin/hub-openswarm-bridge` 被本地修改 7 次（`git log -- openswarm/bin/hub-openswarm-bridge`），该文件正是 `dispatch.sqlite` 的 schema 所在，上游改同一文件必然冲突。

## 4. 同步上游的风险

### R1（高）上游新增迁移版本 → fork 版本号与偏移假设被打破

- fork 侧 45–49 已被占用，其中 **fork 45 已经发布**（`fd5719fc7` 提交说明："Migration 45 keeps the shipped fork routine_backoff"）；fork 46–49 随该提交进入上游 main，但尚未随任何 OpenBot 发布落地（最新发布 0.52.0 早于它，`openbot/CHANGELOG.md:9`）。桌面端迁移不可逆、无备份（`openbot/src/backend/AGENTS.md:7` "Database migrations" 一节、`openbot/docs/architecture/database-migrations.md:1-8`），已发布的版本号不能重写。
- 上游谱系 → fork 谱系的映射在 parity 测试里是**硬编码的 +1 偏移**：`openbot/src/backend/openbot-database-schema-parity.test.ts:64-70`
  ```ts
  for (const [version, recorded] of Object.entries(upstreamSchemaHistory)) {
    expect(actual[String(Number(version) + 1)]).toEqual(recorded);
  }
  expect(actual["49"]).toEqual({});
  ```
- 若上游新增上游版本 48，fork 侧只能追加为 **50**（49 已被 routine_backoff 占用），于是 `+1` 偏移与 `actual["49"] == {}` 两条断言同时失效，必须改成显式映射表。若有人图省事把 49 复用或改写已发布迁移，就等于在用户机器上执行不可逆的错误迁移。

### R2（高）保留 schema 状态机是硬编码的版本阈值

`openbot/src/backend/openbot-database-schema.ts:1057-1078` 用 `version >= 45/46/47/48` 组合出 `fork`、`upstream`、`imported48`、`earlier` 四种允许形状，任何不匹配都会抛
`OpenBot database has a contradictory reserved schema and migration history.`（`:1077`）而**拒绝启动**。fork 追加版本 50 时，只要新版本引入或跳过任何一张保留表，就必须同步扩展这个状态机与 `openbot/docs/architecture/database-migrations.md:115-133` 的状态表，否则"从上游发布升级上来的数据库"会在启动时被判为矛盾状态。

### R3（高）Phanes 仓库的 CI 不跑 OpenBot 的迁移测试

`.github/workflows/phanes.yml:34-48` 只有类型检查、i18n 检查与构建；OpenBot 自己的 vitest（`openbot/.github/workflows/ci.yml:224-225`）因为不在根 `.github/workflows/` 而不会执行。结果是：**合并上游之后，Phanes 侧唯一的自动化门禁是本地 pre-commit**，而 pre-commit 的触发面又是残缺的（见 R4）。第 5 节的检查计划因此必须显式在 CI 或人工执行，不能依赖现有流水线。

### R4（中）pre-commit 的 schema 触发面过窄

`openbot/.githooks/pre-commit:95` 的 `schema_pattern` 只覆盖 4 个 ts 文件加 1 个 json：

```
^src/backend/(openbot-database-schema|openbot-database-schema-parity\.test|channel-schema|mcp-schema)\.ts$|^src/backend/openbot-database-schema-history\.json$
```

**未覆盖**：13 个 schema 分片里的 11 个（`workflows/workflow-schema.ts`、`agent-threads/agent-thread-schema.ts`、`browser-profiles/browser-profile-schema.ts`、`routine-backoff/routine-backoff-schema.ts`、`messaging/messaging-schema.ts`、`database/provider-history-schema.ts`、`agent-tasks/agent-task-schema.ts`、`agent-waits/agent-wait-schema.ts`、`routine-flows/routine-flow-schema.ts`、`routine-results/routine-result-schema.ts`、`agent-memory-selection.ts`），以及 `openbot-database-upstream-schema-history.json`。改这些文件不会触发 parity 测试，而它们都是 `LATEST_SCHEMA_SQL` 的组成部分。

（路径前缀本身没问题：根 hook 委托后，暂存路径按 `--relative=openbot` 计算，在 Phanes 布局下正好是 `src/backend/...`，见 `openbot/.githooks/pre-commit:11-19` 与提交 `a123f92dc`。）

### R5（中）D1 迁移编号不唯一，且"先迁移后部署"的竞态

- `0034` 重号已核验。D1 按**文件名**排序应用，已应用的迁移名记在 D1 自己的迁移表里，因此新文件若文件名排序落在已应用的最高编号之前（例如上游新增 `0035_x.sql`，而生产已应用 `0040_...`），它仍会被执行，但执行顺序排在 `0040` 之后；若该迁移依赖更早建立的表就会失败。现有 41 个文件多数是“只建新表”，掩盖了这个风险。
- `openbot/apps/auth-api/AGENTS.md:14-24` 要求每条迁移对**旧 Worker** 向后兼容（不能加无默认值的 `NOT NULL` 列、不能重命名/删除旧 Worker 仍在读的列），否则需要"扩展 → 部署 → 收缩"两段式发布。上游同步若带来破坏性 D1 迁移，需要在合并窗口里完成两段式，而不是直接合并。

### R6（中）上游仓库标识不一致

见 3.1。同步前必须确认目标是 `nightly-labs/openbot` 还是 `nibiru-labs/openbot`，否则可能同步到错误的上游或已废弃的分支。

### R7（中）openswarm 侧的合并冲突面与内嵌 schema

`openswarm/bin/hub-openswarm-bridge` 同时承载 `dispatch.sqlite` 的 `handoffs` 表 schema（`:139`）与幂等 `ALTER TABLE`（`:142`），且被本地改了 7 次。上游若同时修改该文件，合并冲突会同时影响 schema 与 Phanes 的直连 fleet 逻辑。OpenSwarm 没有版本化迁移，schema 变更无法回滚，只能靠代码回滚。

### R8（低）不可逆的数据操作

- 迁移 48（fork 编号）会复制 `projection_agent_tasks` 的行到 `projection_agent_threads` 并 `DROP TABLE projection_agent_tasks`（`openbot/src/backend/agent-threads/agent-thread-schema.ts:42-54`），是不可逆的数据搬迁。
- 迁移 13 会重写生成的 agent id（`openbot-database-schema.ts:1591` `rewriteGeneratedAgentIds`）；迁移 39 会批量删除事件行并需要 VACUUM（`:826-830`）。
- 这些迁移一旦被上游改写，Phanes 侧无法靠"再迁移一次"补救。

### R9（低）发布期快照夹具可能过期

`openbot/src/backend/released-schemas/*.sql` 与 `pre-registry-schema-migration.test.ts` 覆盖 v8 之前的历史发布。上游若改动早期 schema 行为，这些只读夹具会与新代码不一致。

## 5. 上游同步的兼容性检查计划

以下步骤按顺序执行；每步都有"通过判据"。Step 1–3 在合并前完成（只读），Step 4 起在合并分支上完成。

### Step 0 — 固定基线

```bash
git rev-parse HEAD
git log -1 --format=%H -- openbot/          # 当前 vendored 的 OpenBot 提交
git log -1 --format=%H -- openswarm/
```

记录当前 fork 迁移链末版本：`grep -n "version: 4" openbot/src/backend/openbot-database-schema.ts | tail -1`（当前为 49）。

### Step 1 — 预检上游改动范围（合并前，不改代码）

```bash
git fetch <openbot-upstream> main
git diff --stat <old-upstream-sha>..<new-upstream-sha> -- \
  'openbot/src/backend/openbot-database-schema.ts' \
  'openbot/src/backend/*schema*' \
  'openbot/src/backend/released-schemas/' \
  'openbot/apps/auth-api/migrations/'
```

**通过判据**：输出为空 → 迁移链零风险，只需跑 Step 6/7 的门禁即可合并。

### Step 2 — 判断上游是否新增迁移版本

```bash
git show <new-upstream-sha>:src/backend/openbot-database-schema.ts \
  | grep -nE "version: [0-9]+|BASELINE_SCHEMA_VERSION"
```

- 若上游末版本仍是 49 → 无编号冲突（R1/R2 不触发）。
- 若上游新增了版本 N（上游谱系）→ fork 侧新版本号 = **50 及以上**（fork 45–49 已占用，其中 45 已发布）。此时必须：
  1. 把 `openbot-database-upstream-schema-history.json` 增加 N 的条目；
  2. 把 `openbot-database-schema-parity.test.ts:64-70` 的 `+1` 硬编码改成显式映射（例如 `{"45":46,"46":47,"47":48,"48":50}`）；
  3. 更新 `openbot/docs/architecture/database-migrations.md:33-46` 的谱系表。

### Step 3 — 判断 D1 迁移的新增/修改/排序

```bash
git diff --name-status <old-upstream-sha>..<new-upstream-sha> -- apps/auth-api/migrations/
```

对新文件检查排序位置：若新文件名前缀 ≤ 生产已应用的最高编号（当前 `0040`），必须按"两段式发布"处理（`openbot/apps/auth-api/AGENTS.md:22-24`），并逐条确认对旧 Worker 向后兼容。

### Step 4 — 合并上游

沿用仓库既有做法（`596e0a6ab` / `d86cd4779` 的形态）做显式合并提交：

```bash
git merge --no-ff <new-upstream-sha>
```

冲突处理原则：schema 定义与迁移正文取上游；Phanes 本地的脚本与 examples 改动保留（见 3.4，本地改动不与 schema 重叠）。

### Step 5 — 迁移链一致性检查（合并后，改代码）

逐项核对，缺一不可：

1. `MIGRATIONS`（`openbot-database-schema.ts:660-891`）版本连续、无重复、无空洞 —— 运行时由 `validateMigrationRegistry`（`:980-993`）强制，但要在合并后立刻确认，而不是等到启动失败。
2. 已发布迁移（≤ 49）**一个字都不能改**；新变更只能追加为 50+。
3. `openbot-database-schema-history.json` 补齐每个新版本的条目；已发布条目保持不变（parity 测试会逐条比对 `:72-86`）。
4. `openbot-database-upstream-schema-history.json` 补齐新的上游版本映射。
5. `validateReservedSchema` 的状态机（`:1057-1078`）与文档状态表（`openbot/docs/architecture/database-migrations.md:115-133`）同步扩展。
6. 若上游新增了 DDL：`LATEST_SCHEMA_SQL`（`:561-608`）必须同步镜像，否则新装库缺列、升级库有列 —— 这正是 parity 测试存在的理由（`openbot-database-schema-parity.test.ts:1-9` 的注释）。

### Step 6 — 跑 OpenBot 的迁移测试

```bash
cd openbot && bun install --frozen-lockfile
bun run test:desktop -- src/backend/openbot-database-schema-parity.test.ts \
  src/backend/pre-registry-schema-migration.test.ts \
  src/backend/agent-thread-migration.test.ts \
  src/backend/workflow-migration.test.ts \
  src/backend/browser-profile-migration.test.ts \
  src/backend/routine-backoff-migration.test.ts \
  src/backend/openbot-database.test.ts
bun run test:api          # D1 迁移测试，openbot/apps/auth-api/package.json:22
```

**通过判据**：全部通过。任一失败即停止合并，不得"改测试迁就代码"。

### Step 7 — 双构建路径对拍（不依赖 vitest 的独立复核）

用 `node:sqlite` 起两个内存库：一个走 `createLatestDatabase`（新装路径），一个走完整迁移链（升级路径），比较 `sqlite_master` 的规范化 DDL 与 `PRAGMA table_info`。本次已用该方式确认 D1 链 41/41 可按文件名顺序应用、`foreign_key_check` 为空、`integrity_check` 为 `ok`；桌面链因需要 `@openbot/*` 工作区依赖，本次未能运行。

### Step 8 — 数据保全回归

用 `openbot/src/backend/released-schemas/v0.2.1.sql` 起库并跑迁移链，重点核对：

- `projection_agent_tasks` 的行数与 `projection_agent_threads.legacy_task_id` 映射（迁移 48 的搬迁，`agent-threads/agent-thread-schema.ts:42-54`）；
- `projection_thread_messages` 的 `group_start/group_first/group_id/turn_rank`（迁移 40 的回填）；
- `schema_migrations` 中 v8 之前的历史标记（3/4/6）是否原样保留（`pre-registry-schema-migration.test.ts:264-269`）。

### Step 9 — 发布与回滚

- 先让 OpenBot 侧发布包含新迁移的版本，再在 Phanes 侧合并上游；不要让 Phanes 承载一个 OpenBot 尚未发布的迁移链。当前状态：fork 45 已随发布落地（`fd5719fc7` 提交说明称 “shipped fork routine_backoff”）；fork 46–49 随 `fd5719fc7`（2026-10-28）进入上游 main，但晚于最新发布 0.52.0（2026-10-27，`openbot/CHANGELOG.md:9`），尚未进入任何发布，fork 侧编号因此还有调整余地；而上游发布线的 45/46/47 语义已冻结。
- 记录回滚点：合并前的 Phanes 提交 SHA，以及 OpenBot 侧的对应发布版本。桌面端迁移不可逆，回滚只能靠“停在旧版本”，不能靠“再迁移回去”。

## 6. 建议的最小改进（本次未实施）

1. 在 `.github/workflows/phanes.yml` 的 openbot job 中增加 `bun run test:desktop -- src/backend/openbot-database-schema-parity.test.ts`（与 OpenBot 自身 CI 的做法对齐），消除 R3。
2. 扩展 `openbot/.githooks/pre-commit:95` 的 `schema_pattern`，覆盖全部 13 个 schema 分片与 `openbot-database-upstream-schema-history.json`，消除 R4。
3. 把 `openbot-database-schema-parity.test.ts:64-70` 的 `+1` 硬编码改为显式的"上游版本 → fork 版本"映射表，消除 R1 的隐性假设。

## 7. 本次已核验 / 未能核验

**已核验**

- `MIGRATIONS` 注册表版本 9..49 连续、无重复；加基线 8 共 42 条（对 `openbot-database-schema.ts:660-891` 做文本解析）。
- D1 迁移 41 个文件按文件名顺序可全部应用；`foreign_key_check` 0 行；`integrity_check` 为 `ok`；`0034` 重号确实存在（Node `node:sqlite` 实跑）。
- 上游 `afcc3206` 与 `2f02a65b` 之间，`openbot-database-schema.ts`、`openbot-database-schema-history.json`、`openbot-database-upstream-schema-history.json`、`released-schemas/`、`apps/auth-api/migrations/` 五个路径 `git diff --stat` 均为空。
- Phanes 本地 7 个 openbot 提交均未触及 schema/迁移文件（逐提交 `--stat` 核对）。
- `openbot-database-schema-history.json` 覆盖 76 表 / 65 索引 / 3 触发器；上游历史文件仅含 45/46/47。

**未能核验**

- 上游已发布版本的真实迁移标记集合（无网络、无 tag，只能引用 `openbot/docs/architecture/database-migrations.md:29-46`）。
- OpenBot 的 vitest 迁移测试与 `bun run build`（本工作树无 `node_modules`，机器无 `bun`）。
- `wrangler d1 migrations apply` 在真实 D1 上的行为（本沙箱无 Cloudflare 凭据，且不应触碰凭据）。
