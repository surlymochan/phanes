# HANDOFF — OpenBot + openswarm + RSI 数字员工（"issue 清零"收尾交接）

写于 2026-10-29 约 00:50（+08:00）。交接对象：下一个接手的 Claude / 人。
这份文档合并并取代此前所有 HANDOFF（2026-10-28 的各版）。**仓库是公开的，所以本文不含密码、令牌、私钥、IP、邮箱。** 机器一律用别名（`gocc`、`gorelay`、`govpn`、`govpn2`、`b1`–`b5`、`gores1`）。

---

## 0. 一页结论

> **最终状态（用户指示"不继续了，直接交接"，2026-10-29 约 01:05）**：所有 lane 已停止派发，本轮不再推进。#54 的 CI 已实测：**19 项通过、2 项跳过，仅 NorbiAI `review`（无 runner，永久 pending，非必需）未结束；Team API E2E 全步骤通过**（steer/interrupt、backup failover、token-budget 都绿）。#54 **尚未拿到 Codex 评审回执，未合并**；其中 token-budget 夹具改动（`b5146b9fbb`）的语义风险仍待评审（§4.3）。其余 PR 的红灯都因同一回归，#54 合并后重跑即可。

| 项 | 状态 |
|---|---|
| 上游同步（429 个 upstream commit） | **已合并**：PR #42，main = `fd5719fc7f`；旧 #41 已关闭 |
| Hub 升级（gocc 自建 Hub） | **已完成并验证**：0.44.3 → 0.52.0，DB schema_migrations 最大版本 49，`openbot-cli run Hub` 收到并回复 `HUB-NEW-OK`（2026-10-29 00:27） |
| M10 审计修复（6 份审计 #61–#66） | 已按领域整合成 **12 个开放 PR**（#43–#55），**全部未合并**；每个单提交 + 修复提交，gocc 正式 pre-commit hook 通过 |
| **CI 阻塞** | 所有新 PR 的 `Team API and reverse actions E2E` 红：fork 功能在上游同步后回归。修复已集中到 **#54**；#54 上 steer/interrupt 与 backup failover 两步已转绿，但引入 `token-budget` E2E 回归，C1 的夹具修复（`b5146b9fbb`）已让 #54 CI 全绿，但存在语义风险，见 §4.3 |
| 并发 | 8 路 = MiniMax 4 + Cursor 3 + Codex 1；**不用本机跑 agent**；gocc 只放 1 个 Codex |
| 组网 | "先整合后组网"：M10 整合完成前不动网络；之后做 Tailscale/Headscale + 单点 mihomo（见 §8） |
| 未做 | 协议 PR 合并（pause/continue + context-usage）、#38 重做、B1a 登录 E2E、perf5 codemod、Signal+TURN（#13）、agent-knowledge #10 rebase、CI workflow 补丁推送 |

---

## 1. 用户的长期规则（必须遵守）

1. **全部用中文回复**。仓库内文本（commit、PR 正文、代码注释）按 openbot 的 `AGENTS.md` 写 ASD-STE100 英文。
2. **非重大后果的事自己决定，不要反复问**。用户说这些都是测试环境，被盗问题不大。高风险（数据、迁移、信任边界、对外发送）仍先备份、优先可逆。
3. **并发 8**：MiniMax 4 + Cursor 3 + Codex 1。**严禁用本机 MacBook Pro 跑 agent**。gocc 是中枢机器，不能过载；上面只放 1 个 Codex 和串行验证。
4. **本机 Claude 只拆解、写任务卡、审查、整合**，写码派给执行器（openswarm）。
5. 项目 `AGENTS.md` 的硬规则：
   - 不在本机跑全量检查（lint/类型/构建/全量测试交给 CI）；只跑最窄的相关检查，一次一个。
   - 只写 E2E 测试（`scripts/*-e2e.ts`），不写单元测试（受保护区域除外：迁移、信任边界、脱敏、lint 规则夹具）。
   - 不用 `--no-verify`；不按进程名杀进程（`pkill -f` 之类）；只在被要求时开 PR。
   - **迁移不可逆，且没有自动备份**：任何 Hub 数据操作前先手动备份 `openbot.db`。
6. 安全边界：不要代替用户输入密码；用户曾在聊天里贴过一台机器的登录口令，我让用户自己把公钥装上去（见 §9）。**建议用户轮换那个口令。** 不读取、不打印 `.env*` 与凭据文件。钥匙串弹窗只能由用户在 gocc 屏幕上点击。

