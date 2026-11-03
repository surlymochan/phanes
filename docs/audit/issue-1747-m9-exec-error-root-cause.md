# Issue #1747：M9（gores1，pi）连续 exec_error 秒挂的根因诊断

范围：`openswarm/bin/openswarm`（车道常驻执行器与 run 收尾路径）、`openswarm/bin/pi-isolated`
（pi 隔离启动器）、`tools/check-pi-spawn-failure.py`（取证）、`tools/check-lane-first-run.py`
（首跑验真）。行号均为本次提交后的版本。
诊断环境：本工作树（车道 A2 的执行沙箱）。**本沙箱与 gores1 上 M9 的运行副本不在同一 mount
namespace，也没有任何网络或 SSH 出口**（`nsenter` 被拒；沙箱内无 `/root/.ssh`、无
`/run/openswarm-pi-local/`），因此本次**没有**读到 M9 现场的错误串——见 §9 的诚实声明与
§10 的现场取证清单。

## 0. 本轮针对评审意见的修改（对应上一轮 head `6af4a6b9a` 的两条 BLOCKER）

- **BLOCKER 1（根因与恢复验真未闭环）**
  - 写清**能证明什么、不能证明什么**：历史错误串被丢弃、不可追回（§1.3、§9）；并且
    「spawn 前置条件不满足」与卡片上的**约 2 分钟**签名**不兼容**（§3.4），因此本轮**不再**
    把它当作最可能根因，只把它降级为「需用 run 时长排除/坐实的一个候选」。
  - 给出**可判别的时长对照表**（§3.4）与**一条命令的现场取证包**（§5.1、§10）：取证结果贴回
    issue 即可把落点收敛到一个。
  - 新增**首跑验真闸门** `tools/check-lane-first-run.py`（§6.5）与**spawn 路径证据落盘**
    （§6.3），PAUSE-M9 的解除条件全部机器可判（§8）。
  - 本沙箱没有 M9 的执行副本，也没有网络/SSH 出口，**现场那一步只能由 AD/操作者在 gores1 上
    执行**（§9、§10）；本次**不解除暂停**。
- **BLOCKER 2（取证用的是当前终端环境与当前 release 的 wrapper）**
  - 取证改用 **M9 supervisor 的真实环境**（`/proc/<serve-pid>/environ`；退而求其次是 systemd
    单元环境，或 `--env`/`--env-file` 显式传入），**读不到就不评估**（§5.1、§5.2）——
    上一版「用终端环境跑 preflight」这条误报路径已删除。
  - 取证改用 **supervisor 自己的 wrapper**（运行中的 `openswarm` 旁边那个 `pi-isolated`），
    并给出它的 **sha256 + mtime**，用来区分 Nov01/Nov02 构建（§5.1）。
  - **旧 wrapper 没有 `--preflight` 时不再中止取证**：改用工具内的守卫镜像按同一判定顺序
    评估（§5.3）。
  - 报告把**当前状态**与**历史根因**分成两段，当前状态明确标注「这不是那 9 次失败的证据」
    （§5.4）。

## 1. 结论摘要

1. **卡片分类正确**：9 张互不相关的卡同签名同窗口失败，是车道/主机/版本层故障，不是卡片缺陷；
   9 卡已返池 `lane-ready` 的处置无需回退。
2. **「执行侧实读」里两条关键证据被误读**，必须更正后才能定位：
   - 「pi-sessions 目录为空 → agent 侧无会话」**不成立**：serve 模式下 pi 的会话目录恒为空，
     是结构性事实，不能作为「失败发生在会话建立前」的证据（§3.1）。
   - 「agent 侧无错误串 → 没有错误」**不成立**：常驻执行器的 stderr 被直接丢进 `/dev/null`，
     spawn 失败时 pi-isolated 只往 stderr 写一行就退出，错误串是被**采集丢弃**的（§3.2）。
3. **本次事故的 agent 侧错误串已被销毁，无法追回**：改动前 `LaneChild` 用
   `stderr=subprocess.DEVNULL` 起常驻执行器（§3.2），`pi-isolated` 的 spawn 守卫只往 stderr
   写一行就退出（`pi-isolated:96-127`）。因此
   「那 9 次失败到底挂在哪一段」**不能再靠现场日志复原**，只能靠：(a) run 自己留下的
   `result.json`/`status.json`（时长、error_class、timings）；(b) 现场**当前**的 spawn 前置
   条件（用 supervisor 的真实环境重跑 preflight）。二者都不是那 9 次失败当刻的错误串，
   报告里必须分开写（§5.4）。
