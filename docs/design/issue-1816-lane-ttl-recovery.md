# Issue #1816：常驻 lane 的 TTL 回收判定、用例去时序化与容量回收路径恢复

本文件是 PR #1823 的交付说明：判定（时序 flake 还是真缺陷）、依据、BLOCKER 映射与验收证据。
Refs #1816

## 1. 判定

**真缺陷（回收逻辑有偏差），并且叠加了用例侧的同步缺陷；不是“等待窗口相对 CI 负载偏紧”的纯时序 flake。**

报告的失败是 PR #1808 run 41579348149 中
`test_lane.LaneTests.test_lane_supervisor_recovers_killed_runner_after_ttl_without_new_admission`
在 ubuntu 3.11 与 macOS 3.9/3.11/3.13 报 `AssertionError: timed out waiting for resident lane to
recover a dead runner; last=False`，而 ubuntu 3.9/3.13 未复现。

### 1.1 产品缺陷（根因）

CI 失败时跑的回收实现（PR #1808,`openswarm/bin/openswarm`）用下面两个谓词判断"记录的 executor
是否还能工作":

```
def process_exited(pid):            # 只读 /proc
    try:
        with open(f"/proc/{int(pid)}/stat", ...) as stream:
            fields = stream.read().rsplit(")", 1)[1].split()
    except (OSError, ValueError, TypeError, IndexError):
        return False                 # ← macOS 没有 /proc,恒为 False
    return bool(fields) and fields[0] == "Z"

def process_group_has_live_member(pgid):
    ...
    if not os.path.isdir("/proc"):
        return process_group_alive(pgid)   # ← macOS 退化为 os.killpg(pgid, 0)
```

`pid_alive()` 用 `os.kill(pid, 0)`、`process_group_alive()` 用 `os.killpg(pgid, 0)`。已被 SIGKILL
但尚未被父进程 reap 的进程是 **zombie**：信号仍可投递，两个调用都返回成功，于是被算成“仍在工作”。

TTL 回收的 dead-runner 分支要求 `executor_stopped(rdir)` 为真，才会向协调器发 `recover`:

```
if not executor_stopped(rdir):
    stop_orphaned_executor(rdir)
if not executor_stopped(rdir):
    ... "retained after TTL: executor process group is still live"
    continue
answer = fleet_guard_call(command, {"op": "recover", ...})
```

本用例里的 executor 是**测试进程**的子进程（`subprocess.Popen(..., start_new_session=True)`),
lane supervisor 不是它的父进程，无法 reap 它。于是：

* **macOS**:`process_exited()` 恒为 False（无 /proc),`process_group_has_live_member()` 退化为
  `killpg`,zombie 让它返回 True → 永远“仍在工作” → 回收永远走不到协调器 → 协调器里的
  reservation 永远不释放 → 必然超时。macOS 3.9/3.11/3.13 三个 leg 全部失败，与报告一致。
* **Linux**:`recorded_process_working()` 要求 `process_exited(pid) and process_orphan(pid)` 才把
  zombie 放行，而 `process_orphan()` 又依赖 /proc 的 ppid 字段；更关键的是身份复核是 TOCTOU:

  ```
  def recorded_executor_identity_matches(status):
      pid, identity = status.get("executor_pid"), status.get("executor_identity")
      return bool(pid_alive(pid) and ... and process_identity(pid) == identity)
  ```

  `pid_alive(pid)` 与 `process_identity(pid)` 之间如果进程被 reap,`/proc/<pid>/stat` 读不到、
  `ps -o lstart=` 也返回空 → `process_identity()` 返回 `None` → 判定“身份无法确认” →
  该记录被**永久**保留（`retained after TTL: executor identity cannot be confirmed`)，回收同样
  永远到不了协调器。这个窗口的命中率与机器负载相关 → 与报告里“部分 leg 失败、部分 leg 通过”
  的分布一致。

结论：**回收逻辑的存活判定有真实偏差**（对“已退出、未被 reap”的 executor 判成存活，并且身份复核
存在 TOCTOU)，不是“等待窗口相对 CI 负载偏紧”的时序型 flake。

### 1.2 为什么 CI 表现像“时序 flake”