---

## 2. 机器与 lane 布局

### 2.1 主机

| 别名 | 角色 | 备注 |
|---|---|---|
| 本机 Mac（MacBook Pro） | 规划/审查/整合，**不跑 agent** | openswarm 在 `~/.claude/skills/openswarm`；Mac 的 PATH 里没有 `openswarm`，用全路径 |
| `gocc`（Mac mini） | 中枢：Hub、Codex C1、串行验证、PR 组装 | 走 gorelay 反向隧道；`gocc-alt` 为备用入口。磁盘只有约 228 GiB，曾 ENOSPC（见 §10）。**别过载** |
| `gorelay` | 中转/将来的 Signal+TURN 主机 | Caddy 占 80/443（systemd，多个 `*.caddy` 片段），Ubuntu 24.04，ufw 关；未装 coturn/bun |
| `govpn` | Linux 资源机 | K1 停用（bun 在这里解析不了 npm） |
| `govpn2` | Cursor K2 | 仓库 `/home/ubuntu/work/openbot`；有 Xvfb + 外置 Electron，可做真实 renderer E2E |
| `b1`–`b5` | instabox（`dot-task-01`..`05`） | 仓库 `/data/home/work/openbot`；PATH 需加 `$HOME/.openbot-task-tools/node-v24.20.0-linux-x64/bin:$HOME/.bun/bin`；Bun 1.4.2 |
| `gores1` | 新资源机，root 密钥登录已通 | 已装 openswarm（`/root/tools/openswarm`）和 cursor-agent（`/root/.local/bin`）；DNS 已改成国内上游（`/etc/systemd/resolved.conf.d/10-upstream.conf`）；Clash 暂未装 |

### 2.2 lane（`~/.claude/openswarm/lanes.json`）

| lane | 后端 / 模型 | 主机 | 仓库 |
|---|---|---|---|
| C1 | Codex `gpt-6.1-terra` | gocc | `~/work/pr41-successor`（prepare=`~/.claude/openswarm/link-workspace-deps.sh`） |
| K2 | Cursor composer-2.5 | govpn2 | `/home/ubuntu/work/openbot` |
| K3 | Cursor | b5 | instabox 仓库 |
| K4 | Cursor | b2 | instabox 仓库 |
| B1a / B2a / B3a / B4a | pi + MiniMax | b1 / b2 / b3 / b4 | `~/work/openbot-lane` 或 instabox 仓库 |

策略上限（所有主机一致，`policy.local.json`）：codex 1、minimax-cn 4、cursor 3、agy 0、总 8。
openswarm 版本：全部主机 = `b2a70d4`（之后 main 无新提交，origin 已同步）。

---

## 3. 开放 PR 总表（`nibiru-labs/openbot`，base = main `fd5719fc7f`）

全部 MERGEABLE。"红"指除 NorbiAI `review`（无 runner，永久 pending，非必需）外失败的检查。

| PR | 分支 | 内容 | roadmap | 状态 |
|---|---|---|---|---|
| #43 | `test/m3-approval-on-main` | dispatch/M3 审批 E2E | #4 | 红：Team API E2E（回归） |
| #44 | `docs/architecture-on-main` | 架构文档对齐代码 | #52 | 全绿（等评审回执） |
| #45 | `test/auth-claim-on-main` | auth-claim E2E + 进程组清理 | #12 | 红：Team API E2E |
| #46 | `perf/backend-reliability` | 后端资源上限（审计可靠性 1、4–15） | #64 | 红：Team API E2E |
| #47 | `fix/error-messages-and-codes` | 错误信息与错误码；**预加载解码器多接受 `request_failed`，需安全评审** | #63 | 红：Check（knip）+ Team API E2E |
| #48 | `fix/a11y-keyboard-and-names` | 无障碍 15 条 | #62 | 红：Team API E2E |
| #49 | `docs/ios-self-build-and-privacy-on-main` | iOS 自建 + PRIVACY | #19 | 全绿 |
| #50 | `fix/dispatch-watcher-ends` | dispatch 观察者结束（可靠性 3，早前漏掉） | #64 | 红：Team API E2E |
| #51 | `test/e2e-coverage-gaps` | 14 个新 E2E；已加 B4a 的 ratchet 修复 `3d82719f81` | #66 | 红：Team API E2E；**ci.yml 改动被剥离成补丁**（§7） |
| #52 | `perf/renderer-long-chats-and-lists` | 渲染性能（第 5 条另做 codemod，第 2 条已回退；侧栏基准脚本未跑通，PR 正文已如实写） | #61 | 红：Check（knip）、Tests (desktop 2/2)、Team API E2E |
| #53 | `feat/mobile-chat-parity-client` | 移动端纯客户端（1、7、14、15 + 三个小缺口） | #65 | 红：Check、Team API E2E |
| #54 | `fix/fork-e2e-regressions` | **CI 阻塞修复**（§4），含发布说明 | — | **CI 全绿**（含 Team API E2E）；待 Codex 评审 + 夹具语义评审 |
| #55 | `test/response-process-renderer-e2e` | #8 真实 renderer E2E（K2；已加 `no-changelog` 标签） | #8 | 待 CI |
| #38 | `docs/dispatch-architecture` | 旧 dispatch 架构文档 PR | #52 | 只剩 NorbiAI review；**需在 #43 合并后重做**（见 §11） |

