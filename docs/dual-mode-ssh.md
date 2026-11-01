# Box 上的双模式 SSH

本文档记录 #224 的 box 部署约定。Hub bridge、`openswarm-fleet` 和 SSH
客户端都运行在 box；Mac 不参与派发。下面的路径是示例，部署时替换为 box
上的实际绝对路径。

## Bridge 配置

`hub-openswarm-bridge` 默认读取
`~/.claude/openswarm-hub-bridge/config.json`。文件只保存工作区映射，不保存
密码、token、私钥内容或其他凭据：

```json
{
  "workspace": "/srv/phanes/openbot-workspace",
  "repo": "/srv/phanes/openbot"
}
```

`workspace` 和 `repo` 都必须是绝对路径；`workspace` 是 bridge 接受的
`--cwd`，`repo` 是干净的 git checkout，也是任务基线和结果 bundle 的落点。
不要把 `~`、Mac 路径或个人目录写进该文件。bridge 的交接数据库仍在同一
配置目录中，由 bridge 创建。

## Box SSH 配置

在 box 的 `~/.ssh/openswarm-hub/config` 中使用已经部署的控制面 key
`~/.ssh/id_ed25519_phanes_ctl`。这里只引用 key 路径，不复制或打印 key 内容。
`ControlMaster` 复用同一个控制连接，减少 fleet 查询和 lane assignment 的
重复握手：

```sshconfig
Host ib1 ib2 ib3 ib4 ib5 gocc gorelay govpn govpn2
    IdentityFile ~/.ssh/id_ed25519_phanes_ctl
    IdentitiesOnly yes
    ControlMaster auto
    ControlPersist 10m
    ControlPath ~/.ssh/openswarm-hub/cm-%C
    ServerAliveInterval 15
    ConnectTimeout 10
```

首次部署后只在 box 上验证连通性和远端命令，例如：

```bash
ssh -F ~/.ssh/openswarm-hub/config ib1 -- true
ssh -F ~/.ssh/openswarm-hub/config gocc -- true
```

不要把私钥、密码、known-hosts 内容或 agent socket 放进仓库或 bridge 配置。

box 上 `ib-refresh` 约每 15 分钟续签 instabox 证书，派发机需保持该循环。

## Fleet 主机和十 lane 拓扑

自动路由的主机集合固定为 `ib1`–`ib5`、`gocc`、`gorelay`、`govpn`、
`govpn2`；不加入 `local`、`gores1` 或 copilot。当前十 lane 的容量分配为：

| 主机 | lane 容量 | 允许的 backend |
|---|---:|---|
| ib1 | 2 | pi |
| ib2 | 1 | pi |
| ib3 | 1 | pi |
| ib4 | 1 | pi |
| ib5 | 1 | pi |
| gocc | 1 | agy |
| gorelay | 1 | codex |
| govpn | 1 | codex |
| govpn2 | 1 | codex |

因此总容量是 10，`gocc` 最多 1 个 lane；配置中不得出现 `copilot` 或
`gores1`。在 box 的 bridge/service 环境中设置（JSON 的引号要保留）：

```bash
export OPENSWARM_AUTO_FLEET_HOSTS='["ib1","ib2","ib3","ib4","ib5","gocc","gorelay","govpn","govpn2"]'
export OPENSWARM_AUTO_HOST_BACKENDS='{"local":[],"ib1":["pi"],"ib2":["pi"],"ib3":["pi"],"ib4":["pi"],"ib5":["pi"],"gocc":["agy"],"gorelay":["codex"],"govpn":["codex"],"govpn2":["codex"]}'
```

`OPENSWARM_AUTO_HOST_BACKENDS` 是主机 backend 白名单，不是凭据配置。部署后
以 `openswarm-fleet fleet status --json` 观察实际 lane 名称和状态；若实际
lane 数量不是 10，应先修正 fleet/topology 配置，再启用自动派发。

## 从 box 手动冒烟

以下步骤不经过 Hub，适合验证 SSH、fleet 路由和单个 lane。使用一个临时、
不含凭据的任务卡：

