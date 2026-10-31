# OpenBot 架构漂移可执行改进清单（Refs #163, Refs #18, Refs #108）

- 审计来源：`docs/reviews/openbot-architecture-drift.md`（#108 子任务调研报告）
- 核验基准：当前 main 分支最新提交 `7809f1bc6`
- 核验性质：全量核对原报告全部 9 条已过时断言与 13 条建议修改，核查相关文件在当前工作树中的存在性与最新行号。
- 处理口径：
  - **已失效**：漂移已在近期 PR 中修复（#134 / PR #160、#135 / PR #150、#138 / PR #145），当前代码或文档已一致，无需重复建 issue。
  - **有效**：漂移仍存在于当前仓库，转换为一条具备标题、影响文件、具体改动、验收命令和建议 backend 的可直接建 issue 的任务卡片。

---

## 0. 改进清单概览

| # | 标题 | 状态 | 涉及文档 / 实现文件 | 建议 backend |
| :--- | :--- | :--- | :--- | :--- |
| 1 | docs(openbot): 移除 change-rules 与 AGENTS 中的 Effect 写死版本号 | **已失效** | `openbot/docs/architecture/change-rules.md:5`<br>`openbot/AGENTS.md:52` | pi（已在 PR #160 修复） |
| 2 | docs(openbot): 修正 events.md 中 workflow_trigger_webhooks 迁移号 | **已失效** | `openbot/docs/architecture/events.md:17` | pi（已在 PR #160 修复） |
| 3 | docs(openbot): 纠正 team-api.md 与 browser.md 中 WebRTC 通道数量 | **已失效** | `openbot/docs/architecture/team-api.md:5`<br>`openbot/docs/architecture/browser.md:260-261` | pi（已在 PR #160 修复） |
| 4 | docs(openbot): 更新 README 中 Team API WebRTC 协议版本为 v8 | **已失效** | `openbot/README.md:570` | pi（已在 PR #150 修复） |
| 5 | docs(openbot): 对齐 README 中 packages/i18n 语种数量为 13 种 | **已失效** | `openbot/README.md:513` | pi（已在 PR #150 修复） |
| 6 | docs(openbot): 在 README 架构图中补充 site-router Worker 分支 | **已失效** | `openbot/README.md:498-504` | pi（已在 PR #150 修复） |
| 7 | docs(openbot): 修正 browser.md 中 browser_profiles 表的 fork 迁移号 | **已失效** | `openbot/docs/architecture/browser.md:186-187` | pi（已在 PR #160 修复） |
| 8 | docs(openbot): 纠正 integrations.md 中浏览器端授权交接模块文件名 | **有效** | `openbot/docs/architecture/integrations.md:27` | **pi** |
| 9 | docs(openbot): 澄清 change-rules.md 中 noConsole 豁免与 CI recipe 检查口径 | **有效** | `openbot/docs/architecture/change-rules.md:74-77` | **codex** |
| 10 | docs(openbot): 在 change-rules.md 规则 7 补充 renderer 子域上下文命名约定 | **有效** | `openbot/docs/architecture/change-rules.md:52-54` | **pi** |
| 11 | docs(openbot): 在 structure.md Workspace map 补充顶级目录概括说明 | **有效** | `openbot/docs/architecture/structure.md:27-31` | **pi** |
| 12 | fix(auth-api): D1 迁移编号重号守护与 0034 白名单注解 | **已失效** | `openbot/apps/auth-api/migrations/0034_*.sql`<br>`openbot/apps/auth-api/scripts/check_migration_numbers.py` | codex（已在 PR #145 落地） |
| 13 | docs(openbot): 抽查 integrations.md 第 3 节与第 6 节的代码路径与版本断言 | **有效** | `openbot/docs/architecture/integrations.md:38-160` | **pi** |

---

## 1. 有效待建 Issue 清单（当前仍存在漂移）

### 1.1 条目 8：纠正 integrations.md 中浏览器端授权交接模块文件名

