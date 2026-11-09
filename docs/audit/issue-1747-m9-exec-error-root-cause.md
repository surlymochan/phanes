# Issue #1747：M9（<host>，pi）连续 exec_error 秒挂的根因诊断

状态：**验收未完成；M9 根因尚未确认，现场首跑未验真，PAUSE-M9 保持（§6）**。
现有实现补齐了后续 run 的执行器错误串、spawn 路径采集及首跑闸门；这些机制不能代替本次事故的现场证据。

迁移对应（#1894 / 2d5ec1ae60 之后）：本卡原列的旧路径文件已迁到 `swrm/`，
`openswarm/bin/openswarm` → `swrm/bin/swrm`，`openswarm/bin/pi-isolated` → `swrm/bin/pi-isolated`，
`openswarm/tests/fixtures/fake_pi_rpc.py` → `swrm/tests/fixtures/fake_pi_rpc.py`，
`openswarm/tests/test_lane.py` → `swrm/tests/test_lane.py`，
`openswarm/tests/test_pi_isolation.py` → `swrm/tests/test_pi_isolation.py`。现有代码修复位于 `swrm/` 与 `tools/`；
`openswarm/` 下没有新增或修改任何文件。

## 0. 本轮评审处理

- **本轮取证误报 BLOCKER 已修复（基线 `79ce0b0ee`）**：`lane_pi_spawn_evidence()`、
  `pi-isolated` 的 preflight 与取证工具的 `guard_mirror()` 都在 `os.stat()` 成功后才置
  `broker.exists=true`，并用同一次 stat 的结果判断 `is_socket`。不存在的路径与悬空链接均记录
  `exists=false, is_socket=false`；普通文件记录 `exists=true, is_socket=false`。
  三个入口均新增回归测试：修复前缺失路径和悬空链接共 6 个子用例失败，修复后 9 个子用例全部通过。
- **现场 FOLLOW-UP（既往 BLOCKER 1：根因与恢复验真未闭环）**：当前工作树没有 M9 实际错误串或 spawn 路径，
  也没有 <host> 恢复后首跑的 run ID、状态和提交。撤回此前“根因已定位”的表述。
  M19 的转述及离线 fake executor 记录均不能完成 M9 现场验收。
- **既有环境隔离实现（保留）**：基线已有取证环境的三条规则（第 4 节）和测试覆盖：
  快照/systemd 环境**不继承终端变量**、缺失变量点名、冲突显式报告；未与运行中的 supervisor
  核验时**不给当前状态的确定判定**；旧 wrapper 不支持 `--preflight` 时改用本地守卫镜像，
  不再中止调查。
- M9 实际根因证据、恢复后现场首跑均仍待现场闭环，作为现场 FOLLOW-UP 保留。
- 前版审计把 M19 的错误串当成 M9 的错误、把空闲 soak 当成 M9 首跑成功，这两条撤回（第 2 节）。

## 1. 待核验的共因假说

**待核验假说：<host> 上 idle lane serve（release `a9ac8f33`）持续泄漏僵尸 `git` 进程，
占满主机的进程/线程表，车道的 worktree 准备随即以 EAGAIN 失败，run 约两分钟内以
`failed/exec_error` 结束且无提交。**

以下为上一轮文档的转述，原始来源尚未取得，不能据此确认 M9 的根因：

1. **M19 错误串转述（待核验）**。同机同版本的 M19 run `20261103-134716-6e76f7` 的 `error_tail` 是
   `lane worktree preparation failed (checkout -B): fatal: unable to create threaded lstat:
   Resource temporarily unavailable`——失败发生在车道自己的 `git checkout -B`，不是卡片、不是配额、
   不是模型侧。
2. **机制清楚**。lane serve 是 Linux subreaper（`lane_become_subreaper()`，`PR_SET_CHILD_SUBREAPER`），
   执行端留下的孤儿被它收养；而 `git` 的后台工作（`git gc --auto` fork 的后台 `git`）会脱离父进程，
   死后**没有人 wait**，于是变成 serve 的僵尸进程，一直挂到下次重启。
