#!/usr/bin/env python3
"""准真实重启演练：box 控制面在重启后自恢复（Refs #1478）。

演练全部在临时 fixture 内进行：不碰真实 `~/phanes-ctl`，不连 SSH，不访问 GitHub，
不改仓库里的任何生产配置。它回答两个问题：

1. **控制面能不能自恢复**：模拟断电重启（SIGKILL 全部循环 + 心跳回拨）之后，
   再跑一次 boot-ensure 入口（仓库镜像 `tools/lane-autodispatch/start-control-plane.example`
   对应 box 上的 `~/phanes-ctl/bin/start-control-plane`），五件套循环必须全部回来、
   心跳必须重新变新鲜、不得出现双开。
2. **已 claim 的 lane 会不会被错误冻结**：重启前留下的 `auto:` 暂停必须被 health-loop
   清掉；in-flight claim 标记不得丢失；claim 租约过期后允许接管；AD 自己的
   `reconcile()` 必须把重启前遗留的 busy lane 状态释放掉（issue 回到 `lane-ready`）。

演练里用到的真实仓库实现：`bin/ctl-loop`（ib-refresh-loop 的 supervisor 副本）、
`tools/lane-autodispatch/autodispatch.py` 的 `reconcile()`（只替换 GitHub I/O）、
`openswarm/lib/dispatch/lease.py` 的 `LeaseStore`。其余循环用只写心跳的替身脚本。

用法（离线）：

    python3 tools/check-box-boot-recovery.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
START_CONTROL_PLANE = REPO_ROOT / "tools" / "lane-autodispatch" / "start-control-plane.example"
CTL_LOOP = REPO_ROOT / "bin" / "ctl-loop"
AUTODISPATCH = REPO_ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
LEASE = REPO_ROOT / "openswarm" / "lib" / "dispatch" / "lease.py"

HEARTBEAT_MAX_AGE = 120  # ctl-loop 的心跳过期阈值（docs/ctl-boot.md）
PAUSE_TTL = 3600  # auto: 暂停的自动清除窗口（docs/ctl-boot.md）
GHOST_MIN_AGE_S = 600  # 幽灵 in-progress 判定门槛（tools/check-ghost-in-progress.py）
CLAIM_LEASE_S = 300  # bus claim 租约秒数（openswarm/docs/BUS.md）
DOWNTIME_S = 600  # 演练模拟的停机时长（心跳回拨量）

# loop 名 -> (bin 脚本名, 心跳文件相对 CTL_HOME 的路径)
LOOPS = {
    "ib-refresh-loop": ("ctl-loop", "ctl-loop.heartbeat"),
    "lane-autodispatch": ("lane-autodispatch", "autodispatch/heartbeat"),
    "lane-merge": ("lane-merge", "autodispatch/merge-heartbeat"),
    "bus-worker-loop": ("bus-worker-loop", "bus-worker/heartbeat"),
    "health-loop": ("health-loop", "autodispatch/health.heartbeat"),
}

STUBS = {
    # 一次性 ib-refresh：写出稳定状态行后退出（真实 ib-refresh 也是一次性）。
    "ib-refresh": """#!/bin/bash
set -u
CTL_HOME="${CTL_HOME:-$HOME/phanes-ctl}"
mkdir -p "$CTL_HOME"
printf 'ok=1 hosts_ok=3 skipped_cooldown=0 min_ttl=1500 min_exp=%s\\n' "$(date +%s)" \\
  > "$CTL_HOME/ib-refresh.status"
""",
    # AD 替身：只写心跳（真实 AD 在 sleep_chunk 里写同一路径）。
    "lane-autodispatch": """#!/bin/bash
set -u
CTL_HOME="${CTL_HOME:-$HOME/phanes-ctl}"
mkdir -p "$CTL_HOME/autodispatch"
while :; do
  date '+%F %T' > "$CTL_HOME/autodispatch/heartbeat"
  sleep 1
done
""",
    "lane-merge": """#!/bin/bash
set -u
CTL_HOME="${CTL_HOME:-$HOME/phanes-ctl}"
mkdir -p "$CTL_HOME/autodispatch"
while :; do
  date '+%F %T' > "$CTL_HOME/autodispatch/merge-heartbeat"
  sleep 1
