# Box 重启后控制面自恢复（boot-ensure / start-control-plane）

Refs #1478（Refs #1468）。

## 结论

box 没有 systemd 也没有 cron（见 [`docs/ctl-boot.md`](ctl-boot.md)），**重启后必须人工跑一次**
`~/phanes-ctl/bin/start-control-plane`。仓库里没有叫 `boot-ensure` 的脚本，本文把它称作
**boot-ensure 入口**，指的就是这条现行等价入口：

| 角色 | 位置 | 说明 |
|------|------|------|
| boot-ensure 入口（box 运行副本） | `~/phanes-ctl/bin/start-control-plane` | 幂等：在跑就 `already:`，没在跑就 `start:` |
| boot-ensure 入口（仓库镜像） | `tools/lane-autodispatch/start-control-plane.example` | 本次演练直接跑的就是它 |
| supervisor | `~/phanes-ctl/bin/ctl-loop`（仓库镜像 `bin/ctl-loop`） | `ib-refresh-loop` 的看护，写 `ctl-loop.heartbeat` |

跑完一次之后：

1. 五件套循环（`ib-refresh-loop` / `lane-autodispatch` / `lane-merge` / `bus-worker-loop` /
   `health-loop`）全部回来，心跳重新变新鲜；
2. **已 claim 的 lane 不会被错误冻结**：过期的 `auto:` 暂停被 `health-loop` 清掉，重启前遗留的
   busy lane 状态被 AD 的 `reconcile()` 释放（issue 回到 `lane-ready`），in-flight claim 标记不丢，
   claim 租约过期后允许接管。

一次准真实重启演练的时间线证据见文末「实测证据」。

## 短跑手册（重启后 60 秒）

```bash
# 1) 看门：心跳是否过期（默认阈值 20 分钟，只读、不杀进程）
python3 tools/check-ad-heartbeat-stall.py

# 2) 拉起控制面（幂等；重启后跑一次即可，已在跑的循环会被跳过）
~/phanes-ctl/bin/start-control-plane

# 3) 复核五件套 + 四条心跳（无 SSH）
bash tools/check-ctl-boot.sh                 # 检查真实 ~/phanes-ctl
bash tools/check-ctl-boot.sh --self-test     # 离线 fixture 自检
bash docs/check-ctl-boot.sh --self-test      # 离线路径存在性自检

# 4) 看 lane 是否还被冻结
ls ~/phanes-ctl/autodispatch/PAUSE ~/phanes-ctl/autodispatch/PAUSE-* 2>/dev/null
python3 tools/check-ghost-in-progress.py     # 幽灵 in-progress 判定（只读，不改 GitHub）

# 5) 看 claim 租约是否过期可接管
python3 tools/check-bus-claim-lease.py       # 过期 claim 不永久锁死
```

只想验证「重启后能不能自己回来」而不上真机，跑离线演练（真实进程 + 真实脚本副本，不碰
`~/phanes-ctl`、不连 SSH、不访问 GitHub）：

```bash
python3 tools/check-box-boot-recovery.py
```

## 重启后的现场模型

| 现场残留 | 重启后状态 | 谁负责恢复 |
|----------|------------|------------|
| 五件套进程 | 全部消失 | `start-control-plane`（boot-ensure）重新拉起 |
| 五条心跳文件 | mtime 停在关机前 → 过期（>120s 即被 `ctl-loop` 判不健康） | 新进程写新心跳 |
| `autodispatch/state.json` 里的 lane 占用 | 还在（磁盘文件） | AD `reconcile()` 释放（见下） |
| issue 上的 `in-progress` 标签 | 还在 | 同上，重新贴回 `lane-ready` |
| `bus-worker/<run_id>/claim.json` | 还在 | 受控重启不得丢弃（`openswarm/docs/BUS.md:95-99`） |
| `PAUSE` / `PAUSE-<LANE>` / `PAUSE-BACKEND-*` | 还在 | `auto:` 体由 `health-loop` 在过期后清掉；**手动暂停永不自动清除**，要人工 `rm` |

## 已 claim 的 lane 为什么不会被错误冻结

三条互相独立的恢复路径，任一条成立就不会永久冻结：

1. **AD 自己回收孤儿 lane**：`tools/lane-autodispatch/autodispatch.py:250-271` 的 `reconcile()`
   判定「本地说 busy、远端 lane 空闲、run 仍是 running 但 runner 已死、占用超过 180/600 秒」
   → 去掉 `in-progress`、贴回 `lane-ready`，调用点 `autodispatch.py:556` 随后
   `state.pop(L)` 释放 lane 占用。这是「重启前 lane 正在跑、机器被断电」这一场景的正解。
