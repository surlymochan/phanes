# 故障名额回收实测（kill 注入）

Refs #1477（Refs #1468）。

## 结论

**kill 掉 lane 的执行器或 supervisor 之后，并发名额会被回收，排队中的派发会被放行并跑完（可再派）。**

本仓库的 openswarm 侧已经具备这条回收语义，AGREED v0.2 的 P0 只需要在非生产环境把它**实测**出来，
不需要新增功能：

| 注入 | 回收路径 | 实测回收耗时 | 名额回收后 |
|------|----------|--------------|------------|
| `kill -9` 执行器（lane 内执行器崩溃） | TTL `run-finished` 释放（`fleet_recovery_outcome=run-finished`） | **2.145 s** | 排队的 L2 立刻拿到名额并跑完（23.252 s 内 `status=done`） |
| `kill -9` lane supervisor（宿主/进程组故障） | TTL `dead-runner` 回收（`fleet_recovered_at` 落盘） | **1.399 s** | 排队的 L2 拿到名额并跑完（21.810 s 内 `status=done`） |
| `openswarm stop`（既有优雅停止，对照组） | lane abort → 名额直接释放 | **0.025 s** | 排队的 L2 拿到名额并跑完（20.864 s 内 `status=done`） |

三个场景里，**故障后的名额都不是「自动有人收尸」的**：回收由**存活的 supervisor** 在
`reserve_provider_slot` 的排队重试里顺带做（每秒一次 `recover_expired_fleet_slots`），
或者由**重启后的 supervisor** 做。详见「回收是谁在做」与「边界」。

演练脚本：`tools/quota-reclaim-drill.py`（纯离线，只用 stdlib + 仓内既有语义）。
本卡就是 issue #1477 验收要的那份仓内副本：注入步骤、前后 `lanes-status`/名额快照、
带时间戳的回收成功证据都贴在下面。

## 短跑手册（60–120 秒，离线）

```bash
# 三个场景都跑（executor / supervisor / stop），沙箱在仓库内 .openswarm-tmp/q（已 gitignore）
python3 tools/quota-reclaim-drill.py

# 只跑一个场景
python3 tools/quota-reclaim-drill.py --scenario executor

# 保留沙箱以便人工复查（lanes.json / state.json / runs/ 都在里面）
python3 tools/quota-reclaim-drill.py --scenario supervisor --keep

# 把证据 JSON 落一份到仓内可复核的位置（沙箱之外，不会被演练收走）
python3 tools/quota-reclaim-drill.py --evidence-out docs/quota-reclaim-drill-evidence.json
```

演练不连生产主机、不碰凭据、不改 AD：lane 跑在沙箱里的 `OPENSWARM_HOME`，
pi 后端用仓内 fixture `openswarm/tests/fixtures/fake_pi_rpc.py` 的副本，
容量协调器跑本机 `fleet-capacity`（`HOST=local`，上限 1 个并发名额）。

## 注入步骤（手工等价流程）

演练脚本做的就是下面这套既有语义，手工也可以照着做一遍：

```bash
# 0) 沙箱：两条 lane 各一个 repo，容量上限 1 个名额
export OPENSWARM_HOME=<sandbox>/home
export OPENSWARM_FLEET_CAPACITY_DIR=<sandbox>/capacity      # topology.json: total=1, host:local=1, pi=1, minimax-cn=1
export OPENSWARM_FLEET_GUARD_CONFIG=<sandbox>/guard.json    # command = fleet-capacity local
python3 openswarm/bin/openswarm lane up L1 L2                # 既有语义：起常驻 lane
python3 openswarm/bin/openswarm lane assign L1 --run-id drill-holder --prompt "..."   # 抢到唯一名额
python3 openswarm/bin/openswarm lane assign L2 --run-id drill-waiter --prompt "..."   # 排队：queue_reason=provider concurrency limit

# 1) 前后快照
python3 openswarm/bin/openswarm lane status                  # lanes-status（JSON 数组）
echo '{"op":"status"}' | python3 openswarm/bin/fleet-capacity local   # 名额 reservations

# 2) 注入故障（三个场景，三选一）
kill -9 <executor_pid>            # 场景 A：杀执行器（pid 来自 runs/<id>/status.json 的 executor_pid）
kill -9 <supervisor_pid>          # 场景 B：杀 lane supervisor（pid 来自 lanes/<name>/state.json）
python3 openswarm/bin/openswarm stop drill-holder   # 场景 C：既有优雅停止

# 3) 回收证据：等 TTL（演练里 fleet.recovery_ttl_seconds=2）
#    - reservations 变空
#    - runs/<id>/status.json 出现 fleet_recovered_at
#    - 排队的 run 从 queued 变成 running → finished/done
python3 openswarm/bin/openswarm lane status
echo '{"op":"status"}' | python3 openswarm/bin/fleet-capacity local
```

