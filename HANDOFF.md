# Phanes 交接

最后核实：2026-10-31。本文件是证据索引，不是发布声明；`done`、单项 CI、lane idle 或
HTTP 200 都不能单独构成产品验收。

## 当前结论

- main 已合 #14/#27/#29/#31/#35/#36，当前为
  `df02c552d0b3d2b16d71b60c0b18dd39b71ea2ad`。
- #35（Pi broker 正常 peer-close 竞态）及 #36（不确定 acquire / 已确认可释放 token 的恢复）
  代码和 CI 已通过；12 个目标 lane 已逐槽切到 `df02c552`。运行中的 Hub App 是旧版、构建
  commit 未知；`f853` 仅对应未部署候选 App。
- 五类、六个指定模型均在 `f853` 环境有真实任务终态证据，但它们是连通性/小任务证据，
  **不是**同轮八路或生产闭环验收。
- Cursor 首轮顶层 `partial` 已纠正，后续重跑 `done`；两端 bundle 已回传本机 ref。
- `df02` 逐槽服务滚动切换后的三主机任务均终态 done、预约均已释放；尚未重启任一主机或 Hub App。
  本机 Cursor 全量 23 项中 1 项失败（定向 2 项通过），故 df02 重验、4 MiniMax + 2 Antigravity +
  2 非-fast Cursor 同轮八路验收与 Hub App 替换仍未完成。

## 必须遵守的规则

- 先完成 Hub → OpenSwarm → 执行器闭环；功能验收前禁止新增安全架构或复杂代理。
- 每项任务使用独立工作树；审查任务检查并丢弃修改，禁止自动提交/合入。这不是系统级只读。
- 凭据不明文保存或写日志。正式任务走共享 guard；失败后仅在已确认执行树停止，或已有
  精确、已确认可释放的 reservation token 时释放。
- 不停止活跃或未知 lane；不以配置、空闲状态或不同轮次结果拼接为验收成功。
- 现阶段不得调用 Claude / ccargue。FileVault、登录钥匙串与登录前启动的限制应如实保留，
  不为无人值守降低保护。

## 目标模型与真实任务证据

| 执行器 / 模型 | 真实证据 | 当前结论 |
|---|---|---|
| Pi / `MiniMax-M3.1-Flash` | `20261031-211845-e428c3` done（26.454s）；此前 `20261031-210315-3d250a` budget_exceeded | 可用小任务；长任务/四路负载未通过。 |
| Antigravity / `gemini-3.8-flash-medium` | gocc A1 `20261031-210619-58ae2b` done | 单任务通过；历史 403 不可据此宣称根治。 |
| Cursor / `composer-2.5[fast=false]` | `20261031-210813-19c86f` 顶层 partial（fleet_auto 23 项中 1 项失败）；重跑 `20261031-212617-ada91f` done | 重跑连通性通过；首次失败仍是验收记录，非全 fleet 通过。 |
| Codex / `gpt-6-luna` high | gocc C1 `20261031-210712-41fc99` done | 单任务通过；Hub 自身全生命周期/重启仍未验收。 |
| Copilot / `claude-haiku-5.5` medium | gocc GH1 `20261031-211146-53d3ab` done；另有 `20261031-210631-e2ab15` partial | 常规模型有 done；partial 不得抹去。 |
| Copilot / `claude-sonnet-5.5` medium | gocc GH2 `20261031-210625-76cbe8` done | 困难模型有 done；不替代八路验收。 |

Antigravity 已切回指定账号；这解释了已确认的账号错配，但不足以证明所有历史/自然 403 的
唯一根因或完全消除。无 guard 的候选和直调 CLI 不计入容量。

## 路由、bundle 与并发证据

- Hub/fleet 最小路由来自 #27；当前生产 Hub/lanes 未部署最新 main，故只能称代码通过、部署未验。
- gocc → 本机 ref：`refs/openswarm/20261031-211320-be6f7e` =
  `bd4547d5aa940202b6b960274261048ed3cdee02`（顶层 partial，改动未合入）。
- gores1 → 本机 ref：`refs/openswarm/20261031-212156-cf2eb4` =
  `c5ea7aa962f39fd868984b213e80d96797cd3fd7`（Pi done；仅 `openswarm/README.md` 7 行，未合入）。
- 两条 ref 证明两执行主机的 bundle→本机审查输入路径；不证明 Hub 自动审查/合并闭环。
- 历史同窗曾观察到 Pi4 + AGY2 + Cursor2 预约，随后 Pi 503 或任务 partial；另一自然轮虽全 done
  但仅观察到 6 路。不得拼成“八路通过”。目前也没有基于最新 main 的同轮 4+2+2 证据。

