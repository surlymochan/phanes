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

## 2. 模式 A — SSH Fleet（受控集群）

- [ ] **拓扑与 Lane 状态**：
  - 确认 10+ lane 拓扑（ib1–ib5、gocc、gorelay、govpn、govpn2）处于健康分配状态，无意外下线。
  - 运行 `~/phanes-ctl/bin/lane-autodispatch-status` 检查各 lane 是否在正常轮转。
- [ ] **证书与长连接**：
  - box 上 `ib-refresh` 约每 15 分钟续签 instabox 证书循环正常。
  - SSH `ControlMaster` 连接复用正常，避免高峰握手超时。
- [ ] **资源与退避**：
  - 关注 `govpn` (CX5) 内存与 swap 占用。
  - 关注 `gorelay` ↔ `gocc` 之间 SSH 瞬态退避情况。

## 3. 模式 B — GitHub Issue Bus（非受控执行器）

- [ ] **Bus 仓库与 Policy**：
  - 检查 `$OPENSWARM_HOME/policy.local.json` 中 `bus.repo`（如 `surlymochan/phanes-bus`）及白名单配置。
- [ ] **任务流转闭环**：
  - 运行 `openswarm bus status --repo <bus-repo>` 检查是否有长时间停留在 `openswarm:queued` 未被 claim 的任务。
  - 检查 worker 是否正常推送分支并回写 `openswarm-result` 评论。
- [ ] **安全与凭据隔离**：
  - 确认 issue body、评论和 task 文件中严禁包含真实 token 或私钥。

## 参考与关联
- [`docs/dual-mode-ssh.md`](dual-mode-ssh.md)：SSH fleet 部署与十 lane 拓扑约定
- [`docs/dual-mode-issue-bus.md`](dual-mode-issue-bus.md)：GitHub issue bus 冒烟与最小闭环
- [`brain/autopilot.md`](../brain/autopilot.md)：自动驾驶与 autodispatch 运行说明
- [`tools/lane-autodispatch/autodispatch.py`](../tools/lane-autodispatch/autodispatch.py)：派发器、队列水位 `LOW=10` 阈值与 `replenish` 逻辑
