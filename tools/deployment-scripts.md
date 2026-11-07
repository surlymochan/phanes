# 私有部署脚本配置

这些脚本不会自动探测发布机器或使用个人目录。调用前通过环境变量提供目标；不要把真实路径或主机名写回仓库。

| 脚本 | 必填环境变量 |
| --- | --- |
| `dispatch.sh` | `PHANES_CARDS_DIR`：本地任务卡目录 |
| `osupdate.sh` | `PHANES_BUNDLE_FILE`：待传输的 bundle 文件 |
| `pub.sh` | `PHANES_SCRATCH_DIR`、`PHANES_PUBLISH_HOST`、`PHANES_LOCAL_REPO`，以及下行全部变量 |
| `pub-remote.sh` | `PHANES_REMOTE_REPO`、`PHANES_REMOTE_WORKTREE`、`PHANES_DEPS_SCRIPT`、`PHANES_DEPS_SOURCE`、`PHANES_PATCH_DIR` |
| `addfix.sh` | `PHANES_SCRATCH_DIR`、`PHANES_PUBLISH_HOST`、`PHANES_REMOTE_REPO`、`PHANES_REMOTE_WORKTREE`、`PHANES_PUSH_SCRIPT` |

`pub.sh` 将远端配置传给 `pub-remote.sh`。远端 worktree 路径必须尚不存在；
`addfix.sh` 则使用已经存在的 PR worktree。脚本的其余位置参数保持不变。
发布脚本会提交、推送或创建 PR，仅在明确授权后手动调用；本轮只做静态语法检查。

`swrm/bin/swrm-auto` 使用同目录下的 `swrm` CLI。远端派发需要
`OPENSWARM_AUTO_SSH_HOST`；可选 `OPENSWARM_AUTO_SSH_CONFIG` 指定 SSH 配置文件。
不再默认连接某个发布主机。
`OPENSWARM_AUTO_REPO` 可指定执行仓库，未设置时使用当前目录；输出的本地主机标识为 `local`，远端标识来自配置。

`hub-swrm-bridge` 的 `~/.claude/swrm-hub-bridge/config.json` 除 `workspace`、`repo` 外，
还需配置 `ssh_host`（SSH 目标）、`hosts`（允许接收回执及 reconcile 的 executor host 列表）；
`ssh_config` 为可选配置文件路径。没有允许的 host 时拒绝接收回执，不放宽主机边界。
