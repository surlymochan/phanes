# Fleet topology 期望值

本文记录 box 控制面的 fleet topology 期望值，用于检查配置与实际 lane
容量是否符合约定。部署时应核对实际可用容量。

## Host 限额

机器可读的 topology 约定如下。`hosts` 是自动派发允许使用的 lane host
集合；每个 host 都必须有对应的 `host:<name>` 限额键，避免文档中的 host
在容量控制面没有限额。

```json
{
  "hosts": ["gocc", "gorelay", "govpn", "govpn2"],
  "limits": {
    "total": 5,
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
| `govpn2` | 2 |
| `govpn` | ≤1 |

上述限额合计为 5 个 lane 的上限；实际可用量以部署配置为准。`copilot`
不属于受控 fleet host，也不得配置为 lane backend。

## Backend 限额

| Backend | 期望限额 |
|---|---:|
| `pi` | 按实际配置 |
| `agy` | 按实际配置 |
| `codex` | 按实际配置 |
| `copilot` | 0 |

这些是 box 控制面应维护的期望值；主机集合和 SSH fleet 的基础约定见
[`docs/dual-mode-ssh.md`](dual-mode-ssh.md)，当前目标拓扑与 Copilot 禁用约定见
[`brain/autopilot.md`](../brain/autopilot.md)。