3. **现场计数转述（待核验）**。builder 2026-11-03 14:15 在 <host> 实测：约 4600 个僵尸（以 `git` 为主），
   全部挂在三个**空闲** serve 上（lane serve M9/GH2/M19，release `a9ac8f33`，已运行 18h/18h/13h），
   各约 1533 个，约每 17–40 秒泄漏一个；同机 M8 的 serve（release `df02c552`）为 **0**。
4. **版本对照线索**。M9（2026-11-03 00:47–01:04，九卡 #1694/#1700/#1702/#1709/#1711/
   #1719/#1685/#1705/#1730）、GH2（00:08–00:25，九连挂）、M19（当日六连死：04:23 #1361、04:25 #1793、
   04:36 #1794、04:50 #1771、05:41 #1786、13:48 #1690）都是 <host> 上的 Nov02 release `a9ac8f33`；
   同机跑 Nov01 release `df02c552` 的 M8 同期不速败。
   卡片互不相关，签名相同本身就说明是车道/主机/版本层故障，不是卡片缺陷。
5. **M9 那九次的归属**。前版称 M9 自身的历史 run 目录已被清理，但本轮无法核验清理情况，也未取得它自己的错误串；
   上面的失败点由同机同版本的 M19 实测串 + 僵尸计数 + Nov01/Nov02 对照推定，
   推定的是"M9 与 M19/GH2 同机制"，不是"M9 当时的具体报错文本"。
   本轮改动让下一次同类失败自带实测值（错误串 + spawn 路径），不再需要推定。

## 2. 待核验的现场证据引用

2026-11-08 本轮核验：`git cat-file -t 1e51c5ef77301c3e016650f539e6bfcbb3ddd433`
返回 `fatal: git cat-file: could not get object info`。检索当前 `docs/`、`tools/`、`swrm/`，
下表的 M19 错误串、僵尸计数及 soak 输出只在本文出现，未找到独立原始记录。
因此“本仓库历史里可查”的前版说法撤回。下表保留引用以便现场核验，不代表本轮实读。

| 证据 | 内容 | 来源 |
| --- | --- | --- |
| 现场错误串 | `lane worktree preparation failed (checkout -B): fatal: unable to create threaded lstat: Resource temporarily unavailable`（M19 run `20261103-134716-6e76f7`） | `#1817` 事故文档（提交 `1e51c5ef77301c3e016650f539e6bfcbb3ddd433`，`openswarm/docs/issue-1817-zombie-leak.md`） |
| 僵尸实测 | <host> 约 4600 个僵尸，三个空闲 Nov02 serve 各约 1533 个，泄漏率 1 个 / 17–40 s；M8（Nov01）0 个 | 同上（builder 2026-11-03 14:15 实测） |
| 修复后 soak | 30 分钟 idle serve：`rounds=127`、`reaped=127`、`serve_zombies=0`、`host_zombies=0` | 同提交 `openswarm/docs/evidence/pr-1822-zombie-soak.txt`（当前工作树无此文件，引用的 git 对象不可读取） |
| 上游修复提交 | `1e51c5ef77301c3e016650f539e6bfcbb3ddd433`（#1817：`lane_reap_zombies()` + SIGCHLD 唤醒 + 僵尸计数告警） | 前版引用；当前对象库无法读取，待取得来源 |
| 现场构建身份 | 三个故障 serve 跑的是 release `a9ac8f33`，构建路径 `/releases/a9ac8f3370b72fcd290a84cdc60166066c8fe75c/` | 卡片正文 + `#1817` 事故文档的 release 对照表 |
| 本轮 wrapper 身份 | 本卡改动的 `swrm/bin/pi-isolated`，sha256 `4be8725c339d1901c00561b313664d72ecc140f9ea1e8e50410e9d68caacb102` | 本工作树实测（`sha256sum swrm/bin/pi-isolated`），不代表现场部署身份 |

