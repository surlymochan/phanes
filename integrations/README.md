# Hub 与执行网络连接

本目录提供 OpenBot Hub 调用 OpenSwarm 的连接入口：

- `hub-openswarm-bridge`：Hub 派发、状态查询和结果取得。
- `hub-fleet-gateway`：执行主机上的派发和结果网关。
- `fleet-result-bundle`：将远端任务提交交回本机审查。
- `openswarm-fleet-bundle-adapter`：为尚未内置 `_bundle` 的既有执行器补充目标主机 bundle 命令。

Hub 本机持有 lane 时，不需要额外 gateway、socket 或 relay。启用已有的
OpenSwarm fleet 清单即可：

```text
OPENSWARM_HUB_LOCAL_ROUTER=1
OPENSWARM_AUTO_LOCAL_HOST=local
OPENSWARM_AUTO_FLEET_HOSTS=["gores1","gocc"]
OPENSWARM_AUTO_CLI=/absolute/path/to/openswarm-fleet
OPENSWARM_AUTO_HOST_BACKENDS={"local":[],"gores1":["pi"],"gocc":["agy","copilot"]}
```

`OPENSWARM_AUTO_HOST_BACKENDS` 是管理员部署时设置的主机后端白名单。它避免旧
lane 残留或同名 lane（例如远端 K1）混入 4:2:2 布局；任务卡本身不能指定主机。
`local` 不参与这个 4:2:2 方案；gores1 的 M1–M4 与 gocc 的 A1/A2、GH1/GH2
每次派发都会经
`fleet exec ... lane status` 重新发现；派发、状态查询、停止和结果 bundle
同样使用已配置 fleet 的直接 SSH 传输。lane supervisor 的共享 guard 仍是最终
准入点。手工任务卡不能指定 host 或 SSH 参数。未列入这个配置的历史回执继续走
旧 gateway，避免丢失既有结果查询能力。

当前正式拓扑中，Hub 是本机运行的 OpenBot.app 与本机 bridge；在这项直连
fleet 配置部署前，旧 `auto` 仍由 gores1 执行，任务回执因而会显示 gores1。
不要把这个历史执行路径表述为远端 Hub。

`hub-openswarm-bridge` 在派发和取回结果时都要读取
`~/.claude/openswarm-hub-bridge/config.json`。这是 Hub 本机派发的必需配置：
文件缺失或字段不全时，派发在写入任何交接记录之前就失败（直连 fleet 和旧
gateway 两条路径相同），bridge 只返回 `{"ok": false, "error": ...}`。示例只
含占位路径，按本机实际部署替换；该文件不保存任何凭据：

```json
{
  "workspace": "/path/to/hub-workspace",
  "repo": "/path/to/mapped-repository"
}
```

- `workspace`：允许派发的工作目录。bridge 只接受 `--cwd` 解析后与该字段相同
  的目录，其他目录报 `Hub workspace has no approved repository mapping`。Hub
  自身的工作目录不是仓库，可以与 `repo` 不同。
- `repo`：映射到本次任务的仓库检出目录。派发前它必须是干净的 git 检出
  （`git status --porcelain` 为空），其当前 HEAD 就是任务的基线提交；远端
  结果也 fetch 到该仓库的 `refs/openswarm/<run-id>`，供本机审查。

两个字段都写绝对路径；相对路径按进程工作目录解析，不要依赖。同一目录下的
`dispatch.sqlite` 是 bridge 的交接日志（目录 0700、数据库 0600），不要手工
删除或改写。

若某个既有执行器 runtime 还没有 `fleet _bundle`，无需覆盖它。只在该执行器
runtime 的 `bin/` 同级放置 `openswarm-fleet-bundle-adapter` 和
`fleet-result-bundle`，并把 Hub 本机 `fleet.json` 中该 host 的 `openswarm`
改为 adapter 路径。adapter 仅处理 `fleet _bundle <run-id>`：先调用相邻的
旧 `openswarm-fleet fleet _run-info`，再在目标主机生成 bundle；其余命令原样
委托旧 runtime。它不是常驻服务，也不携带凭据。

入口指向 `openswarm/bin/` 中同名实现，以兼容现有部署路径，避免维护两份代码。Hub 的准入调用仍在 `openbot/src/backend/`，OpenSwarm 的路由和并发协调仍在 `openswarm/`。这些入口尚需随 Phanes 版本部署并完成端到端验收。

### 变更类远端任务的结果验收

对已完成的变更类（mutating）远端任务，操作员按以下步骤确认结果已交回本机映射仓库：

1. 通过正式 Hub bridge 查询任务（不要直接 SSH 到执行主机）：

   ```bash
   hub-openswarm-bridge fleet _run-info <run-id>
   ```

2. 确认返回为终态，且 `result.branch` 为 `openswarm/<run-id>`、`result.commit` 为非空提交哈希。
3. 在本机映射仓库中检查 bridge 已取回的引用：

   ```bash
   git -C <repo> show --stat refs/openswarm/<run-id>
   ```

   其提交哈希应与第 2 步的 `result.commit` 一致（可用 `git -C <repo> rev-parse refs/openswarm/<run-id>` 核对）。

只读任务不产生提交，因此没有 bundle，也没有 `refs/openswarm/<run-id>`；对只读任务不要按上述步骤判定失败。

以上验收只确认任务状态与本机引用，不验证 bundle 传输本身。