4. **「spawn 前置条件不满足」不再是最可能根因**：该假设与卡片记录的**约 2 分钟**时长**不兼容**
   ——wrapper 的守卫失败会在 wrapper 启动后**立即**退出（<1s），车道记的是
   「resident executor process exited / stdout closed」，不是等两分钟（§3.4 的时长对照表）。
   与 2 分钟签名相符的是「pi 起来了、provider/broker 侧出错、pi 的自动重试窗口耗尽」
   （`pi_retry_wait_seconds()`，`openswarm/bin/openswarm:9000`）或「30s 的 `new_session`
   RPC 超时 + 派卡→开跑的前置延迟」（§3.4）。**在拿到现场时长之前不得把任一假设写成根因。**
5. **同机两条车道同签名**（M9 与 GH2）指向**主机侧共享部件**（broker / pi 运行时 / 宿主机），
   而不是单车道配置；但「共享」不等于「已定位到哪个部件」，仍需 §10 的现场取证。
6. **本次改动让下一次同类失败自带证据**：车道常驻执行器的 stderr 被保留并写入
   `result.json`/`events`/车道 state；每个 pi run 记录**自己的 spawn 路径**（wrapper 路径 +
   sha256 + 解析出的 `OPENSWARM_PI_*` + broker socket 的类型，§6.3）；`pi-isolated` 新增
   `--preflight`，可在不启动 pi 的前提下打印 spawn 前置条件判定。
7. **取证脚本必须用 M9 supervisor 的真实环境与真实 wrapper**（§5.2），并且**当前状态的判定不能
   当成历史故障的根因**（§5.4）；这两点是本次评审 BLOCKER 的落点，已在本次提交修掉。

## 2. 失败链路（代码级）

派卡 → 车道 supervisor 接受 assignment → `lane_do_run`（`openswarm/bin/openswarm:10058`）→
`lane_run_turn`（:9401）→ `lane_begin_turn`（:9177，pi 分支 :9204-9207）→ `LaneChild.send`/`wait_response`
→ 失败收尾。三条可能的落点：

| 落点 | 触发条件 | 记录的 error_class | 改动前的 error_tail |
|---|---|---|---|
| `lane_begin_turn` 抛 `RuntimeError`（"executor exited before RPC response" / "executor RPC response timed out"） | 常驻执行器在 `new_session` 前后已死或不应答 | `exec_error`（`lane_do_run` 的 `except`，:10230 附近） | **None**（`lane_finalize_run` 的 attempts 默认 `error_tail=None`） |
| turn 循环里 stdout 关闭 | 执行器中途退出 | `exec_error`（"resident executor stdout closed"） | 同上，但事件文件里只有 `lane_start` |
| serve 循环发现常驻执行器已死 | 执行器在两次 run 之间退出 | `exec_error`（"resident executor process exited"） | 事件文件里只有 `lane_start`；车道 state 的 `last_error` 是固定文案 |

三处的共同点：**执行器自己写在 stderr 上的失败原因从未进入 run 记录**。

## 3. 执行侧实读的两处更正

### 3.1 「pi-sessions 目录为空」是结构性事实，不是失败点证据

- serve 模式把会话目录传给隔离启动器：`sessions = <lane_root>/pi-sessions`、
  `cmd = [binary, "--mode", "rpc", "--session-dir", sessions]`（`openswarm/bin/openswarm:8539-8541`）。
- 但 `pi-isolated` **从不使用这个参数**：`--session-dir` 只在 `argparse` 里声明
  （`openswarm/bin/pi-isolated:18`），RPC 分支固定用沙箱内的 `/tmp/pi-sessions`
  （`pi-isolated:168-169`），而 RPC 模式每次启动都会清空沙箱 `/tmp`
  （`pi-isolated:42-43` 的 `fresh and args.mode == 'rpc'` → `shutil.rmtree`）。
- 结论：`<lane_root>/pi-sessions` 在 serve 模式下**永远是空的**，无论成功还是失败。
  「目录为空」既不能证明「会话未建立」，也不能证明「会话建立过」。这条证据作废。

### 3.2 「无 agent 侧错误串」是采集缺失，不是没有错误

