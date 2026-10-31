# Hub 与执行网络连接

本目录提供 OpenBot Hub 调用 OpenSwarm 的连接入口：

- `hub-openswarm-bridge`：Hub 派发、状态查询和结果取得。
- `hub-fleet-gateway`：执行主机上的派发和结果网关。
- `fleet-result-bundle`：将远端任务提交交回本机审查。

Hub 的自动派发调用 `openswarm auto`，每次派发都会重新读取本地与远端的
lane 状态。路由按 backend 选择空闲 lane，而不是依赖固定 lane 名称，因此扩容时
新增的 MiniMax、Antigravity 或 Cursor lane 可被发现。协调器的全局预约仍是最终
准入点；新增 lane 同时必须被网关允许并接入同一个协调器，不能只编辑 lane 配置。

额外的受信任执行主机通过 `OPENSWARM_AUTO_REMOTE_SOURCES` 配置其逻辑主机名和
既有 SSH alias。这个列表同时用于派发、状态查询、停止和结果 bundle 回传，避免任务
完成后因只认识旧主机而无法审查或回收。该环境变量只能由服务配置设置；任务卡不包含
主机或 SSH 参数。

若 Hub 所在主机只能到达 `gocc`，而 `gocc` 才能到达新执行主机，额外 source 应设为
`{"host":"govpn","ssh_alias":"openswarm-gocc-fleet-relay","via":"gocc"}`（同理
适用于 `govpn2`）。`openswarm-gocc-fleet-relay` 是已有 Hub→gocc SSH 认证下的受限命令，
执行 `hub-fleet-relay`；后者仅按 gocc 服务配置
`OPENSWARM_FLEET_RELAY_TARGETS` 中的固定 host/alias 映射转发 `lanes`、`assign`、
`run_info`、`bundle`、`stop`。这不是常驻代理，也不新增凭据。部署必须先验证该 SSH
入口实际存在，以及远端 runner 已接入同一容量协调器；relay 本身不构成全局并发限制。

入口指向 `openswarm/bin/` 中同名实现，以兼容现有部署路径，避免维护两份代码。Hub 的准入调用仍在 `openbot/src/backend/`，OpenSwarm 的路由和并发协调仍在 `openswarm/`。这些入口尚需随 Phanes 版本部署并完成端到端验收。
