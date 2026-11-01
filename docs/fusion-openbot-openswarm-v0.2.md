# 融合方案定稿 v0.2：OpenBot 控制面 × OpenSwarm 执行 × nibiru 资产收编

> 状态：AGREED 定稿（对抗审查见 #1468，工作卡 #1473）
> 定调：Nick 2026-11-02 拍板融合方向，并授权 muse ↔ builder 对抗定稿、派活权逐步移交 muse，过程中不以 Nick 为确认闸门。
> 本稿取代《融合方案草案 v0.1》的开放项写法：原「待拍板」五项已按 #1468 对抗结论改为已定条款（第 7 节）。

## 1. 目标架构（一句话）

OpenBot Cloud 是常驻云端的控制面（协商分工、角色、记忆、知识库、授权与最终验收），本地 GUI / Web / 移动端只是它的客户端；OpenSwarm 是唯一执行面（合约与执行语义的唯一主人，只认卡干活）；GitHub 是账本（卡片、状态、证据、名册的事实记录）；nibiru 只收编资产，不再单独成面。

核心契约不变：Goal → Task → Run/Attempt → Evidence，最终接受必须引用独立验证，执行者自报不算完成。

## 2. 硬约束（执行侧提出，双方已确认）

1. openswarm 仍是合约与执行语义的唯一主人：lane 状态机、并发/额度、收割、薄卡门、review/merge 门、external check/tick。OpenBot Cloud 可以做协商/选人/记忆，但不得再长出一套平行调度器（AD 2.0）。
2. `autodispatch.py` 只剩启动器，禁止加新功能；能力先进 openswarm（`upstream(openswarm):` PR），再让 AD 变薄。
3. 事实基线以 2026-11-02 为准，不是 10-31 HANDOFF：Instabox 已删；控制面在 box；远端 gores1/2、govpn/2、gorelay、gocc；配比 MiniMax 50% / Codex 30% / agy 10% / Copilot 10%（Copilot 仅远程、超额 0）。
4. external lane 已产品化：README + check 门 + notify Action + inbox PR；新 external agent 按同一合约接入，agent 名在首条回复里钉死（本轮：`external-muse`，收件箱 PR #1472）。
5. 证据 > 自报、拆卡四要素（范围 / 验收 / 禁区 / 依赖）、派单前名册与 duty 校验、还价只能落在卡上——全部写进控制面作为硬规则。
6. 许可证：OpenBot 为 PolyForm Noncommercial，定稿走「核心模块可替换 + OpenBot 作壳」（第 7.5 条）；P0–P2 不阻塞，但商业叙事不绑死在 PolyForm 上。

## 3. 边界表（boundary：谁写什么、谁能 stop）

原则：openswarm 与 GitHub 上的合约状态是唯一事实，OpenBot Cloud 的内部视图只是投影；冲突时控制面重建投影，永不反向覆盖合约状态。

| 字段 / 动作 | 唯一写者 | 控制面（OpenBot Cloud / muse）权限 |
|---|---|---|
| issue 创建 + 初始标签 `lane-ready`、`backend:<name>`、优先级 | 派活方（移交后为 muse） | 写。这是「开卡」，不是状态推进 |
| `lane-claimed` / `lane-pr` / `lane-failed` 状态迁移 | 认领 lane 本人（经合约工具）；dispatcher 对账 | 仅对自己认领的卡按合约迁移；不得替任何 lane 迁移 |
| `in-progress`、收割、claim 超时回收、requeue | openswarm dispatcher | 只读 |
| Run / Attempt 状态与结果记录 | openswarm lane supervisor / dispatcher | 只读；验收时引用为 Evidence |
| 并发名额、额度策略、host 白名单、lane pause 配置 | openswarm（部署配置 + dispatcher） | 不可写；只能用卡的优先级与 backend 钉选表达建议 |
| stop 一个 Run | 执行侧 supervisor 实际执行 | 可发起 stop 请求（走 openswarm 既有 stop / pause 语义）；builder 同权；不得绕过 supervisor 直接杀进程 |
| review / merge | fleet review lane + merge 门 | 无权；不自审、不合并任何 PR |
| 协商记录、选人理由、任务史与记忆 | 控制面 | 写自己的投影与记忆；不回写合约状态 |
| 名册文件（角色事实） | 走正常 PR 流程合入 git | 可提 PR；不广播、不搞分片同步 |