- 改动前 `LaneChild` 用 `stderr=subprocess.DEVNULL` 起常驻执行器
  （改动前 `openswarm/bin/openswarm:8161`）。
- `pi-isolated` 的 spawn 守卫全部只往 stderr 写一行然后退出（`pi-isolated` 的
  `sys.exit('isolated Pi rejected …')` / `'isolated Pi dependencies unavailable'` /
  `'isolated Pi broker socket unavailable'` / `'isolated Pi requires a dedicated worktree'`）。
- 结论：`exec_error` + 空 `error_tail` 是**框架把证据扔了**，不是「没有错误」。
  修好采集之前，任何「失败点」判断都只是猜测。

### 3.3 「serve state.json：last_error=null、current_run=null」同样不能证明没跑过

serve 循环在常驻执行器存活时把 `last_error` 复位为 `None`
（`openswarm/bin/openswarm:10339`、`:10402`），只有崩溃路径才写入
`last_error="resident executor process exited"`（`:10377`、`:10393-10394`）。所以快照读到 null
只说明「看的时候执行器是活的」，不说明「这 17 分钟里没有 run 发生过」。
run 是否发生过要看 `$OPENSWARM_HOME/runs/<run_id>/`。

### 3.4 失败时长对照表：什么签名对应哪个落点

这张表是本次诊断的核心工具：**用 run 自己记录的时长把落点缩小到一个**，而不必先有错误串。
「约 2 分钟」是卡片上写的**派卡→失败墙钟时间**，其中包含派卡等待与工作区准备；run 记录里的
`started`/`finished`/`timings.prepare_s` 才是执行段时长。

| 落点 | 代码位置 | 执行段时长特征 | error_class |
|---|---|---|---|
| wrapper 守卫拒绝启动（socket 未设/不是 socket/依赖缺失/非专用 worktree） | `pi-isolated:96-127`，守卫在 bwrap 之前 | **<1s**（wrapper 立即 `sys.exit`） | `exec_error`（"resident executor stdout closed"） |
| `new_session` RPC 超时 | `lane_begin_turn` 的 `wait_response(sid, 30)`（`openswarm/bin/openswarm:9204-9207`） | **≈30s** | `exec_error`（"executor RPC response timed out"） |
| provider/broker 报错，pi 自动重试窗口耗尽 | `lane_run_turn` 的 `agent_end`/`auto_retry_end`（:9675-9710）+ `pi_retry_wait_seconds()`（:9000） | **几十秒到数分钟**（pi 自己的退避 + 宽限窗口） | `exec_error`（"pi auto-retry exhausted"） |
| 车道停摆看门狗 | `lane_stall_after_seconds()`（:8011，默认 900s） | **≥900s** | `stalled`（不是 exec_error） |
| 预算耗尽 | run deadline | = `budget_minutes` | `budget_exceeded` |

读法：
- 若 run 记录的**执行段**时长 <1s → 落点是 wrapper 守卫，此时 socket/依赖/工作区是嫌疑；
- 若 ≈30s → 落点是 `new_session` 超时（执行器活着但不应答）；
- 若 1–3 分钟且 events 里有 `message_end`/`agent_end` 的 provider 错误 → 落点是 provider/broker 侧，
  与 spawn 无关；
- 若 ≥900s → 是 `stalled` 而不是 `exec_error`，卡片上的分类就抄错了。

**没有 run 记录里的时长，就不能把任何一条写进根因。**

## 4. socket 路径：四条候选互不相同（双路径嫌疑的落点）

| 路径 | 出现位置 | 状态 |
|---|---|---|
| `/run/openswarm-pi-local/broker.sock` | 线上 broker 进程的实际监听路径；**本沙箱自己的 spawn 命令行里可见**：`--ro-bind /run/openswarm-pi-local/broker.sock /run/broker.sock`（`tr '\0' ' ' < /proc/1/cmdline`） | 本车道（A2）在用；仓库里**没有任何代码**产生这个路径 → 它是宿主机部署事实，不是仓库约定 |
| `/run/openswarm-pi/broker.sock` | `openswarm/bin/openswarm-fleet:39-40` 的导出默认值；`openswarm doctor` 的兜底默认值（`openswarm/bin/openswarm:15076-15077`） | 本机不存在 |
| `/run/broker.sock` | `pi-isolated` 沙箱**内部**的挂载点（`pi-isolated:188` `--ro-bind <broker> /run/broker.sock`）；本机存在同名陈旧 socket 文件（2026-11-01 15:21 创建，**无监听者**） | 陈旧文件，会通过 `S_ISSOCK` 检查 |
| `~/Library/Caches/openswarm-pi-broker/broker.sock` | `openswarm/bin/pi-broker-service`（macOS 路径） | 不适用（Linux） |

