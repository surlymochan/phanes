# Fleet topology 期望值

本文记录 box 控制面的 fleet topology 期望值，用于检查配置与实际 lane
容量是否符合约定。总 lane 数应为 **≥10**。

## Host 限额

机器可读的 topology 约定如下。`hosts` 是自动派发允许使用的 lane host
集合；每个 host 都必须有对应的 `host:<name>` 限额键，避免文档中的 host
在容量控制面没有限额。

```json
{
  "hosts": ["node-1.example.invalid", "node-2.example.invalid", "node-3.example.invalid", "node-4.example.invalid", "node-5.example.invalid", "broker", "relay.example.invalid", "worker-a.example.invalid", "worker-b.example.invalid"],
  "limits": {
    "total": 11,
    "host:node-1.example.invalid": 1,
    "host:node-2.example.invalid": 1,
    "host:node-3.example.invalid": 1,
    "host:node-4.example.invalid": 1,
    "host:node-5.example.invalid": 2,
    "host:broker": 1,
    "host:relay.example.invalid": 1,
    "host:worker-a.example.invalid": 1,
    "host:worker-b.example.invalid": 2
  }
}
```

| Host | 最大 lane 数 |
|---|---:|
| `broker` | 1 |
| `relay.example.invalid` | ≤1 |
| `node-1.example.invalid`–`node-4.example.invalid` | 每台 1 |
| `node-5.example.invalid` | 2 |
| `worker-b.example.invalid` | 2 |
| `worker-a.example.invalid` | ≤1 |

上述限额合计为 11 个 lane 的上限；实际可用总量不得低于 10。`copilot`
不属于受控 fleet host，也不得配置为 lane backend。

## Backend 限额

| Backend | 期望限额 |
|---|---:|
| `pi` | 4 |
| `agy` | 2 |
| `codex` | ≥4 |
| `copilot` | 0 |

这些是 box 控制面应维护的期望值；主机集合和 SSH fleet 的基础约定见
[`docs/dual-mode-ssh.md`](dual-mode-ssh.md)，当前目标拓扑与 Copilot 禁用约定见
[`brain/autopilot.md`](../brain/autopilot.md)。
