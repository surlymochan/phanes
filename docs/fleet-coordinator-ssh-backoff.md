# Coordinator：gorelay↔gocc SSH 横幅超时退避与观察指标

本文档记录在 10 路/11 路并行高并发场景下，`gorelay → gocc` reverse tunnel 发生 SSH protocol banner exchange timeout 的现象、现有退避机制（30s → 600s）以及建议的观察与诊断命令。

依据与核对基准：
- `brain/dual-mode-status.md:13`（`coordinator 在 gocc`，gorelay↔gocc 高峰 banner timeout 退避记录）
- `brain/autopilot.md:20-22,34`（10+ 路并行拓扑与状态监控脚本）
- `docs/fleet-topology-expected.md:6-18`（Fleet topology 各主机并发限额约定）
- `docs/dual-mode-ssh.md:32-41,53-75`（ControlMaster 配置与 SSH fleet 拓扑）
- `openswarm/bin/openswarm:3628-3666`（`fleet_guard_call` 8 秒硬超时与 fail-closed 准入）
- `openswarm/bin/openswarm:3668-3699`（`fleet_guard_call_retry` 有限重试与指数退避）
- `openswarm/bin/openswarm:3845-3852`（`fleet_recovery_ttl_seconds` 600s 保护）
- `openswarm/bin/fleet-capacity:12-34,90-115`（`fleet-capacity` 限额、status 与 recover 机制）

---

## 1. 现象说明

### 1.1 架构与背景
在当前的 10+ 并行受控拓扑中（`brain/autopilot.md:22`、`docs/fleet-topology-expected.md:6-18`）：
- 控制面统一运行在 `box`，维护 10–11 个并发 lane。
- 全局资源协调器（`fleet-capacity`）部署在 `gocc` 主机上，负责控制跨主机和跨后端的并发限额。
- 执行器主机（如 `gorelay`，运行 A2 lane 或 codex 执行器）通过反向隧道（reverse tunnel / SSH 转发）连通 `gocc` 上的协调器以进行准入（`acquire`）、释放（`release`）与状态检查（`status`）。

### 1.2 Banner-Exchange Timeout 现象
当 fleet 处于 10 路满负荷执行，且同时伴随多个主机间的 SSH 握手、状态查询、心跳检测与任务派发时：
1. `gorelay` 向 `gocc` 建立 SSH 反向通道或 ControlMaster 控制连接时，在协议握手阶段发生阻塞。
2. 客户端在等待服务端返回 SSH protocol identification banner（`SSH-2.0-...`）时达到超时限制，触发典型错误：
   ```text
   kex_exchange_identification: Connection timed out during banner exchange
   ```
   或：
   ```text
   ssh_dispatch_run_fatal: ... Timeout before protocol banner exchange
   ```
3. 根因主要为瞬间并发连接激增、主机 SSH 守护进程（sshd）在 CPU 负载或网络连接排队时的握手响应延迟。

---

## 2. 现有退避机制（30s → 600s）

为防止瞬时超时引发雪崩与惊群效应（thundering herd），系统具备分层退避与保护设计：

1. **退避阶梯（30s → 600s）**：
   - 当 reverse tunnel 或 SSH 握手因 banner exchange timeout 失败断开时，启动退避重试循环。
   - 初始重试间隔为 **30 秒**（30s）。
   - 若连接持续失败，重试间隔按阶梯或指数退避倍增（30s → 60s → 120s → 240s → 600s），单次最大重试等待上限封顶为 **600 秒**（600s / 10 分钟）。

2. **调用超时与 Fail-Closed 保证**：
   - 依据 `openswarm/bin/openswarm:3628-3666`，单次 `fleet_guard_call` 设定了 8 秒（`FLEET_CALL_TIMEOUT_SECONDS`）硬超时，并杀掉子进程组，隧道挂死不会拖住调用方。
   - 一旦协调器不可达或超时，立即捕获异常抛出 `fleet capacity coordinator unavailable`，采取严格的 **fail-closed** 策略。
   - 任务保留在排队或重试状态，绝不会在未取得协调器 reservation 的情况下误启动执行器。

3. **TTL 超时回收对齐（600s）**：
   - 依据 `openswarm/bin/openswarm:3845-3852`，协调器保留预约的默认恢复 TTL 为 `recovery_ttl_seconds = 600`（600 秒）。
   - 该 600s TTL 与退避上限窗口一致。即使 runner 因隧道断开崩溃或长时间处于最大退避，协调器上的孤儿记录在 600s 超时后仍可经本地存活检测通过 `op: "recover"` 正常清理（`openswarm/bin/fleet-capacity:103-115`），避免 slot 被永久占用。