要点：

- `openswarm-fleet` **只在 `/run/openswarm-pi/broker.sock` 存在时**才导出
  `OPENSWARM_PI_BROKER_SOCKET`（`openswarm/bin/openswarm-fleet:39-40`）。在只提供
  `-local` 路径的宿主机上，用这个包装器起的 supervisor **拿不到 socket 变量**，
  `pi-isolated` 的守卫随即拒绝启动（<1s 的 exec_error，§3.4 第一行）。
- `pi-isolated` 只认 `OPENSWARM_PI_BROKER_SOCKET`；**未设置时** `os.path.realpath('')` 会解析成
  **当前工作目录**，于是守卫报「broker socket unavailable」而不会说清「变量没设」。
  本次改动把这条路径改成显式判定并给出可执行的错误串（§6.1）。
- `openswarm doctor` 与执行器用的是同一个变量（`envvar("PI_BROKER_SOCKET")` 会展开成
  `OPENSWARM_PI_BROKER_SOCKET`），但 doctor 的**兜底默认值**是 `/run/openswarm-pi/broker.sock`，
  与线上 broker 的实际路径不同：环境变量没落到 doctor 进程时，doctor 会报
  「broker socket is not reachable」而执行器报另一种错——两者看起来像两个故障。
- 陈旧 socket 文件能通过 `S_ISSOCK` 检查（`pi-isolated` 只判断文件类型），故障会推迟到沙箱内部
  才暴露，错误串更难读。`--preflight` 现在会主动探测 socket 是否真的有人监听。

## 5. 在 gores1 上取证（一次跑完）

### 5.1 一条命令拿到证据包

```bash
# 在 gores1 上（仓库 checkout 或把 tools/ 下两个脚本拷过去）：
python3 tools/check-pi-spawn-failure.py --lane M9            # 人类可读
python3 tools/check-pi-spawn-failure.py --lane M9 --json     # 机器可读（贴回 issue）
```

它一次性给出四段，且**不修改任何状态**：

1. **supervisor 的真实环境**：优先从 `/proc/<serve-pid>/environ` 读（`serve` 进程由
   `lane serve` 拉起，`OPENSWARM_PI_*` 就是它继承的那份）；supervisor 没在跑时退回
   `systemd --user show -p Environment` 的单元环境；也可以用 `--env KEY=VALUE`（可重复）或
   `--env-file <文件>` 把 supervisor 的环境显式喂进来（例如
   `tr '\0' '\n' < /proc/<pid>/environ > /tmp/m9.env` 后 `--env-file /tmp/m9.env`）。
   **环境读不到时不做 preflight**：当前 shell 的环境不是车道的环境，用它评估会报出车道没有的
   故障（上一版脚本正是这么误报的）。
2. **supervisor 的真实 wrapper**：不是「当前 checkout 的 wrapper」，而是**运行中的
   `openswarm` 二进制旁边那个 `pi-isolated`**（pi 车道必然用它，`lane_config`/`LaneChild`
   都这么解析），并给出它的 **sha256 + mtime**，用来区分 Nov01/Nov02 构建。
3. **当前状态的 spawn 前置条件**：wrapper 支持 `--preflight` 就用它自己的；
   **旧 wrapper 没有 `--preflight` 时不中止**，改用工具内的守卫镜像（同一套判定顺序），
   并在输出里写明「该 wrapper 早于 --preflight，判定由本地镜像给出」。
4. **历史证据**：车道自己的 run 记录（`result.json`/`status.json`/events）——
   run_id、status、error_class、error_tail、时长、`timings`，以及 §3.4 的时长对照。

### 5.2 为什么必须用 supervisor 的环境

| 环境来源 | 含义 | 能否代表 M9 |
|---|---|---|
| `/proc/<serve-pid>/environ` | **M9 supervisor 实际继承的环境** | 能（首选） |
| `systemctl --user show -p Environment <unit>` | 单元里写死的变量 | 近似（单元外的继承变量不在其中） |
| `--env` / `--env-file` | 操作者从 `/proc/<pid>/environ` 抓下来后显式传入 | 能（内容同第一行） |
| 当前 shell | 谁在哪个机器上敲命令就带谁的环境 | **不能**（读不到 supervisor 环境时**不做评估**） |

