# Fleet topology 期望值

本文记录 **2026-11-02** 基线的 box 控制面 fleet topology，用于检查配置与
实际 lane 容量是否符合约定。Instabox 主机已经退役，受控远端 host 固定为
`worker`、`gores2`、`worker-a.example.invalid`、`relay.example.invalid`、
`broker`；控制面在 **box**，box 是本机，不属于受控 fleet host。总 lane 数
应为 **≥10**。

## Host 限额

机器可读的 topology 约定如下。`hosts` 是自动派发允许使用的远程 lane host
集合；每个 host 都必须有对应的 `host:<name>` 限额键，避免文档中的 host
在容量控制面没有限额。

```json
{
  "hosts": ["worker", "gores2", "worker-a.example.invalid", "relay.example.invalid", "broker"],
  "limits": {
    "total": 10,
    "host:worker": 3,
    "host:gores2": 2,
    "host:worker-a.example.invalid": 2,
    "host:relay.example.invalid": 2,
    "host:broker": 1
  }
}
```

| Host | 最大 lane 数 | 允许的 backend |
|---|---:|---|
| `worker` | 3 | `pi` |
| `gores2` | 2 | `pi` |
| `worker-a.example.invalid` | 2 | `codex` |
| `relay.example.invalid` | 2 | `codex` |
| `broker` | 1 | `agy` |

上述限额合计为 10 个 lane 的上限；实际可用总量不得低于 10。`copilot`
不属于受控 fleet host，也不得配置为 lane backend。

## Backend 限额

| Backend | 期望限额 | 说明 |
|---|---:|---|
| MiniMax / `pi` | 5 | 受控 fleet 远程 lane，配比 50% |
| Codex / `codex` | 4 | 受控 fleet 远程 lane |
| `agy` | 1 | 受控 fleet 远程 lane，配比 10% |
| `copilot` | 0 | 仅远程可选；不属于受控 fleet host，受控限额为 0 |

配比按 2026-11-02 基线：MiniMax 50%、Codex 30%、agy 10%、Copilot 10%。
Copilot 仅远程可选、受控限额为 0，它的份额不在受控 fleet 的 10 个 lane
内；受控 fleet 因此按 pi 5 / codex 4 / agy 1 分配。

这些是 box 控制面应维护的期望值；主机集合和 SSH fleet 的基础约定见
[`docs/dual-mode-ssh.md`](dual-mode-ssh.md)，2026-11-02 事实基线与 Copilot
禁用约定见 [`docs/fusion-openbot-swrm-v0.2.md`](fusion-openbot-swrm-v0.2.md)。