## 实测证据

命令与时间（时区统一为 Asia/Shanghai，UTC+08:00；沙箱路径缩写为 `…/.openswarm-tmp/q`）：

```
$ python3 tools/quota-reclaim-drill.py
executor   started 2026-11-02 05:34:52  finished 2026-11-02 05:35:16  ok=True
supervisor started 2026-11-02 05:35:17  finished 2026-11-02 05:35:40  ok=True
stop       started 2026-11-02 05:35:41  finished 2026-11-02 05:36:03  ok=True
```

时间与快照里的 epoch 字段可直接换算核对，例如 `1793568893.076` = `date -d @1793568893`（TZ=Asia/Shanghai）
= 2026-11-02 05:34:53 +08:00。演练脚本用 `time.localtime` 打印时间，在 UTC 主机上会显示 `2026-11-02 21:34:53`，
那是同一个时刻的 UTC 写法，不是另一次运行。

三个场景都 `ok=True`：名额回收后，排队的那条派发都被放行并跑完（`status=done`），
`fleet_reservation_error` 为空。

### 场景 A：`kill -9` 执行器

事件时间线（`tools/quota-reclaim-drill.py` 记录的 `events`）：

| 时间 | 事件 |
|------|------|
| 2026-11-02 05:34:53 | `kill -9 executor`，pid=29521，run=`drill-executor-holder`，lane=L1 |
| 2026-11-02 05:34:55 | **名额回收**（`slot reclaimed`，故障后 2.145 s；`fleet_recovered_at=1793568895.551`） |

注入前（`before`，2026-11-02 05:34:53）：

```
lanes-status:
  L1  state=busy  current_run=drill-executor-holder  run_state=running  last_error=null
  L2  state=busy  current_run=drill-executor-waiter  run_state=queued   last_error=null
名额 reservations:
  [{"host":"local","run_id":"drill-executor-holder","backend":"pi","provider":"minimax-cn","created":1793568893.076}]
slot_holder=drill-executor-holder
  drill-executor-holder: state=running fleet_host=local fleet_reserved_at=1793568893.081 fleet_recovered_at=null
  drill-executor-waiter: state=queued  queue_reason="provider concurrency limit" fleet_reserved_at=null
协调器探针（再加一个名额）: {"ok":true,"admitted":false,"blocked":["host:local","minimax-cn","pi","total"]}
```

注入后（`after`，2026-11-02 05:35:16）：

```
lanes-status:
  L1  state=idle  current_run=null  run_state=null  last_error=null
  L2  state=idle  current_run=null  run_state=null  last_error=null
名额 reservations: []            ← 名额已回收
slot_holder=None
  drill-executor-holder: state=finished status=failed
                         fleet_recovered_at=1793568895.551  fleet_recovery_outcome=run-finished
                         recovered_reason=run_finished_child_resident  fleet_reservation_error=null
  drill-executor-waiter: state=finished status=done  fleet_reserved_at=1793568896.352  ← 名额回收后被放行并跑完
```

### 场景 B：`kill -9` lane supervisor

