# Issue #9：auth-api 本地运行可行性（workerd / miniflare）

> 状态：**已实测**。本文档在 2026-11-08 于 `volto/apps/auth-api` 上跑通了本地 workerd（Cloudflare Vite 插件 + miniflare）运行，并按验收标准逐项核对。
> 路径均相对于仓库根目录；`volto/` 为子目录。

## 1. 结论

- auth-api 是 Cloudflare Worker（`volto/apps/auth-api`），入口为 `src/worker-entry.ts`，由 `src/server/worker-handler.ts` 装配。
- **本地用 workerd/miniflare 运行是可行的**：D1、R2、ratelimit、Cron 绑定都由本地运行时提供，不需要 Cloudflare 账号。
- 唯一的功能性阻塞项是 **B1**：本地 Worker 收不到管理令牌 `SERVER_CLAIMS_ADMIN_TOKEN`，因此 `/v2/admin/server-claims` 对持令牌的请求也返回 401。**已修复**（见第 4 节）。
- B2（开发默认值打开 `AUTH_EXPOSE_DEVELOPMENT_CODE`）、B3（SMTP 变量必须全部留空）是**运行方式的要求**，不是代码缺陷：验收用的本地运行按第 5 节的方式启动即可满足。
- 验收项全部通过：见第 6 节的实测结果。**不需要**自写简化账号服务。

## 2. 入口与配置

| 项目 | 位置 |
|---|---|
| Worker 入口 | `volto/apps/auth-api/src/worker-entry.ts` |
| Worker 处理器 | `volto/apps/auth-api/src/server/worker-handler.ts` |
| Wrangler 配置 | `volto/apps/auth-api/wrangler.jsonc`：`main`；cron；routes；`secrets.required`；顶层 vars；D1 `DB`；R2；ratelimit；`env.test`，vars |
| Vite 配置 | `volto/apps/auth-api/vite.config.ts`：端口默认 3100 与 host 127.0.0.1；运行时变量白名单转发 |
| 绑定校验 | `volto/apps/auth-api/src/server/types.ts`（`isWorkerBindings` / `requireWorkerBindings`） |
| 本地变量白名单 | `volto/apps/auth-api/src/server/runtime-env.ts`（`LOCAL_RUNTIME_KEYS`） |
| 开发密钥脚本 | `volto/scripts/development-secrets.ts`（ticket 密钥对、默认值） |
| 开发启动脚本 | `volto/scripts/run-development.ts` |
| 部署脚本 | `volto/scripts/deploy-auth-api.ts`（管理令牌是可选 secret） |
| 文档 | `volto/docs/self-hosted-server.md`、`volto/docs/self-hosting.md`。两者均未描述本地运行方式。 |

依赖版本（来自 `bun.lock`）：wrangler 4.152.0，@cloudflare/vite-plugin 1.65.0，miniflare 5.20261019.2-beta，workerd 1.20261019.1；`compatibility_date` 为 2026-08-18。

## 3. 需要本地模拟的绑定与变量

| 类型 | 名称 | 本地处理 |
|---|---|---|
| D1 | `DB` | `bun run wrangler d1 migrations apply DB --local`，迁移目录 `volto/apps/auth-api/migrations/`（含 `0034_server_claims.sql`）。本地 SQLite 在 `volto/apps/auth-api/.wrangler/state/v3`，Vite 开发服务器与 `wrangler d1` 用同一份状态 |
| R2 | `AVATARS`、`SKILLS`、`SITES` | 本地运行时自动创建 |
| Ratelimit | 6 个，必需 5 个 | 本地运行时提供 |
| Cron | 每 5 分钟 | 本地不会按时钟触发，需要时用本地 Explorer API 手动调用 |
| 变量 | `AUTH_EXPOSE_DEVELOPMENT_CODE` | 必须保持 `false`（`wrangler.jsonc` 的默认值），不要在本地环境里打开 |
| 变量 | SMTP host/port/username/from/password | **五项必须全部留空**，否则 `readSmtpConfig` 抛出 “incomplete”，所有用到账号服务的路由返回 500 |
| 密钥 | `SERVER_CLAIMS_ADMIN_TOKEN` | 只能来自**进程环境**（见 B1 与第 4 节）。`.dev.vars` 里的同名值会被 Wrangler 丢弃 |
| 密钥 | `REMOTE_TICKET_PUBLIC_JWKS`、`REMOTE_TICKET_PRIVATE_JWK`、`REMOTE_TICKET_KEY_ID` | 进程环境或 `.dev.vars` 均可；JWKS 的 `kid` 必须与 `REMOTE_TICKET_KEY_ID` 一致 |

## 4. 阻塞项与处理结果

