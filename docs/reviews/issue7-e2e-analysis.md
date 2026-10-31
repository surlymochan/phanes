# #7 queue interrupt 与 backup model E2E 调研

## 测试位置与实现

| 场景 | 测试位置 | fake event / 队列证据 |
| --- | --- | --- |
| 中断当前 turn，排队消息继续等待 | `openbot/scripts/steer-interrupt-pause-e2e.ts:280-295`（Stop and send）；`336-342`（teammate interrupt）；`344-352`（cooldown 时消息留在队列）。这里直接断言被中断 delivery、消息启动顺序及队列状态。 | 文件 `:35-65` 描述并实现 fake Codex：`HOLD` turn 保持运行，`finish` 写 `turn/completed`。fake 的 `turn/interrupt` 分支见 `:102-109`，同步结束 held turn。消息队列与状态由真实 mailbox/agent service 驱动。更完整的故障恢复/Stop 后下一条消息完成断言在 `openbot/scripts/stuck-running-deliveries-e2e.ts:512-537`；fake Codex 的中断拒绝无活动 turn 假设见 `:50-57`，事件由 `:81-92` 的 `finish` 发出。 |
| Backup model failover | `openbot/scripts/backup-model-failover-e2e.ts:635-654`。断言主模型失败后同一消息在备用模型完成、模型切换且不展示错误。 | fake Codex turn start 和事件次序在 `:198-232`：先回复 turn start，再发 `turn/started`；`setImmediate` 后，限额错误时发 `error` (`:219-223`) 并以 `failed` 完成。Provider outage fake Claude 的错误文本定义在 `:52-54`，错误生成器在 `:130`。真实 mailbox/turn lifecycle 及发事件的收集位置可继续从 `:432-463` 查阅。 |
| Team API routes 的对应边界 | `openbot/scripts/team-api-routes-e2e.ts:2603-2616`（interrupt route）；`:1411-1524`（backup model settings routes）。 | 这套测试的 fake `TeamApiAgents` 只记录 interrupt 调用，`:325`；它没有执行 turn 生命周期或队列排空。Electron module stub 在 `openbot/scripts/team-api-routes-e2e-electron.mjs:1-25`，不是 provider fake。它不能替代前两项端到端行为测试。 |

## 对照 #7 描述

1. **“中断后排队消息等待”**：目前 `steer-interrupt-pause-e2e.ts` 的 Stop and send 用例明确验证新消息先运行、先前 queued message 保留并在恢复后运行（`:280-295`）。teammate interrupt 用例验证被中断 turn 后新消息启动（`:336-342`）。`stuck-running-deliveries-e2e.ts:531-536` 还明确验证 Stop 后 queued message 完成。因此“中断会吞掉/清掉排队消息”这一旧假设在这些断言中不存在。fake event 仍模拟 interrupt 后发出 `turn/completed`；这符合用例所需的 provider 事件，不等于假设队列消息应立即启动或被丢弃。
2. **“旧 fake event 假设导致 backup model 用例超时”**：当前 failover fake 明确在 provider error 之后发出失败的 `turn/completed`（`:219-223`），并让普通成功 turn 发出完成事件（`:224-226`）。测试本身等待 send 完成后检查模型序列（`:638-645`）。`:384-392` 的 25ms sleep 是有 20 秒 deadline 的状态轮询间隔，不是对固定事件时序的断言。所以仅凭 #7 摘要不能确认“旧 fake event 假设”仍存在；当前代码看不到“缺少 terminal event”或依赖旧事件形状的证据。仍需用指定命令实际运行复现，才能判断该报告的 timeout 是否仍发生。

## 最小修复与验证

- 若 queue interrupt E2E 在目标路径仍超时，优先只修 fake provider：让 `turn/interrupt` 对正在运行的 turn 发出与协议一致的 terminal completion，并由现有 queue assertions 验证 queued delivery 仍保留/继续。不要改 Team API route fake；它只测参数转发，不模拟队列。
- 若 backup failover E2E 超时，先在 fake Codex/Claude 的 provider-error 路径补齐真实 adapter 等待的 terminal/error event 组合及顺序（参照 `backup-model-failover-e2e.ts:219-226`），并让测试等待有界的实际状态；目前文件已有 error + failed completion，故在确认复现前不建议产品侧改动。
- 建议验证：`cd openbot && bun run test:steer-interrupt-pause`；`cd openbot && bun run test:backup-model-failover`。如目标是 Team API 路由回归，再运行 `cd openbot && bun run test:team-api-routes`（脚本在 `openbot/package.json:287`）。本次只读检查源码，未运行 E2E。