- **标题**：`docs(openbot): 纠正 integrations.md 中浏览器端授权交接模块文件名`
- **状态**：有效（未修复）
- **对应报告位置**：`openbot-architecture-drift.md` §3.8 / §6 第 8 条
- **涉及文件（main 验证）**：
  - 文档引用处：`openbot/docs/architecture/integrations.md:27`
  - 渲染器交接实现模块：`openbot/src/renderer/src/features/web-client/web-messaging-connector.ts:4,17`
  - 跨进程类型定义模块：`openbot/packages/contracts/src/web-messaging-install.ts:1`
- **现状核验**：
  `openbot/docs/architecture/integrations.md:27` 当前正文为：
  > `The browser client gets the grant from a return page of its own origin (\`web-messaging-install.ts\`).`
  但 `web-messaging-install.ts` 是 `@openbot/contracts` 包内的跨端类型与常量定义模块，浏览器端实际承接来自页面授权凭据的代码位于 `openbot/src/renderer/src/features/web-client/web-messaging-connector.ts`（该文件引入了 `@openbot/contracts/web-messaging-install`）。
- **具体改动**：
  修改 `openbot/docs/architecture/integrations.md:27` 中的括号说明，指出浏览器端实现位于 `web-messaging-connector.ts`，并清晰标明 `contracts` 类型模块的位置：
  ```markdown
  The browser client gets the grant from a return page of its own origin ([web-messaging-connector.ts](../../src/renderer/src/features/web-client/web-messaging-connector.ts), with contracts types in [web-messaging-install.ts](../../packages/contracts/src/web-messaging-install.ts)).
  ```
- **一条可执行验收命令**：
  ```bash
  grep -q 'web-messaging-connector.ts' openbot/docs/architecture/integrations.md && ! grep -q 'origin (`web-messaging-install.ts`)' openbot/docs/architecture/integrations.md
  ```
- **建议 backend**：`pi`（轻量级文档超链接与路径修正，快速可靠）

---

### 1.2 条目 9：澄清 change-rules.md 中 noConsole 豁免与 CI recipe 检查口径

- **标题**：`docs(openbot): 澄清 change-rules.md 中 noConsole 豁免与 CI recipe 检查口径`
- **状态**：有效（未修复）
- **对应报告位置**：`openbot-architecture-drift.md` §6 第 9 条
- **涉及文件（main 验证）**：
  - 架构规则文档：`openbot/docs/architecture/change-rules.md:74-77`
  - Biome 规则配置：`openbot/biome.json:125-129`
  - CI 检查工作流：`openbot/.github/workflows/remote-desktop-runtime.yml:85`
- **现状核验**：
  `openbot/docs/architecture/change-rules.md:74-77` 写道：
  > `The remote-desktop build recipe scripts that the noConsole override in biome.json excludes are exempt. The "Require a recipe version bump" step of .github/workflows/remote-desktop-runtime.yml lists them...`
  这句话暗示 Biome 豁免清单与 CI 步骤监控的脚本清单完全一致。但实测：
  - `biome.json:125-129` 仅豁免 5 个打印日志的脚本：`build-remote-desktop-runtime.ts`、`create-remote-desktop-release-manifest.ts`、`native-runtime-lock.ts`、`package-remote-desktop-runtime.ts`、`remote-desktop-runtime-release.ts`。
  - `.github/workflows/remote-desktop-runtime.yml:85` 在这 5 个脚本之外，还额外包含 `scripts/mac-runtime-dylibs.ts`（因其无 `console.*` 调用，不需要在 Biome 豁免）。
- **具体改动**：
  在 `openbot/docs/architecture/change-rules.md:74-77` 中微调语句，说明 CI 步骤监控这些构建配方脚本以及一个额外的打包辅助脚本 `scripts/mac-runtime-dylibs.ts`，消除两个清单完全相等的误解。