另有：`nibiru-labs/agent-knowledge` PR #10（分支 `regression-cards-58`，四张回归卡改成 base 失败/候选通过，并恢复了三张误改的 meta）**与 main 冲突，需 rebase**。

### 已合并 / 已关闭

- #42（同步）squash 合并；#41 关闭。
- 旧 #32/#33/#36/#37/#39/#40 已被 #44/#49/#43/#45 替代并关闭。
- Phase 0 已合并：#21、#22、#26–#31、#34、#35。

---

## 4. CI 阻塞：fork 功能在上游同步后回归（最高优先级）

### 4.1 现象
`Team API and reverse actions E2E` 在所有新 PR 上红。#42 上该 job 是 **skipped**，所以同步当时没暴露。

### 4.2 已定位并修好（在 #54）
| 失败 | 原因（文件） | 修复 |
|---|---|---|
| `steer, interrupt and queue pause`：`Timed out waiting for the held turn of the target`（`scripts/steer-interrupt-pause-e2e.ts:200`） | 1）`src/backend/mailbox-store.ts`：消息批处理把排队消息并进了 run-now 的 turn（提交 `2d8949daba`）；2）`src/backend/agent/openbot-tool-router.ts`：自动 steer 覆盖了显式 busy 模式，打断后排队消息起不来（提交 `a4b0de2d66`） | 已改，CI 转绿 |
| `backup model failover`：先 `usage-work` 超时，后 `usage-partial case:stopped` 超时（`scripts/backup-model-failover-e2e.ts:512`） | `src/backend/agent/usage-limit-gate.ts` 把未指定范围的用量限制当成整账号，连配置的备用模型一起拦；`turn-lifecycle.ts` 对"部分/家族限额"也建了账号级暂停 | 已改（`18aa0ea58c`、`a4b0de2d66`），CI 转绿 |

上游同步给 `usage-limit-gate.ts` 加了 `failed()/isFailed()/PROVIDER_FAILURE_HOLD_MS`（provider 错误后 10 分钟内不再切回该模型），同时新增 `scripts/backup-model-failover-e2e.ts`（991 行）。这是两侧合并时"保留了一边"的典型回归。

### 4.3 token-budget 回归（C1 已给出修复，待 CI 验证）
#54 修完后 `token-budget` E2E 红：`Timed out waiting for the refused turn to end`（`scripts/token-budget-e2e.ts:211`）。它在 #46 等其他 PR 上是绿的，所以由 #54 的后端改动引起。
C1 run `20261029-004441-5f72ef` 的结论：失败的是 PLAN-1 场景，发的是 provider 用量限制（不是 token 预算拒绝）；`turn-lifecycle.ts:981` 里"省略 family"现在被当成"只限被拒模型"，turn 变成 failed 而不是 interrupted。C1 **只改了测试夹具**：让该用例发 `family: null`（表示整套餐限额）。已作为提交 `b5146b9fbb` 加进 #54，**CI 重跑后全绿**（Team API E2E 全部步骤通过）。但 CI 绿只说明夹具与现行代码一致，不证明语义正确。

**这是语义风险，需要评审**：它把"省略 family"与"`family: null`"区分成两种含义。若真实 provider（`src/backend/claude-client.ts`）发出的套餐限额也是"省略 family"，那产品行为就是变了：整账号套餐限额只会拦被拒模型。接手后必须：（a）读 `claude-client.ts` 里构造用量限制事件的地方，确认真实套餐限额是否显式带 `family: null`；（b）若不是，应改产品代码让"省略"= 整账号、让"部分/家族"必须显式给出，而不是改夹具；（c）这点要写进 #54 的评审请求里。