两条证据边界，写清楚免得被当成更强的结论：

- **本轮未取得 M9 自己的九次 run 记录**，第 1 节第 5 条的归属是推定，不是 M9 的实测报错文本。
- **当前代码的排除性线索，不能证明现场已修复**：迁移后的 `swrm/bin/swrm` 不含 subreaper 扩展
  （可用 `rg SUBREAPER swrm/` 核对）。这不证明 <host> 已部署该版本，也不能排除其它进程资源耗尽原因；
  现场仍需把修复后的构建重新钉到 <host> 的 systemd unit，旧 unit 备份在
  `<host> /root/openswarm-executor-*.service.bak-20261103`。因此本卡**不**把 `#1817` 的回收逻辑
  移植进 `swrm/bin/swrm`：那会把 subreaper 扩展重新引进来。

## 2.1 卡片列出的两个怀疑项仍需排查

- **broker socket 双路径**（卡片怀疑 `/run/broker.sock` 与 `/run/openswarm-pi-local/broker.sock`
  不一致，spawn 在会话建立前失败）：若取得并核验转述中的 M19 错误串，它可将该条 M19 run 的失败点定在车道自己的 `git checkout -B`
  （worktree 准备），那时执行器还没起来，broker socket 根本没被碰到，但这不能排除 M9 的 socket 路径问题。
  socket 本身仍由 `--preflight` 与守卫镜像逐项核对（`OPENSWARM_PI_BROKER_SOCKET` 是否设置、
  是否是 unix socket、是否还活着），取证报告里单列一行。
- **车道 worktree / lane 状态 wedged**：卡片正文记录 worktree 无 `git index.lock`，
  M19 转述的失败串是 `EAGAIN`（线程创建失败）而不是 lock 冲突；仍需 M9 自身记录排除 worktree 或 lane 状态问题。

## 3. 为什么现场看不到 agent 侧错误串（框架盲点）与本轮修复

- 改动前 `LaneChild` 用 `stderr=subprocess.DEVNULL` 起常驻执行器，
  `pi-isolated` 的 spawn 守卫（配置 / 依赖 / broker socket / worktree）全部**只往 stderr 写一行然后退出**。
  于是 `exec_error` + 空 `error_tail` 是**框架把证据扔了**，不是"没有错误"。
- `serve state.json` 的 `last_error=null` 也不能证明没跑过：serve 循环在执行器存活时把 `last_error`
  复位为 `None`，只有崩溃路径才写入。
- `<lane_root>/pi-sessions` 在 serve 模式下**永远是空的**：`pi-isolated` 不用 `--session-dir`，
  RPC 分支固定用沙箱内的 `/tmp/pi-sessions`。"目录为空"既不能证明会话未建立，也不能证明建立过。

本轮修复（`swrm/bin/swrm`）：

- `LaneChild` 采集常驻执行器的 stderr（保留最近 200 行），`lane_failure_detail()` 把它并进失败原因；
  turn 循环的"执行器已退出 / stdout 关闭"、`lane_do_run` 的兜底 `except`、serve 循环的崩溃路径
  三处失败收尾都带上这段 stderr，`error_tail` 与 `events.1.<backend>.jsonl` 不再是空的。
- 每个 pi run 记录**自己的 spawn 路径**：`status.json` 的 `spawn` 与 `result.json` 的 `attempts[].spawn`
  写 `{"wrapper": …, "wrapper_sha256": …, "env": {OPENSWARM_PI_*}, "broker": {path, exists, is_socket}}`。
  它回答的是"这条 run 是**怎么**起来的"。