- **B0（已解除）**：依赖已在具备网络的机器上安装（`bun install --frozen-lockfile`），下列结论均来自实测。
- **B1（已修复，代码缺陷）**：`src/server/runtime-env.ts` 的 `LOCAL_RUNTIME_KEYS` 不包含 `SERVER_CLAIMS_ADMIN_TOKEN`，`vite.config.ts` 只转发该白名单，因此 `vite dev` 路径下 admin 接口拿不到令牌。
  Wrangler 的开发变量装载（`getVarsForDev`）只保留**名字在 `vars` 里或在 `secrets.required` 里**的变量，所以令牌既不会从进程环境进来，也不会从 `.dev.vars` 进来。
  修复：把 `SERVER_CLAIMS_ADMIN_TOKEN` 加入 `LOCAL_RUNTIME_KEYS`，由 `vite.config.ts` 的 `cloudflare({ config })` 钩子写入本地 Worker 的变量。
  - 不能改用 `secrets.required`：该列表里的名字在部署时必须已经存在于线上 Worker，否则 `wrangler deploy` 失败；而生产上该令牌是可选的（“没有令牌就没有人能签发 server claim”），不能改成必需。
  - 不能改用 `vars`：变量名与线上 secret 同名，`scripts/deploy-auth-api.ts` 已经写明两者不能同名，部署会冲突。
- **B2（运行方式要求）**：`scripts/development-secrets.ts` 的开发默认值把 `AUTH_EXPOSE_DEVELOPMENT_CODE` 设为 `"true"`。本地验证运行不设置该变量，`wrangler.jsonc` 的 `"false"` 生效。
- **B3（运行方式要求，已实测）**：只设置部分 SMTP 变量时 `readSmtpConfig` 直接抛出 “incomplete”，`/v1/auth/email/start` 与 `/v2/admin/server-claims` 都返回 500。五项全部留空时返回 503 `email_delivery_not_configured`。
- **B4（已实测）**：`.dev.vars` 会覆盖 `vars`（把 `EMAIL_SMTP_HOST` 写成非空值即可观察到 500）。但 `.dev.vars` 与进程环境**互斥**：只要 `.dev.vars` 存在，Wrangler 就不再从进程环境取 secret 值。令牌不受此限，因为它由 `runtime-env.ts` 直接从进程环境转发。
- **B5（部分实测）**：`vite dev` 路径已跑通。`wrangler dev` 直接跑 `src/worker-entry.ts` 仍然不可行，Worker 需要 Vite 构建产物；本次没有验证 `dist/<worker>/wrangler.json` 路径。
- **R1（风险）**：本地令牌与其他本地密钥一样不能提交。`.dev.vars` 已被 `.gitignore` 忽略。

## 5. 本地运行命令（已实测）

```bash
# 1. 依赖
cd volto && bun install --frozen-lockfile

# 2. 本地 D1 迁移（Wrangler 4.x 没有 --yes：输出不是终端时会跳过确认）
cd apps/auth-api && bun run wrangler d1 migrations apply DB --local

# 3. 本地 Worker：令牌与五项 SMTP 变量都来自进程环境
cd apps/auth-api
SERVER_CLAIMS_ADMIN_TOKEN=<本地随机值> \
EMAIL_SMTP_HOST= EMAIL_SMTP_PORT= EMAIL_SMTP_USERNAME= EMAIL_SMTP_PASSWORD= EMAIL_FROM= \
REMOTE_TICKET_KEY_ID=openbot-remote-1 \
REMOTE_TICKET_PRIVATE_JWK=<本地 JWK> REMOTE_TICKET_PUBLIC_JWKS=<本地 JWKS> \
bun run vite dev --host 127.0.0.1 --port 3100 --strictPort
```

回归脚本（自动完成上面全部步骤，并逐项核对验收标准）：

```bash
cd volto && bun apps/auth-api/scripts/issue-9-local-workerd-e2e.ts
```

报告写入 `volto/.volto-build/issue-9-local-workerd-e2e/report.json`。

## 6. 验收项与实测结果

| 验收项 | 实测 | 验证方式 |
|---|---|---|
| `/health/live` 返回 200 | 200 | 回归脚本 |
| `/.well-known/jwks.json` 返回 200 | 200 | 回归脚本 |
| 无 token 访问 `/v2/admin/server-claims` 返回 401 | 401 | 回归脚本 |
| 持 `SERVER_CLAIMS_ADMIN_TOKEN` 签发 claim | 200，返回 `claim` 与 `expiresAt` | 回归脚本 |
| `/v2/server-claims/redeem` 兑换 claim | 200，返回 `sessionToken` 与 `user.email` | 回归脚本 |
| 不设 `AUTH_EXPOSE_DEVELOPMENT_CODE` | `/v1/auth/email/start` 返回 503 `email_delivery_not_configured`：打开开发码时它会返回 200 与验证码 | 回归脚本 |
| SMTP 变量留空 | 同上；只留一半 SMTP 变量会得到 500 | 回归脚本 |
| 只监听内部地址 | 只监听 127.0.0.1；非回环地址上的同一端口没有应答 | 回归脚本 |
| 回退方案（自写简化账号服务） | 不需要：本地 workerd 已经跑通签发与兑换 | 第 6 节 |

## 7. 后续建议

1. `docs/self-hosted-server.md` 与 `docs/self-hosting.md` 补一段本地运行说明，避免与第 5 节重复维护。
2. 若要让 `.dev.vars` 也能提供管理令牌，需要 Wrangler 允许该名字进入本地绑定；在此之前保持“从进程环境传入”。
3. `vite dev` 之外仍可评估 `wrangler dev` 路径（B5），它需要构建产物。
4. 本地运行会继承 `vars` 里的 `REMOTE_AUTH_WEBHOOK_URL`（指向生产 Signal）。Cron 触发时可能向生产地址发起投递；本地验证时可以一并留空。
