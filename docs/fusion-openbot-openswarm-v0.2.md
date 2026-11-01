# 融合方案定稿 v0.2：OpenBot 控制面 × OpenSwarm 执行

一句话架构：OpenBot Cloud 做协商、选人、记忆与开卡，openswarm 做合约与执行的唯一主人，nibiru 资产收编进 OpenBot 控制面，执行一律走 openswarm。

## 1. 唯一主人与硬约束

- openswarm 是合约与执行语义的唯一主人：lane 状态机、并发与额度、收割、
  薄卡门、review 与 merge 门、external check 与 tick，全部归 openswarm。
- OpenBot Cloud 只做协商、选人、记忆与开卡，不得再长出第二套调度器，
  不得出现平行调度器与 openswarm dispatcher 争执行权。
- `autodispatch`（AD）只剩启动器：现有 `autodispatch.py` 禁止加新功能；
  新能力先进 openswarm 上游 PR，再让 AD 变薄。本卡不碰 AD。
- 事实基线以 2026-11-02 为准：Instabox 已删；控制面在 box；远端主机为
  gores1、gores2、govpn、gorelay、gocc；配比 MiniMax 50%、Codex 30%、
  agy 10%、Copilot 10%（Copilot 仅远程、超额为 0）。
- 证据大于自报：拆卡须含四要素，派单前做名册与 duty 校验，还价只落卡上。
- 控制面与执行面的状态以 openswarm 与 GitHub 上的合约状态为准；
  控制面的视图只是投影，冲突时重建投影，不得反向覆盖合约状态。
- builder 在 box 与控制面做的一切须可完整复制、无本地隐状态：
  配置进 git 或白名单备份仓，启动靠声明式脚本，不依赖不可迁移的一次性状态；
  移交 external-muse 后同样遵守。

## 2. 边界表（boundary）

冲突时以 openswarm 为准；控制面只能发起请求，不能越权执行。

| 字段 / 动作 | 唯一写者 | 控制面（OpenBot Cloud / external-muse）的权限 |
|---|---|---|
| issue 创建与初始标签 `lane-ready`、`backend:<name>`、优先级 | 派活方（移交后为 external-muse） | 写：这是开卡，不是状态推进 |
| `lane-claimed` / `lane-pr` / `lane-failed` 状态迁移 | 认领 lane 本人（经合约工具）加 openswarm dispatcher 对账 | 仅对自己认领的卡按合约迁移，不得替别人迁移 |
| `in-progress`、收割、超时回收、requeue | openswarm dispatcher | 只读 |
| Run / Attempt 状态与结果 | openswarm lane supervisor / dispatcher | 只读；可引用为证据 |
| 并发名额、额度、host 白名单、lane pause | openswarm（部署配置与 dispatcher） | 不可写；只能经卡的优先级与 backend 钉选提建议 |
| stop 一个 Run | 执行侧 supervisor 执行 | 控制面与 builder 均可发起 stop 请求，走 openswarm 既有 stop / pause 语义；不得绕过 supervisor 直接杀进程 |
| review / merge | fleet review lane 与 merge 门 | 无权；不自审，不合自己的 PR |
| 协商记录、选人理由、记忆 | 控制面 | 写自己的投影与记忆，不回写合约状态 |

## 3. P0 出口清单

- [ ] external-lane 公共池常备至少 1 张 `backend:external` 卡，各 external 名有 inbox
- [ ] #1431 dispatch / review 落地
- [ ] #1464 sandbox-tmp 落地
- [ ] 统一发版：同一 phanes 提交部署到 box 与各远端，逐机留版本记录
- [ ] 故障名额回收经 kill 实测
- [ ] box 重启后 `boot-ensure` / `start-control-plane` 自恢复实测
- [ ] 改写 `docs/fleet-topology-expected.md` 为 2026-11-02 基线，去 ib1–ib5 限额表
- [ ] 拓扑文档更新：gores1、gores2、govpn、gorelay、gocc 为当前主机清单
- [ ] P0 出口不要再对着旧 PR #14 / #2 / #3 清单原样开工

