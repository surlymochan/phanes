# Coordinator：relay.example.invalid↔broker SSH 横幅超时退避与观察指标

本文档记录在 10 路/11 路并行高并发场景下，`relay.example.invalid → broker` reverse tunnel 发生 SSH protocol banner exchange timeout 的现象、现有退避机制（30s → 600s）以及建议的观察与诊断命令。

依据与核对基准：
- `brain/dual-mode-status.md:13`（`coordinator 在 broker`，relay.example.invalid↔broker 高峰 banner timeout 退避记录）
- `brain/autopilot.md:20-22,34`（10+ 路并行拓扑与状态监控脚本）
- `docs/fleet-topology-expected.md:6-18`（Fleet topology 各主机并发限额约定）
- `docs/dual-mode-ssh.md:32-41,53-75`（ControlMaster 配置与 SSH fleet 拓扑）
- `swrm/bin/swrm:3345-3354`（`fleet_guard_call` 超时与 fail-closed 准入）
- `swrm/bin/fleet-capacity:12-34,90-115`（`fleet-capacity` 限额、status 与 recover 机制）

---

## 1. 现象说明

### 1.1 架构与背景
在当前的 10+ 并行受控拓扑中（`brain/autopilot.md:22`、`docs/fleet-topology-expected.md:6-18`）：
- 控制面统一运行在 `box`，维护 10–11 个并发 lane。
- 全局资源协调器（`fleet-capacity`）部署在 `broker` 主机上，负责控制跨主机和跨后端的并发限额。
- 执行器主机（如 `relay.example.invalid`，运行 A2 lane 或 codex 执行器）通过反向隧道（reverse tunnel / SSH 转发）连通 `broker` 上的协调器以进行准入（`acquire`）、释放（`release`）与状态检查（`status`）。

### 1.2 Banner-Exchange Timeout 现象
当 fleet 处于 10 路满负荷执行，且同时伴随多个主机间的 SSH 握手、状态查询、心跳检测与任务派发时：
1. `relay.example.invalid` 向 `broker` 建立 SSH 反向通道或 ControlMaster 控制连接时，在协议握手阶段发生阻塞。
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
   - `fleet_guard_call` 对每次协调器调用设置 8 秒硬超时；acquire 最多连续尝试 3 次，按 0.2s、0.4s 退避。
   - 连续失败时 run 保持 `queued`，状态写入 `queue_reason: capacity-unreachable`、`capacity_unreachable: true` 与最后错误；stderr 记录 acquire 失败次数。
   - acquire 仅在确认获得 reservation 后启动执行器，协调器不可达不会误判为容量不足。
   - acquire 发出前先写入 `fleet-reservation.json`；响应丢失后仍使用同一 token 重试和恢复，避免丢失协调器上已经提交的预约。
   - acquire 连续失败后返回 queued，不进入 finalizing，也不记为 budget_exceeded；lane 保留当前任务，在可处理控制消息的 30 秒退避后重新准入。独立 run 的 runner 也保持存活，在可响应停止请求的 30 秒退避后重试同一任务、工作区和 token，协调器恢复后自动继续执行。
   - acquire 网络调用和退避不持有本机 provider-concurrency 全局锁；确认远端准入后重新加锁检查本机限额，避免慢链路拖延其他本机任务或并发超限。
   - release 先写入 `fleet-release-pending.json`，由独立进程补发，结束流程不等待网络。补发单次仍有 8 秒超时，失败后指数退避至 60 秒，成功后删除待发记录。CLI/常驻 supervisor 启动会扫描 runs 下的待发记录，重启补发进程；每个 run 的文件锁避免重复消费者。
   - release 落盘后立即移交进程内预约所有权，允许 AGY 上游 403 的既有重试路径继续；下一次 acquire 等待待发记录确认清除后才发起新预约，避免跨进程缓存残留或旧 release 删除新 token。
   - consult 未获准入也可能已经在远端提交预约；结束时按落盘 token 补发 release，清理临时目录前检查预约和待发记录，保留全部恢复凭据。

3. **预约清扫兜底**：
   - 预约不会仅因年龄超过 600 秒自动过期。acquire 结果未知时保留 token 证据，后续以同一 token 恢复；已结束执行器的 release 可安全幂等补发。
   - supervisor 异常退出且执行器生死未知的预约，仍需依据本机进程证据确认全部执行器已停止，再使用显式 `recover`；不通过 TTL 释放可能仍在执行的 slot。

4. **派发节拍平缓**：
   - box 上的 `lane-autodispatch` 循环按 60s 一 tick 运行（`brain/autopilot.md:10-11`），保证派发层不会对正在退避的主机产生密集连接冲击。

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
- **Lane 运行状态**：观察 `A2`（relay.example.invalid）以及 `A1`（broker）的状态是否为 `busy`、`idle` 或异常停留在 `queued`。
- **心跳时戳（Heartbeat）**：检查各 lane 的最后心跳时间；若心跳超过 **3 分钟** 未更新，说明对应 worker 可能被阻塞在网络握手或退避中。
- **派发日志输出**：
  ```bash
  tail -n 100 ~/phanes-ctl/autodispatch/autodispatch.log
  ```
  核对是否出现 `coordinator unavailable`、`banner exchange timeout` 或退避等待日志。

---

### 3.2 观察协调器预约状态（coordinator status）

通过 box 上的受控 SSH 渠道向 `broker` 主机查询协调器当前预约快照：

```bash
ssh -F ~/.ssh/swrm-hub/config broker -- \
  python3 ~/.claude/swrm-fleet-capacity/fleet-capacity broker <<< '{"op":"status"}'
```

若直接在 `broker` 宿主机或本地测试环境下：

```bash
python3 swrm/bin/fleet-capacity broker <<< '{"op":"status"}'
```

**返回结构与重点观察指标**：
```json
{
  "ok": true,
  "reservations": [
    {
      "host": "relay.example.invalid",
      "run_id": "run-xxxxxx",
      "backend": "agy",
      "provider": null,
      "created": 1730401200.0
    }
  ]
}
```

- **`reservations` 列表**：
  - 检查是否存在 `host: "relay.example.invalid"` 的陈旧 reservation。
  - 计算 `created` 距离当前时间的秒数：若已超过 600s，需评估是否发生 runner 断开而未 release，可由 owner 确认执行器停止后进行显式 recover。
- **总数与限额对比**：
  - 核对 `reservations` 总数是否达到 `LIMITS["total"]`（8 或 `topology.json` 中的配置值，见 `swrm/bin/fleet-capacity:13-31`）。
  - 核对 `host:relay.example.invalid` 与 `host:broker` 的实际占用数是否触顶导致后续任务被 `blocked`。

---

## 4. 辅助排查与应急操作

1. **测试 SSH 与 ControlMaster 状态**：
   在 box 上验证到 `broker` 与 `relay.example.invalid` 的直接通道是否通畅：
   ```bash
   ssh -F ~/.ssh/swrm-hub/config -O check broker
   ssh -F ~/.ssh/swrm-hub/config -O check relay.example.invalid
   ```

2. **临时暂停对应 Lane**：
   若退避导致频繁重试，可临时暂停 `A2` lane（`brain/autopilot.md:36,47`）：
   ```bash
   touch ~/phanes-ctl/autodispatch/PAUSE-A2
   ```
   排查恢复后再移除标记文件以恢复调度。
