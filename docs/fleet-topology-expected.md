# Fleet topology 期望值

本文记录 box 控制面的 fleet topology 期望值，用于检查配置与实际 lane
容量是否符合约定。总 lane 数应为 **≥10**。

**2026-11-02 起**：Instabox 节点已下线，不再属于受控 fleet。受控 lane host
集合改为 box 控制面 + `gores1`、`gores2`、`govpn`、`govpn2`、`gorelay`、
`gocc`；其中 `box` 是控制面本机（lane 在 box 上直接派发），其余六台是经
SSH fleet 派发的远端主机。历史快照见
[`brain/dual-mode-status.md`](../brain/dual-mode-status.md)（2026-11-01）。

## Host 限额

机器可读的 topology 约定如下。`hosts` 是自动派发允许使用的 lane host
集合；每个 host 都必须有对应的 `host:<name>` 限额键，避免文档中的 host
在容量控制面没有限额。`control_plane_host` 标出控制面本机，它不经 SSH
fleet 派发，其余 host 才是 SSH fleet 主机。

```json
{
  "control_plane_host": "box",
  "hosts": ["box", "gores1", "gores2", "govpn", "govpn2", "gorelay", "gocc"],
  "limits": {
    "total": 11,
    "host:box": 1,
    "host:gores1": 2,
    "host:gores2": 2,
    "host:govpn": 1,
    "host:govpn2": 2,
    "host:gorelay": 1,
    "host:gocc": 2
  }
}
```

| Host | 最大 lane 数 | 说明 |
|---|---:|---|
| `box` | 1 | 控制面本机，lane 在 box 上直接派发 |
| `gores1` | 2 | pi（MiniMax） |
| `gores2` | 2 | pi（MiniMax） |
| `gocc` | 2 | agy |
| `gorelay` | 1 | agy |
| `govpn` | 1 | codex |
| `govpn2` | 2 | codex |

上述限额合计为 11 个 lane 的上限（每台受控主机 1–2 条，按现行远程配比
习惯）；实际可用总量不得低于 10。

## Backend 限额

| Backend | 期望限额 | 说明 |
|---|---:|---|
| `pi`（MiniMax-M3.1-Flash） | 5 | `box`、`gores1`、`gores2` |
| `codex` | 3 | `govpn`、`govpn2` |
| `agy` | 3 | `gocc`、`gorelay` |
| `copilot` | 0 | 仅远程手工使用 |

`copilot` 不属于受控 fleet host：受控 fleet 限额为 0（超额 0），也不进
`OPENSWARM_AUTO_HOST_BACKENDS` 的 lane backend 白名单；需要时只能在远端
主机上手工触发，不参与自动派发。

这些是 box 控制面应维护的期望值；主机集合和 SSH fleet 的基础约定见
[`docs/dual-mode-ssh.md`](dual-mode-ssh.md)。历史拓扑快照与 Copilot 禁用
约定见 [`brain/autopilot.md`](../brain/autopilot.md)（2026-11-01 快照）。