## 4. 控制面宕机映射

- 已 claim 的 lane：继续跑完当前 Run，不冻结、不回收；lane 执行由
  supervisor 自治，证据照常落 GitHub；控制面恢复后按 openswarm 状态对账重建投影。
- claim 超时仍由 dispatcher 按合约计时，不因控制面宕机暂停或延长。
- 新卡 mint：停止。控制面宕机等同派活方消失，语义等同 dispatcher 侧的
  PAUSE-MINT（在场时 replenish 跳过新卡生成）：控制面不新建卡；
  若宕机超过 30 分钟，builder 可在 box 侧置 PAUSE-MINT 阻止 starter 池自动补卡。
- 新 claim 分开看：fleet 内部卡由 box 上 dispatcher 管辖，独立于 OpenBot Cloud
  存活，照常；external 卡的 claim 发生在 GitHub 侧，也照常。
  宕机只停控制面发起的新工作，不停执行面自治范围。
- host pause 映射：单 host 故障或维护走 `paused_lane` / drain，只停该 host
  接新 Run，不影响其他 host，也不冻结该 host 上已在跑的 Run 到自然结束
  （除非 supervisor 判定危险）。与全局 PAUSE-MINT 的区别：一个停某台接单，
  一个停新生卡；host pause 不是全局停摆。

## 5. 移交终点

- 移交选项为 (c)：builder 退出日常派活，只保留红线否决；日常开卡、
  贴 `lane-ready`、选 backend、盯超时与返工由 external-muse 负责；
  PR 合并仍走 fleet review / merge 门，谁都不自合。
- 移交终点：定稿加首批拆卡后，日常派活与场景推进归 external-muse；
  builder 仅红线否决。整套系统须完全脱离 phanes builder 也能运行：
  openswarm 加 AD 启动器加 external-muse 日常开卡加 fleet review / merge，
  box 仍作计算资源，不走 Grok Bot。
- 红线否决五类：安全、凭据、许可证、破坏性动作、合约被绕过；
  否决必须落卡说明理由。builder 另保留 openswarm 合约所有权、
  禁止 AD 加功能、薄卡门、merge 前 lane review、PolyForm 核心可替换。
- 第一周（2026-11-02 – 11-08）：external-muse 以 external lane 为入口实跑
  派活闭环，至少独立开 3 张合规卡（四要素齐全）并跟到验收关单；
  builder 只做 review 与红线监督，不代开卡。出口：3 张卡证据齐全，
  无一张因拆卡质量被打回。
- 第二周（2026-11-09 – 11-16）：派活范围扩到 fleet 日常卡，
  external-muse 每日维护 ready 池水位与超时清单；builder 日常派活动作归零
  （紧急止损除外）。出口：连续 7 天无 builder 代派记录、ready 池无卡因缺
  验收或依赖误入。若任一周出口未达，移交顺延一周，不自动扩大权限。
- 移交完成后状态写回定稿文档与 HANDOFF。

## 6. Q5 已定条款

- 私密 bus 仓：私密协调与私密卡走新建私有 bus 仓，公开仓只放可公开内容。
- 自托管 Hub：Hub 自托管（box 或自有云主机），不依赖第三方沙箱作控制面落点。
- 9910 退役：9910 在 P4 协商能力上线、且事件加轮询双通道经实测可替代其
  唤醒职能后退役；退役前只保留常驻成员实时通道，不再新增接入。
- 公约瘦身：公约瘦身为一页机器可校验的授权策略（身份、授权来源、
  安全硬规则、保留人工确认项），其余治理内容降为知识库材料。
- 许可证：OpenBot PolyForm Noncommercial 是硬风险，定稿明确走核心模块
  可替换加 OpenBot 作壳：协商、角色、记忆等核心模块独立于 OpenBot 本体、
  可整体替换，商业叙事不绑死 PolyForm；P0–P2 不因此阻塞。
- 待补缺口：除 SSH 外增加 websocket / stream 派活（不稳定或无公网 IP 场景），
  该缺口另卡跟进，不阻塞本定稿 v0.2 的生效。

AGREED v0.2
