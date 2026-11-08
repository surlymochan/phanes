# GH2 车道连续 exec_error（#1734）诊断与处置

本文记录 [GH2 诊断卡 #1734] 的结论、判据、证据采集方法与验收步骤。

现象（来自卡片）：2026-11-03 00:17–00:25，GH2（gores1 / pi）连续 4 个 run
（#1714 / #1716 / #1717 / #1718）均在约 2 分钟内以 `failed/exec_error` 结束、
无提交；同期 gores1 上 M8/M9 正常；总控已设 `PAUSE-GH2` 止损。

监督侧补充证据（卡片评论，2026-11-03 00:26 巡查单）：GH2 实际是 **9 连挂**，
时段 00:08:40–00:25:12，卡号 #1700 / #1685 / #1694 / #1702 / #1713 / #1714 /
#1716 / #1717 / #1718；00:25:55 又派了 #1663（在 `PAUSE-GH2` 生效前约 40 秒，
已在途，止损只能拦新派）。9 卡分流：#1713 已在 lane-ready/in-progress 跳过，
其余 8 张已回 lane-ready（环境故障致死，非卡面问题）。同一批评论把诊断范围
扩到同主机 M19（gores1 / pi，本日六连死），因此本卡按 GH2 + M19 + CX9 三条
车道给结论（见第 5 节）。

---

## 0. 结论摘要

1. **GH2 的连挂（卡片记 4 次、监督侧巡查记 9 次）是 executor 侧的“硬失败”，不是卡面问题、不是配额、不是风控、也不是超时。**
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
4. **与 CX9（#1709/#1710）、同主机 M19 是否同根因尚未知。** `failed/None` 与 `exec_error` 是不同的粗粒度记录，不能证明远端执行状态或排除共同故障；需取得两侧现场证据和 #1709/#1710 结论后判断。
5. **下一步的外部依赖**（本 worktree 无法完成，见第 8 节）：需要在 gores1 上采集
   连挂 run 目录（巡查口径 9 个）的 `events.*.pi.jsonl.err` 与 broker 请求日志，跑一次小卡验证，
   证据到齐即可摘 `PAUSE-GH2`。

---

## 1. 事实与边界

| 项 | 内容 | 来源 |
|---|---|---|
| 失败时间窗 | 卡片记 00:17–00:25；监督侧巡查记 00:08:40–00:25:12 | 卡片 + 评论 |
| 失败 run | 卡片记 #1714 / #1716 / #1717 / #1718（4 个）；巡查记 9 连挂：#1700 / #1685 / #1694 / #1702 / #1713 / #1714 / #1716 / #1717 / #1718 | 卡片 + 评论 |
| 同型扩展 | M19（gores1 / pi）当日六连死：#1361（04:23 无提交）/ #1793 / #1794 / #1771 / #1786 / #1690 | 评论（builder） |
| 车道 | GH2 = gores1 / pi；M19 = gores1 / pi；CX9 = gores2 | 卡片 + 评论 |
| 错误签名 | `failed/exec_error`，无提交 | 卡片 |
| 单次时长 | 约 2 分钟 | 卡片 |
| 同主机对照 | gores1 上 M8/M9 同期正常 | 卡片 |
| 止损 | `PAUSE-GH2`（legacy 标记文件，等价于 pause scope `lane:GH2`）、`PAUSE-M19` | `openswarm/lib/dispatch/pause.py:174` |

**本卡未能验证的部分（如实标注）**：本 worktree 里没有 fleet 主机、没有 gores1 的 run 目录、
没有 broker 请求日志，也没有 GitHub issue 的实时内容。因此第 2/3 节的判据是**代码级结论**，
第 4 节是最可能的机制假设，第 5 节是与 CX9 / M19 的**签名级**对比（根因未知）；
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
| 调度侧重试逻辑异常 | `exec_error` 的重试表是 `{"max": 2, "then": "lane_failed"}`（`openswarm/lib/dispatch/retry.py:67-77`），第 3 次起贴 `lane-failed`（`openswarm/lib/dispatch/engine.py:1441-1442`）；连挂次数与该表一致，属预期行为，不是 bug |

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
# 1) 连挂 run 的执行器 stderr（第一现场证据）
#    卡片给的是 issue 号，run 目录名是时间戳：巡查时段 00:08:40-00:25:12 → 20261103-0008* … 20261103-0025*
for d in ~/.claude/openswarm/runs/20261103-00{08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25}*; do
  [ -d "$d" ] || continue
  echo "== $d"; tail -n 40 "$d"/events.*.err
done

# 2) 归类与证据行（本卡新增工具；--json 便于回帖）
python3 tools/lane-exec-error-triage.py --runs ~/.claude/openswarm/runs --lane GH2 \
        --since 2026-11-03T00:05:00+08:00

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

比较输出恒为 `same_root_cause=None`：签名相同不能证明同根因，签名不同也不能排除同根因；
缺少任一车道时同样只报证据不足（并列出还缺的证据），工具不会替现场下结论。

### 5.1 同主机 M19 六连死：gores1 pi 栈共因待证

builder 的诊断范围扩展（卡片评论，2026-11-03）指出：gores1 的 pi 栈故障簇不限于
GH2 —— 同主机 M19 当日六连死（04:23 #1361 无提交、04:25 #1793、04:36 #1794、
04:50 #1771、05:41 #1786、13:48 #1690，均 `exec_error` / 速败），builder 已设
`PAUSE-M19`。

同主机、同 backend、同 `exec_error` 形态指向“gores1 的 pi 栈共因”这一**假设**，但本卡
不给结论：签名相同不能证明同根因，签名不同也不能排除同根因；要判定共因，必须拿到
两侧的 `events.*.pi.jsonl.err` 与 broker 请求日志，并核对 `lanes.json` 里两者的
`broker_socket` 是否同一个 socket（见 4.2 / 4.3）。

因此 GH2、M19、CX9 三条车道的处置口径一致：**证据到齐前只报告“根因未知”**，
按第 6 节判据逐车道试跑，摘哪条道的暂停以该车道的结论为准。

---

## 6. 验收：GH2 试跑一张小卡 + 摘 `PAUSE-GH2`

1. **先取证再恢复**：按 4.2 采集证据，把 `tools/lane-exec-error-triage.py` 的输出（带证据行）贴到本卡。
2. **修复动作**：按 4.3 判读表处理（最常见是重建 broker socket / 修 `broker_socket` 配置）。
3. **健康检查**：`python3 bin/openswarm doctor --lane GH2` 全部 `ok`
   （含 `pi_broker` 项，`openswarm/bin/openswarm:14409-14471`），
   并确认 lane supervisor 在跑：`python3 bin/openswarm lane status --json`。
4. **试跑一张小卡**（在 gores1 上，`GH2` 必须**成功结束**且 `error_class` **非** `exec_error`）：

   a) **常驻 lane 路径**（最快，验的就是同一条 broker socket / 会话链路）：

      ```sh
      python3 bin/openswarm lane up GH2          # 若 lane 未起
      python3 bin/openswarm lane assign GH2 --title "GH2 健康检查小卡" \
          --prompt "只读检查：读 openswarm/policy.json 的 pi 段并原样回报 provider 与 default_model，不要改任何文件。"
      ```

   b) **调度路径**（最贴近故障现场）：临时只放行 GH2（其余 lane 保持 PAUSE），
      把一张小卡标成 `lane-ready`，然后 `python3 bin/openswarm dispatch tick`，
      确认它被派到 GH2 并正常结束。

   判据（两项都要满足才算通过）：`result.json` 的 `status` 为 `done`（小卡成功结束），
   且 `error_class` 不为 `exec_error`；`failed/auth`、`failed/rate_limit`、
   `budget_exceeded` 等其它失败一律**不算通过**（故障未恢复时不得据此摘暂停）。