| 时间 | 事件 |
|------|------|
| 2026-11-02 05:35:18 | `kill -9 lane supervisor`，pid=29756，run=`drill-supervisor-holder`，lane=L1 |
| 2026-11-02 05:35:19 | `lane up L1`（既有语义：重启 supervisor，0.427 s） |
| 2026-11-02 05:35:20 | **名额回收**（故障后 1.399 s；`fleet_recovered_at=1793568920.445`） |

注入前（05:35:18）与场景 A 同形：L1 `run_state=running` 占住唯一名额，
L2 `run_state=queued`（`queue_reason=provider concurrency limit`），
协调器探针 `admitted=false`（`blocked=["host:local","minimax-cn","pi","total"]`）。

注入后（05:35:40）：

```
lanes-status:
  L1  state=idle  current_run=null  run_state=null  last_error=null
  L2  state=idle  current_run=null  run_state=null  last_error=null
名额 reservations: []            ← 名额已回收
slot_holder=None
  drill-supervisor-holder: state=running（supervisor 被杀，终态没写出来）
                           fleet_reserved_at=1793568918.218  fleet_recovered_at=1793568920.445  ← 回收证据
                           fleet_reservation_error=null
  drill-supervisor-waiter: state=finished status=done  fleet_reserved_at=1793568920.499  ← 被放行并跑完
```

### 场景 C：`openswarm stop`（对照组）

| 时间 | 事件 |
|------|------|
| 2026-11-02 05:35:42 | `openswarm stop drill-stop-holder`（既有语义：优雅停止） |
| 2026-11-02 05:35:42 | **名额回收**（故障后 0.025 s） |

注入后（05:36:03）：

```
lanes-status:
  L1  state=idle  current_run=null  run_state=null  last_error=null
  L2  state=idle  current_run=null  run_state=null  last_error=null
名额 reservations: []            ← 名额已回收
slot_holder=None
  drill-stop-holder: state=finished status=stopped  fleet_reserved_at=1793568942.187  fleet_recovered_at=null
  drill-stop-waiter: state=finished status=done     fleet_reserved_at=1793568943.439  ← 被放行并跑完
```

### 机器可读副本

演练每次都会把完整证据写成 JSON：沙箱内 `evidence.json`（`--keep` 时保留），
以及 `--evidence-out` 指定的路径（在沙箱之外，不会被演练收走）。
上面三段快照就是这份 JSON 的 `scenarios[].before/after` 字段。

## 回收是谁在做

回收逻辑全部在既有的 openswarm 代码里，本次**没有改动**任何一行：

| 环节 | 位置 | 说明 |
|------|------|------|
| 恢复入口 | `openswarm/bin/openswarm:3615` `recover_expired_fleet_slots()` | 按 TTL 扫 `runs/*/status.json`，挑出可回收的 reservation |
| 回收候选 | `openswarm/bin/openswarm:3668`（`run-finished`）、`:3670`（`dead-runner`） | `run-finished`：执行器异常退出后子进程已不在、名额被标 `retained after abnormal executor exit`，随后走 TTL 释放并落 `fleet_recovered_at`；`dead-runner`：`runner_pid` 已死（supervisor 被 kill） |
| 谁来调用 | `openswarm/bin/openswarm:3817`（`reserve_provider_slot` 的排队重试）、`:3723` `lane_recover_expired_fleet_slots()` | 存活的 supervisor 每秒重试一次回收；**排队中的那条 lane 也能顺手把别人的名额收回来** |
| 名额上限 | `openswarm/bin/openswarm:3826` 起 | 本地并发统计跳过已死的 runner，所以杀掉的 lane 不再占名额，排队的那条才被放行 |
| 协调器 | `openswarm/bin/fleet-capacity` | `status/acquire/release/recover`，名额是持久化的，崩溃不自动清 |

三条语义与 issue 的「kill 后回收并可再派」一一对应：

