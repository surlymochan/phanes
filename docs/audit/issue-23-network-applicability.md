# Issue #23 评估：历史自托管网络 / 多出口需求是否仍然适用

- 评估日期：2026-10-31
- 评估基线：本仓库工作树 `HEAD`（`df02c552d`），另参考 `git` 历史中的相关提交
- 评估方式：仅依据仓库内容静态核对。未访问网络、未访问 issue 追踪服务、未读取或引用任何凭据、密钥与私有地址。

## 0. 结论

**主建议：暂缓（defer）——当前阶段不新建自托管网络 / 多出口设施。**

拆分后的两个子项结论不同：

| 子项 | 结论 | 依据摘要 |
| --- | --- | --- |
| 多出口（多个出口 IP / 代理池 / 出口轮换） | **关闭（close）：不适用** | 仓库内不存在任何多出口相关的配置项、代码或文档；现有限额全部是"账号 / 主机 / 后端"维度，多出口不会改变任何上限 |
| 自建网络接入新执行主机 | **暂缓（defer）** | 该形态在仓库里的唯一实现是已被回滚的反向 socket 隧道（`443f34554` → `c33d8d4d5`）；同一能力已改用配置方式表达（`45c8ada52`，在 `origin/brain/fleet-multihost`，**未合入本分支**），本分支仍缺这层扩展 |

也就是说：**多出口这一项可以直接关闭；自建网络的"接入新主机"部分保留为待办，但不应在当前阶段重做已被回滚的传输层方案。**

## 1. 评估范围

检索范围：`openswarm/`、`openbot/`、`integrations/`、`tools/`、`.github/`、仓库根 `README.md` 与 `HANDOFF.md`，以及 `git log` / `git show` 中的相关提交历史。

检索关键词：`egress`、`proxy`、`HTTP(S)_PROXY`、`no_proxy`、`ssh`、`socket`、`tunnel`、`vpn/tailscale/wireguard`、`instabox`、`自托管/自建/出口`，以及 host 别名清单。

说明：issue 正文不在仓库内，因此"历史自托管网络需求"在本文中一律以**仓库内可验证的形态**指代（反向 socket 隧道、SSH 强制命令通道、单出口代理、instabox 临时盒等），不对 issue 原文做逐字引用。

## 2. 仓库中现存的"网络"相关物

### 2.1 控制面自建部署（OpenBot 上游能力，与执行网络无关）

| 位置 | 内容 |
| --- | --- |
| `openbot/docs/self-hosting.md:1-40` | 自建账号服务 + Signal + TURN 的部署指南（Cloudflare Workers/D1/R2、一台 Linux 服务器、SMTP） |
| `openbot/deploy/self-host/README.md:1-40` | 自建 Signal/TURN 捆绑包（Caddy + coturn + systemd）；`:25` 要求"一个静态公网 IPv4"，`:34` 起是端口与防火墙清单 |
| `openbot/src/main/private-network-detection.ts:3-17` | 通过网卡地址前缀判断 Tailscale 是否连接 |
| `openbot/src/renderer/src/features/team/team-webrtc-private-network.ts:1-7, 9-24, 32-45` | 私网模式：过滤掉所有非私网 ICE candidate，不用 STUN/TURN |
| `openbot/src/main/host-service.ts:152` | `privateNetwork?: () => boolean` 开关 |

这些属于 OpenBot 上游"用户接入 Hub"的能力，Phanes 未改动；其网络模型是**单一公网地址 + 单条私网路径**，本身不含"多出口"概念。

### 2.2 执行面传输（当前 Hub 回路真正在用的）

| 位置 | 内容 |
| --- | --- |
| `openswarm/bin/openswarm:8581-8603` | `fleet_config()`：主机种类只有 `local` / `ssh` / `instabox` |
| `openswarm/bin/openswarm:8619-8630` | `host_run()`：`ssh <alias> --` 或 `instabox ssh <box> --` 两种出站传输 |
| `openswarm/bin/openswarm:9276-9280` | `instabox_states()`：状态探测不唤醒暂停的临时盒 |
| `openbot/src/backend/dispatch/dispatch-service.ts:72-83` | Hub 侧的 fleet schema，种类同上 |
| `openbot/src/backend/dispatch/dispatch-service.ts:481-487` | Hub 从 `$OPENSWARM_HOME/fleet.json` 读取主机清单 |
| `openbot/src/backend/dispatch/dispatch-service.ts:500-503` | 按 `kind` 选择 `ssh` / `instabox` 传输 |
| `openbot/src/backend/dispatch/dispatch-process.ts:79, 91-95` | 传输参数拼装（任务卡经 stdin 送到远端） |
| `openswarm/bin/hub-openswarm-bridge:21` | Hub → 调度主机：SSH 固定强制命令通道（专用 SSH 配置 + 固定主机别名） |
| `openswarm/bin/hub-fleet-gateway:13` | 执行主机 → 网关：另一条 SSH 固定强制命令通道 |
| `integrations/README.md:5, 14-16, 34-35` | Hub 与执行网络的连接入口说明；直连 fleet 的环境变量与 Hub 本机配置要求 |