旧实现把两件本来确定的事交给了时序：一是“executor 到底还活着吗”（答案取决于无关父进程何时
reap)，二是“身份复核时进程还在吗”(TOCTOU）。这两点被负载放大成“有时 15 秒内没恢复、有时恢复了”,
于是看起来像时序 flake；但把存活语义改成“已退出即已停、且先判退出再谈身份”之后，同一台机器、
同样负载下不再需要任何更宽的窗口。

### 1.3 用例侧的第二处缺陷（同步，而非超时长度）

旧用例等待的是**协调器的 reservation 列表**变空：

```
self.wait_for(lambda: not self.fleet_reservations(), timeout=15,
              description="resident lane to recover a dead runner")
status = self.run_state(run_id)
self.assertIn("fleet_recovered_at", status)
```

但回收顺序是“先让协调器删 reservation，后由 supervisor 写 run 状态标记 `fleet_recovered_at`”。
两者之间存在窗口：等待者可能看到 reservation 已空、却读到还没有 `fleet_recovered_at` 的
`status.json` → 断言失败而回收其实已经发生。再加上固定 15 秒总超时在 6 路并行的 CI 上偏紧，
这条用例同时依赖“总时长”和“跨进程可见顺序”两件与被测行为无关的事。

### 1.4 结论与处置

| 结论 | 处置 |
| --- | --- |
| 存活判定把“已退出未 reap”的 executor 算作存活，且身份复核是 TOCTOU → **真缺陷** | 改判存活语义：`process_exited()`（`waitid(..., WNOWAIT)` + `/proc` 状态位 + `ps -o state=` 回退，跨平台一致）+ `process_group_members()`（跳过 `Z` 状态成员）；“先判退出、再谈身份”消除 TOCTOU |
| 用例用轮询 + 固定 15 秒总超时，且跨进程读顺序无保证 → **用例缺陷** | 改为**事件同步**：等待 supervisor 自己发布的恢复事件 `fleet_recovered_runs`（它在 run 状态标记之后写），并用“进展标记 + stall 判定”代替“只有总超时”的等待 |
| 重基丢失 release-pending / acquire-pending / run-finished 三条回收路径 → **回退** | 恢复这三条路径及其测试（见 §3） |

判定不放宽任何断言、不 skip、不删除用例；等待窗口不是靠放大超时“敷衍”的——等待条件本身换成了
被测组件发布的确定事件。

## 2. 改动清单

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `swrm/bin/swrm` | `process_exited()` :4024、`process_group_members()` :4059 | zombie 视为已退出；进程组成员统计跳过 `Z` 状态 |
| `swrm/bin/swrm` | `executor_status_fields()` :4098 | 记录 executor 进程组与出生身份，TTL 回收才有精确的组可停 |
| `swrm/bin/swrm` | `recover_expired_fleet_slots()` :4196 | 恢复 release-pending / acquire-pending / run-finished 三条路径；dead-runner 分支沿用本轮修复 |
| `swrm/bin/swrm` | `release_fleet_slot()` :3861 | 支持 `confirmed` / `pending_release` / `guard_command`；RPC 失败时把确切 token 落盘为 `fleet_release_pending` |
| `swrm/bin/swrm` | `reserve_provider_slot()` :4333 | 落盘 `fleet_host` / `fleet_reserved_at` / `fleet_reservation_token` / `fleet_reservation_pending`；排队时顺带做一次 TTL 回收 |
| `swrm/bin/swrm` | `lane_run_turn()` :8463 | lane 起 turn 时用 `executor_status_fields()` 记录 executor 组 |
| `swrm/tests/test_lane.py` | `wait_for_lane_event()` :177、目标用例 :204 | 事件同步等待（进展标记 + stall 判定），替代 15 秒硬超时轮询 |
| `swrm/tests/test_fleet_capacity.py` | :184 与 :222 至 :479 | 注入时钟的 TTL 边界用例、zombie 判定用例、三条恢复路径的回归用例 |
| `swrm/tests/test_fleet_ttl_resident.py` | 全文 | 恢复 run-finished 路径的专门测试文件 |
| `tools/ci-openswarm.sh` | 全文 | #1894 把 CI 门禁改名为 `tools/ci-swrm.sh` 后，保留旧入口名的兼容包装 |

## 3. 恢复的回收路径（重基回退的补齐）

上一头 `dd95b825c6`（重基前）的 `recover_expired_fleet_slots` 有四类候选，本 PR 起初只保留
dead-runner 一类，其余三类在重基到 main（#1894 把 vendored openswarm 换成 swrm）时丢失：

| 路径 | 判定条件 | 处置 | 覆盖测试 |
| --- | --- | --- | --- |
| `release-pending` | `fleet_release_pending` 里有确切 token（此前一次**已确认安全**的 release 因协调器不可达而失败） | 只重试那个 token，**不看、不杀**任何进程 | `test_finished_release_failure_recovers_after_process_restart` |
| `acquire-pending` | `fleet_reservation_pending is True` 且本次运行没有记录任何 executor（协调器已提交、回复丢失） | 释放那个确切 token | `test_timeout_after_committed_acquire_persists_ttl_recovery_metadata` |
| `run-finished` | runner 仍活着，但运行已发布终态 `result.json`，且 lane 已切到别的 run 或终态已超过 TTL | 只释放本 run 的 token，**不碰 lane 常驻子进程** | `test_finished_run_releases_its_reservation_while_the_resident_child_lives`、`test_lane_switching_to_another_run_releases_the_finished_run`、`test_terminal_resident_lane_without_confirmed_release_is_retained`、`test_unfinished_run_keeps_its_reservation_while_the_resident_child_lives`、`test_live_runner_with_persisted_reservation_is_not_recovered` |
| `dead-runner` | runner 已死 | 本轮修复：zombie 视为已停，确认身份后停掉记录的进程组，再向协调器发 `recover` | `test_lane_supervisor_recovers_killed_runner_after_ttl_without_new_admission`、`test_killed_runner_recovers_after_ttl_when_executor_is_stopped`、`test_ttl_recovery_waits_for_the_configured_ttl_with_an_injected_clock` |

另外恢复“排队中的准入也顺带做一次 TTL 回收”，对应
`test_next_admission_reaps_a_confirmed_dead_runner_after_ttl`。

## 4. BLOCKER 映射（本轮必须逐条关账）

| BLOCKER | 改动文件：行 | 测试名 | 结果 |
| --- | --- | --- | --- |
| **B1** 用例仍有轮询与 15 秒硬超时、回收实现未修 | `swrm/tests/test_lane.py:177`（`wait_for_lane_event`：事件谓词 + 进展标记 + stall 判定，无固定总超时）、`swrm/tests/test_lane.py:204`（目标用例改为等 supervisor 发布的 `fleet_recovered_runs` 事件）、`swrm/bin/swrm:4024`（`process_exited`）、`swrm/bin/swrm:4059`（`process_group_members`）、`swrm/bin/swrm:4196`（回收实现） | `tests.test_lane.LaneTests.test_lane_supervisor_recovers_killed_runner_after_ttl_without_new_admission`、`tests.test_fleet_capacity.FleetCapacityTests.test_recovery_counts_an_unreaped_executor_as_stopped`、`tests.test_fleet_capacity.FleetCapacityTests.test_ttl_recovery_waits_for_the_configured_ttl_with_an_injected_clock` | 目标用例本地连续 50 次全部通过（§5.1）；两个单元用例通过；CI 全矩阵（ubuntu-24.04/macos-latest × Python 3.9/3.11/3.13）以 PR 头的 6 个 job 为准（§5.2） |
| **B2** 未写明判定与依据，缺少 50 次与 CI 全矩阵证据 | `docs/design/issue-1816-lane-ttl-recovery.md:1`（本文 §1 判定与依据）、`swrm/tests/test_lane.py:118`（用例文件内的判定与映射注释）、`swrm/tests/test_fleet_capacity.py:184`（注入时钟用例 docstring） | 同上 + `tests.test_fleet_ttl_resident.FleetTtlResidentTests`(5 个用例） | 判定=**真缺陷**（存活判定把“已退出未 reap”的 executor 算作存活、身份复核 TOCTOU)+用例同步缺陷，依据见 §1.1–§1.4；本地证据见 §5.1/§5.3，CI 矩阵见 §5.2 |

## 5. 验收证据

### 5.1 目标用例本地连续 50 次

```
$ cd swrm && for i in $(seq 1 50); do
      python3 -m unittest tests.test_lane.LaneTests.test_lane_supervisor_recovers_killed_runner_after_ttl_without_new_admission
  done
run 50/50 ok
TOTAL=50 FAILED=0
```

（每次都是独立进程、独立临时目录；用例内部不再有固定总超时，只有“supervisor 停止推进”时才失败。）

### 5.2 CI 全矩阵

CI 工作流 `.github/workflows/phanes.yml` 的 job `Swrm tests (${{ matrix.os }}, Python ${{ matrix.python }})`
矩阵为 `os: [ubuntu-24.04, macos-latest] × python: ["3.9", "3.11", "3.13"]`，共 6 个 job,
每个 job 执行 `bash tools/ci-swrm.sh`(= `cd swrm && python -m unittest discover -s tests -v` +
离线 CLI 冒烟 + 受影响目录的 tools/ 单测）。

本地可用同一门禁复现该矩阵的单机等价物：

```
$ BASE_SHA=a4a49b8318883ff6e122f7ab08223d6d368f984c bash tools/ci-swrm.sh
```

(`tools/ci-openswarm.sh` 是它的兼容入口，见 §2。）

**CI 全矩阵的最终结果以 PR 头在 GitHub 上的 6 个 job 为准**（本沙箱无法访问 GitHub，未在此处伪造
结果）。本 PR 针对报告失败的两个成因（存活判定的 TOCTOU/zombie 语义、用例的跨进程同步）都做了
代码级修复，并在本地用同一门禁脚本与 50 次连续运行验证（§5.1、§5.3）。

### 5.3 本地全量

```
$ cd swrm && python3 -m unittest discover -s tests
Ran 498 tests ... FAILED (failures=3, errors=0, skipped=4)
```

3 个失败均为**沙箱环境**导致、与本 PR 无关：
`test_lane.LaneTests.test_cursor_systemd_unit_is_printed_not_installed`（容器内 uid 0 无 passwd 条目，
`getpass.getuser()` 抛 `OSError`）、
`test_pi_isolation.PiIsolationTests.test_host_secrets_and_network_are_not_visible` 与
`test_pi_isolation.PiIsolationTests.test_linked_git_worktree_and_node_remain_available`
(`bwrap: No permissions to create a new namespace`）。
