# Hub 与执行网络连接

本目录提供 OpenBot Hub 调用 OpenSwarm 的连接入口：

- `hub-openswarm-bridge`：Hub 派发、状态查询和结果取得。
- `hub-fleet-gateway`：执行主机上的派发和结果网关。
- `fleet-result-bundle`：将远端任务提交交回本机审查。

Hub 的自动派发调用 `openswarm auto`，每次派发都会重新读取本地与远端的
lane 状态。路由按 backend 选择空闲 lane，而不是依赖固定 lane 名称，因此扩容时
新增的 MiniMax、Antigravity 或 Cursor lane 可被发现。协调器的全局预约仍是最终
准入点；新增 lane 同时必须被网关允许并接入同一个协调器，不能只编辑 lane 配置。

入口指向 `openswarm/bin/` 中同名实现，以兼容现有部署路径，避免维护两份代码。Hub 的准入调用仍在 `openbot/src/backend/`，OpenSwarm 的路由和并发协调仍在 `openswarm/`。这些入口尚需随 Phanes 版本部署并完成端到端验收。
