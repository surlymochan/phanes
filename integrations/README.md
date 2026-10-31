# Hub 与执行网络连接

本目录提供 OpenBot Hub 调用 OpenSwarm 的连接入口：

- `hub-openswarm-bridge`：Hub 派发、状态查询和结果取得。
- `hub-fleet-gateway`：执行主机上的派发和结果网关。
- `fleet-result-bundle`：将远端任务提交交回本机审查。

Hub 本机持有 lane 时，不需要额外 gateway、socket 或 relay。启用已有的
OpenSwarm fleet 清单即可：

```text
OPENSWARM_HUB_LOCAL_ROUTER=1
OPENSWARM_AUTO_LOCAL_HOST=local
OPENSWARM_AUTO_FLEET_HOSTS=["gores1","gocc"]
OPENSWARM_AUTO_CLI=/absolute/path/to/openswarm-fleet
```

`local` 的 K1/K2、gores1 的 M1–M4 与 gocc 的 A1/A2 每次派发都会经
`fleet exec ... lane status` 重新发现；派发、状态查询、停止和结果 bundle
同样使用已配置 fleet 的直接 SSH 传输。lane supervisor 的共享 guard 仍是最终
准入点。手工任务卡不能指定 host 或 SSH 参数。未列入这个配置的历史回执继续走
旧 gateway，避免丢失既有结果查询能力。

当前正式拓扑中，Hub 是本机运行的 OpenBot.app 与本机 bridge；在这项直连
fleet 配置部署前，旧 `auto` 仍由 gores1 执行，任务回执因而会显示 gores1。
不要把这个历史执行路径表述为远端 Hub。

入口指向 `openswarm/bin/` 中同名实现，以兼容现有部署路径，避免维护两份代码。Hub 的准入调用仍在 `openbot/src/backend/`，OpenSwarm 的路由和并发协调仍在 `openswarm/`。这些入口尚需随 Phanes 版本部署并完成端到端验收。