`swrm/bin/pi-isolated` 新增 `--preflight`：按真实启动顺序评估 spawn 前置条件并打印 JSON
（`guard` / `error` + 解析出的 runtime、broker socket、worktree、sandbox 依赖），**不启动 pi、不碰 provider**。
守卫错误串改成自描述（点名变量与解析后的路径，例如
`isolated Pi broker socket unavailable: OPENSWARM_PI_BROKER_SOCKET is not set …`）；
未设置的变量不再被 `realpath('')` 悄悄解析成工作目录。

## 4. 取证：supervisor 的真实环境，绝不借终端的环境

`tools/check-pi-spawn-failure.py`（只读）一次给出四段，且**不修改任何状态**：

1. **supervisor 的真实环境**：优先 `/proc/<serve-pid>/environ`；读不到才退回 systemd 单元环境或
   显式 `--env` / `--env-file` 快照。
2. **supervisor 的真实 wrapper**：运行中的二进制旁边那个 `pi-isolated`（含 sha256 + mtime）。
3. **当前状态的 spawn 前置条件**：wrapper 支持 `--preflight` 就用它自己的；旧 wrapper 用工具内的
   守卫镜像（同一套判定顺序），**不因旧 wrapper 中止调查**。
4. **历史证据**：车道自己的 run 记录（`result.json` / `status.json` / events）——run_id、status、
   error_class、error_tail、时长、`timings`、`spawn`。

环境可信度三条规则（上一轮 BLOCKER 的落点）：

| 环境来源 | 含义 | 能否代表 M9 | 工具的行为 |
| --- | --- | --- | --- |
| `/proc/<serve-pid>/environ` | **M9 supervisor 实际继承的环境** | 能（首选） | 评估前置条件，给出**确定结论** |
| `--env` / `--env-file` 快照 | 操作者抓下来的环境 | 需核验 | **不评估**，除非与运行中的 supervisor 逐项一致（此时才算 verified） |
| `systemctl --user show -p Environment` | 单元里写死的变量 | 需核验 | 同上；**不完整就不给确定结论** |
| 当前 shell | 谁在哪个机器上敲命令就带谁的环境 | **不能** | **绝不**用来补齐缺失变量；只用来**报冲突** |

- **缺失变量点名**：环境里没有 `OPENSWARM_PI_BROKER_SOCKET` / `OPENSWARM_PI_RUNTIME` 时，报告直接点名，
  并写明"未评估"。
- **环境冲突检测**：快照/单元环境与运行中的 supervisor 不一致时报**冲突**并点名变量；
  与终端环境不一致时同样报冲突（终端的值**不打印**，避免把外来路径写进报告）。冲突**不静默取一个**。
- **不给确定结论**：环境未核验时结论是"未评估：环境 unverified（…）"，不会出现
  "spawn preconditions hold now / precondition failed now"这种对主机下判断的措辞。
- **当前状态 ≠ 历史根因**：`current_verdict` 只描述**现在**的主机状态；`historical` 段只由 run 记录得出。

`tools/check-lane-first-run.py`（只读）是 PAUSE-M9 的解除闸门（§6）：读取全部留存 run，
首跑必须 `status=done`、**有提交**、`error_class` 不是 `exec_error`，否则退出码 1 并点名问题。

## 5. 首跑验真

### 5.1 前版报告的本地首跑（离线，2026-11-08，非现场验真）：run_id=20261108-142653-60ca1a，commit=ad2d5424861721ba5c92f0774bd46e1bbcce81ce

前版报告称修复后的车道运行时在仓库内跑通了第一条带提交的 run（执行器用 `swrm/tests/fixtures/fake_pi_rpc.py`，
一次受控单卡，临时 home 在 `.swrm-tmp/verify-m9/`）：

- `status=done`、`error_class=null`，产出提交 `ad2d5424861721ba5c92f0774bd46e1bbcce81ce`；
- `status.json` 的 `spawn` 记录了 wrapper 路径、wrapper 的 sha256 与 broker socket；
- `tools/check-lane-first-run.py --lane M9 --home <home> --json` 对这条 run 判定 `passed=true`（退出码 0），
  打印的 `run_id` 与 `commit` 即上面两个值。