- **一条可执行验收命令**：
  ```bash
  grep -q 'mac-runtime-dylibs.ts' openbot/docs/architecture/change-rules.md
  ```
- **建议 backend**：`codex`（需理解 CI 流程与 linter 规范之间的语义纽带）

---

### 1.3 条目 10：在 change-rules.md 规则 7 补充 renderer 子域上下文命名约定

- **标题**：`docs(openbot): 在 change-rules.md 规则 7 补充 renderer 子域上下文命名约定`
- **状态**：有效（未修复）
- **对应报告位置**：`openbot-architecture-drift.md` §6 第 10 条
- **涉及文件（main 验证）**：
  - 架构规则文档：`openbot/docs/architecture/change-rules.md:52-54`
  - 实际上下文文件：
    - `openbot/src/renderer/src/features/conversation/conversation-controller-context.tsx:1`
    - `openbot/src/renderer/src/features/custom-providers/provider-detection-context.tsx:1`
    - `openbot/src/renderer/src/features/updates/whats-new-context.tsx:1`
- **现状核验**：
  `openbot/docs/architecture/change-rules.md:52-54` 规则 7 要求：
  > `Put renderer state in a domain context module inside that domain's feature directory, src/renderer/src/features/<domain>/<domain>-context.tsx...`
  但渲染器代码中存在 3 处文件名包含子域修饰的上下文：
  1. `conversation/conversation-controller-context.tsx`（非 `conversation-context.tsx`）
  2. `custom-providers/provider-detection-context.tsx`（非 `custom-providers-context.tsx`）
  3. `updates/whats-new-context.tsx`（非 `updates-context.tsx`）
- **具体改动**：
  在规则 7 中补充一句命名例外规范：“Sub-domain or dedicated feature contexts may use `<subdomain>-context.tsx` (for example, `conversation-controller-context.tsx` or `whats-new-context.tsx`).”
- **一条可执行验收命令**：
  ```bash
  grep -E -q 'sub-?domain|-controller-context|whats-new' openbot/docs/architecture/change-rules.md
  ```
- **建议 backend**：`pi`（规则文档条目增补）

---

### 1.4 条目 11：在 structure.md Workspace map 补充顶级目录概括说明

- **标题**：`docs(openbot): 在 structure.md Workspace map 补充顶级目录概括说明`
- **状态**：有效（未修复）
- **对应报告位置**：`openbot-architecture-drift.md` §6 第 11 条
- **涉及文件（main 验证）**：
  - 架构结构文档：`openbot/docs/architecture/structure.md:3-30`
  - 仓库顶级目录：
    - `openbot/deploy/`: `self-host` 部署脚本与配置
    - `openbot/examples/`: `org-health-local` 组织诊断工作流用例
    - `openbot/marketplace/`: `plugin-catalog` 与 `production-catalog` 目录
    - `openbot/resources/`: `managed-skills`、`agent-runtime`、`cli` 等内置资源
    - `openbot/vendor/`: `remote-desktop` 第三方内嵌运行时
- **现状核验**：
  `openbot/docs/architecture/structure.md:3-30` 的 Workspace map 列举了 `apps/`、`packages/`、`remote/`、`src/`、`scripts/`、`tools/`、`.agents/skills/`，但未覆盖上述 5 个实际存在且维护中的顶级目录，容易让维护者误认为是临时或遗留文件。
- **具体改动**：
  在 `openbot/docs/architecture/structure.md` 的 Workspace map 代码块末尾追加 `deploy/`、`examples/`、`marketplace/`、`resources/`、`vendor/` 的简要职责说明。
- **一条可执行验收命令**：
  ```bash
  grep -q 'deploy/' openbot/docs/architecture/structure.md
  ```
- **建议 backend**：`pi`（纯文档结构补全）

---

### 1.5 条目 13：抽查 integrations.md 第 3 节与第 6 节的代码路径与版本断言

