# Fleet topology 期望值

本文记录 box 控制面的 fleet topology 期望值，用于检查配置与实际 lane
容量是否符合约定。总 lane 数应为 **≥10**。

## Host 限额

机器可读的 topology 约定如下。`hosts` 是自动派发允许使用的 lane host
集合；每个 host 都必须有对应的 `host:<name>` 限额键，避免文档中的 host
在容量控制面没有限额。

```json
{
  "hosts": ["ib1", "ib2", "ib3", "ib4", "ib5", "gocc", "gorelay", "govpn", "govpn2"],
  "limits": {
    "total": 11,
    "host:ib1": 1,
    "host:ib2": 1,
    "host:ib3": 1,
    "host:ib4": 1,
    "host:ib5": 2,
    "host:gocc": 1,
    "host:gorelay": 1,
    "host:govpn": 1,
    "host:govpn2": 2
  }
}
```

| Host | 最大 lane 数 |
|---|---:|
| `gocc` | 1 |
| `gorelay` | ≤1 |
| `ib1`–`ib4` | 每台 1 |
| `ib5` | 2 |
| `govpn2` | 2 |
| `govpn` | ≤1 |

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