这条首跑验证的是**采集链路与闸门**：失败 run 会带出执行器的错误串与 spawn 路径，成功 run 会带出提交。
复现命令（复现的是行为；run id 与 commit 每次执行都不同，产物在本工作区的 `.swrm-tmp/verify-m9/`）：

```bash
python3 -m pytest -q swrm/tests/test_lane_spawn_evidence.py
python3 -m pytest -q swrm/tests/test_lane.py -k spawn
```

### 5.2 现场首跑（<host>）：解除 PAUSE-M9 的前置条件

M9 恢复后的受控首跑尚未执行：run_id、开始时间、`status` 与实际产出的 commit 均未取得（待回填）。
现场首跑必须走 `tools/check-lane-first-run.py` 这道闸门，命令与判据见第 6 节；
在它通过之前，PAUSE-M9 保持。

## 6. PAUSE-M9 的正式解除条件

受控验真与正式恢复自动派卡分开执行，不存在"先正式解除才可验证"的循环条件。
正式解除必须同时满足：

1. **现场根因已确认且故障部件已修复或替换**：先取得 M9 自身的错误串或 spawn 路径，
   记录来源、时间及部署身份，并核验 M19/GH2 是否同因。若确认僵尸泄漏假说，则验证 <host> 上 M9/GH2/M19 的 systemd unit 指向的构建包含 `#1817` 的修复
   （`1e51c5ef77301c3e016650f539e6bfcbb3ddd433`）或等效实现，且现场 `ps` 读到的僵尸数为 0
   （对照现场基线：Nov02 三个 serve 各约 1533 个）。
2. **环境与 socket 一致**：`tools/check-pi-spawn-failure.py --lane M9 --json` 报告
   `environment.verified=true`、无 `conflicts`、无 `missing`，且 `preflight` 的 `guard` 为空。
3. **受控首跑通过**：在自动派卡仍隔离的前提下跑一次单卡，然后
   ```bash
   python3 tools/check-lane-first-run.py --lane M9 --since <受控验真开始时刻> --json
   ```
   退出码 0、`status` 为 done/completed、`commit` 非空、`error_class` 不是 `exec_error`；
   把 run_id、开始时间与实际产出的 commit 回填本文第 5.2 节。
4. **稳定运行**：再验证至少两次运行不再 exec_error，保留失败计数与主机 health 证据。
5. **操作者核对以上证据后**才解除 PAUSE-M9 与维护隔离。本轮不解除任何暂停。

M19 与 GH2 保持各自暂停和各自验真要求。同主机共因需由各车道自己的记录支持，
不能用 M9 的一次成功替代其它车道的恢复验收。

## 7. 现场只读取证（在 <host> 执行，不解除暂停）

```bash
python3 tools/check-pi-spawn-failure.py --lane M9 --json
python3 tools/check-pi-spawn-failure.py --lane M19 --json
python3 tools/check-pi-spawn-failure.py --lane GH2 --json
```

默认历史输出有条数限制，用 `--runs` 覆盖事故窗口的全部记录并核对时间。
检查 `environment.source` 与 `verified` 标志、真实 wrapper 路径及 sha256；
当前 preflight 只代表当前状态。若历史 run 未包含错误（本次 M9 九次 run 的记录尚未取得），
另取事故窗口的 journal/部署记录与进程表，明确来源与时间，不把当前探针当作历史故障证据。

## 8. 离线验证范围

```bash
python3 -m pytest -q tools/tests/test_issue_1747_m9_spawn_evidence.py
python3 -m pytest -q tools/tests/test_check_pi_spawn_failure.py tools/tests/test_check_lane_first_run.py
python3 -m pytest -q swrm/tests/test_lane_spawn_evidence.py
python3 -m pytest -q swrm/tests/test_lane.py
```