4. **派发节拍平缓**：
   - box 上的 `lane-autodispatch` 循环按 60s 一 tick 运行（`brain/autopilot.md:10-11`），保证派发层不会对正在退避的主机产生密集连接冲击。

---

## 2.1 执行器侧容量调用的超时与重试（Refs #1694）

2026-11-02 晚实测：经同一路径人工查询容量 90 秒无返回，runner 无限期挂在 `acquire` 上，车道显示 busy 实则零请求，且没有重试与降级路径。为此 executor 侧全部容量调用点（`acquire` / `release` / `recover` / `status`）统一收敛到 `fleet_guard_call_retry`（`openswarm/bin/openswarm:3668-3699`）：

| 项目 | 取值 | 位置 |
| --- | --- | --- |
| 单次调用超时 | 8 秒（`FLEET_CALL_TIMEOUT_SECONDS`，可用 `OPENSWARM_FLEET_CALL_TIMEOUT` 覆盖） | `openswarm/bin/openswarm:3542-3560` |
| 单次调用重试 | 最多 3 次（`FLEET_CALL_MAX_ATTEMPTS`，可用 `OPENSWARM_FLEET_CALL_ATTEMPTS` 覆盖） | 同上 |
| 退避 | 指数退避 0.5s → 1.0s（`FLEET_CALL_BACKOFF_SECONDS`） | `openswarm/bin/openswarm:3668-3699` |
| 不可达阈值 | 持续 60 秒后标记 `capacity-unreachable`（`FLEET_CAPACITY_UNREACHABLE_SECONDS`） | `openswarm/bin/openswarm:3727-3744` |

行为约定：

- **超时不拖死 runner**：单次调用硬超时（8s）并杀掉子进程组（`fleet_guard_kill`），调用方最多等 3 次尝试 + 2 次退避（≈25.5s）后让出，run 保持 `state=queued`。
- **不误判为容量不足**：`queue_reason` 区分三种情形——本地并发上限 `provider concurrency limit`、协调器明确拒绝 `fleet capacity limit reached`、协调器不可达 `capacity coordinator unreachable`。
- **不丢弃已获预留**：acquire 超时后仍保留 `fleet_reservation_token` / `fleet_reservation_pending`，交由 TTL 清扫（`recover_expired_fleet_slots`，`openswarm/bin/openswarm:3888-3990`）对账。
- **明确日志**：run 目录新增 `capacity.jsonl`，记录调用点（`op`）、耗时（`seconds`）、重试次数（`attempts`）与错误；run 状态新增 `capacity_unreachable` / `capacity_unreachable_since` / `fleet_capacity_last`，供 #1687 的阻塞诊断直接引用。lane 状态（`lanes/<lane>/state.json`）同步写入 `capacity_unreachable`，并在协调器不可达时附 `fleet_capacity_visible`（协调器可见的预约数）。
- **release 不阻塞 run 结束**：run 结束路径只做 1 次有界尝试（`attempts=1`）；失败则写入待重发队列 `$OPENSWARM_HOME/fleet-release-queue.json`，由预留清扫 `flush_fleet_release_queue`（`openswarm/bin/openswarm:3793-3830`）补发（与 #1685 的清扫口径一致），run 状态里的 `fleet_release_pending` 仍作为兜底。
- **正常路径无额外开销**：协调器亚秒返回时只发起 1 次调用、无退避、不写 `capacity.jsonl`。

观察命令：

```bash
# 最近一次容量调用记录（调用点 / 耗时 / 重试次数）
tail -n 5 "$OPENSWARM_HOME/runs/<run-id>/capacity.jsonl"

# run 状态里的不可达标记（供 #1687 引用）
python3 -c 'import json,os;print(json.load(open(os.path.join(os.environ["OPENSWARM_HOME"],"runs","<run-id>","status.json"))))' \
  | grep -E 'capacity_unreachable|queue_reason|fleet_capacity_last'

# 待重发的 release 队列
cat "$OPENSWARM_HOME/fleet-release-queue.json"
```

---

## 3. 建议观察命令

### Harvest fetch 的有界重试与 lane-failed 阈值（Refs #947）

`tools/lane-autodispatch/autodispatch.py` 在收集（harvest）完成分支时，先把 fetch 失败分类（`classify_harvest_fetch_error`）：