上一版脚本用「运行脚本的终端环境」跑 preflight：终端里没有
`OPENSWARM_PI_BROKER_SOCKET` 就会报「socket 未设置」，而那不是 M9 的环境——这条误报路径已
删除（脚本现在按上表取环境，并把来源写进输出）。

### 5.3 旧 wrapper 的情况

M9 在 2026-11-03 00:47 之前跑的是 Nov01/Nov02 构建的 wrapper。**旧 wrapper 没有
`--preflight`**：直接调用会得到 argparse 的「unrecognized arguments」，上一版脚本在这里
`break`，取证就停在这一步。现在的行为是：

- 先看 wrapper 源码里有没有 `--preflight`（`wrapper_info()`），
- 没有就用工具内的守卫镜像按**同样的判定顺序**评估（provider/model → 依赖 → broker socket
  类型 → socket 是否有人监听 → worktree），
- 输出里带上 wrapper 的 sha256，评审能确认「当时部署的到底是哪个构建」，
- 并且**只把结论写成「当前状态」**，不写成历史根因（§5.4）。

### 5.4 当前状态 ≠ 历史根因（本次评审 BLOCKER 的落点）

报告里两段结论必须分开：

- **current_verdict**：`spawn preconditions hold now` / `spawn precondition failed now: <guard>`，
  并附一句「这是**现在**的主机状态，不是那 9 次失败的证据」。
- **historical**：只由 run 记录得出。如果那 9 个 run 的 `error_tail` 全是空的，historical 段
  写的是**事实**：「执行器 stderr 被丢弃，agent 侧错误串从未落盘，无法追回；现存的历史证据
  只有 run 自己的时长与 error_class」。

**不允许**把「现在 preflight 是绿的」写成「当时不是 socket 的问题」，也不允许把
「现在 preflight 是红的」写成「这就是那 9 次失败的原因」。

### 5.5 手工取证（脚本不可用时的等价命令）

```bash
# 1) supervisor 的 pid 与它继承的 pi 变量
pgrep -af "lane serve M9"
tr '\0' '\n' < /proc/$(pgrep -f "lane serve M9" | head -1)/environ | grep -E 'OPENSWARM_(PI|HOME|LANES)'

# 2) supervisor 真正用的 wrapper 与它的构建
readlink -f /proc/$(pgrep -f "lane serve M9" | head -1)/exe   # 解释器
ls -l /proc/$(pgrep -f "lane serve M9" | head -1)/cwd
sha256sum <supervisor 的 openswarm 旁边的 pi-isolated>

# 3) 车道状态与最近 run 的真实错误串（注意路径：$OPENSWARM_HOME，默认 ~/.claude/openswarm；
#    候选运行时是 ~/.claude/openswarm-executor-candidate）
cat "${OPENSWARM_HOME:-$HOME/.claude/openswarm}/lanes/M9/state.json"
for d in "${OPENSWARM_HOME:-$HOME/.claude/openswarm}"/runs/*/; do
  python3 -c "import json,sys;r=json.load(open(sys.argv[1]));print(r.get('id'),r.get('status'),r.get('error_class'),r.get('seconds'),[a.get('error_tail') for a in r.get('attempts',[])])" "$d/result.json"
done

# 4) 判定失败发生在哪一段（对照 §3.4 的时长表）
python3 -c "import json,sys;s=json.load(open(sys.argv[1]));print(s.get('started'),s.get('finished'),s.get('timings'),s.get('last_error'))" \
  "${OPENSWARM_HOME:-$HOME/.claude/openswarm}"/runs/<run>/status.json

# 5) 执行器最后说了什么（改动后才有内容）
tail -5 "${OPENSWARM_HOME:-$HOME/.claude/openswarm}"/runs/<run>/events.1.pi.jsonl

# 6) broker socket 现状
ls -l /run/openswarm-pi-local/broker.sock /run/openswarm-pi/broker.sock /run/broker.sock 2>&1
ss -xlp | grep -i broker
```

判读表：