这些测试覆盖环境隔离、旧 wrapper 的本地评估、首跑闸门与文档字段，
但**文档的正则匹配不证明现场证据真实，也不等于验收完成**：第 5.1 节是离线首跑，
第 5.2 节的现场首跑与第 6 节的解除条件仍然待办。

### 上一轮执行结果（基线 `79ce0b0ee`，2026-11-08）

- 原验收命令 `python3 -m pytest -q openswarm/tests/test_lane.py`：退出码 4，旧路径不存在。
  宿主机应运行迁移后的 `python3 -m pytest -q swrm/tests/test_lane.py`。
- 在工作区短临时目录下运行 `swrm/tests/test_lane.py`、`swrm/tests/test_pi_isolation.py`、
  `swrm/tests/test_lane_spawn_evidence.py`、`tools/tests/test_check_pi_spawn_failure.py`、
  `tools/tests/test_check_lane_first_run.py` 和 `tools/tests/test_issue_1747_m9_spawn_evidence.py`：
  **44 passed、63 failed、2 skipped**。socket 创建/绑定受到沙箱限制
  （`PermissionError: [Errno 1] Operation not permitted`）；本轮未修改运行时代码。
- 单独运行上述三个 tools 模块：**40 passed、11 failed**；其中 10 项在 socket 操作处受限，
  1 项为 `AuditDocumentTests::test_the_audit_states_the_root_cause_of_the_incident`，
  因本文如实保留 M9 根因尚未确认而失败。保留该验收测试，不删改或绕过。
- `git diff --check` 通过。以上不是现场验真，也不是测试全绿；需要宿主机重跑上述六个模块，
  并取得第 5.2 节的 <host> 真实首跑记录后再评审。本轮不提交、不推送、不写 GitHub。

### 本轮取证误报修复验证（基于 `79ce0b0ee`，2026-11-08）

- 新增测试先在未修复代码上复现三处误报（缺失路径、悬空链接共 6 个子用例失败）；
  修复后运行下列命令：**3 passed、9 subtests passed**。
  ```bash
  TMPDIR="$PWD/.t" python3 -m pytest -q swrm/tests/test_lane.py swrm/tests/test_pi_isolation.py tools/tests/test_check_pi_spawn_failure.py -k broker_existence_requires_a_successful_stat
  ```
- 迁移后的 lane 全模块：**3 passed、46 failed、2 skipped**，失败均发生在 lane control socket
  绑定处，错误为 `[Errno 1] Operation not permitted`。耗时 61 秒，以后台进程运行并轮询。
- 其余五个相关模块（第 8 节列出的测试，另含 `swrm/tests/test_pi_isolation.py`）：
  **44 passed、17 failed**。16 项在 socket 绑定处被沙箱拒绝；剩余 1 项为基线已有的
  `AuditDocumentTests::test_the_audit_states_the_root_cause_of_the_incident`，仍因现场根因未确认而失败。
  原有复现测试全部保留，没有删改或绕过断言。
- 原验收命令 `python3 -m pytest -q openswarm/tests/test_lane.py` 仍退出 4（旧路径不存在）。
  宿主机应在有 socket/namespace 权限的环境执行：
  ```bash
  mkdir -p .t
  TMPDIR="$PWD/.t" python3 -m pytest -q swrm/tests/test_lane.py
  TMPDIR="$PWD/.t" python3 -m pytest -q swrm/tests/test_pi_isolation.py swrm/tests/test_lane_spawn_evidence.py tools/tests/test_check_pi_spawn_failure.py tools/tests/test_check_lane_first_run.py tools/tests/test_issue_1747_m9_spawn_evidence.py
  ```
- 现场 FOLLOW-UP：取得 M9 实际根因证据、核验 M19/GH2 是否同因、记录恢复后首跑提交。
  在第 6 节条件全部满足前保持暂停。本轮不提交、不推送、不写 GitHub。
