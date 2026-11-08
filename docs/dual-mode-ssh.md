# Box 上的双模式 SSH

本文档记录 #224 的 box 部署约定。Hub bridge、`swrm-fleet` 和 SSH
客户端都运行在 box；Mac 不参与派发。下面的路径是示例，部署时替换为 box
上的实际绝对路径。

## Bridge 配置

`hub-swrm-bridge` 默认读取
`~/.claude/swrm-hub-bridge/config.json`。文件只保存工作区映射，不保存
密码、token、私钥内容或其他凭据：

```json
{
  "workspace": "/srv/phanes/volto-workspace",
  "repo": "/srv/phanes/volto"
}
```

`workspace` 和 `repo` 都必须是绝对路径；`workspace` 是 bridge 接受的
`--cwd`，`repo` 是干净的 git checkout，也是任务基线和结果 bundle 的落点。
不要把 `~`、Mac 路径或个人目录写进该文件。bridge 的交接数据库仍在同一
配置目录中，由 bridge 创建。

## Box SSH 配置

在 box 的 `~/.ssh/swrm-hub/config` 中使用已经部署的控制面 key
`~/.ssh/id_ed25519_fleet_operator`。这里只引用 key 路径，不复制或打印 key 内容。
`ControlMaster` 复用同一个控制连接，减少 fleet 查询和 lane assignment 的
重复握手：

```sshconfig
Host worker gores2 worker-a.example.invalid relay.example.invalid broker
    IdentityFile ~/.ssh/id_ed25519_fleet_operator
    IdentitiesOnly yes
    ControlMaster auto
    ControlPersist 10m
    ControlPath ~/.ssh/swrm-hub/cm-%C
    ServerAliveInterval 15
    ConnectTimeout 10
```

首次部署后只在 box 上验证连通性和远端命令，例如：

```bash
ssh -F ~/.ssh/swrm-hub/config worker -- true
ssh -F ~/.ssh/swrm-hub/config broker -- true
```

不要把私钥、密码、known-hosts 内容或 agent socket 放进仓库或 bridge 配置。

Instabox fleet 及其证书续签循环随 2026-11-02 基线一起退役，派发机不再
需要维持该循环。

## 跳板 / ProxyCommand 拓扑

跳板拓扑的事实来源是 box 端真正生效的 ssh config：AD 用 `~/.ssh/config`
（AD 的 `ssh` 不带 `-F`，见 `tools/lane-autodispatch/autodispatch.py`），
`swrm-fleet` 用上面那段 `~/.ssh/swrm-hub/config`。仓库里能核对到的
只有这几点：

- 受控 fleet 主机集合固定为 `worker`、`gores2`、`worker-a.example.invalid`、
  `relay.example.invalid`、`broker`
  （`docs/fleet-topology-expected.md:15-17`）。`bastion` **不是** fleet 主机，
  在本仓库里只作为引用回归样例出现（`tools/check-ssh-opts-quote.py:18`、
  `swrm/tests/test_autodispatch.py:17`），不是可核对的跳板拓扑。
- 上面那段示例 ssh config 对这 5 台主机只配了 `IdentityFile` 与 ControlMaster，
  没有 `ProxyJump` / `ProxyCommand`；真实跳板链以 box 上实际生效的 ssh config 为准。
- 方向：coordinator 部署在 `broker`，是 `relay.example.invalid` 通过反向隧道连 `broker`
  （relay.example.invalid→broker，见 `docs/fleet-coordinator-ssh-backoff.md:21-22`）；
  `broker` 并不是“经 relay.example.invalid 跳板”的机器。

因此 **AD 代码里不写死任何跳板主机名**：

- 只有运行配置显式给出跳板时，AD 才注入 `ProxyCommand`：来源是
  `$AD_JUMP_HOSTS`（JSON 对象）或 `~/phanes-ctl/autodispatch/jump-hosts.json`
  （JSON 对象 `{"<host>": "<ProxyCommand>"}`）；默认空表 → 完全依赖 box 端
  ssh config 的 `ProxyJump`。
- 优先级提醒：命令行上的 `-o ProxyCommand=` 会先于 config 里的 `ProxyJump`
  生效（OpenSSH 把 `ProxyJump` 展开成 `ProxyCommand`，显式给出的优先）。所以
  表里的条目会覆盖 box 端既有跳板路由，改表前先核对 box 上的 `~/.ssh/config`。

值的写法（示例仅示意格式，取值以 box 上实际 ssh config 为准）：

```json
{
  "broker": "ssh -o BatchMode=yes -o ConnectTimeout=20 -W %h:%p <跳板别名>"
}
```

值可以只写跳板别名（AD 会展开成 `ssh -o BatchMode=yes -o ConnectTimeout=20 -W %h:%p <别名>`），
也可以写完整的 ProxyCommand 命令串。内层 ssh 只继承 `BatchMode` / `ConnectTimeout`
（不继承外层的 ControlMaster），目的是首次连接不弹 host key / 口令提示而挂到外层超时。
两种写法都含空格，写进 `core.sshCommand` 时必须经 `shlex.quote` 引用，否则 git 解析
命令串时会把 `ProxyCommand` 拆碎、让 `ssh` 打印 usage（Refs #907，校验脚本
`python3 tools/check-ad-ssh-command.py`）。表里只放跳板路由，不要放凭据（key 路径由
ssh config 的 `IdentityFile` 负责）。

