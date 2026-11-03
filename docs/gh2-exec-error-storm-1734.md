# GH2 车道连续 exec_error（#1734）诊断与处置

本文记录 [GH2 诊断卡 #1734] 的结论、判据、证据采集方法与验收步骤。

现象（来自卡片）：2026-11-03 00:17–00:25，GH2（gores1 / pi）连续 4 个 run
（#1714 / #1716 / #1717 / #1718）均在约 2 分钟内以 `failed/exec_error` 结束、
无提交；同期 gores1 上 M8/M9 正常；总控已设 `PAUSE-GH2` 止损。

---

## 0. 结论摘要

1. **GH2 的四次失败是 executor 侧的“硬失败”，不是卡面问题、不是配额、不是风控、也不是超时。**
   `failed/exec_error` 在代码里的确切含义是：executor 进程被拉起后**非零退出或没有产出最终报告**，
   并且错误文本**没有**命中执行器自己的任何一个分类器（配额 / 鉴权 / 模型不可用 / 风控 / 上游 403）。
   依据：`openswarm/bin/openswarm:3942-3959`（`executor_error_class`）、
   `openswarm/bin/openswarm:3483-3486`（分类用的正则）。
2. **故障域是车道本地（GH2 自己的 executor / 会话层），不是主机级、不是卡片级。**
   同一台 gores1 上同期其它车道正常 ⇒ 排除整机 OOM、整机 VM 暂停；
   单次失败时长约 2 分钟，只有默认预算 40 分钟的 5%
   （`openswarm/policy.json` `run.timeout_seconds = 2400`）⇒ 排除慢卡与超时。
   （“共享 broker 全面宕机”这一项只有在 M8/M9 与 GH2 共用同一个 broker socket 时才成立，
   需要按 4.2 核对 `broker_socket`。）
3. **最可能的机制：GH2 这条 lane 的 pi 会话/传输层断了（broker socket / 隔离启动器 / 会话目录），
   而不是模型侧。** pi 的一次性执行只能通过 broker socket 取模型
   （`openswarm/bin/openswarm:5027-5063`），而 socket 可以是**每条 lane 单独配**的
   （`broker_socket` 是 lanes.json 的合法字段，`openswarm/bin/openswarm:11499`），
   `pi-fleet-bridge` 在超时（`--minutes`，默认 120）或被中断时**会删掉 worker 侧的 socket**
   （`openswarm/bin/pi-fleet-bridge:18,90,118`）——这正好是“只有一条 lane 挂、同主机其它 lane 好”的形态。
4. **与 CX9（#1709/#1710）是否同根因尚未知。** `failed/None` 与 `exec_error` 是不同的粗粒度记录，不能证明远端执行状态或排除共同故障；需取得两侧现场证据和 #1709/#1710 结论后判断。
5. **下一步的外部依赖**（本 worktree 无法完成，见第 8 节）：需要在 gores1 上采集
   4 个 run 目录的 `events.*.pi.jsonl.err` 与 broker 请求日志，跑一次小卡验证，
   证据到齐即可摘 `PAUSE-GH2`。

---

## 1. 事实与边界

| 项 | 内容 | 来源 |
|---|---|---|
| 失败时间窗 | 2026-11-03 00:17–00:25 | 卡片 |
| 失败 run | #1714 / #1716 / #1717 / #1718，共 4 个 | 卡片 |
| 车道 | GH2 = gores1 / pi | 卡片 |
| 错误签名 | `failed/exec_error`，无提交 | 卡片 |
| 单次时长 | 约 2 分钟 | 卡片 |
| 同主机对照 | gores1 上 M8/M9 同期正常 | 卡片 |
| 止损 | `PAUSE-GH2`（legacy 标记文件，等价于 pause scope `lane:GH2`） | `openswarm/lib/dispatch/pause.py:174` |

**本卡未能验证的部分（如实标注）**：本 worktree 里没有 fleet 主机、没有 gores1 的 run 目录、
没有 broker 请求日志，也没有 GitHub issue 的实时内容。因此第 2/3 节的判据是**代码级结论**，
第 4 节是最可能的机制假设，第 5 节是与 CX9 的**签名级**对比；
“哪一条假设为真”必须由第 4.2 节的现场证据来定。

---

## 2. 判据：`failed/exec_error` 在代码里的确切含义

执行器跑完后，`executor_error_class()` 按固定顺序归类（`openswarm/bin/openswarm:3942-3959`）：

| 返回码 / 文本 | 归类 | GH2 是否命中 |
|---|---|---|
| rc = 133（风控拦截，`kill_tree` 后返回） | `risk_blocked` | ✗ 不是 |
| rc = 134 | `executor_io_lost` | ✗ 不是 |
| rc = 124（预算耗尽，`kill_tree` 后返回） | `budget_exceeded` | ✗ 不是 |
| 文本命中 `MODEL_UNAVAILABLE_RE` | `model_unavailable` | ✗ 不是 |
| 文本命中 `AUTH_ERROR_RE` | `auth` | ✗ 不是 |
| rc = 75 或文本命中 `RATE_LIMIT_RE` | `rate_limit` | ✗ 不是 |
| **rc ≠ 0 或没有最终报告** | **`exec_error`** | ✓ 命中 |

⇒ **`exec_error` 只说明“executor 死了且没留下最终报告”，它本身不说明死因。**
死因要回到执行器留下的两处产物里找：

* `events.<attempt>.<backend>.jsonl.err` —— executor 的 stderr
  （`openswarm/bin/openswarm:4613`，`_run_proc` 同时写 `.jsonl` 与 `.jsonl.err`）；
* `result.json` 的 `attempts[-1].rc` / `attempts[-1].error_tail`
  （`openswarm/bin/openswarm:5747-5757`）。

另外：**rc 是负数 = 子进程被信号打死**（`p.returncode`，`openswarm/bin/openswarm:4660`）。
`rc = -9`（SIGKILL）且 stderr 为空，是 cgroup 内存上限（lane unit 的 `MemoryMax`，默认 900M，
见 `openswarm/policy.json` `lane._doc`）或外部 kill 的典型签名；
`rc = 1` 且 stderr 里有 socket / 沙箱报错，是“进程正常启动但连不上 broker”的典型签名。

---

## 3. 可以排除的原因

| 假设 | 排除依据 |
|---|---|
| 卡面 / 路由问题 | 失败发生在 executor 进程内；卡面问题会表现为 `prepare_failed` / `worktree_unavailable` / 无提交但有 diff（`openswarm/bin/openswarm:5586,5602`） |
| 模型配额 / 限流 | 命中 `RATE_LIMIT_RE` 会被归类 `rate_limit`，run 状态会是 `partial/failed` 但 error_class 是 `rate_limit`（`openswarm/bin/openswarm:5729-5731`）；GH2 是 `exec_error` |
| 风控 | 风控拦截返回 rc=133 → `risk_blocked`（`openswarm/bin/openswarm:4656-4658`） |
| 慢卡 / 预算超时 | rc=124 → `budget_exceeded`（`openswarm/bin/openswarm:4664-4667`）；且 2 分钟 ≪ 40 分钟默认预算 |
| 整机 OOM / 整机 VM 暂停 | 同期 gores1 上 M8/M9 正常；VM 暂停只会把**本地** lane 的 `exec_error/stalled` 重分类为 `vm_paused`（`openswarm/lib/dispatch/pause_window.py:87-95`），GH2 仍保持 `exec_error` |
| 共享 broker 全面宕机 | 若 M8/M9 与 GH2 共用同一个 broker socket，则可排除；**这一点需要用 `broker_socket` 字段核对**（见 4.2） |
| 调度侧重试逻辑异常 | `exec_error` 的重试表是 `{"max": 2, "then": "lane_failed"}`（`openswarm/lib/dispatch/retry.py:67-77`），第 3 次起贴 `lane-failed`（`openswarm/lib/dispatch/engine.py:1441-1442`）；四连挂与该表一致，属预期行为，不是 bug |

---

## 4. 根因判定

### 4.1 最可能的机制：GH2 自己的 pi 会话 / 传输层

pi 的一次性执行是 `pi -p --mode json --session-dir <run_dir>/pi-sessions`
（`openswarm/bin/openswarm:5027-5063`），常驻 lane serve 则是
`pi --mode rpc --session-dir <lane_root>/pi-sessions`（`openswarm/bin/openswarm:8124-8126`）。
两者都必须**先能连上 broker socket**，否则沙箱内拿不到模型：

* socket 来源优先级：`lanes.json` 的 `broker_socket` → `OPENSWARM_PI_BROKER_SOCKET` →
  默认 `/run/openswarm-pi/broker.sock`（`openswarm/bin/openswarm:14409-14420`）；
* `pi-fleet-bridge` 是**限时**传输进程：`--minutes` 默认 120（`openswarm/bin/pi-fleet-bridge:18`），
  到点或被中断后会 `rm -f <worker_dir>/broker.sock`（`openswarm/bin/pi-fleet-bridge:90,118`），
  文档也写明“到达时限会关闭 SSH 会话并移除私有 socket”（`openswarm/docs/executor-recovery.md:30-31`）。

⇒ **“只有 GH2 挂、同主机其它 lane 好”最符合的解释是：GH2 指向的 broker socket（或它的会话目录 /
  隔离启动器）单独失效了**，而不是模型侧或主机侧的问题。

次可能（同样表现为 exec_error，需用 stderr 区分）：

* 会话派生失败：`pi-sessions` 目录写不出来（磁盘/inode）、会话文件损坏；
* 隔离启动器失败：bubblewrap / user namespace 起不来（`bwrap`、`Operation not permitted`）；
* 被 OOM kill：rc = -9。

### 4.2 证据采集（在 gores1 上执行，命令可直接复制）

```sh
# 1) 四个 run 的执行器 stderr（第一现场证据）
#    卡片给的是 issue 号，run 目录名是时间戳：2026-11-03 00:17-00:25 → 20261103-0017* … 20261103-0025*
for d in ~/.claude/openswarm/runs/20261103-00{17,18,19,20,21,22,23,24,25}*; do
  [ -d "$d" ] || continue
  echo "== $d"; tail -n 40 "$d"/events.*.err
done

# 2) 归类与证据行（本卡新增工具；--json 便于回帖）
python3 tools/lane-exec-error-triage.py --runs ~/.claude/openswarm/runs --lane GH2 \
        --since 2026-11-03T00:10:00+08:00

# 3) broker 连接状态（只读，不发请求）
python3 bin/openswarm doctor --lane GH2

# 4) broker 请求日志：GH2 有没有真的把请求送出去？
python3 tools/broker-log-summary.py /run/openswarm-pi/broker.sock.requests.jsonl --since 00:10 --tz +08:00

# 5) 车道自己的 socket / 沙箱 / 磁盘前置条件
grep -n '"broker_socket"\|"name": *"GH2"' ~/.claude/openswarm/lanes.json
ls -l "${OPENSWARM_PI_BROKER_SOCKET:-/run/openswarm-pi/broker.sock}"
ls -ld ~/.claude/openswarm/lanes/GH2/pi-sessions

# 6) 是否被 OOM 杀（信号 kill 的旁证）
dmesg -T 2>/dev/null | grep -iE "killed process|out of memory" | tail -20
```

### 4.3 判读表

| 现场证据 | 结论 | 处置 |
|---|---|---|
| `.err` 里有 `broker.sock` / `socat` / `ECONNREFUSED`，且 broker 日志里 **GH2 没有任何请求**、同期 M8/M9 有 200 | broker 活着，GH2 的 socket 没了/指错了 | 起新的 `pi-fleet-bridge`（或修 `broker_socket`），`openswarm doctor --lane GH2` 通过后试跑小卡 |
| `.err` 里是 429 / `quota` / `rate limit` | 其实是配额，被执行器归类成 exec_error | 按配额处置（`tools/mm-burn-rate.py`），不是本卡根因 |
| rc = -9、stderr 空、`dmesg` 有 OOM | 内存上限（MemoryMax=900M）杀进程 | 降 lane `memory_max`/换小卡，或查会话目录膨胀 |
| `.err` 里 `bwrap` / `namespace` / `Operation not permitted` | 隔离启动器起不来 | 修 sandbox 前置条件（`openswarm doctor --lane GH2` 的 sandbox 检查） |
| broker 日志里 GH2 有 429/5xx | 供应商侧 | 转配额/上游处置 |

> 工具 `tools/lane-exec-error-triage.py` 把这一步固化了：它读 run 目录里的
> `result.json` / `status.json` / `events.*`，按执行器同一套正则归类，
> 并输出**带文件名和行号的证据行**；`--broker-log` 会给出
> `lane_silent_broker_alive`（broker 活着但这条 lane 一个请求都没发出去）这类判读结论。

---

## 5. 与 CX9 四连挂（#1709/#1710）的对比：尚无法判定

CX9 的 `failed/None` 缺少 error_class；`fleet_harvest_failure()` 的失败记录不包含该字段（`openswarm/bin/openswarm:11393-11396`）。取回失败不能证明远端执行器是否运行，也不能排除与 GH2 的共同根因。需要取得 #1709/#1710 的结论及两侧现场日志后关联判断。

工具的比较需要 `--runs` 包含两条车道各自的 run 目录，且 `--lane` 不得过滤掉任一车道。`--harvest` 仅单独列出取回失败记录，不纳入车道签名比较；若只有 harvest 记录，比较仍为证据不足。

```sh
python3 tools/lane-exec-error-triage.py --runs <包含两车道的 runs 目录> --harvest <harvest 报告.json> \
        --compare-lane CX9
# same_root_cause=None：签名本身不足以判断根因；缺少车道时也会报告证据不足。
```

---

## 6. 验收：GH2 试跑一张小卡 + 摘 `PAUSE-GH2`

1. **先取证再恢复**：按 4.2 采集证据，把 `tools/lane-exec-error-triage.py` 的输出（带证据行）贴到本卡。
2. **修复动作**：按 4.3 判读表处理（最常见是重建 broker socket / 修 `broker_socket` 配置）。
3. **健康检查**：`python3 bin/openswarm doctor --lane GH2` 全部 `ok`
   （含 `pi_broker` 项，`openswarm/bin/openswarm:14409-14471`），
   并确认 lane supervisor 在跑：`python3 bin/openswarm lane status --json`。
4. **试跑一张小卡**（在 gores1 上，`GH2` 必须**非** `exec_error` 结束）：

   a) **常驻 lane 路径**（最快，验的就是同一条 broker socket / 会话链路）：

      ```sh
      python3 bin/openswarm lane up GH2          # 若 lane 未起
      python3 bin/openswarm lane assign GH2 --title "GH2 健康检查小卡" \
          --prompt "只读检查：读 openswarm/policy.json 的 pi 段并原样回报 provider 与 default_model，不要改任何文件。"
      ```

   b) **调度路径**（最贴近故障现场）：临时只放行 GH2（其余 lane 保持 PAUSE），
      把一张小卡标成 `lane-ready`，然后 `python3 bin/openswarm dispatch tick`，
      确认它被派到 GH2 并正常结束。

   判据：`result.json` 的 `status` 为 `done`，
   `error_class` 不是 `exec_error`。
