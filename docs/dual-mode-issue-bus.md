# Phanes 双模式 Issue Bus 冒烟

## 适用范围

受控主机优先走 SSH fleet；无法纳入受控 fleet 的执行器（例如云端 agent 或他人的主机）才走 GitHub issue bus。本说明以
`surlymochan/phanes` 为目标仓库，记录一次可复现的 bus 初始化和协议冒烟路径。

## 推荐的 bus 仓

推荐新建专用的私有仓库 `surlymochan/phanes-bus`，把任务卡、claim 和 result 评论与代码仓库分开，便于单独收紧权限。这里只写创建步骤，不在本仓库自动创建远端仓库：

```sh
gh repo create surlymochan/phanes-bus --private
openswarm bus init surlymochan/phanes-bus
```

也可以直接复用 `surlymochan/phanes`；此时把下面配置中的 `bus.repo` 和 `openswarm bus init` 参数都改成
`surlymochan/phanes`，并确保参与者对 issue、评论和代码分支拥有所需权限。

两台主机都需要已登录 GitHub CLI（不要把 token 写进 issue、配置文件或本文）：

```sh
gh auth status
```

## 本地 policy

在 planner 和 worker 各自的 `$OPENSWARM_HOME/policy.local.json` 中加入以下配置。这里的
`surlymochan` 是 GitHub login；`worker_repos` 限制 worker 只能处理 Phanes 任务。

```json
{
  "bus": {
    "repo": "surlymochan/phanes-bus",
    "allowed_executors": ["surlymochan"],
    "allowed_planners": ["surlymochan"],
    "worker_repos": ["surlymochan/phanes"],
    "worker_verify_allow": ["true"],
    "poll_seconds": 10,
    "labels_prefix": "openswarm",
    "open_pr": false
  }
}
```

`worker_verify_allow` 中的 `true` 仅用于本次协议冒烟；实际任务应替换为目标仓库明确允许的、精确匹配的验证命令。
`open_pr: false` 表示 worker 只推送 `openswarm/<run-id>` 分支，不自动开 PR。

## 最小冒烟序列

先在 planner 侧准备一个已推送到 `origin` 的基线和任务文件；`github-issue` 要求基线已被远端分支或 tag 广播：

```sh
cd /path/to/phanes
git status --short
git push origin HEAD
openswarm run --backend github-issue --cwd "$PWD" --base "$(git rev-parse HEAD)" \
  --budget-minutes 10 --verify 'true' --task-file /path/to/task.md
```

在允许执行的 worker 主机上运行一次：

```sh
openswarm bus-worker --bus surlymochan/phanes-bus --name surlymochan-phanes-smoke \
  --backend codex --once
```

`bus-worker --once` 适合冒烟、手动补跑或临时消费一项任务：领取并处理当前可见任务后即退出，便于明确控制执行窗口。需要持续接收任务的 worker 则应运行常驻 loop（省略 `--once`），由它按 `poll_seconds` 周期轮询 bus；适合长期在线的受控主机，但应配合进程托管与日志监控。

检查 bus 状态并观察结果：

```sh
openswarm bus status --repo surlymochan/phanes-bus
openswarm watch <run-id>
```

成功的最小协议闭环是：issue 从 `openswarm:queued` 被 worker claim，worker 推送
`openswarm/<run-id>`，并在 issue 评论中写入 `openswarm-result`；planner 随后取得该结果并写入本地 run 记录。不要把
真实 token、私密环境变量或其他凭据放进任务文件、issue body 或评论。

受控主机仍应使用 SSH fleet；只有需要跨信任边界派发时才采用这里的 GitHub issue bus。
