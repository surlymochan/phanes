# #1817 空闲 lane serve 泄漏僵尸 git：根因、修复与验收

## 现场（2026-11-03 14:15，builder 实证，gores1）

M19 run `20261103-134716-6e76f7` 以 `exec_error` 秒败，`result.json` 的 `error_tail` 是

```
lane worktree preparation failed (checkout -B): fatal: unable to create threaded lstat: Resource temporarily unavailable
```

同机约有 4600 个僵尸进程（以 `git` 为主），全部挂在三个**空闲** serve 上
（lane serve M9/GH2/M19，release `a9ac8f33`，已运行 18h/18h/13h），各约 1533 个，
约每 17–40 秒泄漏一个；同机 M8 的 serve（release `df02c552`）没有僵尸。
僵尸占满进程/线程表，同机任何新 `git` 起线程失败 —— 这是 PAUSE-M9/PAUSE-GH2
（00:17–00:55 `exec_error` 簇）与 M19 失败的最可能根因。

## 根因

lane serve 是 Linux subreaper（`lane_become_subreaper()`），执行端（executor）留下的
孤儿进程会被它收养。`git` 在自己的后台工作里会脱离父进程（`git gc --auto` fork 一个
后台 `git gc` 就走人），这个孤儿死掉之后**没有人 wait**，于是变成 serve 的僵尸进程，
一直挂到下次重启：空闲 serve 每 17–40 秒泄漏一个，与现场数量级一致。

修复前的空闲轮询路径里没有任何一次“回收被收养的孤儿”，所以每一条脱离的 `git`
都变成僵尸；`lane serve` 空闲时的每一次轮询（本改动之前根本没有回收动作）只会
让它们一直挂着。

## 修复

### 1. 回收被收养的孤儿（`openswarm/bin/openswarm`）

- `lane_reap_zombies(child)`：用 `waitpid(-1, WNOHANG)` 回收**所有**死掉的子进程
  （被收养的孤儿在内）。执行端进程自己的退出状态不丢：`proc.returncode` 被原样交回
  给子进程对象，`child.alive()` 仍然报告崩溃。
- `lane_idle_poll()` / `lane_serve_poll()`：空闲 serve 每一次轮询都调用它。
- `lane_child_wakeup()`：SIGCHLD 唤醒管道。subreaper 收到 SIGCHLD 时立刻醒来回收，
  不用等下一次轮询，所以空闲 serve **连一瞬间的僵尸都不持有**（轮询间隔 ≤0.5s
  时的瞬时僵尸窗口被压到微秒级）。

### 2. 僵尸计数与告警

- serve 侧：lane state 里记 `zombie_count`（主机 Z 状态进程数）、`zombie_warning`
  （超过阈值时的告警）和 `zombies_reaped`（这个 supervisor 累计回收的子进程数）。
  阈值 `lane.zombie_warn_count` 可配置，默认 **200**。
- health 侧：`lib/dispatch/health.py` 把 `zombie_count` 当成一种主机压力，
  阈值 `resources.health.zombie_pause_count` 可配置，默认 **200**；超阈值时
  `openswarm fleet health --apply` 暂停该主机（scope `host:<name>`），派发 tick
  不再给这个主机派活；压力消退后 probe 释放主机，tick 重新派发。

## 回归测试

```sh
cd openswarm
python -m unittest tests.test_lane_zombies -v      # 空闲 serve 不留僵尸
python -m unittest tests.test_dispatch_health -v    # 僵尸压力 → 暂停 → 恢复派发
```

- `tests/test_lane_zombies.py::RealLaneServeZombieTests`：起**真的** `lane serve`，
  让执行端在 lane worktree 里反复跑真的 `git`（post-commit hook 留下脱离的后台
  `git`，即 #1817 的形状），再用 `ps` 读 supervisor 进程的僵尸子进程数：
  每轮都被回收，进程表里始终没有僵尸。
- `tests/test_dispatch_health.py::ZombiePressureDispatchTickTests`：真的 health probe
  （`fleet health --apply`）+ 真的 dispatch tick：超阈值 → 主机被暂停 → tick 不派活；
  压力消退 → probe 释放主机 → tick 重新派发。

## 执行端 30 分钟验收（ps 为据）

```sh
cd openswarm
python3 tests/lane_zombie_soak.py --minutes 30 --interval 10 --out docs/evidence/pr-1822-zombie-soak.txt
```

脚本起一个真的空闲 `lane serve`（执行端每 10 秒做一轮 git 轮询，每轮留下一个孤儿），
每 5 秒用 `ps` 读一次 supervisor 的僵尸子进程数，并在每次回收之后立刻再读一次。
日志（`openswarm/docs/evidence/pr-1822-zombie-soak.txt`）给出每次采样的
`rounds`（轮询轮数）、`reaped`（supervisor 回收数）、`serve_zombies`（ps 读到的
supervisor 僵尸子进程数）和 `host_zombies`（整机 Z 状态进程数）。

## 与执行端 release 的对应关系

| release | 含义 |
| --- | --- |
| `a9ac8f33` | 现场三个空闲 serve 跑的版本：空闲轮询不回收被收养的孤儿，持续泄漏僵尸 git |
| `df02c552` | 现场处置时临时钉住的版本（M8 的 serve 跑的就是它，零僵尸）；**不是**本修复 |
| 本 PR 之后 | 含 `lane_reap_zombies` + SIGCHLD 唤醒 + 僵尸计数告警的 openswarm 版本，需要重新钉到 gores1 的 systemd unit（`/root/openswarm-executor-*.service`），现场备份在 `gores1 /root/openswarm-executor-*.service.bak-20261103` |