### 4.4 接手动作（#54 CI 已绿时的第一步）
先请 Codex 评审 #54（评论里点名 §4.3 的语义问题），核对 `claude-client.ts`，再合并。若不再需要改代码，下面第 1–2 步可跳过。

1. 读 C1 结果：`ssh gocc` 后读 `~/.claude/openswarm/runs/20261029-004441-5f72ef/result.json`。
2. 若 `commit` 非空：到 gocc `~/work/openbot` 里 `git fetch ~/.claude/openswarm/lanes/C1/wt openswarm/<run-id>:refs/pub/c1-5`，在 `~/work/pub-fix-fork-e2e-regressions` worktree 里 `git cherry-pick -n <sha>`，用正式 hook 提交，`git push origin HEAD`，再在 Mac 上跑 `pushbr.sh fix/fork-e2e-regressions` 推到 GitHub（见 §6）。
3. #54 的 CI 全绿且有 Codex 评审回执后**先合并 #54**。之后其他 PR 的 CI 重跑即可吃到修复（PR 的 CI 在 merge ref 上跑），不需要往每个 PR 单独加提交。
4. 若 token-budget 仍红且 C1 无解：把 `scripts/token-budget-e2e.ts` 的"refused turn"断言与 `turn-lifecycle.ts` 的修改并排读；对比 `git show 44c1e550da:src/backend/agent/turn-lifecycle.ts`（同步前 fork 行为）。**不要改测试去迁就**，除非能指出上游哪一行表明新行为是有意的。

---

## 5. Hub（gocc 上的自建 OpenBot Hub）

- 应用在 `/Applications/OpenBot.app`，LaunchAgent `app.openbot.hub`；本地账户服务 `app.openbot.local-auth` 在 `127.0.0.1:3100`。
- DB：`~/Library/Application Support/OpenBot/openbot.db`。CLI：`node /Applications/OpenBot.app/Contents/Resources/cli/openbot-cli.mjs`（需 `PATH` 里有 node：`/opt/homebrew/bin:$HOME/.local/node-v24.14.1/bin`）。
- 升级/回滚脚本：`~/.local/bin/hub-swap old|new`。应用备份：`~/openbot-app-backups/OpenBot-44c1e55.app`（0.44.3）、`OpenBot-fd5719f.app`（0.52.0）。
- DB 备份：`~/openbot-db-backups/` 下 `openbot-hub-v45fork-20261027.db`、`openbot-hub-pre-swap2-20261028-2355.db`、`openbot-hub-pre-release-fd5719f-20261028.db`。
- **新版本第一次启动会向 macOS 钥匙串要权限（`SecItemCopyMatching`）**，必须有人在 gocc 屏幕上点"始终允许"，否则 app 卡死、不打开数据库。这次已点。
- 当前状态：0.52.0 运行中；`schema_migrations` 最大版本 49；Hub 线程里有 `HUB-NEW-OK` 的 assistant 回复。
- 回滚：`ssh gocc hub-swap old`（但 DB 已迁到 v49，旧 app 不认；回滚前必须先用升级前的 DB 备份替换 DB，**先停 Hub**）。
- roadmap #3（Hub 派活给 lane 并审查结果）：Local scripts 开关已开，`openbot-cli run Hub` 可用；**还差**"Hub 把任务卡派给一个 lane、收到结果并审查"的一次完整演示。
- 还没在 #67/#68 上记录这次升级的证据（命令与输出见上），接手后补一条评论。

---

## 6. 流水线与脚本（本仓库 `tools/`）

这些脚本原来在会话临时目录里，**临时目录会消失**，所以一并存入本仓库 `tools/`（原路径常量是 Mac 上的会话目录，使用前改 `S=`/`C=` 变量）。