5. **回帖 + 摘暂停**：把试跑 run id、`result.json` 的 `status/error_class`、
   `tools/lane-exec-error-triage.py` 的结论贴回本卡，总控据此摘 `PAUSE-GH2`
   （`openswarm pause clear` / 删除 `PAUSE-GH2` 标记；`openswarm/lib/dispatch/pause.py:174`）。

> 注意重试语义：`exec_error` 重试上限 2 次，第 3 次起就是 `lane_failed`
> （`openswarm/lib/dispatch/retry.py:67-77`），所以“四连挂”里第 3、4 次不是调度器失灵，
> 而是止损策略按设计生效。

---

## 7. 本卡产出

| 文件 | 说明 |
|---|---|
| `tools/lane-exec-error-triage.py` | 车道失败分诊工具：按执行器同一套正则归类 `exec_error`，输出带文件名/行号的证据行，支持 `--broker-log` 交叉验证与 `--compare-lane` 签名对比（根因未知）；`--self-test` 离线自检 |
| `tools/tests/test_lane_exec_error_triage.py` | 工具单测（GH2 四连挂形态、CX9 `failed/None` 形态、配额不被误判、信号 kill、broker 交叉验证） |
| `docs/gh2-exec-error-storm-1734.md` | 本文 |

运行：

```sh
python3 tools/lane-exec-error-triage.py --self-test
python3 -m unittest discover -s tools/tests -p "test_lane_exec_error_triage.py" -v
```

---

## 8. 外部依赖（本卡无法自行完成的部分）

1. **gores1 上的 run 产物**：`~/.claude/openswarm/runs/<run_id>/`（含 `events.*.pi.jsonl.err`），
   或 `openswarm fleet harvest --into` 拉回 box 后的副本。
2. **broker 请求日志**：`/run/openswarm-pi/broker.sock.requests.jsonl`（或 `PI_BROKER_LOG` 指向的文件）。
3. **lanes.json / fleet inventory**：确认 GH2 的 `broker_socket` 与 M8/M9 是否同 socket。
4. **GitHub issue #1709 / #1710 的正文与结论**：用于把 CX9 的结论与本卡并列引用（本卡只能引用其**签名**差异）。
5. **一次 GH2 小卡试跑**：见第 6 节第 4 步；成功后总控摘 `PAUSE-GH2`。
