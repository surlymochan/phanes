# Fleet topology 期望值

本文记录 2026-11-02 box 控制面的 fleet topology。Instabox 主机已退役；受控
host 为 `gores1`、`gores2`、`govpn`、`govpn2`、`gorelay`、`gocc`。
`box` 是控制面本机，不属于受控 fleet host。总 lane 数应为 **≥10**。

## Host 限额

机器可读的 topology 约定如下。`hosts` 是自动派发允许使用的远程 lane host
集合；每个 host 都必须有对应的 `host:<name>` 限额键。

```json
{
  "hosts": ["gores1", "gores2", "gocc", "gorelay", "govpn", "govpn2"],
  "limits": {
    "total": 10,
    "host:gores1": 2,
    "host:gores2": 2,
    "host:govpn": 1,
    "host:govpn2": 2,
    "host:gorelay": 1,
    "host:gocc": 2
  }
}
```

| Host | 最大 lane 数 |
|---|---:|
| `gores1` | 2 |
| `gores2` | 2 |
| `govpn` | 1 |
| `govpn2` | 2 |
| `gorelay` | 1 |
| `gocc` | 2 |

## Backend 限额

| Backend | 期望限额 | 说明 |
|---|---:|---|
| MiniMax / `pi` | 4 | 远程 |
| Codex / `codex` | ≥4 | 远程 |
| `agy` | 2 | 远程 |
| `copilot` | 0 | 仅远程可选；不属于受控 fleet host，且不得在受控 fleet 派发 |

box 上的本地 lane runtime 用 `OPENSWARM_AUTO_LOCAL_HOST=box` 标识，backend
白名单应为 `"box":[]`，以避免本地 lane 被探测或接收自动派发。主机集合和
SSH fleet 的约定见 [`docs/dual-mode-ssh.md`](dual-mode-ssh.md)。