| 脚本 | 用途 |
|---|---|
| `dispatch.sh <ssh\|ibx> <host> <lane> <card> <title> [verify]` | 把一张任务卡派给远端 lane（`--base` 默认写死为旧基线，调用时传第 7 个参数） |
| `watch8.sh "<host:transport:run-id:label> ..."` | 批量监视多个 run，只输出终态/风险/停滞事件 |
| `collect.sh` | 收取 run 结果 |
| `pub-remote.sh`（gocc 上执行） | 在 gocc 从 main 起 worktree `~/work/pub-<branch>`，链接依赖，`cherry-pick -n` 若干提交成**一个**提交，剥离 `.github/workflows` 改动成 `~/work/ci-wiring-<branch>.patch`，走正式 hook 提交并推到 gocc bare |
| `pub.sh` / `pub2.sh` | Mac 侧入口：`pub.sh` 从执行器主机打 bundle 再组装；`pub2.sh` 用 gocc 上已有的 sha 组装，然后推 GitHub 并 `gh pr create` |
| `addfix.sh <ibx\|ssh> <host> <repo> <run-id> <branch> <msg>` | 把某个 run 的提交作为新提交加到已有 PR 分支 |
| `pushbr.sh <branch>` | 把 gocc 上的 PR 分支取回 Mac 推到 GitHub |
| `mkcard.py`、`mkrebase.py`、`mkpr.py` | 生成任务卡 / 重放旧 PR 的任务卡 / PR 正文模板 |
| `link-workspace-deps.sh` | worktree 里链接基线 `node_modules`，但 `@openbot/*` 指向 worktree |
| `osupdate.sh` | 把 openswarm 更新分发到各主机 |
| `dropexport.sh`、`hostprep.sh`、`rehearse.ts` | 剥离导出文件改动、主机准备、Hub DB 迁移演练 |
| `cards/`、`pr/` | 全部任务卡与 PR 文本（`b2-test13*.md` 两张含疑似敏感串，未收录） |

### 典型操作

```bash
# 1. 看 lane 与 run
ssh gocc 'export PATH=$HOME/.local/bin:$HOME/bin:$PATH; openswarm lane status'
# 2. 重派 C1（base 要在 gocc 仓库里存在）
ssh gocc 'cat > /tmp/card-C1.md' < cards/<card>.md
ssh gocc 'export PATH=$HOME/.local/bin:$PATH; openswarm lane assign C1 --base <sha> --task-file /tmp/card-C1.md --title "..." --budget-minutes 60'
# 3. 把 run 提交组装成 PR
bash pub2.sh <branch> <msg-file> "<title>" <body-file> <sha> [<sha> ...]
```

**注意**：`ssh gocc "~/.claude/..."` 里的 `~` 会被远端 shell 展开成 gocc 的家目录，路径在不同主机不一样；执行器主机上的 openswarm 在 `PATH`（`~/.local/bin` 或 `~/bin`），不是 `~/.claude/skills/openswarm/bin/openswarm`。

---

## 7. CI workflow 改动推不上去

GitHub 令牌没有 `workflow` scope，任何改 `.github/workflows/*` 的提交都推不上去。`pub-remote.sh` 会把这部分剥成补丁：

- `gocc:~/work/ci-wiring-test-e2e-coverage-gaps.patch`（#51 对应；其他 PR 若有类似补丁也在 `~/work/ci-wiring-*.patch`）。

**需要用户动手一次**：`gh auth refresh -h github.com -s workflow`，然后在对应 worktree 里 `git apply` 补丁、提交、推送。否则 #51 新增的 E2E 不会被 CI 执行（仅脚本入库）。

---

## 8. 网络方案（已决策，未实施）

用户决定"先整合后组网"：先把 M10 合并完，再做组网。目标：

1. **Tailscale/Headscale 组网**，DERP 放在 gorelay；
2. **只在一个地方放 mihomo（Clash）**，默认只用 `bwh-hy2-direct`；
3. gores1 现在是普通资源机（暂无 Clash）；
4. Signal+TURN（roadmap #13）放在 `relay.surlymo.cn`：DNS 已解析到 gorelay，安全组 3478/5349/49152-65535 已开，443 给 Signal（Caddy）；**等组网后做**，账户服务只在 gocc。

本机 Clash 配置在 `~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml`（节点：bwh-hy2-direct、bwh-tcp-direct、bwh-tcp、bwh-hy2、govpn-sg、govpn2-sg、bwh-hy2-isp、bwh-tcp-isp；分组 PROXY、bwh-isp-auto），**尚未部署到任何机器**。该文件含节点凭据，别复制进仓库。

---

## 9. 新资源机 gores1 与已知网络坑