## 异常回收证据与边界

- #29 已在 `f853`；#36 已合 `df02`。#36 仅重试两类精确 token：从未记录 executor 的 acquire
  回包丢失，或既有安全调用点确认的 release pending；不会因 terminal 状态杀 resident child。
- Pi 路径已独立确认两个 df02 回收缺陷，capacity 正在修复；作为 [#3：预约回收](https://github.com/surlymochan/phanes/issues/3)
  的未解决事项，修复、定向回归及最新现场复验完成前，不得将 #36 回收语义标为最终通过。
- 旧 `kill -9` probe `20261031-213156-b8da94` 的后代仅 `sleep 120`，在 TTL 前已自行退出；
  它只证明“执行树已停止后 TTL 回收”，不应单独作为活后代终止证据。
- 后续受控 `sleep 900` 真实 kill-9 probe 已通过：同任务、已记录 identity/PGID 的后代跨越 TTL
  存活时预约保持；既有 reaper 停止该任务树后才释放准确预约，resident lane 未受影响。它覆盖
  活后代回收路径；仍须在 `df02` 逐槽服务重验中再次确认。
- 受控 rc=17 fixture `20261031-214327-330b2f`：无后代、gores1→gocc guard、0.075s 后
  `failed/exec_error` 且预约为空。fixture 已删除；它只覆盖正常非零退出立即释放。
- rc=17 与两次 kill-9 probe 都是受控 standalone/non-resident 路径；不替代常驻 lane 的完整任务
  生命周期验收，也不授权手工回收未知进程。

## 主机、部署与 App

- 正式 12 槽：gocc A1/A2/C1/GH1/GH2、gores1 M1–M4/K1、local K1/K2 均已逐槽切到 `df02`；
  M3/M4 本机候选已停止且不计入容量。此前完整五类/六模型验收发生于 `f853`；`df02` 逐槽服务
  滚动切换后已完成三主机真实任务且预约归零，但本机 Cursor 全量 23 项有 1 项失败（仅定向 2 项
  通过），尚未重启任一主机或 Hub App，不能称最新版验收通过。
- OpenBot.app 的运行中构建 SHA 未知。已从 `f853` 构建**未部署**候选并通过包检查；候选是
  ad-hoc 签名，现网为开发签名，Keychain/TCC 与登录状态连续性未证明。不得替换现网 app。
- 有 Hub/服务重启及个别主机重启的历史证据；本次 `df02` 部署尚未重启任何主机或 Hub App。#35/#36
  最新部署后的主机重启和六模型真实任务矩阵均未完成，统一标记未验证。

## 下一步（按此顺序）

1. 修复/解释本机 Cursor 全量 23 项的 1 个失败，重跑全量；定向 2 项通过不得替代它。完成 [#3](https://github.com/surlymochan/phanes/issues/3)
   中已确认的两个 df02 Pi 回收缺陷修复、定向回归和现场复验。
2. 补齐当前 df02 逐槽服务重验的证据账本（主机、lane、commit、模型、guard、命令、结果与回滚点）；
   保留 `f853` 与旧 app，不替换 app。
3. 在最新 main 做一轮同窗 4 Pi + 2 AGY + 2 非-fast Cursor：记录 run ID、路由、模型、预约峰值、
   终态、bundle/ref 与最终零预约；第九项排队/补位也需记录。不能混用轮次。
4. 从正式 Hub 自动派单并自动回传一项远端 bundle；独立审查 diff，但不自动合入任务改动。
5. 做服务重启和三主机重启后的同一矩阵；登录/FileVault 未满足时如实记录边界。

## 跟踪与回滚

- [#1：统一入口](https://github.com/surlymochan/phanes/issues/1)
  · [#2：结果交接](https://github.com/surlymochan/phanes/issues/2)
  · [#3：预约回收](https://github.com/surlymochan/phanes/issues/3)
  · [#5：Hub / fleet 验收](https://github.com/surlymochan/phanes/issues/5)
  · [#6：三 Bot 场景](https://github.com/surlymochan/phanes/issues/6)
- App 回滚点是现运行 `OpenBot.app` 与其 LaunchAgent；lane 回滚点是每条服务的旧脚本/配置。
  只在 idle lane 切换；新版本无法经 guard 获取/释放或无法完成短任务时，仅回滚该 lane 并保留证据。
- Issue 是工作队列而不是验收；操作前重新核对 main、CI、审查、部署版本与现场状态。