| 观察 | 结论 |
|---|---|
| run 的执行段时长 <1s | 落点是 wrapper 守卫：查 `OPENSWARM_PI_BROKER_SOCKET` / 依赖 / worktree |
| run 的执行段时长 ≈30s | 落点是 `new_session` RPC 超时（执行器活着但不应答） |
| run 的执行段时长 1–3 分钟且 events 里有 provider 错误 | 落点在 provider/broker 层，与 spawn 无关 |
| `timings.prepare_s` 很大、`first_event_s` 为空 | 失败在 prepare/工作区阶段，不是 agent |
| `timings.first_event_s` 有值 | 会话已建立，失败在模型/工具阶段 |

## 6. 本次改动（让下一次失败自带证据）

### 6.1 `openswarm/bin/pi-isolated`

- 新增 `--preflight`：按真实启动顺序评估 spawn 前置条件，打印 JSON 判定
  （`guard`/`error` + 解析出的 runtime、broker socket、worktree、sandbox 依赖），**不启动 pi、
  不碰 provider**。
- 守卫错误串改成自描述：点名变量与解析后的路径，例如
  `isolated Pi broker socket unavailable: OPENSWARM_PI_BROKER_SOCKET is not set …`。
  未设置变量不再被 `realpath('')` 悄悄解析成工作目录。

### 6.2 `openswarm/bin/openswarm`

- `LaneChild` 采集常驻执行器的 stderr（保留最近 200 行），`stderr_text()` 暴露尾部
  （`openswarm/bin/openswarm:8720` `lane_failure_detail()`）。
- 失败收尾把 stderr 尾部并入 `error_tail`：`lane_do_run` 的异常分支、turn 循环里
  「执行器已退出 / stdout 关闭」两个落点、serve 循环的崩溃路径。
- `error_tail` 现在一定落进 `result.json` 的 `attempts[]`（此前该分支恒为 `None`），
  并同步写入 `events.1.<backend>.jsonl` 与车道 `state.json` 的 `last_error`。
- 分类也用上 stderr：在「turn 结束但没有报告」这条分类路径（`lane_failure()`）上，
  provider 的 auth/限流字样出现在 stderr 时，`exec_error` 会被纠正为 `auth`/`rate_limit`
  （与 agy 车道既有行为一致）。

### 6.3 spawn 路径证据（本次新增）

每个 pi run 的 `status.json` 与失败 run 的 `attempts[].spawn` 现在记录**它自己的 spawn 路径**
（`lane_pi_spawn_evidence()`，`openswarm/bin/openswarm:8740`）：

```json
{"wrapper": "/…/releases/<build>/openswarm/bin/pi-isolated",
 "wrapper_sha256": "…",
 "env": {"OPENSWARM_PI_RUNTIME": "…", "OPENSWARM_PI_BROKER_SOCKET": "…"},
 "broker": {"path": "/run/openswarm-pi-local/broker.sock", "exists": true, "is_socket": true}}
```

它回答的是「这条 run 是**怎么**起来的」，正是「spawn 路径证据」；与 `error_tail`（agent 侧
错误串）合起来，下一次同类失败不再是无字可查。取证脚本会直接读它（§5.1）。

### 6.4 `tools/check-pi-spawn-failure.py`（重写，只读）

§5.1 的取证包：supervisor 环境 → supervisor 的 wrapper（含 sha256）→ 当前 preflight
（支持 `--preflight` 的 wrapper 用它自己的；旧 wrapper 用本地守卫镜像）→ 历史 run 记录。
current/historical 两段结论分开（§5.4）。

### 6.5 `tools/check-lane-first-run.py`（新增，只读）

PAUSE-M9 的解除闸门：取「某时刻之后该车道的第一个 run」，检查它 `status=done`、
`commit` 非空、`error_class` 不是 `exec_error`，并把 commit 打出来。

```bash
python3 tools/check-lane-first-run.py --lane M9 --since 2026-11-03T01:10:00Z
# 退出码 0 = 首跑验真通过（会打印 commit）
```

## 7. 判别矩阵（对照实验 M8 / M19 的读法）