## Fleet 主机和十 lane 拓扑（2026-11-02 基线）

事实基线以 2026-11-02 为准：Instabox 主机已退役，自动路由的远端主机集合
固定为 `worker`、`gores2`、`worker-a.example.invalid`、
`relay.example.invalid`、`broker`；控制面在 **box**，box 是本机，不加入受控
fleet host。当前十 lane 的容量分配为：

| 主机 | lane 容量 | 允许的 backend |
|---|---:|---|
| worker | 3 | pi |
| gores2 | 2 | pi |
| worker-a.example.invalid | 2 | codex |
| relay.example.invalid | 2 | codex |
| broker | 1 | agy |

因此总容量是 10，`broker` 最多 1 个 lane；配置中不得出现 `copilot` 或
box 本机 lane backend。限额与 backend 配比的机器可读约定见
[`docs/fleet-topology-expected.md`](fleet-topology-expected.md)。在 box 的
bridge/service 环境中设置（JSON 的引号要保留）：

```bash
export OPENSWARM_AUTO_LOCAL_HOST=box
export OPENSWARM_AUTO_FLEET_HOSTS='["worker","gores2","worker-a.example.invalid","relay.example.invalid","broker"]'
export OPENSWARM_AUTO_HOST_BACKENDS='{"box":[],"worker":["pi"],"gores2":["pi"],"worker-a.example.invalid":["codex"],"relay.example.invalid":["codex"],"broker":["agy"]}'
```

`OPENSWARM_AUTO_HOST_BACKENDS` 是主机 backend 白名单，不是凭据配置；box 的
空白名单确保控制面本机的 lane 不被探测或接收自动派发。部署后
以 `swrm-fleet fleet status --json` 观察实际 lane 名称和状态；若实际
lane 数量不是 10，应先修正 fleet/topology 配置，再启用自动派发。

已退役主机（2026-11-02 起不再探测、不再派发）：Instabox fleet
`node-1.example.invalid`–`node-5.example.invalid`，以及
`worker-b.example.invalid`（其容量由 `worker` 承接）。这些名字只作为退役
记录出现，不得写进自动派发配置。

## 从 box 手动冒烟

以下步骤不经过 Hub，适合验证 SSH、fleet 路由和单个 lane。使用一个临时、
不含凭据的任务卡：

```bash
cd /srv/phanes/volto-workspace
umask 077
task=$(mktemp /tmp/swrm-ssh-smoke.XXXXXX.md)
cat >"$task" <<'EOF'
Repository: volto
Task type: docs
只检查当前 checkout 的版本并返回，不修改文件。
EOF

swrm-fleet fleet status --json
swrm-fleet fleet exec worker -- lane status
swrm-fleet fleet exec worker -- lane assign M1 --task-file "$task" \
  --title "SSH fleet smoke" --budget-minutes 5
```

若需直接登录目标 host 做同样的检查，先把不含凭据的任务卡放到目标 host，
再执行原生命令（这里的 `M1` 仅为该 host 在 `fleet status` 中显示的 lane
示例）：

```bash
scp -q -F ~/.ssh/swrm-hub/config "$task" worker:/tmp/swrm-ssh-smoke.md
ssh -F ~/.ssh/swrm-hub/config worker -- \
  swrm-fleet lane assign M1 --task-file /tmp/swrm-ssh-smoke.md \
  --title "SSH fleet smoke" --budget-minutes 5
```

`lane assign` 返回 run id 后，用同一 host 查询并等待结果：

```bash
swrm-fleet fleet exec worker -- runs --active --json
swrm-fleet fleet exec worker -- lane status
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

`swrm-fleet fleet harvest` 从各 fleet host 收回 lane 结果时，每次远端 fetch 最多
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
swrm-fleet fleet harvest --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["fetch_fails"])'
grep -h '"fetch_fails"' ~/.claude/swrm-executor-candidate/harvest/*/*.json
```

`fetch_fails` 为 0 表示远端结果一次就抓回来了；大于 0 表示该 host 的 SSH 抓取发生过
瞬时失败；等于尝试上限（3）表示有界重试已经放弃，必须人工确认该 lane 的结果是否已收回。

## BatchMode 探测与 hosts_ok 汇总契约 (Refs #954)

fleet 探活（probe）与容量汇总统一使用 `ssh -o BatchMode=yes` 探测受控 host。
探测开关在 `ib-refresh`（生产汇总路径）：fleet JSON 里写
`"ssh_probe": {"enabled": true, "ssh_config": "~/.ssh/swrm-hub/config"}`，
或命令行 `--ssh-probe --ssh-config ~/.ssh/swrm-hub/config`。

若探测非零退出（含 `Permission denied` / `Connection closed` / 超时），该 host 记为
`probe_status: failed`，并且**不计入** `hosts_ok`；只有退出码 0 才 +1 `hosts_ok`。
离线契约断言见 `tools/check-fleet-batchmode-probe.py`（用假探测输出驱动同一条生产路径，
勿连真实 instabox）。
