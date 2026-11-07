# 全舰队状态单页聚合（Refs #1718）

## 背景

2026-11-02 晚排障要在五个地方来回翻：车道忙闲（`lanes-status.json`）、gh 标签队列、容量预留（`fleet-capacity`）、MiniMax 烧量（broker 请求日志）、主机探活（`fleet-health.json`）。每个口径都有自己的时钟，**过期的数字和新鲜的数字长得一模一样**——那晚 `lanes-status.json` 的 `_at` 停滞了 5 分钟，看的人却以为它是实时的。

舰队需要一个单一状态面：一次读到全部关键水位，并且每个数字都带着自己的采集时间与来源。

## 产出

| 产物 | 位置 | 说明 |
|---|---|---|
| `status.json` | `$OPENSWARM_HOME/fleet-status.json`（`--out` 可改） | 机器可读的聚合页 |
| 一屏文本 | stdout（`--brain-log` 可追加进脑日志） | 脑日志友好的摘要，每分区一行 |

## 分区

| 分区 | 内容 | 来源 | 采集时间 |
|---|---|---|---|
| `lanes` | 忙/闲车道、在途卡号、状态异常的车道 | `lanes-status.json`（AD 扁平格式或 dispatch schema 2） | 快照自带的 `_at`（**不是文件 mtime**） |
| `pool` | 池深度**双口径并列**：gh 标签口径与 AD `_ready` 口径，给出 delta | `gh issue list --label lane-ready` + `lanes-status.json` | 两个口径各自的时间戳（gh 取数时刻 / 快照 `_at`） |
| `capacity` | 预留总数、最老预留的年龄、按主机/后端分布、限额占用 | `fleet-capacity status` 或 `capacity.sqlite` | 文件 mtime |
| `hosts` | 每台主机存活、最差状态、每台探活时间 | `fleet-health.json` | 文件 mtime + 每台 `updated_at` |
| `burn` | MiniMax 近 15 分钟烧量（liveness 口径：请求数、200/429、输入/输出 token） | MiniMax broker 请求日志 JSONL | 文件 mtime |
| `needs_human` | needs-human 数量与 PR 号 | `gh pr list --label needs-human` | 文件 mtime / 取数时刻 |
| `pauses` | 当前打开的 PAUSE 开关清单（含作用域、原因、年龄、剩余时长） | pause store `<home>/pauses/*.json` + 旧式 `PAUSE*` 标记 | 最新文件 mtime |

## 时钟口径

车道与 AD 池口径的采集时间取**快照自带的 `_at`**，不取文件 mtime：

- 文件 mtime 只说明「文件最后一次被写是什么时候」。快照被复制、被 `touch`、或者被别处的旧内容覆盖时，mtime 会跳到当下，而快照里的读数其实停在 21:00——停滞的 `_at` 会被当成新鲜的读数，正好掩盖 issue 里点出的那次故障。
- `_at` 是 dispatcher 写进自己快照里的时钟（`tools/lane-autodispatch/autodispatch.py:421`、`swrm/lib/dispatch/engine.py:2167`），复制和改写都改不动它。
- 快照没有 `_at`、或 `_at` 解析不出来时，该读数**没有时钟**：车道区直接标 `未核实`，AD 池口径标 `未核实`，数字仍然照给，但页面不假装它是新鲜的。
- 池区有两个时钟，所以每个口径各带各的 `collected_at`，分区 envelope 取**最旧的那个**口径：任一口径停滞，整区即被标 `过期`。
- 其余分区（容量、主机、烧量、待人工、开关）仍以文件 mtime 为采集时间，主机区另给每台的 `updated_at`。

## 语义

- **每个分区自带采集时间与来源**：分区 envelope 里是 `collected_at` / `collected_at_epoch` / `source` / `age_s` / `stale`。
- **过期一眼可见**：分区年龄超过 `--max-age`（默认 300 秒）标 `过期`，页面 `ok` 置假。
- **采集失败不拖垮整页**：某分区采集失败时该区标 `未核实`（`verified: false`、`status: "unverified"`）并附 `error`，其余分区照常产出，页面照样写出；能读到的数字仍以 `未核实: <原因> | <读数>` 的形式留在原位。
- **双口径并列**：池深度同时给 gh 口径与 AD `_ready` 口径并给 delta；两者不一致本身就是「有一边过期」的信号。

## 用法