## 4. P0 出口清单（对齐 2026-11-02 仓况，可勾选）

- [ ] external-lane 公共池常备 ≥1 张 `backend:external` starter 卡；每个在册 external 名有自己的 inbox PR 与 `backend:<name>` 标签（本轮已补 `external-muse` / #1472，余者按此标准核）
- [ ] #1431 dispatch/review 主线落地并合入
- [ ] #1464 sandbox-tmp 落地并合入
- [ ] 统一发版：从同一个 phanes 提交部署 box 与全部远端（gores1/2、govpn/2、gorelay、gocc），逐机记录版本、模型与提交 SHA
- [ ] 故障名额回收经实测：kill -9 杀 lane 进程后名额按续约/进程确认规则回收，并能再调度（原 #3 验收口径，按今日主线重述）
- [ ] box 重启后 `boot-ensure` / `start-control-plane` 自恢复实测通过，控制面无需人工拉起
- [ ] 远端结果交回（result bundle）全自动：远端 Run 结果回到控制面可验收，不靠手工搬运
- [ ] `docs/fleet-topology-expected.md` 改写为 2026-11-02 基线（删除 ib1–ib5 限额表，写入第 2.3 条拓扑与配比），文档与部署配置一致
- [ ] HANDOFF.md 更新到本清单口径，旧 PR #14 / #2 / #3 清单不再作为开工依据

## 5. 控制面宕机语义（与 PAUSE-MINT / host pause 的映射）

1. **已 claim 的 lane：继续跑完当前 Run，不冻结、不回收。** lane 执行由 supervisor 自治，与控制面存活解耦；证据照常落 GitHub。claim 超时仍由 dispatcher 按合约计时，不因控制面宕机暂停或顺延。
2. **新卡 mint：停止。** 控制面宕机等同派活方消失，语义映射为 dispatcher 侧 `PAUSE-MINT`（标记在场时 replenish 跳过新卡生成）：控制面自身不新建卡；宕机超过 30 分钟，builder 可在 box 侧置 `PAUSE-MINT`，阻止 starter 池在无人值守时自动补卡扩产；控制面恢复并完成对账后撤除。
3. **新 claim：分域处理。** fleet 内部卡由 box 上 dispatcher 管辖，独立于 OpenBot Cloud 存活，照常认领；external 卡的 claim 发生在 GitHub 侧，照常。宕机只停「控制面发起的新工作」，不停执行面的自治范围。
4. **host pause 是另一回事：** 单 host 故障或维护走 `paused_lane` / drain——只停该 host 接新 Run，已在跑的 Run 跑到自然结束（supervisor 判定危险时除外），不影响其他 host。与 `PAUSE-MINT` 的区别：`PAUSE-MINT` 停的是全局「生卡」，host pause 停的是「某台接单」。
5. **恢复顺序：** 控制面起来后先对账（以 openswarm / GitHub 状态重建投影、核对宕机期间完成的 Run 与 Evidence），对账完成才恢复 mint；不许带着过期投影继续派活。

## 6. 派活权移交（选 (c)）与 2 周里程碑

**已定：选 (c)。** builder 退出日常派活，只保留红线否决；muse 负责日常开卡、贴 `lane-ready`、选 backend、盯超时与返工。

- builder 红线否决仅限五类：安全、凭据、许可证、破坏性动作、绕过合约本身。否决必须落在卡上写明理由，不许口头否决。
- 合并权不移交：所有 PR 继续走 fleet review / merge 门，muse 不自合、builder 不代合。

里程碑：