- gores1 的登录口令曾被用户贴在聊天里，我没有用口令登录，而是让用户自己把本机公钥装上去（用户执行成功）。**建议轮换该口令，并确认 sshd 已禁用口令登录。**
- 本机到 gorelay 的 SSH 复用主连接（ControlMaster）会卡死，处理：`ssh -O stop gorelay`；已记入 cli-use-memory 的 `lessons.md`。
- gocc 的默认 SSH 通道走 gorelay 反向隧道，曾多次 banner 超时；`gocc-alt` 为备用。

---

## 10. 迁移设计（不可逆区域，勿改）

- fork 的迁移 **45 = routine_backoff 保持不变**。上游的 browser_profiles / workflows / agent_threads 追加为 **46–48**，**49** 给导入的历史补 routine_backoff。
- 按物理 schema 声明/约束/索引识别来源；保留上游 45/46/47 的原 marker/timestamp；在写入前和附加迁移事务锁内检查，**拒绝矛盾或未知的 schema**。
- 第一父提交的 38 个历史条目完整前缀逐项相同；原 routine SQL 文件未改。
- 旧"把 fork45 改成 48 并替换 45 marker"的方案已作废：不能改已发布的操作，也不能靠改测试期望过关。
- 已做的演练只证明了：非空 `routine_backoff/tasks` 在 45→49 保全。真实 Hub 升级已完成（见 §5）。
- gocc 磁盘曾满（ENOSPC，Codex 中途 panic）：`/` 目前 228 GiB，约 5 GiB 可用、70% 已用。**清理前逐个看 `git status`**；`~/work/` 下很多 `pr-*`、`sync-*`、`pub-*` worktree 可清，但 `pub-fix-fork-e2e-regressions`（#54 工作区）和 `pr41-successor`（C1 仓库）别删。

---

## 11. 待办清单（按优先级）

**当前所有 lane 空闲，没有在跑的 run**（最后一个 C1 run `20261029-004441-5f72ef` 已 partial 收取）。监视器均已结束，接手无需清理后台任务。


| # | 事项 | 怎么做 |
|---|---|---|
| 1 | **#54：CI 已绿；请 Codex 评审并核对 token-budget 夹具语义，然后合并** | §4.3 / §4.4 |
| 2 | 合并 #54，再让其余 PR 重跑 CI | `gh pr merge 54 --squash`（需评审回执）；其余用 `gh run rerun --failed` 或推空提交 |
| 3 | 协议 PR：pause/continue + context-usage | 两条链在共享文件冲突；任务卡 `cards/c1-proto-merge.md`。pause/continue 在 gocc `6769e66`→`98f259bdad`→`364e2b6844`；context-usage 在 b2 `cb6db55`→`c0eeecfab0`。合并后需 Codex 评审并确认 Team API 已发布适配器语义不变 |
| 4 | #38 重做 | 等 #43 合并（`agents.md` 冲突）；B2a 的 re-apply 提交 `db53a1b3fd` |
| 5 | B1a 登录 E2E（`e6face66f2`） | 依赖 `scripts/server-mode-cli-e2e.ts`，main 上没有；等对应 server-mode PR 合并后再补，或让 B1a 改成不依赖该文件 |
| 6 | #52 合并后重跑 perf5 codemod | 想法在 K4 `dd6825d26c`：`scripts/ui-subpath-imports-codemod.ts`，对新主干**重跑脚本**，不要 cherry-pick |
| 7 | agent-knowledge #10 rebase | 与 main 冲突；#58 四张卡 |
| 8 | Codex 评审回执 | #43–#55 与 agent-knowledge #10 都需要回执再合并（规则：没有回执不合并，见 §12-1） |
| 9 | 合并顺序建议 | #54 → #44/#49（文档，加 `no-changelog`）→ #43/#45 → #46/#50 → #47/#48 → #51 → #52 → #53 → #55 |
| 10 | 关 issue（只在有证据时） | #4、#12、#19、#52、#58、#61–#66、#8、#3；#63 的 errors 第 10 条按决定不修（协议限制），在 #63 里写明 |
| 11 | #3 | Hub 派一张卡给 lane、收结果、审查，记到 issue |
| 12 | #67/#68 | 补 Hub 升级证据（§5） |
| 13 | 用户一次性动作 | `gh auth refresh -s workflow`（§7）；轮换 gores1 口令（§9） |
| 14 | 组网 + #13 | §8，M10 合并后 |

---

## 12. 规则与教训