2. **幽灵 `in-progress` 兜底**：`tools/check-ghost-in-progress.py:36`（门槛 600 s）识别
   「标签在、lane 记录没了」的 issue，给出重贴命令；`autodispatch.py:20` 的
   `DISPATCH_EXCLUDING_LABELS` 含 `in-progress`，所以不回收就永远不再被派发——这正是必须做回收的原因。
3. **bus claim 租约**：`openswarm/docs/BUS.md:66,99` —— claim 默认 300 s 租约，过期不永久锁死，
   其它白名单 worker 可以接管；`openswarm/lib/dispatch/lease.py` 的 `LeaseStore.expired()` 同理：
   过期且运行已停 → 可回收，运行仍在 → 不回收（不误伤在跑的任务）。

另外，`PAUSE-<LANE>` 是**人工**冻结：body 不是 `auto:` 开头时 `health-loop` 永不自动清除
（`docs/ctl-boot.md`「Health pauses」一节）。重启后如果 lane 仍被冻结，先看是不是留下了手动暂停文件。

## 实测证据：准真实重启演练

演练脚本：`tools/check-box-boot-recovery.py`（纯离线，只用 stdlib）。

- 真实进程：五个循环都是真的后台进程，用真的 `SIGKILL` 杀；
- 真实脚本：`bin/ctl-loop` 的副本当 `ib-refresh-loop` 的 supervisor，
  `tools/lane-autodispatch/start-control-plane.example` 的副本当 boot-ensure 入口；
- 真实仓库代码：AD 的 `reconcile()`（只替换 GitHub I/O）、`openswarm/lib/dispatch/lease.py`；
- 模拟断电：SIGKILL 全部循环 + 心跳文件回拨 600 s（停机 10 分钟）；
- 不碰真实 `~/phanes-ctl`，不连 SSH，不访问 GitHub。

命令与输出（2026-11-01，fixture 路径已缩写为 `…/box-boot-recovery-XXXX/home`）：