5. **回帖 + 摘暂停**：把试跑 run id、`result.json` 的 `status/error_class`、
   `tools/lane-exec-error-triage.py` 的结论贴回本卡，总控据此摘 `PAUSE-GH2`
   （`openswarm pause clear` / 删除 `PAUSE-GH2` 标记；`openswarm/lib/dispatch/pause.py:174`）。
6. **同一条判据适用于 M19 与 CX9**：各自车道的小卡试跑成功（`status=done`）且
   `error_class` 非 `exec_error` 之后，才建议摘该车道的暂停（`PAUSE-M19` 等），
   三条车道的摘挂顺序以各自结论为准。

> 注意重试语义：`exec_error` 重试上限 2 次，第 3 次起就是 `lane_failed`
> （`openswarm/lib/dispatch/retry.py:67-77`），所以“连挂”里第 3 次起不是调度器失灵，
> 而是止损策略按设计生效。

---

## 7. 本卡产出

| 文件 | 说明 |
|---|---|
| `tools/lane-exec-error-triage.py` | 车道失败分诊工具：按执行器同一套正则归类 `exec_error`，输出带文件名/行号的证据行，支持 `--broker-log` 交叉验证与 `--compare-lane` 签名对比（根因未知）；缺车道时只报证据不足；`--self-test` 离线自检 |
| `tools/tests/test_lane_exec_error_triage.py` | 工具单测（GH2 连挂形态、CX9 `failed/None` 形态、配额不被误判、信号 kill、broker 交叉验证） |
| `tools/tests/test_lane_exec_error_triage_1734.py` | #1734 复审单测（B1：签名相同/不同都不定根因、缺 `error_class` 状态未知、工具头说明；B3：缺比较车道的文本输出） |
| `tools/tests/test_gh2_exec_error_storm_1734.py` | #1734 文档验收单测（B2：试跑必须成功且 `error_class` 非 `exec_error`；交付说明的 B1/B2/B3 映射） |
| `docs/gh2-exec-error-storm-1734.md` | 本文 |