**要点：跨主机流量全部是"出站 SSH + 固定强制命令"，没有入站端口、没有常驻隧道服务、没有出口选择逻辑。**

### 2.3 供应商凭据集中与单一上游

| 位置 | 内容 |
| --- | --- |
| `openswarm/bin/pi-minimax-broker.mjs:22, 27, 55, 75` | broker 只有**一个**上游（单一模型 API 主机），并发闸门 `maxInFlight = 4`，监听私有 Unix socket（0600） |
| `openswarm/bin/pi-broker-service:9-15, 34` | broker 服务：私有目录 0700、socket 路径与启动方式 |
| `openswarm/bin/pi-worker-proxy:13-14, 32, 44` | worker 侧 socket → SSH 强制命令中继 |
| `openswarm/bin/pi-fleet-bridge:15-18, 76-81` | 有时限的临时 SSH `-L` / `-R` 端口转发（分钟级，结束即清理） |
| `openswarm/bin/pi-local-http-bridge.py:15, 17, 32, 121` | 本机 HTTP 中继（默认仅监听回环地址），按 token 校验 |
| `openswarm/bin/pi-isolated:43, 53, 63-67` | 执行沙箱：bubblewrap 内只经 `socat` 连到 broker socket，外部网络不可达 |
| `openswarm/docs/executor-recovery.md:9-29` | 上述机制的书面说明：凭据留在持有方主机，worker 只拿到 socket，桥接是有时限的传输进程 |

这一组机制是"**单一上游 + 单点凭据 + 并发闸门**"，设计上恰恰是为了**不**把凭据分散到多台主机，因此也不构成多出口需求。

### 2.4 容量协调

| 位置 | 内容 |
| --- | --- |
| `openswarm/bin/fleet-capacity:12-16` | 允许的后端集合、按主机/后端/provider 的限额表、认证主机白名单（仅三个已登记主机） |
| `openswarm/bin/fleet-capacity:72-79` | 协调器状态：私有目录 0700、SQLite 0600 |
| `openswarm/bin/fleet-capacity:109, 122-124` | 按 `host` / `backend` / `provider` 三个维度计费与拦截 |
| `openswarm/policy.json:258-263` | `concurrency`：按后端与 provider 声明的并发上限（另含 `total`） |
| `openswarm/README.md:690-697` | 协调器通过受限 SSH 强制命令端点访问，使用专用 fleet-control 密钥；强制命令钉死身份 |

**要点：所有上限都是"账号 / 主机 / 后端"维度，没有"来源 IP"维度。** 多出口既不改变这些上限，仓库里也没有任何按出口做计费或调度的代码。

### 2.5 每台主机上的出口整形（单条固定路径）

| 位置 | 内容 |
| --- | --- |
| `openswarm/bin/agy-via-local-proxy:66-67` | Antigravity 走本机回环上的单个 HTTP 代理（单代理、单出口） |
| `openswarm/bench/swebench/harness/arms.py:25-39, 129-138` | 基准环境用"死胡同代理"关闭通用出网，仅通过 `no_proxy` 放行模型 API 主机 |

**要点：仓库内与出口相关的配置只有"限制一条路径"或"指定一条固定路径"，没有任何"多出口池 / 轮换 / 按出口分流"的实现。**

### 2.6 历史网络扩展：已回滚的反向 socket 隧道

| 提交 | 内容 |
| --- | --- |
| `443f34554` "Bridge remote fleet guards through gocc coordinator" | 新增 `fleet-capacity-stdio`、`fleet-capacity-reverse-tunnel`、`fleet-capacity-socket-client` 与文档 `openswarm/docs/fleet-reverse-socket.md`，用于让一批自建网络主机（该文档以 `govpn` / `govpn2` 为主机示例）通过**反向 Unix socket + SSH 转发**接入共享协调器；远端以 socket client 作为 guard command |
| `c33d8d4d5` "Revert expanded fleet transport changes" | 整体回滚上述改动（−555 行），并删除 `openswarm/docs/fleet-reverse-socket.md` 与相关测试。该提交信息未给出回滚原因 |
| `45c8ada52`（`origin/brain/fleet-multihost`，**未合入本分支**） | 改用配置扩展：协调器读取 `topology.json`（主机 + 限额），`openswarm` 接受 `OPENSWARM_FLEET_HOSTS` 追加守护属主，使**新执行主机**可以接入而无需新增传输层 |
| `tools/watch8.sh:3` | 历史批次清单里出现过自建网络主机别名与 instabox 临时盒两类远端主机 |