```bash
# 线上：读本机控制面数据，产出 status.json 与一屏摘要
python3 tools/fleet-status-page.py \
  --lanes-status ~/phanes-ctl/autodispatch/lanes-status.json \
  --gh-live \
  --capacity-dir ~/.claude/swrm-fleet-capacity \
  --hosts ~/.claude/swrm/fleet-health.json \
  --broker-log ~/.claude/swrm/pi-broker.requests.jsonl \
  --pauses ~/.claude/swrm/pauses \
  --markers ~/phanes-ctl/autodispatch \
  --tz +08:00 \
  --out ~/.claude/swrm/fleet-status.json \
  --brain-log ~/phanes-ctl/brain/fleet-status.log

# 离线回放（不联网、不碰运行中的目录）
python3 tools/fleet-status-page.py --self-test
```

`--strict` 时只要有分区未核实或过期就以非零码退出（便于放进巡检），默认仍然以 0 退出并把整页写出来。

## 2026-11-02 晚回放

回放数据在 `swrm/tests/fixtures/fleet-status-2026-11-02/`，回放结果（`--now 2026-11-02T21:05:00+08:00`）：

```
fleet status @ 2026-11-02T13:05:00+00:00  sections=7 unverified=0 stale=0
[车道] busy=5 idle=5 unknown=1 in_flight=5 (#1711,#1712,#1713,#1715,#1716)  @2026-11-02T13:04:12+00:00  lanes-status.json  [age=48s]
[池] gh=7 ad=9 delta=-2 by_backend=agy:2,codex:2,pi:3 gh@2026-11-02T13:04:55+00:00 ad@2026-11-02T13:04:12+00:00  gh issue list --label lane-ready + lanes-status.json  [age=48s]
[容量] reservations=3 oldest=34m00s oldest_run=20261102-2011-a41c by_host=gocc:1,gores1:1,local:1  @2026-11-02T13:04:58+00:00  fleet-capacity status  [age=2s]
[主机] alive=3/4 worst=unreachable down=govpn  @2026-11-02T13:03:00+00:00  fleet-health.json  [age=2m00s]
[烧量] 15m requests=6 ok=5 rate_limited=1 in_tok=19190 out_tok=5130  @2026-11-02T13:04:59+00:00  broker requests jsonl  [age=1s]
[待人工] count=2 (#980,#981)  @2026-11-02T13:04:58+00:00  gh pr list --label needs-human  [age=2s]
[开关] count=2 lane:CX5(11m40s) mint(1m40s)  @2026-11-02T13:03:20+00:00  pause store + PAUSE markers  [age=1m40s]
```

同一份回放里的三种故障：

1. **快照停滞**（把 `lanes-status.json` 的 `_at` 改成 6 分钟前，文件 mtime 保持当下）：车道区与池区一起被标 `过期`，页面 `ok` 置假——这正是那晚的故障，现在藏不住了。

```
[车道] busy=5 idle=5 unknown=1 in_flight=5 (#1711,#1712,#1713,#1715,#1716)  @2026-11-02T12:59:00+00:00  lanes-status.json  [过期6m00s]
[池] gh=7 ad=9 delta=-2 by_backend=agy:2,codex:2,pi:3 gh@2026-11-02T13:04:55+00:00 ad@2026-11-02T12:59:00+00:00  gh issue list --label lane-ready + lanes-status.json  [过期6m00s]
```

2. **快照没有时钟**（`_at` 缺失或解析不出来）：车道区与池区标 `未核实`，读数仍然照给，其余五个分区照常产出。

```
[车道] 未核实: lanes-status.json has no valid _at stamp: the snapshot does not say when it was taken | busy=5 idle=5 unknown=1 in_flight=5 (#1711,#1712,#1713,#1715,#1716)  lanes-status.json
[池] 未核实: lanes-status.json has no valid _at stamp: the dispatcher reading cannot be dated | gh=7 ad=9(未核实) delta=-2 by_backend=agy:2,codex:2,pi:3 gh@2026-11-02T13:04:55+00:00  gh issue list --label lane-ready + lanes-status.json  [age=5s]
```

3. **某个分区采不到**（把 `--hosts` 指向不存在的文件）：只有主机区变成 `未核实`，其余六个分区照常产出。

## 实现位置

- 聚合与渲染（纯函数，不读文件、不碰时钟）：[`swrm/lib/dispatch/fleet_status.py`](../swrm/lib/dispatch/fleet_status.py)
- 采集与落盘：[`tools/fleet-status-page.py`](../tools/fleet-status-page.py)
- 测试：[`swrm/tests/test_fleet_status.py`](../swrm/tests/test_fleet_status.py)