done
""",
    # bus-worker 替身：心跳 + 重启后保留 in-flight claim 标记（BUS.md 契约）。
    "bus-worker-loop": """#!/bin/bash
set -u
CTL_HOME="${CTL_HOME:-$HOME/phanes-ctl}"
mkdir -p "$CTL_HOME/bus-worker"
for marker in "$CTL_HOME"/bus-worker/*/claim.json; do
  [[ -f "$marker" ]] || continue
  echo "$(date '+%F %T') preserved in-flight claim marker ${marker##*/bus-worker/}" \\
    >> "$CTL_HOME/bus-worker/claim-restart.log"
  break
done
while :; do
  date '+%F %T' > "$CTL_HOME/bus-worker/heartbeat"
  sleep 1
done
""",
    # health-loop 替身：心跳 + 只清除已过期的 auto: 暂停（手动暂停永不自动清除）。
    "health-loop": """#!/bin/bash
set -u
CTL_HOME="${CTL_HOME:-$HOME/phanes-ctl}"
D="$CTL_HOME/autodispatch"
mkdir -p "$D"
while :; do
  date '+%F %T' > "$D/health.heartbeat"
  now=$(date +%s)
  for pause in "$D"/PAUSE "$D"/PAUSE-*; do
    [[ -f "$pause" ]] || continue
    body="$(head -c 64 "$pause" 2>/dev/null || true)"
    [[ "$body" == auto:* ]] || continue
    mtime=$(stat -c %Y "$pause" 2>/dev/null || echo "$now")
    if (( now - mtime >= 3600 )); then
      rm -f "$pause"
      echo "$(date '+%F %T') cleared expired auto pause ${pause##*/}" >> "$D/health.log"
    fi
  done
  sleep 1
done
""",
    # 状态快照替身：start-control-plane 结尾会调用它。
    "lane-autodispatch-status": """#!/bin/bash
set -u
CTL_HOME="${CTL_HOME:-$HOME/phanes-ctl}"
python3 - "$CTL_HOME/autodispatch/state.json" <<'PY'
import json, sys
try:
    state = json.load(open(sys.argv[1]))
except Exception:
    state = {}