- **标题**：`docs(openbot): 抽查 integrations.md 第 3 节与第 6 节的代码路径与版本断言`
- **状态**：有效（调研任务）
- **对应报告位置**：`openbot-architecture-drift.md` §6 第 13 条
- **涉及文件（main 验证）**：
  - 待抽查文档：`openbot/docs/architecture/integrations.md:38-160`（第 3 节 Provider 驱动与第 6 节 Team API 扩展）
  - 关联实现：
    - `openbot/src/backend/provider-drivers.ts`
    - `openbot/packages/contracts/src/agent-providers.ts`
    - `openbot/packages/contracts/src/team-protocol/`
- **现状核验**：
  `integrations.md` 开头声明“Each section has a working example with real file paths”。第 2 节已发现文件名引用错误（条目 8）。第 3 节与第 6 节含有多达数十处文件路径引用与协议常量声明，尚未逐一与代码基线对齐。
- **具体改动**：
  编写自动化脚本扫描第 3 节与第 6 节中所有代码路径与常量符号，验证它们在最新 main 上的可解析性与准确性，若存在漂移则形成修改 PR。
- **一条可执行验收命令**：
  ```bash
  python3 -c "import re, os; content = open('openbot/docs/architecture/integrations.md').read(); links = [l for l in re.findall(r'\[([^\]]+)\]\(([^)]+\.ts)\)', content) if not os.path.exists(os.path.normpath(os.path.join('openbot/docs/architecture', l[1])))]; assert len(links) == 0, f'Broken markdown ts links: {links}'"
  ```
- **建议 backend**：`pi`（只读调研、链接与断言自动化核对）

---

## 2. 已失效条目清单（近期 PR 已修复或已解决）

以下条目在原报告提交后，已在主线分支由对应 PR 处理完毕，在当前 main（`7809f1bc6`）上已恢复一致，无需再建 issue。

### 2.1 条目 1：Effect 版本写死 4.0.0
- **原报告位置**：`openbot-architecture-drift.md` §3.1 / 表格 #1, #2 / §6 第 1 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `541edc9ae`（PR #160 / #134）中已统一修改为弹性描述：
  - `openbot/docs/architecture/change-rules.md:5`：已更新为 `catalog-pinned \`effect\` 4.x package`
  - `openbot/AGENTS.md:52`：已更新为 `catalog-pinned \`effect\` 4.x package`
  不再写死 4.0.0，与 `openbot/package.json:73` 中的 `catalog:` 锁定对齐。
- **现实验收命令**：
  ```bash
  grep -q 'catalog-pinned `effect` 4.x package' openbot/docs/architecture/change-rules.md && grep -q 'catalog-pinned `effect` 4.x package' openbot/AGENTS.md
  ```

---

### 2.2 条目 2：`workflow_trigger_webhooks` 迁移号写 v46
- **原报告位置**：`openbot-architecture-drift.md` §3.2 / 表格 #3 / §6 第 2 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `541edc9ae`（PR #160 / #134）中已修复：
  - `openbot/docs/architecture/events.md:17`：表格中已改为 `workflow_trigger_webhooks (schema v47)`
  - `openbot/docs/architecture/events.md:23-25`：已补充 `schema v47 the workflow tables. See Database migrations... for the fork numbering.`
  与实现代码 `openbot/src/backend/openbot-database-schema.ts:869-873`（版本 47 执行 `WORKFLOW_SCHEMA_SQL`）完全一致。
- **现实验收命令**：
  ```bash
  grep -q '`workflow_trigger_webhooks` (schema v47)' openbot/docs/architecture/events.md
  ```

---

### 2.3 条目 3：WebRTC DataChannel 数量写三条
- **原报告位置**：`openbot-architecture-drift.md` §3.3 / 表格 #4 / §6 第 3 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `541edc9ae`（PR #160 / #134）中已修复：
  - `openbot/docs/architecture/team-api.md:5`：已更新为 `four ordered WebRTC DataChannels: \`rpc\`, \`events\`, \`files\`, and \`desktop\``
  - `openbot/docs/architecture/browser.md:260-261`：已更新为 `reuses the Remote Desktop websocket tunnel, which rides the \`desktop\` DataChannel, rather than adding a WebRTC channel of its own`，消除了与 `browser.md` 的口径冲突。
