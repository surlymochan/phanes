# 私有部署脚本配置

这些脚本不会自动探测发布机器或使用个人目录。调用前通过环境变量提供目标；不要把真实路径或主机名写回仓库。

| 脚本 | 必填环境变量 |
| --- | --- |
| `dispatch.sh` | `PHANES_CARDS_DIR`：本地任务卡目录 |
| `osupdate.sh` | `PHANES_BUNDLE_FILE`：待传输的 bundle 文件 |
| `pub.sh`、`pub2.sh` | `PHANES_SCRATCH_DIR`、`PHANES_PUBLISH_HOST`、`PHANES_LOCAL_REPO`，以及下行全部变量 |
| `pub-remote.sh` | `PHANES_REMOTE_REPO`、`PHANES_REMOTE_WORKTREE`、`PHANES_DEPS_SCRIPT`、`PHANES_DEPS_SOURCE`、`PHANES_PATCH_DIR` |
| `addfix.sh` | `PHANES_SCRATCH_DIR`、`PHANES_PUBLISH_HOST`、`PHANES_REMOTE_REPO`、`PHANES_REMOTE_WORKTREE`、`PHANES_PUSH_SCRIPT` |

`pub.sh` 将远端配置传给 `pub-remote.sh`。远端 worktree 路径必须尚不存在；
`addfix.sh` 则使用已经存在的 PR worktree。脚本的其余位置参数保持不变。
发布脚本会提交、推送或创建 PR，仅在明确授权后手动调用。
离线回归 `python3 -m unittest discover -s tools/tests -p test_publish_config.py -v`
用记录器替代 Git、SSH、SCP、gh，验证两个脚本，不连接真实部署。

`swrm/bin/swrm-auto` 使用同目录下的 `swrm` CLI。远端派发需要
`OPENSWARM_AUTO_SSH_HOST`；可选 `OPENSWARM_AUTO_SSH_CONFIG` 指定 SSH 配置文件。
不再默认连接某个发布主机。
`OPENSWARM_AUTO_REPO` 可指定执行仓库，未设置时使用当前目录；输出的本地主机标识为 `local`，远端标识来自配置。

`hub-swrm-bridge` 的 `~/.claude/swrm-hub-bridge/config.json` 除 `workspace`、`repo` 外，
还需配置 `ssh_host`（SSH 目标）、`hosts`（允许接收回执及 reconcile 的 executor host 列表）；
`ssh_config` 为可选配置文件路径。没有允许的 host 时拒绝接收回执，不放宽主机边界。

## 本地配置与示例

复制 `tools/deployment.env.example` 到仓库根目录 `.deployment.env`，填入本机值后
手动 `source .deployment.env`。该文件由 `.gitignore` 排除，脚本不会自动执行它。
示例及历史文档中的主机角色、`.example.invalid` 域名和 `/path/to/` 路径均为占位值；
经过脱敏的历史记录不能用于证明当前部署状态。

`PHANES_PR_REPO` 可显式指定 `owner/repo`；未设置时，`gh` 在
`PHANES_LOCAL_REPO` 中解析当前仓库。`pushbr.sh` 也需要
`PHANES_LOCAL_REPO` 和 `PHANES_PUBLISH_HOST`；`mkpr.py` 使用 `PHANES_SCRATCH_DIR/pr`。
`watch8.sh` 需要参数或 `PHANES_WATCH_RUNS`，格式为
`host:ssh:run-id:lane`（多条以空格分隔）；`dropexport.sh` 使用 `PHANES_WORKTREE_ROOT`。

Pi 桥接脚本需要 `SWRM_BROKER_HOST`（或 `--broker-host`）；跨主机桥另需
`SWRM_WORKER_HOST`（或 `--worker`）。`hub-fleet-gateway` 的结果查询接受
`local` 或 `OPENSWARM_AUTO_SSH_HOST`，与自动派发返回的主机标识一致。
`fleet-capacity` 的认证入口使用逻辑角色 `broker`、`worker`、`local`；
SSH forced-command 部署必须将身份映射到这些角色，不把机器名作为协议角色。

## 状态目录兼容

默认目录为 `~/.claude/swrm`，不会自动移动、合并或删除旧目录。
已有部署应继续显式设置 `OPENSWARM_HOME` 指向原目录；兼容环境键保留。
要迁移默认目录，先停止对应控制面并备份旧目录，再由运维复制到新目录、核对数据并切换。
本轮仅用隔离目录验证显式覆盖，不操作真实旧状态；真实存量迁移仍需部署侧验收。

`lane-autodispatch` 在远端 executor 上解析状态目录，读取 lane/run 和写入
`pr-validation.*` 使用同一规则：优先 `OPENSWARM_HOME`，其次兼容键 `XSWARM_HOME`。
未设置时，依次选择已有的 `~/.claude/swrm-executor-candidate`、
`~/.claude/openswarm-executor-candidate`、`~/.claude/swrm`、`~/.claude/openswarm`；
都不存在时使用 `~/.claude/swrm-executor-candidate`。旧目录名仅用于存量兼容。
多个目录并存时应显式设置远端环境变量，避免选择错误部署；设置在 AD 本机不会传到 SSH 远端。
该解析不会迁移或合并目录；显式指定的目录不存在时也不会回退到其他部署。

离线回归：`python3 -m unittest discover -s tools/tests -p test_autodispatch_state_home.py -v`，
覆盖自定义目录（含空格）、兼容环境键、新旧默认目录及验证记录写入一致性。