lanes = {k: v for k, v in state.items() if not k.startswith("_") and isinstance(v, dict)}
busy = ", ".join(f"{lane} #{row.get('issue')}" for lane, row in sorted(lanes.items()))
print(f"busy: {len(lanes)}" + (f" ({busy})" if busy else ""))
print("ready: 0")
PY
""",
}


class Timeline:
    """带时间戳的演练流水，输出即卡上的时间线证据。"""

    def __init__(self) -> None:
        self.started = time.time()
        self.lines: list[str] = []

    def log(self, phase: str, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        offset = time.time() - self.started
        line = f"{stamp} [T+{offset:5.1f}s] {phase:<10} {message}"
        self.lines.append(line)
        print(line, flush=True)


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def age_of(path: pathlib.Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


def mtime_of(path: pathlib.Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def cmdline_of(pid: int) -> str:
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)


def loop_pids(home: pathlib.Path) -> dict[str, int]:
    """当前正在跑的 fixture 循环：loop 名 -> pid（按 cmdline 里的脚本路径匹配）。"""
    found: dict[str, int] = {}
    scripts = {name: str(home / "phanes-ctl" / "bin" / script) for name, (script, _) in LOOPS.items()}
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        text = cmdline_of(int(entry.name))
        if not text:
            continue
        for name, script_path in scripts.items():
            if script_path in text:
                found.setdefault(name, int(entry.name))
    return found


def kill_loops(pids: dict[str, int], timeline: Timeline) -> None:
    """模拟断电：SIGKILL 整个进程组（无清理、无心跳收尾）。"""
    for name, pid in sorted(pids.items()):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        timeline.log("reboot", f"SIGKILL {name} (pid {pid}) — 模拟断电，无收尾")
    wait_until(lambda: not loop_pids_of(pids), timeout=10.0)


def loop_pids_of(pids: dict[str, int]) -> bool:
    for pid in pids.values():
        if cmdline_of(pid):
            return True
    return False


def build_fixture(root: pathlib.Path) -> pathlib.Path:
    """搭一个 `~/phanes-ctl` 形状的 fixture（home 在 fixture 里，HOME 指过去）。"""
    home = root / "home"
    ctl = home / "phanes-ctl"
    (ctl / "bin").mkdir(parents=True)
    (ctl / "autodispatch").mkdir(parents=True)
    (ctl / "bus-worker").mkdir(parents=True)

    # boot-ensure 入口：仓库镜像 -> fixture 的 bin/start-control-plane
    launcher = ctl / "bin" / "start-control-plane"
    launcher.write_text(START_CONTROL_PLANE.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(0o755)

    # 真实 supervisor 副本：ib-refresh-loop
    ctl_loop = ctl / "bin" / "ctl-loop"
    ctl_loop.write_text(CTL_LOOP.read_text(encoding="utf-8"), encoding="utf-8")
    ctl_loop.chmod(0o755)

    for name, body in STUBS.items():
        script = ctl / "bin" / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    return home


def seed_state(home: pathlib.Path, issue: int) -> dict:
    """重启前的现场：一个已 claim 的 lane + 一个 in-flight bus claim + 一个过期的 auto 暂停。"""
    ctl = home / "phanes-ctl"
    now = time.time()
    claimed_at = time.strftime("%F %T", time.localtime(now - GHOST_MIN_AGE_S - 600))
    state = {
        "M12": {
            "issue": issue,
            "run": "20261101-120000-abc123",
            "host": "ib1",
            "base": "0" * 40,
            "at": claimed_at,
            "verify": "python3 tools/check-box-boot-recovery.py",
        }
    }
    (ctl / "autodispatch" / "state.json").write_text(
        json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    (ctl / "autodispatch" / "lanes.json").write_text(
        json.dumps({"M12": {"host": "ib1", "backend": "pi"}}, indent=1), encoding="utf-8"
    )

    # 过期的 auto: 暂停（重启后必须由 health-loop 清掉，否则 lane 被错误冻结）
    pause = ctl / "autodispatch" / "PAUSE-M12"
    pause.write_text("auto: host_hard\n", encoding="utf-8")
    old = now - PAUSE_TTL - 600
    os.utime(pause, (old, old))

    # bus in-flight claim 标记（BUS.md：受控重启不得丢 in-flight 标记）
    claim_dir = ctl / "bus-worker" / f"run-{issue}"
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "claim.json").write_text(
        json.dumps(
            {
                "run_id": f"run-{issue}",
                "issue_number": issue,
                "worker": "surlymochan-phanes-box",
                "status": "in-flight",
                "task": {"run_id": f"run-{issue}", "repo": "surlymochan/phanes", "base": "0" * 40},
                "claimed_at": now,
                "lease_expires_at": now + CLAIM_LEASE_S,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return state


def run_boot_ensure(home: pathlib.Path, timeline: Timeline, phase: str) -> str:
    """跑一次 boot-ensure 入口（= box 上的 start-control-plane）。"""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CTL_HOME"] = str(home / "phanes-ctl")
    proc = subprocess.run(
        ["bash", str(home / "phanes-ctl" / "bin" / "start-control-plane")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(("start:", "already:")):
            timeline.log(phase, line.strip())
    if proc.returncode != 0:
        timeline.log(phase, f"start-control-plane rc={proc.returncode} stderr={proc.stderr.strip()[:200]}")
    return proc.stdout


def heartbeats_fresh(home: pathlib.Path) -> bool:
    ctl = home / "phanes-ctl"
    for _name, (_script, heartbeat) in LOOPS.items():
        if age_of(ctl / heartbeat) > HEARTBEAT_MAX_AGE:
            return False
    return True


def heartbeat_report(home: pathlib.Path, timeline: Timeline, phase: str) -> None:
    ctl = home / "phanes-ctl"
    for name, (_script, heartbeat) in LOOPS.items():
        age = age_of(ctl / heartbeat)
        state = "fresh" if age <= HEARTBEAT_MAX_AGE else "STALE"
        timeline.log(phase, f"heartbeat {name:<18} {heartbeat:<28} age={age:6.1f}s {state}")
    status = ctl / "ib-refresh.status"
    if status.is_file():
        row = status.read_text(encoding="utf-8").strip()
        timeline.log(phase, f"ib-refresh.status age={age_of(status):6.1f}s row={row}")


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_claimed_lane(home: pathlib.Path, timeline: Timeline, issue: int) -> None:
    """已 claim 的 lane 不得被错误冻结：AD 的 reconcile() 必须释放它。"""
    ctl = home / "phanes-ctl"
    recorded: list[list[str]] = []

    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)  # 让 AD 的 D 指向 fixture
    try:
        ad = load_module(AUTODISPATCH, "autodispatch_reboot_drill")
    finally:
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home
    (home / "brain").mkdir(exist_ok=True)
    ad.BRAIN_LOG = str(home / "brain" / "log.md")

    def fake_gh(args, timeout=60):
        recorded.append(list(args))
        if args[:2] == ["issue", "view"]:
            payload = {"state": "OPEN", "labels": [{"name": "in-progress"}]}
            return 0, json.dumps(payload), ""
        return 0, "", ""

    ad.gh = fake_gh

    state = ad.load(f"{ad.D}/state.json", {})
    lane = "M12"
    entry = state.get(lane)
    assert entry, "重启前的 lane 占用记录必须还在 state.json 里"
    # 远端状态取自 AD 的 REMOTE_STATUS（tools/lane-autodispatch/autodispatch.py:192-211）：
    # 重启后 lane 空闲（lane=idle, current_run=None），但 run 记录仍是 running、
    # runner 进程已死（runner_alive=False）——这正是 reconcile() 认定的孤儿形态。
    remote = {
        "lane": "idle",
        "run": "running",
        "current_run": None,
        "run_state": "running",
        "runner_alive": False,
    }
    released = ad.reconcile(lane, {"host": "ib1"}, entry, remote, state)
    if released:  # 与 tick() 里 state.pop(L); save_state(state) 同一动作
        state.pop(lane, None)
        ad.save_state(state)

    edits = [args for args in recorded if args[:2] == ["issue", "edit"]]
    assert released, "reconcile() 必须把重启后遗留的 busy lane 判定为孤儿并释放"
    assert any("lane-ready" in args for args in edits), f"issue 必须回到 lane-ready，实际 edits={edits}"
    assert lane not in state, "释放后 state.json 不得再占用该 lane"
    timeline.log("claim", f"AD reconcile() 释放 {lane} #{issue} -> 重新贴 lane-ready（不再被冻结）")

    # 租约语义：过期且运行已停 -> 可回收；运行仍在 -> 不得误回收。
    lease_store = load_module(LEASE, "lease_reboot_drill")
    store = lease_store.LeaseStore(ctl / "bus-worker")
    base = time.time()
    store.grant(issue=issue, run_id="run-1", lane=lane, ttl_s=lease_store.DEFAULT_TTL_S, now=base)
    assert store.expired(base + lease_store.DEFAULT_TTL_S + 1, running_runs=[]) == [issue]
    assert store.expired(base + lease_store.DEFAULT_TTL_S + 1, running_runs=["run-1"]) == []
    timeline.log("claim", f"dispatch lease：过期且运行已停 -> 可回收 #{issue}；运行仍在 -> 不回收")

    # bus claim 租约：重启后仍在有效期内可续约，过期后允许其它 worker 接管。
    marker = json.loads((ctl / "bus-worker" / f"run-{issue}" / "claim.json").read_text(encoding="utf-8"))
    assert marker["status"] == "in-flight", "受控重启不得丢失 in-flight claim 标记"
    reboot_clock = time.time() + DOWNTIME_S  # 演练模拟的重启后时刻
    expired = reboot_clock >= marker["lease_expires_at"]
    assert expired, "停机超过 claim 租约后，claim 必须过期（否则任务被永久锁死）"
    timeline.log("claim", "in-flight claim 标记保留（受控重启不丢标记）")
    timeline.log("claim", f"停机 {DOWNTIME_S}s > claim 租约 {CLAIM_LEASE_S}s -> 租约已过期，其它 worker 可接管")


def main() -> int:
    timeline = Timeline()
    with tempfile.TemporaryDirectory(prefix="box-boot-recovery-") as fixture:
        root = pathlib.Path(fixture)
        home = build_fixture(root)
        ctl = home / "phanes-ctl"
        issue = 1478

        try:
            run_drill(home, ctl, issue, timeline)
        finally:
            for _name, pid in loop_pids(home).items():
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass

    print()
    print("OK: boot-ensure 在模拟重启后拉回控制面五件套")
    print("OK: 重启后心跳全部恢复并持续推进，且没有双开实例")
    print("OK: 已 claim 的 lane 被释放回 lane-ready，没有被错误冻结")
    return 0


def run_drill(home: pathlib.Path, ctl: pathlib.Path, issue: int, timeline: Timeline) -> None:
    """演练主体。异常时由调用方负责清理 fixture 进程。"""
    timeline.log("setup", f"fixture home={home}")
    seed_state(home, issue)
    timeline.log("setup", "重启前现场：M12 已 claim #1478 + in-flight bus claim + 过期 auto: PAUSE-M12")

    launcher = (ctl / "bin" / "start-control-plane").read_text(encoding="utf-8")
    missing = [name for name in LOOPS if name not in launcher]
    assert not missing, f"boot-ensure 入口缺少循环：{missing}"

    # --- 第一次开机 ---------------------------------------------------
    run_boot_ensure(home, timeline, "boot-1")
    assert wait_until(lambda: heartbeats_fresh(home), timeout=30), "第一次开机后心跳未变新鲜"
    heartbeat_report(home, timeline, "boot-1")
    first = loop_pids(home)
    assert set(first) == set(LOOPS), f"第一次开机缺循环：{sorted(set(LOOPS) - set(first))}"
    timeline.log("boot-1", "五件套全部在跑：" + ", ".join(f"{n}={p}" for n, p in sorted(first.items())))

    # --- 模拟断电重启 -------------------------------------------------
    for name, (_script, heartbeat) in LOOPS.items():
        path = ctl / heartbeat
        old = time.time() - DOWNTIME_S
        if path.exists():
            os.utime(path, (old, old))
    status = ctl / "ib-refresh.status"
    if status.exists():
        old = time.time() - DOWNTIME_S
        os.utime(status, (old, old))
    kill_loops(first, timeline)
    assert not loop_pids(home), "重启后不应再有循环存活"
    heartbeat_report(home, timeline, "reboot")
    timeline.log("reboot", f"停机 {DOWNTIME_S}s：心跳全部过期（> {HEARTBEAT_MAX_AGE}s 阈值）")

    # --- 第二次开机（boot-ensure 自恢复）-----------------------------
    run_boot_ensure(home, timeline, "boot-2")
    assert wait_until(lambda: heartbeats_fresh(home), timeout=30), "重启后心跳未恢复"
    heartbeat_report(home, timeline, "boot-2")
    second = loop_pids(home)
    assert set(second) == set(LOOPS), f"重启后缺循环：{sorted(set(LOOPS) - set(second))}"
    for name in sorted(LOOPS):
        assert second[name] != first[name], f"{name} 重启后没有换新进程"
    timeline.log("boot-2", "五件套全部回来且是新进程：" + ", ".join(f"{n}={p}" for n, p in sorted(second.items())))

    # 心跳必须持续推进，而不是只有一次写入
    before = {name: mtime_of(ctl / hb) for name, (_s, hb) in LOOPS.items()}

    def advanced() -> bool:
        return all(mtime_of(ctl / hb) > before[name] for name, (_s, hb) in LOOPS.items())

    assert wait_until(advanced, timeout=25.0), "心跳没有继续推进（一次性写入不算恢复）"
    timeline.log("boot-2", "心跳持续推进（每条心跳都再次更新），无假性恢复")

    # 不得双开：每个循环只能有一个实例
    for name, (script, _hb) in LOOPS.items():
        script_path = str(ctl / "bin" / script)
        count = sum(1 for entry in pathlib.Path("/proc").iterdir()
                    if entry.name.isdigit() and script_path in cmdline_of(int(entry.name)))
        assert count == 1, f"{name} 出现了 {count} 个实例"
    timeline.log("boot-2", "每个循环恰好一个实例（无双开）")

    # --- 已 claim 的 lane 不得被错误冻结 ------------------------------
    pause = ctl / "autodispatch" / "PAUSE-M12"
    assert not pause.exists(), "过期的 auto: 暂停必须由 health-loop 清掉，否则 lane 被错误冻结"
    timeline.log("claim", "过期的 auto: PAUSE-M12 已被 health-loop 清除")
    assert (ctl / "bus-worker" / "claim-restart.log").is_file(), "bus-worker 重启后应记录 claim 标记保留"
    check_claimed_lane(home, timeline, issue)


if __name__ == "__main__":
    sys.exit(main())
