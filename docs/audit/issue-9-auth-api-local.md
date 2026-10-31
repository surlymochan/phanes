# Issue #9：auth-api 本地运行可行性（workerd / miniflare）

> 范围：仅静态代码审阅。未安装依赖、未执行网络安装、未运行 wrangler / vite，因此下列结论均标注为“已读代码确认”或“未验证”。
> 路径均相对于仓库根目录；`openbot/` 为子目录。

## 1. 结论

- auth-api 是 Cloudflare Worker（`openbot/apps/auth-api`），入口为 `src/worker-entry.ts`，由 `src/server/worker-handler.ts` 装配。
- 需要本地模拟的绑定：D1 `DB`、R2 `AVATARS` / `SKILLS` / `SITES`、5 个必需 ratelimit（`LIVE_ACTIVITY` 可选）、Cron 触发器。Miniflare / wrangler 本地模式可以提供这些资源。
- 阻塞项集中在两处：`vite dev` 路径无法把 `SERVER_CLAIMS_ADMIN_TOKEN` 传入运行时（B1），以及开发默认值会开启 `AUTH_EXPOSE_DEVELOPMENT_CODE`（B2）。两者都需要在运行前用 `.dev.vars` 或代码修正规避。
- 结论：**在本地用 wrangler/miniflare 运行是可行的，但需要先解决 B1、B2，并在具备依赖的机器上实测。** 本次审阅未实测任何命令。

## 2. 入口与配置

| 项目 | 位置 |
|---|---|
| Worker 入口 | `openbot/apps/auth-api/src/worker-entry.ts:1-4` |
| Worker 处理器 | `openbot/apps/auth-api/src/server/worker-handler.ts:23-38` |
| Wrangler 配置 | `openbot/apps/auth-api/wrangler.jsonc`：`main` L6；cron L19-21；routes L22-36；`secrets.required` L37-46；顶层 vars L47-66；D1 `DB` L67-74；R2 L75-91；ratelimit L92-123；`env.test` L203，vars L227-242 |
| Vite 配置 | `openbot/apps/auth-api/vite.config.ts`：端口默认 3100 与 host 127.0.0.1（L72-79，L136-140）；运行时变量白名单转发 L50-58 |
| 绑定校验 | `openbot/apps/auth-api/src/server/types.ts:8-40`（绑定定义），`L102-149`（`isWorkerBindings` / `requireWorkerBindings`） |
| 本地变量白名单 | `openbot/apps/auth-api/src/server/runtime-env.ts:5-50`、`L5-68` |
| 开发密钥脚本 | `openbot/scripts/development-secrets.ts:49-56`（ticket 密钥对）、`L65-78`（默认值，`AUTH_EXPOSE_DEVELOPMENT_CODE` 在 L69 为 `"true"`） |
| 开发启动脚本 | `openbot/scripts/run-development.ts:27-33` |
| 部署脚本 | `openbot/scripts/deploy-auth-api.ts:42`（管理 token 为可选） |
| 文档 | `openbot/docs/self-hosted-server.md` L25-53（claim 流程）、L51-53（token 风险）；`openbot/docs/self-hosting.md` L52-70、L172。两者均未描述本地运行方式。 |

依赖版本（来自 `bun.lock`）：wrangler 4.152.0，@cloudflare/vite-plugin 1.65.0，miniflare 5.20261019.2-beta，workerd 1.20261019.1；`compatibility_date` 为 2026-08-18。

## 3. 需要本地模拟的绑定与变量

| 类型 | 名称 | 本地处理 |
|---|---|---|
| D1 | `DB`（L67-74） | `wrangler d1 migrations apply DB --local`，迁移目录 `openbot/apps/auth-api/migrations/`（含 `0034_server_claims.sql`） |
| R2 | `AVATARS`、`SKILLS`、`SITES`（L75-91） | wrangler 本地模式自动创建 |
| Ratelimit | 6 个，必需 5 个（L92-123） | miniflare 本地实现 |
| Cron | L19-21 | 本地不会自动触发，需要时手动调用 |
| 变量 | `AUTH_EXPOSE_DEVELOPMENT_CODE`（L48 为 `"false"`） | 保持 `false`，覆盖脚本默认值（B2） |
| 变量 | SMTP host/port/username/from（L49-52） | 必须与 password 一起留空（见 B3） |
| 密钥 | `SERVER_CLAIMS_ADMIN_TOKEN` | 仅放入 `.dev.vars`，不得写入 wrangler.jsonc 或命令行参数 |
| 密钥 | `REMOTE_TICKET_PUBLIC_JWKS`、`REMOTE_TICKET_PRIVATE_JWK`、`REMOTE_TICKET_KEY_ID` | `.dev.vars`；校验见 `remote-control-plane.ts:455-480`，L473 |

## 4. 阻塞项