- **现实验收命令**：
  ```bash
  grep -q 'four ordered WebRTC DataChannels' openbot/docs/architecture/team-api.md && grep -q 'rides the `desktop`' openbot/docs/architecture/browser.md
  ```

---

### 2.4 条目 4：README Team API 协议版本写 v3
- **原报告位置**：`openbot-architecture-drift.md` §3.4 / 表格 #5 / §6 第 4 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `c26c0f310`（PR #150 / #135）中已修复：
  - `openbot/README.md:570`：已从 `protocol v3` 修改为 `WebRTC protocol v8 carries RPC, events, and binary files to desktop and mobile clients.`
  与 `openbot/packages/contracts/src/team-protocol/v8.ts:11` 保持一致。
- **现实验收命令**：
  ```bash
  grep -q 'protocol v8 carries RPC, events, and binary files' openbot/README.md
  ```

---

### 2.5 条目 5：README i18n 语种清单只列 5 种
- **原报告位置**：`openbot-architecture-drift.md` §3.5 / 表格 #6 / §6 第 5 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `c26c0f310`（PR #150 / #135）中已修复：
  - `openbot/README.md:513`：已更新为 `interface text in 13 languages for desktop, web and mobile. See docs/i18n.md to add text or a language.`
  不再逐个列举 5 种旧语言，与 `packages/i18n/src/messages/` 下实际存在的 13 个语种目录对齐。
- **现实验收命令**：
  ```bash
  grep -q 'interface text in 13 languages' openbot/README.md
  ```

---

### 2.6 条目 6：README 架构图只画 1 个 Cloudflare Worker
- **原报告位置**：`openbot-architecture-drift.md` §3.6 / 表格 #7 / §6 第 6 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `c26c0f310`（PR #150 / #135）中已修复：
  - `openbot/README.md:498-504`：架构图树状结构中已补上第二条 Worker 分支：
    ```text
    Cloudflare Workers
    ├── TanStack Start + Solid 2 account API
    │   ├── D1 accounts, email challenges, hashed sessions, and team tunnels
    │   └── R2 account avatars
    └── site-router Worker
        └── published sites from private R2 storage
    ```
  与 `apps/site-router/wrangler.jsonc` 存在相符合，与 `ARCHITECTURE.md:3` 口径一致。
- **现实验收命令**：
  ```bash
  grep -q 'site-router Worker' openbot/README.md && grep -q 'published sites from private R2 storage' openbot/README.md
  ```

---

### 2.7 条目 7：`browser_profiles` 表迁移号写 45
- **原报告位置**：`openbot-architecture-drift.md` §3.7 / 表格 #8 / §6 第 7 条
- **状态**：**已失效**
- **失效理由**：
  在 commit `541edc9ae`（PR #160 / #134）中已修复：
  - `openbot/docs/architecture/browser.md:186-187`：已更新为 `Named profiles are rows of the \`browser_profiles\` table (fork migration 46; see [Database migrations](database-migrations.md#what-the-fork-appends)).`
  明确了 fork 后的迁移编号为 46，并附带了权威锚点链接。
- **现实验收命令**：
  ```bash
  grep -q 'fork migration 46' openbot/docs/architecture/browser.md
  ```

---

### 2.8 条目 12：D1 迁移编号重号守护（0034）
- **原报告位置**：`openbot-architecture-drift.md` §6 第 12 条
- **状态**：**已失效（已落地）**
- **失效理由**：
  在 commit `6c5aa133e`（PR #145 / #138，Refs #20）中已实现静态守护：
  - 编写了校验脚本 `openbot/apps/auth-api/scripts/check_migration_numbers.py`，明确白名单放行历史已应用的重号 `KNOWN_DUPLICATES = {"0034"}`，并在注释中写明“0034 is already duplicated in the applied migration history. Never rename an applied migration”。
  - 配套单元测试 `openbot/apps/auth-api/scripts/test_check_migration_numbers.py`。
  - CI 工作流 `.github/workflows/phanes.yml:33` 已挂载该检查，防止未来再次发生重复编号。