- **transient（瞬时）**：传输或 SSH 抖动，M4#904 现场的 `fetch-pack: unexpected disconnect`、`bad pack header` 即属此类；此外还有 `early eof`、`index-pack failed`、`RPC failed`、`Connection reset`、`Connection closed`、`broken pipe`、banner/超时、`Permission denied`。
- **fatal（致命）**：其它错误（例如 `could not read from remote repository`），重试无意义。

一次 harvest 周期内最多尝试 **3 次**（初次 + 2 次重试），每次间隔 20 秒（`HARVEST_FETCH_MAX_ATTEMPTS` / `HARVEST_FETCH_RETRY_DELAY`）；达到上限后停止重试，不会无限等待。日志会记录每次失败的 `attempt x/3`，并在放弃时记录 `failed after x/3 attempts`。

transient 失败的处置（Refs #947）：

- 前 N 次（默认 **3** 次，`HARVEST_FETCH_MAX_TRANSIENT_FAILURES`，可用 `AD_HARVEST_MAX_FAILURES` 覆盖）连续 transient 失败**只记日志并指数退避** `min(600, 30 * 2 ** (失败次数 - 1))` 秒（30s → 60s → 120s → 240s → 480s → 600s 封顶）；issue 保持 `in-progress`，lane 在 `fetch_backoff_until` 到期前不再发起 harvest。
- 超过阈值（第 N+1 次失败）或遇到 fatal 错误才把 issue 标成 `lane-failed` 并附说明评论；fetch 成功后计数器清零。

离线契约检查（不连 SSH、不触碰真实 lane state）：

```bash
python3 tools/check-ad-harvest-backoff.py
```

在高并发运行或疑似发生连接退避时，应通过以下两组核心命令观察系统状态：

### 3.1 观察派发与 Lane 状态（lane-autodispatch-status）

在 box 控制面上执行：

```bash
~/phanes-ctl/bin/lane-autodispatch-status
```

或直接读取状态缓存文件：

```bash
cat ~/phanes-ctl/autodispatch/lanes-status.json
```

**重点观察指标**：
- **Lane 运行状态**：观察 `A2`（gorelay）以及 `A1`（gocc）的状态是否为 `busy`、`idle` 或异常停留在 `queued`。
- **心跳时戳（Heartbeat）**：检查各 lane 的最后心跳时间；若心跳超过 **3 分钟** 未更新，说明对应 worker 可能被阻塞在网络握手或退避中。
- **派发日志输出**：
  ```bash
  tail -n 100 ~/phanes-ctl/autodispatch/autodispatch.log
  ```
  核对是否出现 `coordinator unavailable`、`banner exchange timeout` 或退避等待日志。

---

### 3.2 观察协调器预约状态（coordinator status）

通过 box 上的受控 SSH 渠道向 `gocc` 主机查询协调器当前预约快照：

```bash
ssh -F ~/.ssh/openswarm-hub/config gocc -- \
  python3 ~/.claude/openswarm-fleet-capacity/fleet-capacity gocc <<< '{"op":"status"}'
```

若直接在 `gocc` 宿主机或本地测试环境下：

```bash
python3 openswarm/bin/fleet-capacity gocc <<< '{"op":"status"}'
```

**返回结构与重点观察指标**：
```json
{
  "ok": true,
  "reservations": [
    {
      "host": "gorelay",
      "run_id": "run-xxxxxx",
      "backend": "agy",
      "provider": null,
      "created": 1730401200.0
    }
  ]
}
```

- **`reservations` 列表**：
  - 检查是否存在 `host: "gorelay"` 的陈旧 reservation。
  - 计算 `created` 距离当前时间的秒数：若已超过 600s，需评估是否发生 runner 断开而未 release，可由 owner 进行 TTL recover。
- **总数与限额对比**：
  - 核对 `reservations` 总数是否达到 `LIMITS["total"]`（8 或 `topology.json` 中的配置值，见 `openswarm/bin/fleet-capacity:13-31`）。
  - 核对 `host:gorelay` 与 `host:gocc` 的实际占用数是否触顶导致后续任务被 `blocked`。

---

## 4. 辅助排查与应急操作

1. **测试 SSH 与 ControlMaster 状态**：
   在 box 上验证到 `gocc` 与 `gorelay` 的直接通道是否通畅：
   ```bash
   ssh -F ~/.ssh/openswarm-hub/config -O check gocc
   ssh -F ~/.ssh/openswarm-hub/config -O check gorelay
   ```

2. **临时暂停对应 Lane**：
   若退避导致频繁重试，可临时暂停 `A2` lane（`brain/autopilot.md:36,47`）：
   ```bash
   touch ~/phanes-ctl/autodispatch/PAUSE-A2
   ```
   排查恢复后再移除标记文件以恢复调度。