已回滚文档的原文要点（`git show c33d8d4d5^:openswarm/docs/fleet-reverse-socket.md`）：远端主机不需要保存到协调器的反向登录凭据；协调端以固定身份的 socket 服务接收，远端以 socket client 作为 guard command；socket 或转发不可用时任务保持 queued、不自动释放不确定的预约。

**要点：仓库历史上"自托管网络"的实现形态就是这套反向 socket 隧道，并且已被整体回滚。**

### 2.7 全量检索的否定结论

- `policy.json` 顶层键为 `_doc / agy / bus / codex / concurrency / copilot / cursor / deepseek / difficulty_map / efforts / fleet / hermes / local_sources / never_efforts / ocgo / pi / rate_limit / recommended_concurrency / review / routing / run / thresholds / workspace` —— **没有 proxy / egress / network / ip 相关键**。
- 在 `openswarm/`、`tools/`、`integrations/`、`README.md`、`HANDOFF.md`、`.github/` 中检索 `egress`、`public ip`、`ip pool`、`rotate ip`、`proxy pool`、`出口` 等词，除"regression"之类的误命中外**无任何多出口相关命中**（仓库其余位置的 "egress" 命中只是 OpenBot 技能目录里把 "egress guard" 列为一种防御性代码类别的泛指，例如 `openbot/marketplace/production-catalog/bundles/pstack/skills/why/references/source-playbook.md:17`，不是网络出口配置）。
- `openswarm/policy.json` 中唯一的网络相关键是 `fleet.recovery_ttl_seconds / recovery_poll_seconds`（`:55-58`），属于恢复语义，与出口无关。

## 3. 当前 Hub 回路实际需要的网络能力

按 `integrations/README.md` 与 `openswarm/bin/*` 归纳，Hub 回路只需要下面五项：

1. **Hub → 执行主机的出站 SSH**，且必须落在固定强制命令上（派发、状态、停止、取回 bundle 四类操作各有固定入口，见 `hub-openswarm-bridge:21`、`hub-fleet-gateway:13`）。
2. **执行主机 → 模型供应商的出站 HTTPS**，按后端各自进行。
3. **凭据集中主机 → 供应商的唯一上游**，以及其它主机到该上游的 socket 转发（SSH 强制命令中继 / 有时限 `-L`、`-R` / 本机 HTTP 中继，见 2.3）。
4. **一个全 fleet 共享的容量协调器**，带按主机/后端/provider 的上限与显式释放语义（`fleet-capacity:12-16, 72-79, 109-124`）。
5. **每台主机上对执行器的本地隔离**（bubblewrap 命名空间 + 仅暴露 broker socket，见 `pi-isolated:43, 53, 63-67`）。

**明确不需要的能力：**

- 任何入站端口（Hub 与执行主机之间全部是出站连接）；
- 多出口 IP、代理池、出口轮换（见 2.4、2.7：上限是账号级与协调器级，且不存在任何出口维度的策略）；
- 自建 overlay 网络（SSH 已经提供同等可达性，且固定强制命令比自建隧道提供更强的身份钉死与更小的凭据面）。

## 4. 适用性判断

### 4.1 多出口：不再适用，建议关闭

- 仓库内不存在多出口相关的配置项、代码或文档（第 2.7 节为全量否定结论）。
- 现有限额全部是账号 / 主机 / 后端 / provider 维度（`policy.json:258-263`、`fleet-capacity:12-14, 109-124`）。多出口不会放宽任何一条限额，因此不会提高当前回路的吞吐上限。
- 仓库中唯一与"出口"相关的配置是"固定一条路径"（`agy-via-local-proxy:66-67`）和"关闭通用出网"（`arms.py:25-39`），都不是多出口。
- 没有找到任何"按来源 IP 限流 / 封禁"的证据（ledger 记录、provider 错误处理、策略文件里都没有 IP 维度）。

因此，把"多出口"作为一项能力去实现，当前没有任何仓库内依据支撑；建议 **close**。

### 4.2 自建网络接入新执行主机：仍可能适用，但建议暂缓