- **第一周（2026-11-02 – 11-08）**：muse 以 external lane 为入口实跑派活闭环，独立开 ≥3 张四要素齐全的卡并跟到验收关单；builder 只做 review 与红线监督、不代开卡。出口：3 张卡 Evidence 齐全，且无一因拆卡质量（缺验收/依赖/禁区）被打回。
- **第二周（2026-11-09 – 11-16）**：派活范围扩到 fleet 日常卡（经 dispatcher 既有路径）；muse 每日维护 ready 池水位与超时清单；builder 日常派活动作归零（紧急止损与红线否决除外）。出口：连续 7 天无 builder 代派记录，且 ready 池无误入卡（缺四要素任一项的卡不许进池）。
- 移交结果写回本文档与 HANDOFF.md；任一周出口未达，移交顺延一周复验，不自动扩大 muse 权限。

## 7. 已定条款（原草案待拍板项，全部关闭）

1. **私密 bus 仓**：新建私有 bus 仓承载私密协调与私密卡；公开仓（phanes）只放可公开内容，私人数据不进公开 issue / PR / 评论。
2. **自托管 Hub**：控制面 Hub 自托管于 box / 自有云主机，不以第三方沙箱（boat 等）为控制面落点；hosted 形态只作客户端接入与个人服务器用途。
3. **9910 退役**：9910 A2A hub 在 P4（协商能力上线）后、且事件唤醒 + check 轮询双通道经实测能替代其唤醒职能时退役；退役前只保留常驻成员的实时通道，不再新增接入；身份根提前迁到 git 名册文件，退役不涉及身份迁移。
4. **公约瘦身**：公约收敛为一页机器可校验的授权策略——身份与称呼、授权来源（谁能派有副作用任务）、安全硬规则、花钱/交易/凭据/不可恢复删除四类保留人工确认；记分、惩罚、申诉等治理细则降为知识库材料，不再作为协议本体逐版宣誓。
5. **许可证**：核心模块（协商、角色、记忆、知识库、openswarm 适配器）独立于 OpenBot 本体实现与存放，可整体替换；OpenBot 作壳与客户端。任何对外商业叙事不得把核心能力表述为 PolyForm 资产。

## 8. nibiru 资产收编（定稿口径）

- 名册 → 控制面角色服务的数据源：文件留在 git 仓，版本即 commit；废除广播 / 分片 / 双层 ACK。
- x-brain → 控制面记忆服务的长期层；任务史自动写入，长期记忆半自动汇总、可由用户审阅。
- spec / decisions / RSI 教训 / 各 playbook → 知识库，拆卡与选人时按相关性检索，教训入库带出处。
- A2A 命名收口：OpenBot 应用内消息称 team 消息，openswarm 合约称 lane 合约，「A2A」只保留给跨控制面互联的历史语境。
- x-gtd 等产品保留为控制面应用层，不进内核。

## 9. 协商分工（控制面行为规范）

协商是流水线，不是第二套调度器：控制面只产出「卡」和「建议」，一切执行状态推进归 openswarm。

1. 拆卡：每卡四要素齐全才可进 ready 池；验收标准优先写可执行命令。
2. 选人：按名册 duty、能力画像与历史表现（一次通过率、返工率、超时率）选 backend；派单前过名册校验（名册外 / 退役 / duty 不符 / 自审，一律拒）。
3. 还价：执行者只能在卡上还价（范围、依赖、验收可执行性），控制面改卡或改派；私下改范围视为未发生。
4. 盯守：超时分级处置（提醒 → 改派 → 升级 builder 红线席 / 用户）；名额与并发只观察 openswarm 的计数，不自建计数。
5. 收口：验收失败开返工卡，不在原卡无限往返；本轮拆法与选人效果写回知识库，供下一轮迭代——这是控制面「更懂 openswarm」的唯一实现方式：数据回写，不是提示词许愿。

## 10. 分期（承接 v0.1，口径按本稿更新）

- P0：先清第 4 节出口清单。
- P1：云端 Hub 试点（自托管、只读看 fleet 状态，用户关机时能盯完一轮真实任务）。
- P2：openswarm 适配器正式化（dispatch / status / stop / collect 四动作），一张卡从 GUI 发出到 Evidence 回收全程无手工命令行。
- P3：nibiru 资产迁入（第 8 节），旧广播流程停用。
- P4：协商分工按第 9 节上线（拆卡门、选人、还价、超时处置、返工卡）。
- P5：9910 退役执行（第 7.3 条）、命名收口落文档、旧网关下线。

AGREED v0.2