运行：

```sh
python3 tools/lane-exec-error-triage.py --self-test
python3 -m pytest -q tools/tests/test_lane_exec_error_triage.py
python3 -m pytest -q tools/tests/test_lane_exec_error_triage_1734.py \
                    tools/tests/test_gh2_exec_error_storm_1734.py
```

---

## 8. 外部依赖（本卡无法自行完成的部分）

1. **gores1 上的 run 产物**：`~/.claude/openswarm/runs/<run_id>/`（含 `events.*.pi.jsonl.err`），
   或 `openswarm fleet harvest --into` 拉回 box 后的副本。
2. **broker 请求日志**：`/run/openswarm-pi/broker.sock.requests.jsonl`（或 `PI_BROKER_LOG` 指向的文件）。
3. **lanes.json / fleet inventory**：确认 GH2 的 `broker_socket` 与 M8/M9 是否同 socket。
4. **GitHub issue #1709 / #1710 的正文与结论**：用于把 CX9 的结论与本卡并列引用（本卡只能引用其**签名**差异）。
5. **一次 GH2 小卡试跑**：见第 6 节第 4 步；成功后总控摘 `PAUSE-GH2`。

---

## 9. 交付说明（本轮修复 → 测试 → 结果）

复审（第 2 轮，CX4 / codex-gpt-6.1-terra）对 PR 头 `c95494e0e` 给出 CHANGES，要求
逐条给出 B1 / B2 / B3 的修复与验证证据。下表逐条对应；本 worktree 的 git 元数据只读，
提交由 AD 代做（本轮不写 GitHub）。

