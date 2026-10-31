# 远端执行器接入 gocc 协调器

`govpn` 和 `govpn2` 的 lane 通过 gocc 已有的 SSH 主动连接接入共享
协调器。远端机器不需要保存到 gocc 的反向登录凭据。

每个远端目标有两部分：

1. gocc 在本机启动一个固定身份的 socket 服务。例如 `govpn` 使用：

   ```sh
   fleet-capacity-stdio --caller govpn \
     --socket "$HOME/.claude/openswarm-fleet-capacity/govpn.sock"
   ```

2. 同一台 gocc 运行现有 SSH 身份下的反向 Unix socket 转发：

   ```sh
   fleet-capacity-reverse-tunnel --host govpn \
     --local-socket "$HOME/.claude/openswarm-fleet-capacity/govpn.sock" \
     --remote-socket /root/.cache/openswarm-fleet/coordinator.sock
   ```

   `govpn2` 使用独立的本地与远端 socket 路径，并将 `--caller` 改为
   `govpn2`。外层既有 launchd 或 systemd 保持进程；SSH 断开时该命令
   退出，让外层按已有策略重启。

远端的 `/etc/openswarm-fleet-capacity.json` 使用 socket client 作为原有
guard command，例如：

```json
{
  "command": [
    "/root/tools/openswarm-executor-candidate/bin/fleet-capacity-socket-client",
    "--socket",
    "/root/.cache/openswarm-fleet/coordinator.sock"
  ]
}
```

远端 lane 的 `OPENSWARM_FLEET_GUARD_REQUIRED=1` 保持开启。socket 或 SSH
转发不可用时，client 以非零状态返回 JSON 错误；已有 lane admission 会将
任务保留为 queued，不启动执行器，也不会自动释放不确定的已有预约。

目标服务设置 `OPENSWARM_EXECUTOR_HOST=govpn` 或 `govpn2`，并设置
`OPENSWARM_EXECUTOR_BIN` 为正式 candidate 的 `openswarm-fleet` 路径；调用方
身份由 gocc 的 SSH 强制命令固定。任务输入不能指定这些字段。