| 对照实验结局 | 判读 |
|---|---|
| M8（Nov01 构建，同机）首跑成功出提交 | 排除「Nov02 版本回归」与「主机层故障」；指向 **M9 单车道配置/环境**（lanes.json 的 model/provider/env、broker socket 指向、worktree） |
| M8 也秒挂 | 指向**主机层**（broker 卡死/在途请求占满 `maxInFlight=4`、磁盘、cgroup）或两台车道共享的部署配置 |
| M19（Nov02 构建，同机）持续成功 | 排除「Nov02 版本回归」（前提：M19 也是 pi 车道；否则该证据无效） |
| GH2 同签名 9 连挂 | 说明不止 M9：优先查 broker 层（同一 broker 服务多车道）与宿主环境，而非车道配置 |
| broker 请求日志有 M9 lane 的记录且非 2xx | 故障在 provider 层（额度/限流/上游），不是 spawn |
| broker 请求日志**没有** M9 lane 的任何记录 | 故障在 spawn/会话层，与 §4 的 socket 嫌疑一致 |

补充说明（版本证据）：Nov01 = `df02c552`，Nov02 = `a9ac8f33`（`git merge-base --is-ancestor`
确认 `df02c552` 是 `a9ac8f33` 的祖先，区间 595 个提交）。区间内与 pi 相关的改动只有两处：
`86d89f1c6`（`pi-isolated` 的 bwrap PID namespace 兜底）与 `93bb560b7`（pi auto-retry 宽限 +
broker 审计日志）。**pi 车道在 Nov02 增加了「env/env_file 不支持」的硬校验**
（`openswarm/bin/openswarm:8206` 的 `lane_env_config_error`），若 M9 的 lanes.json 给 pi 车道
配了 `env`/`env_file`，Nov02 会直接判定 lanes.json 非法——但这会让 `lane serve` 起不来，
与「M9 serve 存活」矛盾，故 M9 大概率没有配 `env`/`env_file`；**broker socket 环境变量因此只能
来自 systemd 单元或 supervisor 继承的环境**，这正是 §5.2 要核实的点。

补充说明（宿主机）：上一轮诊断所在宿主机（`hostname` = `VM-4-2-ubuntu`）上可见的 broker 进程
是 `/usr/bin/node …/releases/45c8ada5…/openswarm/bin/pi-minimax-broker.mjs
/run/openswarm-pi-local/broker.sock … MiniMax-M3.1-Flash`（Nov01 起）——与卡片描述的
「M9 pi broker 存活（Nov01，MiniMax-M3.1-Flash）」逐项吻合；但该进程的 mount namespace
对工作树不可见，socket 目录与 lane 目录都读不到，**无法据此断定该机就是 gores1**。

## 8. PAUSE-M9 的解除条件

解除 `~/phanes-ctl/autodispatch/PAUSE-M9` 必须**同时**满足：

1. **根因已定位并写进 issue**：§5.1 的取证包贴回 issue，且结论基于 §3.4 的**时长对照**
   （run 记录里的执行段时长），不是基于「现在的 preflight 绿/红」。
2. **首跑验真**：解除后第一张卡必须 `status=done` 且 `commit` 非空（出提交），
   `result.json` 的 `error_class` 不是 `exec_error`。用
   `python3 tools/check-lane-first-run.py --lane M9 --since <解除时刻>` 判定，退出码 0，
   并把打印出来的 commit 贴回 issue。
3. **连续稳定**：随后连续 ≥2 次派卡不再出现 `exec_error`（`retry.py` 的 `exec_error` 计数不增长，
   车道不再被「N 连败自动暂停」重新挂起）。
4. **若根因指向版本/构建**：M8 与 M9 必须回到同一构建，或在回滚后的构建上重跑第 2、3 条；
   不接受「只在另一台车道验证过」。
5. **不以卡片内容作为解除理由**：本卡与 9 张被返池的卡都与根因无关，不得用「换卡重试」
   代替第 1–3 条。
6. **未定位根因前不得解除**（本卡原始要求）。**本次提交不解除暂停。**

## 9. 本次仍未闭环的部分（诚实声明）

- **M9 现场的错误串仍未拿到**，而且**在本沙箱内不可能拿到**：本工作树（车道 A2 的执行沙箱）
  与 gores1 上 M9 的运行副本不在同一 mount namespace（`nsenter` 被拒），沙箱无网络、无
  SSH 出口、无 `/root/.ssh`，`$OPENSWARM_HOME` 下也没有 M9 的 lane/run 目录。那 9 次失败的
  历史错误串已随 stderr→`/dev/null` 一起丢失，**不可追回**。