```bash
cd /srv/phanes/openbot-workspace
umask 077
task=$(mktemp /tmp/openswarm-ssh-smoke.XXXXXX.md)
cat >"$task" <<'EOF'
Repository: openbot
Task type: docs
只检查当前 checkout 的版本并返回，不修改文件。
EOF

openswarm-fleet fleet status --json
openswarm-fleet fleet exec ib1 -- lane status
openswarm-fleet fleet exec ib1 -- lane assign M1 --task-file "$task" \
  --title "SSH fleet smoke" --budget-minutes 5
```

若需直接登录目标 host 做同样的检查，先把不含凭据的任务卡放到目标 host，
再执行原生命令（这里的 `M1` 仅为该 host 在 `fleet status` 中显示的 lane
示例）：

```bash
scp -q -F ~/.ssh/openswarm-hub/config "$task" ib1:/tmp/openswarm-ssh-smoke.md
ssh -F ~/.ssh/openswarm-hub/config ib1 -- \
  openswarm-fleet lane assign M1 --task-file /tmp/openswarm-ssh-smoke.md \
  --title "SSH fleet smoke" --budget-minutes 5
```

`lane assign` 返回 run id 后，用同一 host 查询并等待结果：

```bash
openswarm-fleet fleet exec ib1 -- runs --active --json
openswarm-fleet fleet exec ib1 -- lane status
```

也可针对当前空闲 lane，把上例中的 `M1` 替换为 `fleet status` 显示的 lane
名称；不要猜测 lane 名称或把 host 写进任务卡。冒烟完成后确认 run 已结束、
lane 回到 `idle`，再删除临时卡：

```bash
rm -f -- "$task"
```

这个手动流程只验证 box→SSH→fleet→lane assign；Hub bridge 的端到端验收还应
另行用已配置的绝对 `workspace`/`repo` 执行一次 detached dispatch。

## Harvest 的 fetch 失败计数

`openswarm-fleet fleet harvest` 从各 fleet host 收回 lane 结果时，每次远端 fetch 最多
尝试 **3 次**（初次 + 2 次重试），间隔 20 秒；只有 `Connection closed`、`broken pipe`、
banner 超时、`bad pack header` 等 SSH/git 瞬时错误才会重试（与 autodispatch harvest
同一分类），非瞬时错误直接判失败（退避约定见
[`docs/fleet-coordinator-ssh-backoff.md`](fleet-coordinator-ssh-backoff.md)）。

有界重试结束后，失败的 fetch 次数会写成 `fetch_fails`，不再只留在日志里：

- harvest 清单（`--json`）里每个 `completed[]` / `failed[]` 条目都带 `fetch_fails`；
- 清单顶层的 `fetch_fails` 是本次 harvest 全部远端 fetch 的失败次数合计；
- 收回的结果 bundle（`<into>/<host>/<run-id>.json`）顶层同样带 `fetch_fails`。

健康巡检读这个字段即可判断 SSH 是否在抖动：

```bash
openswarm-fleet fleet harvest --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["fetch_fails"])'
grep -h '"fetch_fails"' ~/.claude/openswarm-executor-candidate/harvest/*/*.json
```

`fetch_fails` 为 0 表示远端结果一次就抓回来了；大于 0 表示该 host 的 SSH 抓取发生过
瞬时失败；等于尝试上限（3）表示有界重试已经放弃，必须人工确认该 lane 的结果是否已收回。

## BatchMode 探测与 hosts_ok 汇总契约 (Refs #954)

fleet 探活（probe）与容量汇总统一使用 `ssh -o BatchMode=yes` 探测受控 host。
探测开关在 `ib-refresh`（生产汇总路径）：fleet JSON 里写
`"ssh_probe": {"enabled": true, "ssh_config": "~/.ssh/openswarm-hub/config"}`，
或命令行 `--ssh-probe --ssh-config ~/.ssh/openswarm-hub/config`。

若探测非零退出（含 `Permission denied` / `Connection closed` / 超时），该 host 记为
`probe_status: failed`，并且**不计入** `hosts_ok`；只有退出码 0 才 +1 `hosts_ok`。
离线契约断言见 `tools/check-fleet-batchmode-probe.py`（用假探测输出驱动同一条生产路径，
勿连真实 instabox）。
