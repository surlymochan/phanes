# WebSocket / stream 派活通道（Refs #1483）

## 动机

有些执行端没有公网 IP，或者 IP 经常变化：控制面无法 SSH 进去派活。
本通道反过来——执行端主动向外拨一个 WebSocket 连接，领取 Run、回传
状态与 Evidence。它与现有 SSH fleet、GitHub external lane 并列，最终
汇入同一 Task/Run/Evidence 合约，**不替代 GitHub 账本**。

## 协议

实现：`openswarm/lib/transport/websocket.py`，纯标准库（执行端最小主机
无需安装 `websockets` 之类依赖）。JSON 文本帧（RFC 6455）：

| 方向 | 消息 | 含义 |
| --- | --- | --- |
| 执行端 → 中继 | `hello {executor, token}` | 鉴权 |
| 中继 → 执行端 | `welcome {executor}` | 鉴权通过 |
| 执行端 → 中继 | `claim {issue}`（带 delivery id） | 领取一张卡 |
| 中继 → 执行端 | `assigned {issue, run_id}` | 已派活 |
| 执行端 → 中继 | `evidence {issue, run_id, evidence}`（带 delivery id） | 回传 Evidence 指针 |
| 中继 → 执行端 | `ack {duplicate}` | 已收（`duplicate=true` 表示重放） |
| 任一方向 | `error {error}` | 拒绝原因 |
| 任一方向 | ping / pong 帧 | 心跳 |

`evidence` 指针是不透明字符串（URL、账本 key、fixture 路径均可），
中继只存储、不解析、不二次计数。

## 鉴权

- 中继持有 token → 执行端名的映射；`hello` 里的 token 用
  `hmac.compare_digest` 常量时间比对，token 永不记日志。
- 未鉴权时 `claim`/`evidence` 一律拒绝（`hello first`）；token 错误
  直接断开连接。
- PoC 走明文 `ws://`：生产部署必须在前面终结 TLS（`wss://`）并
  轮换 token，否则 token 在传输层可见。这是 PoC 的已知局限，不是
  协议本身的缺口。

## 重连与心跳

- 心跳：WebSocket ping/pong。pong 超时即判定连接已死。
- 重连：执行端重拨、重新 `hello`，把未收到 ack 的 evidence 按原
  delivery id 重发；中继按 delivery id 去重，同一份 evidence 只存
  一次。断线本身不产生失败结论。

## 与 lane 状态机映射

中继**不直接改 GitHub 标签**，控制面按以下映射接管：

| 中继事件 | lane 状态 |
| --- | --- |
| `claim` | `in-progress`（卡离开 `lane-ready`，执行端持有） |
| 心跳存活 | `in-progress`（租约仍有效） |
| `evidence` | `in-progress`（已回传，review/merge 仍待 lane 处理） |
| 断线且租约过期 | 回 `lane-ready` 重新排队，**绝不直接记 `lane-failed`** |

## 明确不做

- 不绕过 lane review/merge；不打印密钥。
- 不做第二套调度器：中继只分发控制面已经给它的卡，不自己选卡、
  不排优先级、不做 quota。
- 不碰 `autodispatch.py`：能力进 openswarm，调度入口保持不变。

## PoC 验证

最小 PoC（测试即演示）：一个无入站端口的假执行端只向外拨号，
领取一张 fixture 卡并回传 Evidence 指针；覆盖断线重连、重复投递
幂等、未授权拒绝：

```bash
python3 -m unittest openswarm.tests.test_transport_websocket -v
```