- 杀执行器 → child 以 -9 退出 → `normal_child_exit` 不成立
  （`openswarm/bin/openswarm:9432`）→ 名额先被标 `retained after abnormal executor exit`，
  再由 TTL 的 `run-finished` 路径释放（场景 A 实测 2.145 s）；
- 杀 supervisor → `runner_pid` 已死 → 走 `dead-runner` 候选（场景 B 实测 1.399 s）；
- `openswarm stop` → 走 lane abort（`openswarm/bin/openswarm:12572`），
  名额由 lane 自己释放（场景 C 实测 0.025 s）。

## 边界（本演练没有证明什么）

- **回收需要有人干活**：回收由**存活的 supervisor**（或重启后的 supervisor）在
  `reserve_provider_slot` 的排队重试里顺带做。**两条 lane 的 supervisor 同时死掉时没人 reap**，
  名额要等 supervisor 回来才回收 —— 「kill 掉就一定有人收尸」是不成立的。
  场景 B 里之所以能回收，是因为演练用既有 `lane up L1` 把 supervisor 拉了回来。
- **只有一条 lane 时也能回收**：本演练特意用两条 lane，是为了让「名额被回收后**可再派**」这件事
  有第二个观测者；单 lane 场景下没有排队者，回收同样会发生但看不到放行。
- **名额归谁由协调器决定**：L1 先派，但 L2 的 supervisor 也可能先进入 `reserve_provider_slot`。
  演练不预设赢家，而是断言「谁真的 running 谁是 holder」，另一条才是 waiter；
  谁抢到名额不影响结论。
- **沙箱是隔离的，但不是沙箱化的**：演练把宿主环境里的 `OPENSWARM_*` / `XSWARM_*` 全部清掉
  （只留自己设的 `OPENSWARM_HOME` / `OPENSWARM_FLEET_CAPACITY_DIR` / `OPENSWARM_FLEET_GUARD_CONFIG`），
  所以不会误用生产的 `lanes.json`，也不会让 `OPENSWARM_PI_RPC_BIN` 顶掉假执行器；
  但它毕竟跑在宿主上，仍然会起真实进程、真实 socket。
- **只测了 pi 后端 + 本机协调器**：没有覆盖 codex/agy 等后端，也没有覆盖多机
  （`gocc`/`gores1`）的 `HOST` 拓扑；跨机回收走的是同一套 `fleet-capacity` 协议，但本次没有实测。
- **AF_UNIX 路径长度**：lane 控制 socket 路径有硬上限（macOS 103 字节、Linux 107 字节，
  与 `openswarm/bin/openswarm:7627` `lane_validate_socket_path` 一致）。仓库路径太深时演练会直接报错，
  换短一点的 `--workdir` 即可。
- **沙箱目录只删自己的**：演练只删带 `.quota-reclaim-drill-sandbox` 标记（或空）的目录，
  并且拒绝 `/`、`$HOME`、仓库根这类危险路径；默认沙箱 `.openswarm-tmp/` 已进 `.gitignore`，
  演练中途被打断也不会把 `git status` 弄脏。
- **没有证明生产 box 上的表现**：生产上 `fleet.recovery_ttl_seconds` 是策略里的真实值（不是演练里的 2 秒），
  回收延迟会跟着 TTL 走；生产还有真实 pi 凭据与真实 broker，本次一律不碰。

## 相关文件

| 文件 | 作用 |
|------|------|
| `tools/quota-reclaim-drill.py` | 本次的故障名额回收演练（离线、可重复、只读生产） |
| `openswarm/bin/openswarm` | 回收语义本体：`recover_expired_fleet_slots` / `reserve_provider_slot` / `lane_validate_socket_path` |
| `openswarm/bin/fleet-capacity` | 名额协调器（`status/acquire/release/recover`） |
| `openswarm/tests/fixtures/fake_pi_rpc.py` | 演练用的假 pi 执行器（`LANE_TEST_WAIT` 占住名额 20 s） |
| `.gitignore` | 收走演练沙箱 `.openswarm-tmp/` |
