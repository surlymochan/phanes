# 双模式运维检查清单 (Dual-mode Checklist)

本文档归纳 Phanes 双模式（模式 A：SSH fleet 受控集群；模式 B：GitHub issue bus 外部执行器）的日常运维巡检与检查清单。

## 1. 队列与任务水位检查（重点）

`lane-autodispatch` 通过 `lanes-status.json` 输出全局派发状态，核心监控字段包括 `_busy`、`_ready`、`_blocked`。

- [ ] **检查 `_ready` 队列水位**：
  - 当 `lanes-status.json` 中 `_ready` 低于 10 时，表明可派发任务不足，需及时补充 backlog 或触发 planner：
    1. **补充 backlog 任务卡**：将新任务 markdown 放置到 `~/phanes-ctl/autodispatch/backlog/*.md` 或合并入 `docs/backlog/next/*.md`。
    2. **触发 planner 规划任务**：若无现成 backlog，通过创建带有 `planner` 标签（如 `p1,backend:codex,planner`）的 issue，由 planner lane 自动生成新一批次任务至 `docs/backlog/next/`。
    3. **自动补仓告警**：`autodispatch.py` 在 `ready < 10` 且 backlog 为空时会记录警告日志（`WARNING queue low: X dispatchable lane-ready issue(s) left`）。
- [ ] **检查 `_blocked` 依赖等待**：
  - 确认标记有 `依赖：#xxx` 的 issue 是否因上游 PR 未合入或未关闭而积压。

## 2. 模式 A — SSH Fleet（受控集群，5 项巡检）

每项提供单行巡检命令：

| 检查项 | 巡检目标 | 一行命令 |
|---|---|---|
| **ssh BatchMode** | 验证 box 至受控节点非交互连通性与 ControlMaster 复用 | `ssh -o BatchMode=yes -F ~/.ssh/openswarm-hub/config gores1 true` |
| **lane idle** | 检查 10+ 路真实 lane 运行水位与空闲（idle）分配状态 | `~/phanes-ctl/bin/lane-autodispatch-status` |
| **coordinator** | 向 gocc 协调器查询当前全局并发预约快照 | `ssh -F ~/.ssh/openswarm-hub/config gocc -- python3 ~/.claude/openswarm-fleet-capacity/fleet-capacity gocc <<< '{"op":"status"}'` |
| **autodispatch heartbeat** | 检查 autodispatch 派发守护循环最后心跳（>3 分钟异常） | `cat ~/phanes-ctl/autodispatch/heartbeat` |
| **merge heartbeat** | 检查 lane-merge 自动合并循环日志与最新时戳（>3 分钟异常） | `tail -n 20 ~/phanes-ctl/autodispatch/merge.log` |

### 详细步骤与判定说明

- [ ] **1. ssh BatchMode**：
  - 命令：`ssh -o BatchMode=yes -F ~/.ssh/openswarm-hub/config gores1 true`
  - 判定：退出码为 0 说明非交互认证与长连接复用正常；若非零退出（含 Permission denied、Connection closed 或超时）则判定 probe 探测失败，容量/健康汇总不得 +1 hosts_ok，需排查 key 或 host 状态（`ib-refresh` 开启 `ssh_probe` 后该 host 记为 `probe_status: failed` 且不计入 hosts_ok；离线断言见 tools/check-fleet-batchmode-probe.py）。
- [ ] **2. lane idle**：
  - 命令：`~/phanes-ctl/bin/lane-autodispatch-status`
  - 判定：输出中各 lane（M1–M4、A1、A2、CX1–CX5）应处于 `busy` 或 `idle` 轮转中，不可异常挂死在非预期状态。
- [ ] **3. coordinator**：
  - 命令：`ssh -F ~/.ssh/openswarm-hub/config gocc -- python3 ~/.claude/openswarm-fleet-capacity/fleet-capacity gocc <<< '{"op":"status"}'`
  - 判定：返回 `{"ok": true, "reservations": [...]}`，若报 `coordinator unavailable` 说明协调器或网络退避中。