- **现实验收命令**：
  ```bash
  python3 openbot/apps/auth-api/scripts/check_migration_numbers.py openbot/apps/auth-api/migrations
  ```

---

## 3. 外部环境依赖断言说明（非文档漂移）

原报告第 5 节列出的 4 组断言属于依赖本仓库外部真实环境（真实 Stripe 账户与生产 Worker、真实 x.ai 与 ACP registry 实时服务、未安装的 `node_modules` 文件），本仓库内只读审计既不能证实也不能证伪：
- `openbot/docs/architecture/servers.md:574-618`（Stripe Secret Key 与 Price）
- `openbot/docs/architecture/servers.md:620-660`（商业许可金额基准）
- `openbot/docs/architecture/providers.md:127-131`（上游 registry 实时响应）
- `openbot/src/renderer/AGENTS.md:12`（外部包 CHEATSHEET.md）

上述断言在当前代码库中代码入口和常量定义均一致，未发现代码层漂移，不需单独立项。

---

## 4. 本地一键核验脚本

可直接在仓库根目录执行以下命令，验证所有有效条目的当前漂移状态与已失效条目的修复状态：

```bash
python3 - <<'EOF'
import subprocess, sys

checks = [
    # 已失效条目验证（应全部返回 0）
    ("条目1 Effect版本已修复", "grep -q 'catalog-pinned `effect` 4.x package' openbot/docs/architecture/change-rules.md && grep -q 'catalog-pinned `effect` 4.x package' openbot/AGENTS.md"),
    ("条目2 Webhook迁移号已修复", "grep -q '`workflow_trigger_webhooks` (schema v47)' openbot/docs/architecture/events.md"),
    ("条目3 DataChannel数量已修复", "grep -q 'four ordered WebRTC DataChannels' openbot/docs/architecture/team-api.md && grep -q 'rides the `desktop`' openbot/docs/architecture/browser.md"),
    ("条目4 README协议版本已修复", "grep -q 'protocol v8 carries RPC, events, and binary files' openbot/README.md"),
    ("条目5 README语种已修复", "grep -q 'interface text in 13 languages' openbot/README.md"),
    ("条目6 README Worker图已修复", "grep -q 'site-router Worker' openbot/README.md"),
    ("条目7 browser_profiles迁移号已修复", "grep -q 'fork migration 46' openbot/docs/architecture/browser.md"),
    ("条目12 D1重号守护已生效", "python3 openbot/apps/auth-api/scripts/check_migration_numbers.py openbot/apps/auth-api/migrations"),
    # 有效条目验证（当前文档中漂移确仍存在）
    ("条目8 漂移仍存在(引用旧模块名)", "grep -q '(`web-messaging-install.ts`)' openbot/docs/architecture/integrations.md"),
    ("条目9 漂移仍存在(规则未说明mac-runtime-dylibs)", "! grep -q 'mac-runtime-dylibs' openbot/docs/architecture/change-rules.md"),
    ("条目10 漂移仍存在(规则未提及子域命名)", "! grep -E -q 'sub-?domain|-controller-context|whats-new' openbot/docs/architecture/change-rules.md"),
    ("条目11 漂移仍存在(Workspace map未包含deploy等目录)", "! grep -q 'deploy/' openbot/docs/architecture/structure.md"),
]

all_passed = True
for name, cmd in checks:
    res = subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = "PASS" if res.returncode == 0 else "FAIL"
    if status == "FAIL":
        all_passed = False
    print(f"[{status}] {name}")

sys.exit(0 if all_passed else 1)
EOF
```