- 因此**本次没有宣称根因**：§3.4 给出了把落点缩小到一个的判定方法，§5 给出了取到现场数据的
  一条命令，但「是哪一个落点」必须由 §10 的现场取证回答。
- **M9 恢复后的首跑验真也没能在本次完成**（同样因为没有 M9 的执行副本）；闸门已实现为
  `tools/check-lane-first-run.py`，等 AD/操作者在 gores1 上跑。
- 现场 broker 进程早于带审计日志的版本（审计日志见 `93bb560b7`），所以
  `broker.sock.requests.jsonl` 在该故障窗口**不存在**，§7 表格里「broker 请求日志」一列对本次
  事故不可用；后续需把 broker 升到带审计日志的构建，该证据链才对下一次事故有效。
- M8（`#1693`）与 M19（`#1710`）的对照结果属于在途实验，本工作树无法观测，
  §7 的判读表按其结局给出结论映射。

## 10. 现场取证清单（AD/操作者在 gores1 上跑，一次跑完）

```bash
# 在 gores1 上，仓库 checkout 里：
python3 tools/check-pi-spawn-failure.py --lane M9 --json > /tmp/m9-spawn-evidence.json
python3 tools/check-pi-spawn-failure.py --lane M9            # 人读的同一份
# 把两份贴回 issue #1747；历史段里的 run 时长用于套 §3.4 的表。

# 解除暂停之后：
python3 tools/check-lane-first-run.py --lane M9 --since <解除时刻> --json
# 退出码 0 且 commit 非空 = 首跑验真通过（把 commit 贴回 issue）
```

## 11. 本次新增测试与实测结果

| 测试 | 结果 |
|---|---|
| `openswarm/tests/test_pi_isolation.py::PiPreflightTests`（4 项：变量未设/模型不匹配/socket 活着/socket 陈旧） | 通过 |
| `openswarm/tests/test_lane.py::LaneTests::test_pi_spawn_failure_keeps_the_executor_stderr_in_the_failed_run`（pi 执行器在会话建立前带 stderr 退出 → run 的 `error_tail` 与事件文件必须含 agent 侧错误串） | 通过 |
| `openswarm/tests/test_lane.py::LaneTests::test_pi_run_records_the_spawn_path_it_was_started_with`（run 必须记录自己是用哪个 wrapper、哪个 sha256 起的） | 通过 |
| `tools/tests/test_check_pi_spawn_failure.py`（12 项：supervisor 定位/只读 pi 变量/认 supervisor 旁边的 wrapper/旧 wrapper 无 preflight/守卫镜像三态/run 记录读取/current≠historical 标注/旧 wrapper 不中止取证/环境读不到就不评估） | 通过 |
| `tools/tests/test_check_lane_first_run.py`（5 项：since 解析/闸门三种结局/端到端 PASS 与 FAIL） | 通过 |
| `openswarm/tests/test_pi_isolation.py::PiIsolationTests`（既有沙箱用例） | 本机跳过/失败：沙箱内核不允许创建 user namespace（`bwrap: No permissions to create new namespace`），与本次改动无关 |
| `openswarm/tests/test_lane.py` 全量 | 106 项全过（`test_late_crash_without_descendants_releases_shared_fleet_slot` 偶发失败——**在未改动的 main 副本上同样偶发**，本机既有 flake） |
| `openswarm/tests/` 全量（`python3 -m unittest discover -s tests`） | 1919 项，3 failures + 5 errors，**与本次改动无关**：同一组失败在未改动的 main 副本上复现（`test_pi_isolation` 3 项因 bwrap 不能建 namespace，`test_fleet_refresh` 5 项因本机 ssh/openssl 不可用） |

`openswarm/tests/fixtures/fake_pi_rpc.py` 新增一个触发词 `LANE_TEST_EXIT_WITH_STDERR`：模拟
pi-isolated 的 spawn 守卫失败（往 stderr 写一行原因后退出），供上面的回归用例使用。

### 审查补充：环境完整性与验收状态

仅直接读取运行中 supervisor 的 `/proc/<pid>/environ` 后才评估当前守卫。systemd 单元环境和 `--env` / `--env-file` 可能不完整或无法验证来源，脚本不会用终端环境补齐，也不会据此评估；上文有关这些备用来源可用于评估的描述以此为准。当前检查不能证明历史根因。现场错误或 spawn 证据、修复及首跑提交仍未取得，PAUSE-M9 必须保持，直至这些验收条件全部满足。