1. **先有评审回执再合并**：曾提前合并漏掉 P1（MCP 脱敏顺序）和 P2（日语否定），在 #35 补修。
2. 别用 `-q` 管道隐藏合并冲突（曾误关 #56）；关闭前用 `git merge-base --is-ancestor` 验证。
3. 任务卡不要让执行器 commit，由 runner 提交；否则 pass 定义失真。
4. 一张卡一个 issue；PR 正文末写 `openswarm-run: <ids>`。
5. 执行器行为：
   - 沙箱里 Codex 起不了假 provider CLI，也不能绑 socket；这类 E2E 只能靠 CI 或执行器宿主验证。
   - MiniMax 曾在工作区外 `rm`（lesson L-0035，已修）。
   - 误报/漏报都出现过：C1 无改动被标 `done`、IO 丢失被误判。已在 openswarm 修（见 §13）。
6. **别在 PR 里误提交 `.tmp-wt/selfhost`**（gitlink）：#47、#51 都中过，CI 的 API job 会因 submodule 报错。已有修复提交（`07f48b11cf`、`b8176f1c2b`）。
7. pre-commit hook 会跑类型/knip，常见坑：jsdom 类型、`TS6142 jsx`、未使用导出（#47 两个类、#52 `withoutAgentRow`、#53 `continuesSenderRun`）。
8. 编辑习惯：gocc 是 macOS，`sed -i` 要 `-i ''`，或用 Python；zsh 不会对 `$var` 分词。
9. 派单前确认 `--base` 提交在目标主机仓库里存在。
10. 别杀共享的 SSH master；别按名杀进程。

---

## 13. openswarm 本轮改动（已部署到所有主机，main = `b2a70d4`）

- `ENFORCED_RISKS` 加入 "computer use"；Codex 执行器参数 `--disable computer_use`。
- secret-print 正则收窄（源码文件扩展名不再误报）。
- `rm` 判定允许对 /tmp 里**单个文件**的非递归删除；`rm -rf "$VAR"` 仍拦（任务卡里要写字面相对路径）。
- `codex_command_io_empty` 只数"`aggregatedOutput is None` 且 `durationMs` 为 0"。
- 无提交/无 diff 的执行不再算 `done`，记为 `partial`/`no_change_evidence`（`--read-only` 例外）。
- Codex 过期 lane 的 app-server 自动重启（按指纹）。
- 仓库：`github.com/surlymochan/openswarm`，gocc 上 `~/tools/openswarm`（同步方式：`git push gocc-tools main:refs/heads/incoming -f` 后 `ssh gocc 'cd ~/tools/openswarm && git merge --ff-only incoming'`）。

---

## 14. 备份与路径速查

| 内容 | 位置 |
|---|---|
| M10 提交 bundle 备份 | Mac `nibiru/m10-bundles-20261028/`；gocc `refs/m10/*`、`refs/pub/*`（`~/work/openbot`） |
| 本轮证据（未入库，本地忽略目录） | Mac openbot 仓库 `.openbot-build/handoff-preparation/2026-10-28/`、`TASKS.md` |
| 完整对话记录（上一轮之前的上下文） | `~/.claude/projects/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/62926203-41d2-4936-8582-8fe69de7f53c.jsonl` |
| 本轮之前的对话记录 | 同目录 `15d25874-4bae-4076-a152-c35cd5181d5e.jsonl` |
| 共享记忆（经验教训） | `~/Documents/workspace/cli-use-memory/global/lessons.md`（不是 git 仓库，**没有版本控制**） |
| Claude 项目记忆 | `~/.claude/projects/-Users-chenchao-Documents-workspace-nibiru-openbot-openbot/memory/`（`reply-in-chinese`、`decide-for-me-unless-high-risk`、`openswarm-lane-caps`） |
| roadmap / 设计 | `nibiru-labs/openbot-roadmap`：`docs/design-rsi.md`、`docs/audits/openbot-*.md`、`docs/reports/` |
| 每周复盘 | Mac launchd `com.openswarm.weekly-retro`（周一 09:00），开 agent-knowledge PR |

### M10 提交线索（全部已进入 §3 的 PR，留作溯源）

- 可靠性（C1）：`81b85e0→174fae3→e132173→460935f→ec51097→dd538d4→b11bc81→37c02c0`
- 无障碍：`cd1a340→d3144ad→77db65e→38cafef→2b9693d→25f0842`（K4）
- 错误：K3 `a4dcafc→f74d19b→858fbbd→4eb93be`；B1a `61afeca`（改 `src/preload`，已在 #47 里要求安全评审）；B2a `13f8f9c→63d1708`；K4 `82d1578`
- 性能：K2 `0592c3a→8c0047e→a897d69→7f1b4c9→1b98bcd`；K3 `f3903a4`、`75083b0`；K4 `e6a8d27`
- 移动端：K4 `3cb28d9→04d119a`（merge `54627701a7`）；K3 `714b06d`；B3a `2b48001`