- **B1（车道比较不得凭粗粒度签名定论）**：本轮改动 —— `tools/lane-exec-error-triage.py` 的 `compare_lanes()` 恒返回 `same_root_cause=None`，理由改为“签名相同或不同都不能决定根因”；工具头说明同步改写（不再声称“签名相同才是同根因”，也不再声称缺少 `error_class` 的车道没进入执行器分类，改为**远端执行状态未知**）；本文第 5 节保持 CX9 / M19 的根因**尚无法判定**，等 #1709/#1710 结论与两侧现场证据到齐再判断。对应测试 —— `tools/tests/test_lane_exec_error_triage_1734.py` 的 `test_equal_signatures_do_not_prove_a_common_root_cause`、`test_different_signatures_do_not_rule_a_common_root_cause_out`、`test_a_lane_without_an_error_class_keeps_its_remote_state_unknown`、`test_the_header_does_not_deduce_the_root_cause_from_the_signature`、`test_the_header_says_that_the_signature_cannot_decide_the_root_cause`、`test_the_header_says_that_a_missing_error_class_leaves_the_state_unknown`，以及 `tools/tests/test_gh2_exec_error_storm_1734.py` 的 `test_the_cx9_comparison_stays_open`、`test_the_cx9_comparison_refers_to_the_other_cards`。结果：通过。

- **B2（GH2 小卡试跑的通过判据）**：本轮改动 —— 第 6 节判据改为“两项都要满足才算通过”：`result.json` 的 `status` 为 `done`（小卡成功结束）且 `error_class` 不为 `exec_error`；删掉了上一轮只要 `error_class` 非 `exec_error` 即可放行的弱判据，`failed/auth` / `failed/rate_limit` / `budget_exceeded` 一律不算通过；M19 与 CX9 用同一条判据，摘暂停以各自车道的结论为准。对应测试 —— `tools/tests/test_gh2_exec_error_storm_1734.py` 的 `test_the_trial_run_criterion_requires_a_successful_run`、`test_the_trial_run_criterion_names_the_error_class`、`test_the_weak_not_exec_error_criterion_is_gone`。结果：通过。

- **B3（缺比较车道时的输出与 harvest 证据）**：本轮改动 —— `compare_lanes()` 在缺少任一车道时返回 `same_root_cause=None`，`reason` 里点名缺哪条车道、还需要哪些现场证据；`render_text()` 改为先取 `compare.get("lanes")` 再按位取值，缺车道时打印 `-` 而不是崩溃；本文第 5 节写清示例所需输入（`--runs` 要含两条车道各自的 run 目录，`--lane` 不得过滤掉被比较的车道）与证据不足时的输出；`--harvest` 的取回失败记录单独列出，不并入车道签名比较。对应测试 —— `tools/tests/test_lane_exec_error_triage_1734.py` 的 `test_a_missing_comparison_lane_is_reported_as_insufficient_evidence`、`test_the_documented_command_runs_and_reports_the_missing_lane`、`test_two_lanes_on_the_same_host_are_not_joined_by_the_tool`，以及 `tools/tests/test_lane_exec_error_triage.py` 的 `test_missing_lane_and_equal_signatures_remain_unknown`。结果：通过。

### 9.1 验证证据（本轮实际执行）

```text
$ python3 -m unittest tools.tests.test_lane_exec_error_triage_1734 tools.tests.test_lane_exec_error_triage
Ran 20 tests in 0.196s

OK
$ python3 -m unittest tools.tests.test_gh2_exec_error_storm_1734
Ran 8 tests in 0.009s

OK
$ python3 tools/lane-exec-error-triage.py --self-test
ok
```

复审侧同款命令（本轮 CI / 评审环境的验收口径）：

```sh
python3 -m pytest -q tools/tests/test_lane_exec_error_triage.py
python3 -m pytest -q tools/tests/test_lane_exec_error_triage_1734.py \
                    tools/tests/test_gh2_exec_error_storm_1734.py
```

> 说明：本 worktree 的沙箱里没有安装 `pytest`（`pip install pytest` 被环境的 CA 证书
> 缺失挡住），上面的结果用同一批 `unittest` 用例跑出；这些用例是纯 `unittest.TestCase`，
> `pytest` 可直接收集执行。