- [ ] **4. autodispatch heartbeat**：
  - 命令：`cat ~/phanes-ctl/autodispatch/heartbeat`
  - 判定：输出 ISO 时间戳；对比当前时间若滞后超过 3 分钟，表明 `autodispatch.py` 循环可能阻塞或已退出。
- [ ] **5. merge heartbeat**：
  - 命令：`tail -n 20 ~/phanes-ctl/autodispatch/merge.log`
  - 判定：日志最新输出应在 3 分钟内；确认 `merge-loop.sh` 正常轮询 Linux-green `fleet/*` PR。

## 3. 模式 B — GitHub Issue Bus（非受控执行器，5 项巡检）

每项提供单行巡检命令：

| 检查项 | 巡检目标 | 一行命令 |
|---|---|---|
| **bus repo** | 查询 Issue bus 仓库连通性与未决任务列表 | `openswarm bus status --repo surlymochan/phanes-bus` |
| **gh auth** | 验证 GitHub CLI 认证状态与登录身份 | `gh auth status` |
| **digest** | 校验任务卡首评的 task digest SHA256 防篡改摘要 | `gh issue view <issue-number> --repo surlymochan/phanes-bus --json comments --jq '.comments[] \| select(.body \| startswith("openswarm-task-digest"))'` |
| **claim** | 外部 worker 单次认领消费与认领状态 | `openswarm bus-worker --bus surlymochan/phanes-bus --name <worker-name> --backend codex --once` |
| **result branch** | 检查代码仓库中执行器推送的 openswarm/* 结果分支 | `git ls-remote origin 'refs/heads/openswarm/*'` |

### 详细步骤与判定说明

- [ ] **1. bus repo**：
  - 命令：`openswarm bus status --repo surlymochan/phanes-bus`
  - 判定：成功列出 open issues 及其状态标签（`openswarm:queued`、`openswarm:claimed`、`openswarm:done` 等）；若未初始化需先执行 `openswarm bus init surlymochan/phanes-bus`。
- [ ] **2. gh auth**：
  - 命令：`gh auth status`
  - 判定：当前登录用户需在 `policy.local.json` 的 `allowed_planners` 或 `allowed_executors` 白名单中。
- [ ] **3. digest**：
  - 命令：`gh issue view <issue-number> --repo surlymochan/phanes-bus --json comments --jq '.comments[] | select(.body | startswith("openswarm-task-digest"))'`
  - 判定：首评必须包含有效 `openswarm-task-digest: sha256:...` 且与任务 body 一致，否则 worker 必须拒绝认领。
- [ ] **4. claim**：
  - 命令：`openswarm bus-worker --bus surlymochan/phanes-bus --name <worker-name> --backend codex --once`
  - 判定：worker 认领并处理任务后回写 `/claim <name>` 与 `openswarm:claimed` 标签；若只想核查历史认领可运行 `gh issue view <issue-number> --repo surlymochan/phanes-bus --comments | grep '/claim'`。
- [ ] **5. result branch**：
  - 命令：`git ls-remote origin 'refs/heads/openswarm/*'`
  - 判定：worker 执行成功后应推送到目标仓库 `openswarm/<run-id>` 分支，并由 planner 负责拉取校验。

## 参考与关联
- [`docs/dual-mode-ssh.md`](dual-mode-ssh.md)：SSH fleet 部署与十 lane 拓扑约定
- [`docs/dual-mode-issue-bus.md`](dual-mode-issue-bus.md)：GitHub issue bus 冒烟与最小闭环
- [`brain/autopilot.md`](../brain/autopilot.md)：自动驾驶与 autodispatch 运行说明
- [`brain/dual-mode-status.md`](../brain/dual-mode-status.md)：双模式盘点与状态
- [`docs/fleet-coordinator-ssh-backoff.md`](fleet-coordinator-ssh-backoff.md)：协调器状态观察与退避说明
- [`tools/lane-autodispatch/autodispatch.py`](../tools/lane-autodispatch/autodispatch.py)：派发器、心跳写入与队列水位