---

## 15. 分析与建议

**1. 先修 CI，再谈合并。** 12 个 PR 红在同一个原因（fork 功能回归），修复集中在 #54，是现在唯一的关键路径。合并 #54 后其他 PR 重跑即可，不要往每个 PR 单独补修复提交（会制造 12 份重复提交和冲突）。

**2. 同步合并是回归的根因，别再信"#42 绿了"。** #42 上该 job 被跳过，导致这个回归藏了半天。建议：（a）在 #42 之后的 main 上加一个"同步类 PR 必须跑完整 Team API E2E"的规则；（b）把这两个 fork 功能（备用模型故障切换、steer/interrupt/pause）列入"同步后必跑清单"，写进 agent-knowledge 的回归卡。

**3. Codex 的沙箱起不了假 provider CLI，是反复"部分完成"的根因。** C1 前三次都是 partial，没产出改动，直到我把 CI 报错原文贴进任务卡才出修复。建议：对依赖假 provider/socket 的 E2E，让执行器**只读代码 + 读 CI 日志**，用 CI 当验证；或在 govpn2（有 Xvfb 与真实环境）上跑 Cursor。任务卡里要写明"你不能跑这个脚本"，否则它会花预算在启动上。

**4. 每个 CI 迭代约 6–10 分钟，设计成"提交→CI→读日志→再派"的闭环。** 现在是手工；建议写一个小脚本，轮询 PR 的 job 步骤结论（`gh api .../actions/jobs/<id> --jq '.steps[]|select(.conclusion=="failure")'`），失败时自动取 `--log-failed` 里的 `Error:` 行并生成任务卡。我这轮已手工做了两次。

**5. 评审回执是合并闸门。** 12 个 PR 都在排队等 Codex 评审。合并顺序按 §11-9 走；文档类 PR 最先；#47（预加载解码器）与 #53（协议）必须有安全/协议评审。**`request_failed` 这条放宽解码是信任边界相关，别靠自动通过。**

**6. 并发利用率偏低。** 现在只有 C1 在跑，K2/K3/K4/B1a–B4a 都空闲。可派的低冲突活：
   - B2a：#38 重做的前置整理（等 #43）；
   - K3/K4：perf5 codemod（等 #52）；
   - 任一 Cursor：给 token-budget/steer 类失败写更窄的 E2E 复现（在 govpn2 上能跑真 provider 假件）；
   - MiniMax：agent-knowledge #10 rebase 与回归卡补充。
   但**别让两个执行器同时改 `src/backend/agent/*`**（C1 正在改）。

**7. 磁盘与 gocc 保护。** gocc 只剩约 5 GiB 可用。C1 + 串行验证 + 多个 worktree 很容易再次 ENOSPC。接手后第一件事：清理已合并的 `~/work/pr-*` 与 `pub-*` worktree（逐个 `git status`），再继续。

**8. 安全待办（不是阻塞，但该做）。** 轮换曾贴在聊天里的口令；确认 gores1 sshd 禁用口令登录；GitHub 令牌补 `workflow` scope 后再推 CI 补丁；本机 Clash 配置含节点凭据，别入库。

**9. 里程碑判断。** "issue 清零"的真实剩余量：CI 修复（小）+ 评审回执与合并（中，取决于评审速度）+ 协议 PR 与 #38/登录 E2E（中）+ 组网与 Signal+TURN（大，且依赖用户配合）。**只要 #54 一合，M10 代码侧基本进入收口；#13 与 #3 才是需要额外时间的两块。**

---

## 16. 未核实项（别当事实用）

- #54 的 token-budget 夹具改动（`b5146b9fbb`）语义是否正确：**CI 已绿但语义未验证**，风险见 §4.3。
- steer/interrupt 修复的真实原因是否为"自动 steer 覆盖 busy 模式"：CI 已转绿支持这个判断，但没有逐行复现。
- 各 PR 的 NorbiAI `review`（无 runner）永久 pending，已知非必需。
- Codex 对 #43–#55 的评审回执：**没有逐个核对**。
- 本机 CLA：不能代用户勾选。