- **B0（已确认）**：本次审阅未安装依赖，无法实际运行。这是本次审阅的范围限制，不是代码缺陷。
- **B1（已确认）**：`src/server/runtime-env.ts:5-50` 的 `LOCAL_RUNTIME_KEYS` 不包含 `SERVER_CLAIMS_ADMIN_TOKEN`，`vite.config.ts:50-58` 只转发该白名单，因此 `vite dev` 路径下 admin 接口无法通过变量获得 token。未验证 `@cloudflare/vite-plugin` 是否会读取 `.dev.vars`。
- **B2（已确认）**：`scripts/development-secrets.ts:69` 默认将 `AUTH_EXPOSE_DEVELOPMENT_CODE` 设为 `"true"`。若直接使用该脚本生成的变量，将违反验收项“不得设置 AUTH_EXPOSE_DEVELOPMENT_CODE”。
- **B3（已确认）**：`src/server/email-delivery.ts:80-100` 的 `readSmtpConfig` 在五个 SMTP 变量只设置了部分时直接抛出 “incomplete”。本地运行必须全部留空。claim 流程不发送邮件。
- **B4（未验证）**：`wrangler dev` 不校验 `secrets.required`（L37-46）。`.dev.vars` 覆盖 `vars` 的行为尚未实测。
- **B5（未验证）**：`vite build` 后 wrangler 配置的输出路径（推测为 `dist/<worker>/wrangler.json`）；`--local` 是否忽略 routes 与 custom_domain；直接对 `src/worker-entry.ts` 运行 `wrangler dev` 大概率不可行，应使用构建产物。
- **R1（风险）**：`self-hosted-server.md` L51-53 提示 token 风险，本地 `.dev.vars` 同样需要避免提交。

## 5. 运行命令（待实测）

以下命令未执行。需在已安装依赖的机器上验证，且不得把真实密钥写入仓库或命令行。

```bash
# 1. 依赖（仅在本机已有 lockfile 时执行，不做网络安装）
cd openbot && bun install --frozen-lockfile

# 2. 本地 .dev.vars（不得提交；SMTP 五项留空；AUTH_EXPOSE_DEVELOPMENT_CODE=false）
cd apps/auth-api
cat > .dev.vars <<'EOF'
AUTH_EXPOSE_DEVELOPMENT_CODE=false
SERVER_CLAIMS_ADMIN_TOKEN=<本地随机值>
REMOTE_TICKET_KEY_ID=<本地值>
REMOTE_TICKET_PUBLIC_JWKS=<本地值>
REMOTE_TICKET_PRIVATE_JWK=<本地值>
EOF

# 3. 本地 D1 迁移
npx wrangler d1 migrations apply DB --local

# 4a. 路径 A：wrangler / miniflare（推荐，需先构建）
npx vite build
npx wrangler dev --local --config dist/<worker>/wrangler.json --port 8787

# 4b. 路径 B：vite dev（受 B1 限制，admin 接口可能不可用）
npx vite dev --host 127.0.0.1 --port 3100
```

## 6. 验收项与代码映射

| 验收项 | 代码位置 | 预期结果 | 验证命令 |
|---|---|---|---|
| `/health/live` 返回 200 | `src/routes/health/live.ts:4-10` | 200，`{"service":"openbot-auth-api","status":"ok"}` | `curl -i http://127.0.0.1:8787/health/live` |
| `/.well-known/jwks.json` 返回 200 | `src/routes/[.]well-known/jwks[.]json.ts:67-75` | 需要 ticket 密钥变量，否则会失败 | `curl -i http://127.0.0.1:8787/.well-known/jwks.json` |
| 无 token 访问 `/v2/admin/server-claims` 返回 401 | `src/routes/v2/admin/server-claims.ts:25-41`（401 在 L31-32）；`src/server/request-auth.ts:282-284`；`src/server/admin-token.ts:9`（未设置时返回 false） | 401 | `curl -i -X POST http://127.0.0.1:8787/v2/admin/server-claims` |
| 持 token 签发 claim | `server-claims.ts` 的同一路由；`auth-service.ts:34`（TTL 15 分钟）；`auth-service.ts:456-500`（创建 claim）；`migrations/0034_server_claims.sql` | 返回 claim 信息 | `curl -X POST ... -H "Authorization: Bearer $SERVER_CLAIMS_ADMIN_TOKEN"`（token 从 `.dev.vars` 读取，不要写入命令行） |
| 兑换 claim | `src/routes/v2/server-claims/redeem.ts:50-63`；`auth-service.ts:478-500`；无效 claim 返回 401 `server_claim_invalid`（`auth-service.ts:860-862`） | 成功兑换为 200；无效为 401 | `curl -X POST http://127.0.0.1:8787/v2/server-claims/redeem ...` |
| 未设置 `AUTH_EXPOSE_DEVELOPMENT_CODE` | `wrangler.jsonc:48`（值为 `"false"`）；`request-auth.ts:58-65`；`scripts/development-secrets.ts:69`（需覆盖） | 值为 false 或未设置 | 检查 `.dev.vars` 与生成的 wrangler.json |
| SMTP 留空 | `wrangler.jsonc:49-52`（host/port/username/from 有值，password 缺失）；`email-delivery.ts:80-100` | 五项全部留空，否则抛出 incomplete | 检查 `.dev.vars` 中 SMTP 五项均为空 |
| 回退方案 | 若本地 workerd 无法跑通，需自行实现简化账户服务 | 评估项，未在本次审阅中展开 | 无 |

## 7. 后续建议

1. 在具备依赖的机器上按第 5 节实测，记录 B4、B5 的实际结果。
2. 修复 B1：将 `SERVER_CLAIMS_ADMIN_TOKEN` 加入 `runtime-env.ts` 白名单，或文档说明只使用 wrangler 路径。
3. 修复 B2：本地脚本默认值改为 `false`，或在文档中要求覆盖。
4. 在 `docs/` 中补充本地运行说明，避免与 `self-hosted-server.md` 混淆。
5. 若 workerd 路径不可行，评估简化账户服务的范围（见验收项最后一行）。