```
$ python3 tools/check-box-boot-recovery.py
2026-11-01T20:41:41Z [T+  0.0s] setup      fixture home=…/box-boot-recovery-XXXX/home
2026-11-01T20:41:41Z [T+  0.0s] setup      重启前现场：M12 已 claim #1478 + in-flight bus claim + 过期 auto: PAUSE-M12
2026-11-01T20:41:41Z [T+  0.0s] boot-1     start: ib-refresh-loop
2026-11-01T20:41:41Z [T+  0.0s] boot-1     start: lane-autodispatch
2026-11-01T20:41:41Z [T+  0.0s] boot-1     start: lane-merge
2026-11-01T20:41:41Z [T+  0.0s] boot-1     start: bus-worker-loop
2026-11-01T20:41:41Z [T+  0.0s] boot-1     start: health-loop
2026-11-01T20:41:41Z [T+  0.0s] boot-1     heartbeat ib-refresh-loop    ctl-loop.heartbeat           age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.0s] boot-1     heartbeat lane-autodispatch  autodispatch/heartbeat       age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.0s] boot-1     heartbeat lane-merge         autodispatch/merge-heartbeat age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.0s] boot-1     heartbeat bus-worker-loop    bus-worker/heartbeat         age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.0s] boot-1     heartbeat health-loop        autodispatch/health.heartbeat age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.0s] boot-1     ib-refresh.status age=   0.0s row=ok=1 hosts_ok=3 skipped_cooldown=0 min_ttl=1500 min_exp=1793565701
2026-11-01T20:41:41Z [T+  0.0s] boot-1     五件套全部在跑：bus-worker-loop=7789, health-loop=7796, ib-refresh-loop=7772, lane-autodispatch=7774, lane-merge=7778
2026-11-01T20:41:41Z [T+  0.0s] reboot     SIGKILL bus-worker-loop (pid 7789) — 模拟断电，无收尾
2026-11-01T20:41:41Z [T+  0.0s] reboot     SIGKILL health-loop (pid 7796) — 模拟断电，无收尾
2026-11-01T20:41:41Z [T+  0.0s] reboot     SIGKILL ib-refresh-loop (pid 7772) — 模拟断电，无收尾
2026-11-01T20:41:41Z [T+  0.0s] reboot     SIGKILL lane-autodispatch (pid 7774) — 模拟断电，无收尾
2026-11-01T20:41:41Z [T+  0.0s] reboot     SIGKILL lane-merge (pid 7778) — 模拟断电，无收尾
2026-11-01T20:41:41Z [T+  0.2s] reboot     heartbeat ib-refresh-loop    ctl-loop.heartbeat           age= 600.2s STALE
2026-11-01T20:41:41Z [T+  0.2s] reboot     heartbeat lane-autodispatch  autodispatch/heartbeat       age= 600.2s STALE
2026-11-01T20:41:41Z [T+  0.2s] reboot     heartbeat lane-merge         autodispatch/merge-heartbeat age= 600.2s STALE
2026-11-01T20:41:41Z [T+  0.2s] reboot     heartbeat bus-worker-loop    bus-worker/heartbeat         age= 600.2s STALE
2026-11-01T20:41:41Z [T+  0.2s] reboot     heartbeat health-loop        autodispatch/health.heartbeat age= 600.2s STALE
2026-11-01T20:41:41Z [T+  0.2s] reboot     停机 600s：心跳全部过期（> 120s 阈值）
2026-11-01T20:41:41Z [T+  0.3s] boot-2     start: ib-refresh-loop
2026-11-01T20:41:41Z [T+  0.3s] boot-2     start: lane-autodispatch
2026-11-01T20:41:41Z [T+  0.3s] boot-2     start: lane-merge
2026-11-01T20:41:41Z [T+  0.3s] boot-2     start: bus-worker-loop
2026-11-01T20:41:41Z [T+  0.3s] boot-2     start: health-loop
2026-11-01T20:41:41Z [T+  0.3s] boot-2     heartbeat ib-refresh-loop    ctl-loop.heartbeat           age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.3s] boot-2     heartbeat lane-autodispatch  autodispatch/heartbeat       age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.3s] boot-2     heartbeat lane-merge         autodispatch/merge-heartbeat age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.3s] boot-2     heartbeat bus-worker-loop    bus-worker/heartbeat         age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.3s] boot-2     heartbeat health-loop        autodispatch/health.heartbeat age=   0.0s fresh
2026-11-01T20:41:41Z [T+  0.3s] boot-2     ib-refresh.status age=   0.0s row=ok=1 hosts_ok=3 skipped_cooldown=0 min_ttl=1500 min_exp=1793565701
2026-11-01T20:41:41Z [T+  0.3s] boot-2     五件套全部回来且是新进程：bus-worker-loop=7832, health-loop=7837, ib-refresh-loop=7817, lane-autodispatch=7819, lane-merge=7825
2026-11-01T20:41:46Z [T+  5.3s] boot-2     心跳持续推进（每条心跳都再次更新），无假性恢复
2026-11-01T20:41:46Z [T+  5.3s] boot-2     每个循环恰好一个实例（无双开）
2026-11-01T20:41:46Z [T+  5.3s] claim      过期的 auto: PAUSE-M12 已被 health-loop 清除
2026-11-01 20:41:46 M12 #1478 run 20261101-120000-abc123 orphaned (remote lane idle, current_run=None, run_state=running, runner dead) → requeued (lane-ready)
2026-11-01T20:41:46Z [T+  5.3s] claim      AD reconcile() 释放 M12 #1478 -> 重新贴 lane-ready（不再被冻结）
2026-11-01T20:41:46Z [T+  5.3s] claim      dispatch lease：过期且运行已停 -> 可回收 #1478；运行仍在 -> 不回收
2026-11-01T20:41:46Z [T+  5.3s] claim      in-flight claim 标记保留（受控重启不丢标记）
2026-11-01T20:41:46Z [T+  5.3s] claim      停机 600s > claim 租约 300s -> 租约已过期，其它 worker 可接管

OK: boot-ensure 在模拟重启后拉回控制面五件套
OK: 重启后心跳全部恢复并持续推进，且没有双开实例
OK: 已 claim 的 lane 被释放回 lane-ready，没有被错误冻结
```

演练断言的检查点：

