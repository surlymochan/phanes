# SSH fleet lane 状态路径约定

## 结论

`openswarm-auto` 的 fleet 探测通过 `fleet exec <host> -- lane status` 在目标主机
执行当前 release 的 `openswarm-fleet`。`openswarm-fleet` 会把目标主机的
`OPENSWARM_HOME` 固定为：

```text
~/.claude/openswarm-executor-candidate
```

因此每台主机上 lane `<L>` 的唯一期望状态文件是：

```text
~/.claude/openswarm-executor-candidate/lanes/<L>/state.json
```

release 代码与可变状态必须分开。release 使用版本目录和一个稳定软链：

```text
~/tools/openswarm-executor-candidate/releases/<sha>/bin/openswarm-fleet
~/tools/openswarm-executor-candidate/current -> releases/<sha>
```

fleet 配置或 PATH 中的入口应解析到
`~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet`；不要把
`lanes/` 放进 release 目录，也不要让 `state.json` 通过 release 目录的软链隐藏
另一份状态。`current` 更新后，状态路径保持不变，remote status 探测无需知道
`<sha>`。

## 主机清单

以下主机全部使用同一状态路径和 release 软链约定；`<L>` 是该主机
`lanes.json` 中配置的 lane 名称：

| 主机 | release 入口 | 期望 lane 状态路径 |
| --- | --- | --- |
| `ib1` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `ib2` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `ib3` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `ib4` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `ib5` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `govpn` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `govpn2` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `gorelay` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `gocc` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |
| `box` | `~/tools/openswarm-executor-candidate/current/bin/openswarm-fleet` | `~/.claude/openswarm-executor-candidate/lanes/<L>/state.json` |

## 本机 dry-run

脚本只打印上述合同，不连接 SSH，也不读取生产主机或凭据：

```sh
tools/check-lane-status-paths.sh --dry-run
```

在已经部署 candidate runtime 的主机上，可额外检查本机 data 目录、`current` 软链
和入口：

```sh
tools/check-lane-status-paths.sh --check-local
```

代码依据：`openswarm/bin/openswarm-fleet:8` 固定 `OPENSWARM_HOME`，
`openswarm/bin/openswarm:6686-6688` 组成 `lanes/<L>/state.json`，
`openswarm/bin/openswarm:8520-8544` 的 `lane_status_rows()` 读取该文件。