- 该形态在本仓库的唯一实现（反向 socket 隧道）已被整体回滚（`443f34554` → `c33d8d4d5`），回滚后仓库内不再有对应代码与文档。
- 同一需求已改用配置方式表达（`45c8ada52`：`topology.json` + `OPENSWARM_FLEET_HOSTS`），但**该提交尚未合入本分支**：当前 `fleet-capacity:15-16` 仍只接受三个已登记主机，`openswarm/bin/openswarm` 中也没有 `OPENSWARM_FLEET_HOSTS`。
- 结论：能力缺口存在（新增主机族接入协调器），但它是**配置扩展**问题，不是"必须新建自建网络"的问题。重做被回滚的隧道方案会引入常驻隧道进程与新的凭据面，收益不足。

因此建议 **defer**：等确有新增执行主机族需要接入时，先把 `45c8ada52` 形态的配置扩展合入并验证，不要重做传输层。

### 4.3 控制面自建部署：不纳入本 issue

`openbot/docs/self-hosting.md` 与 `openbot/deploy/self-host/` 属 OpenBot 上游"用户接入 Hub"的能力，Phanes 未改动其语义，也不参与执行网络。除非决定把 OpenBot 自建部署纳入 Phanes 部署范围，否则与本 issue 无关。

## 5. 触发重新评估的条件

出现下列任一情况时，重新打开本评估：

1. 出现按来源 IP 的供应商限流 / 封禁证据（ledger 记录、provider 错误或策略中出现 IP 维度）；
2. 需要接入位于不可直达网络的新执行主机族，且 `topology.json` + SSH 别名方案不满足；
3. 决定把 OpenBot 控制面自建部署（Signal / TURN / 账号服务）纳入 Phanes 部署范围；
4. 出现需要执行器持有**多个**供应商凭据或需要按出口分流计量的明确需求。

## 6. 本次评估未覆盖的部分

- 未访问 issue 追踪服务或外网，因此未核对 issue 正文与历史讨论；
- 未做运行期验证（未部署任何主机、未发起真实派发），全部结论来自静态核对与 `git` 历史；
- 未评估 OpenBot 上游仓库自身的网络实现，仅在第 2.1 节列出其与本 issue 的边界。

## 附录：核对清单（文件 : 行号）

| 主题 | 位置 |
| --- | --- |
| fleet 主机种类与出站传输 | `openswarm/bin/openswarm:8581-8603, 8619-8630, 9276-9280` |
| Hub 侧 fleet schema 与传输选择 | `openbot/src/backend/dispatch/dispatch-service.ts:72-83, 481-487, 500-503` |
| 远端任务卡投递 | `openbot/src/backend/dispatch/dispatch-process.ts:79, 91-95` |
| Hub ↔ 执行主机固定强制命令 | `openswarm/bin/hub-openswarm-bridge:21`；`openswarm/bin/hub-fleet-gateway:13` |
| 连接入口与配置要求 | `integrations/README.md:5, 14-16, 34-35` |
| MiniMax broker（单上游、并发闸门） | `openswarm/bin/pi-minimax-broker.mjs:22, 27, 55, 75` |
| broker 服务 / worker 中继 / 临时桥 / 本机 HTTP 中继 | `openswarm/bin/pi-broker-service:9-15, 34`；`openswarm/bin/pi-worker-proxy:13-14, 32, 44`；`openswarm/bin/pi-fleet-bridge:15-18, 76-81`；`openswarm/bin/pi-local-http-bridge.py:15, 17, 32, 121` |
| 执行沙箱只连 broker socket | `openswarm/bin/pi-isolated:43, 53, 63-67`；`openswarm/docs/executor-recovery.md:9-29` |
| 容量协调器与限额维度 | `openswarm/bin/fleet-capacity:12-16, 72-79, 109, 122-124`；`openswarm/policy.json:258-263`；`openswarm/README.md:690-697` |
| 单出口代理 / 基准断网 | `openswarm/bin/agy-via-local-proxy:66-67`；`openswarm/bench/swebench/harness/arms.py:25-39, 129-138` |
| 已回滚的反向 socket 隧道 | `443f34554`（新增）；`c33d8d4d5`（回滚，删除 `openswarm/docs/fleet-reverse-socket.md`）；已回滚文档原文见 `git show c33d8d4d5^:openswarm/docs/fleet-reverse-socket.md` |
| 未合入的配置扩展 | `45c8ada52`（`origin/brain/fleet-multihost`，不在本分支祖先中） |
| 历史主机清单 | `tools/watch8.sh:3` |
| 控制面自建部署（边界说明） | `openbot/docs/self-hosting.md:1-40`；`openbot/deploy/self-host/README.md:1-40`；`openbot/src/main/private-network-detection.ts:3-17`；`openbot/src/renderer/src/features/team/team-webrtc-private-network.ts:1-45`；`openbot/src/main/host-service.ts:152` |