- 五件套在重启后全部是新进程（pid 与 boot-1 不同），每个循环恰好一个实例（无双开）；
- 五条心跳在 boot-ensure 后 30 s 内全部变新鲜，并且 5 s 后再次更新（不是一次性写入的假恢复）；
- `ib-refresh.status` 被重新刷新（`ib-refresh-loop` 真的又跑了一次预检）；
- 重启前留下的过期 `auto: PAUSE-M12` 被 `health-loop` 清掉；
- 重启前留下的 busy lane 状态被 AD `reconcile()` 释放，issue 回到 `lane-ready`；
- in-flight claim 标记保留，停机超过 300 s 租约后允许其它 worker 接管。

同一批契约的既有离线检查（本次一并跑过，全绿）：

```
$ python3 tools/check-ctl-loop-reap.py
OK: fresh heartbeat + live pid does not restart
OK: expired heartbeat restarts once and reaps a live old pid
OK: missing/exited pid restarts once without duplicate instances

$ python3 tools/check-ghost-in-progress.py
ghost in-progress detection: OK

$ python3 tools/check-bus-claim-lease.py
ok: bus claim lease expiry prevents deadlock and allows other workers to take over (过期 claim 不永久锁死)

$ bash tools/check-ctl-boot.sh --self-test
OK self-test passed cleanly
```

## 本次发现：boot-ensure 镜像少了一个循环（已修）

演练第一次运行就直接失败在：

```
AssertionError: boot-ensure 入口缺少循环：['health-loop']
```

仓库镜像 `tools/lane-autodispatch/start-control-plane.example` 原来只拉起四个循环，缺
`health-loop`，与 `docs/ctl-boot.md` 的五件套契约、`tools/check-ctl-boot.sh` 的断言不一致。
影响：重启后 `health-loop` 不回来 → 过期的 `auto:` 暂停永远没人清 → **已 claim 的 lane 会被错误冻结**。

本次已把镜像补齐为五件套（Refs #1478），并给演练加了静态断言防止再漂移：

```
start_one ib-refresh-loop   "$HOME/phanes-ctl/bin/ctl-loop"
start_one lane-autodispatch "$HOME/phanes-ctl/bin/lane-autodispatch"
start_one lane-merge        "$HOME/phanes-ctl/bin/lane-merge"
start_one bus-worker-loop   "$HOME/phanes-ctl/bin/bus-worker-loop"
start_one health-loop       "$HOME/phanes-ctl/bin/health-loop"
```

**需要在 box 上核对**：`~/phanes-ctl/bin/start-control-plane` 是否也有 `health-loop` 这一行。
仓库镜像不是运行副本（见 `docs/ctl-boot.md`「部署说明（仓库镜像 ≠ 运行副本）」一节），
线上副本若同样缺这一行，重启后 lane 仍可能被错误冻结。

## 边界（本演练没有证明什么）

- 演练跑在临时 fixture 里，**没有真的重启物理 box**：真实重启还会带来 SSH 证书过期
  （`ib-refresh` 的 `hosts_ok` 门槛）、磁盘/内存告警触发 `PAUSE-HOST-*`、真实
  `~/phanes-ctl` 里遗留的其它状态文件等，这些不在本次范围内。
- 演练里 AD 的 GitHub I/O 是假的（`reconcile()` 的 `gh` 调用被替换），真实 issue 的
  `gh issue edit` 是否成功要看线上 GitHub 状态；判定逻辑本身是仓库里的原实现。
- 真实 box 上 `start-control-plane` 用 `pgrep -f` 判断「已在跑」：如果机器上还有同名残留进程
  （例如没退干净的旧循环），它会打印 `already:` 而不重启——这是既有行为，本次未改。
- 手动暂停（body 不以 `auto:` 开头）重启后仍然冻结 lane，需要人工 `rm`，这是设计如此。

## 相关文件

| 文件 | 作用 |
|------|------|
| `tools/check-box-boot-recovery.py` | 本次的准真实重启演练（离线，可重复） |
| `tools/lane-autodispatch/start-control-plane.example` | boot-ensure 入口的仓库镜像（本次补齐 health-loop） |
| `bin/ctl-loop` | `ib-refresh-loop` 的 supervisor 副本（演练里真实参与） |
| `docs/ctl-boot.md` | 控制面五件套、心跳路径、暂停规则的权威文档 |
| `tools/check-ad-heartbeat-stall.py` | 重启后心跳是否过期的看门脚本 |
| `tools/check-ghost-in-progress.py` | 幽灵 `in-progress` 判定（只读） |
| `tools/check-bus-claim-lease.py` | 过期 claim 可接管的契约检查 |
| `openswarm/docs/BUS.md` | bus claim 租约与受控重启的 in-flight 标记契约 |
