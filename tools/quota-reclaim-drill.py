#!/usr/bin/env python3
"""故障名额回收演练（Refs #1477，Refs #1468）。

在隔离沙箱内跑真实的 openswarm lane / 容量协调器路径，实测「lane/host 故障后名额回收」：

1. 起两条常驻 lane（L1/L2，pi 后端 + 仓内假执行器），容量上限设为 1 个并发名额；
2. 两条 lane 各派一个长任务，唯一名额归谁由协调器决定（真的 running 的那条是 holder，
   另一条排队等名额）；
3. 注入故障：
   - 场景 A `executor`：kill -9 holder 的执行器进程（模拟 lane 内执行器崩溃）；
   - 场景 B `supervisor`：kill -9 holder 所在 lane 的 supervisor（模拟宿主/进程组故障）；
   - 场景 C `stop`：用既有 ``openswarm stop`` 优雅停止 holder（对照组，不算故障注入）；
4. 观察 openswarm 回收名额：协调器 reservations 清空、run status 出现 fleet_recovered_at、
   排队的任务被放行并跑完（可再派）。

只使用既有语义：``lane up/down/assign``、``openswarm stop`` 与 TTL 回收
（``fleet.recovery_ttl_seconds``）。不连生产主机、不碰凭据、不改 AD 代码。

用法::

    python3 tools/quota-reclaim-drill.py                 # 三个场景都跑
    python3 tools/quota-reclaim-drill.py --scenario executor
    python3 tools/quota-reclaim-drill.py --keep          # 保留沙箱以便复查
    python3 tools/quota-reclaim-drill.py --evidence-out docs/quota-reclaim-drill-evidence.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "openswarm" / "bin" / "openswarm"
CAPACITY = ROOT / "openswarm" / "bin" / "fleet-capacity"
FAKE_PI = ROOT / "openswarm" / "tests" / "fixtures" / "fake_pi_rpc.py"

# 演练只用本机协调器，不冒充 gocc/gores1 等生产宿主。
HOST = "local"
LANES = ("L1", "L2")
TASK = "LANE_TEST_WAIT hold the only fleet slot for the drill"

# 沙箱标记：只有带这个标记（或空目录）的路径才允许被演练删除。
SANDBOX_MARKER = ".quota-reclaim-drill-sandbox"
# 默认沙箱放在仓库内（.gitignore 已收），路径要短，否则 AF_UNIX socket 会超限。
DEFAULT_WORKDIR = ROOT / ".openswarm-tmp" / "q"
# AF_UNIX sun_path 上限，与 openswarm/bin/openswarm 的 lane_validate_socket_path 一致：
# macOS 104 字节、Linux 108 字节，都要给结尾的 NUL 留一字节。
SOCKET_PATH_LIMIT = 103 if sys.platform == "darwin" else 107


def stamp(when=None):
    return time.strftime("%F %T", time.localtime(when if when is not None else time.time()))


class Drill:
    def __init__(self, workdir, keep=False, evidence_out=None):
        self.workdir = pathlib.Path(workdir).absolute()
        self.keep = keep
        self.evidence_out = pathlib.Path(evidence_out).absolute() if evidence_out else None
        self.home = self.workdir / "home"
        self.capacity_dir = self.workdir / "capacity"
        self.guard = self.workdir / "guard.json"
        # 宿主环境不封闭就会漏进来：OPENSWARM_LANES_FILE 会指向生产的 lanes.json，
        # OPENSWARM_PI_RPC_BIN 会顶掉 lane 的假执行器。演练只留自己设置的那几个。
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("OPENSWARM_", "XSWARM_"))}
        self.evidence = {"scenarios": []}

    # ---------------------------------------------------------------- setup
    def prepare(self):
        self.reset_sandbox()
        for path in (self.home, self.capacity_dir, self.workdir / "bin"):
            path.mkdir(parents=True, exist_ok=True)
        # 标记文件：下次重跑时这就是「这个目录确实是演练沙箱」的凭据。
        (self.workdir / SANDBOX_MARKER).write_text(
            "openswarm quota reclaim drill sandbox; safe to delete\n", encoding="utf-8")
        # 假执行器：仓内 fixture 的副本，避免改动仓内文件的可执行位。
        fake = self.workdir / "bin" / "fake_pi_rpc.py"
        shutil.copyfile(FAKE_PI, fake)
        fake.chmod(0o755)
        # 每条 lane 一个独立 repo：两条 lane 不共享 .git，故障注入不会互相干扰。
        repos = {}
        for name in LANES:
            repo = self.workdir / f"repo-{name}"
            repo.mkdir()
            repos[name] = repo
            self.git(repo, "init", "-q")
            self.git(repo, "config", "user.name", "Quota Reclaim Drill")
            self.git(repo, "config", "user.email", "drill@example.invalid")
            (repo / "README.md").write_text("drill base\n")
            self.git(repo, "add", "README.md")
            self.git(repo, "commit", "-q", "-m", "drill base")
        # 容量拓扑：本机只有 1 个并发名额，任何一条占用的 lane 都会挡住另一条。
        (self.capacity_dir / "topology.json").write_text(json.dumps({
            "hosts": [HOST],
            "limits": {"total": 1, f"host:{HOST}": 1, "pi": 1, "minimax-cn": 1},
        }))
        self.guard.write_text(json.dumps({"command": [sys.executable, str(CAPACITY), HOST]}))
        (self.home / "lanes.json").write_text(json.dumps({"lanes": [
            {"name": name, "backend": "pi", "model": "fake-model", "provider": "minimax-cn",
             "repo": str(repos[name]), "binary": str(fake)} for name in LANES]}))
        (self.home / "policy.local.json").write_text(json.dumps({
            "pi": {"require_isolation": False},
            "concurrency": {"total": 1, "pi": 1, "minimax-cn": 1},
            "fleet": {"recovery_ttl_seconds": 2, "recovery_poll_seconds": 0.5},
        }))
        self.env.update({
            "OPENSWARM_HOME": str(self.home),
            "OPENSWARM_FLEET_CAPACITY_DIR": str(self.capacity_dir),
            "OPENSWARM_FLEET_GUARD_CONFIG": str(self.guard),
        })
        # lane 控制 socket 走 AF_UNIX，路径长度有硬上限；演练目录必须足够短。
        for name in LANES:
            socket = self.home / "lanes" / name / "ctl.sock"
            size = len(os.fsencode(socket))
            if size > SOCKET_PATH_LIMIT:
                raise RuntimeError(f"drill sandbox path is too long for the lane control socket "
                                   f"({size} > {SOCKET_PATH_LIMIT} bytes): {socket}")

    def reset_sandbox(self):
        """Delete a previous sandbox, but only one that is provably this drill's."""
        path = self.workdir
        self.assert_safe_workdir(path)
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"drill workdir must be a directory: {path}")
        if not self.is_drill_sandbox(path):
            raise RuntimeError(f"refusing to delete {path}: it carries no {SANDBOX_MARKER} marker "
                               f"and is not empty, so it is not a drill sandbox")
        shutil.rmtree(path)

    @staticmethod
    def is_drill_sandbox(path):
        """A sandbox is ours when it carries the marker file or is still empty."""
        if (path / SANDBOX_MARKER).exists():
            return True
        return not any(path.iterdir())

    @staticmethod
    def assert_safe_workdir(path):
        """Refuse targets that are obviously not a scratch sandbox."""
        forbidden = {pathlib.Path("/"), pathlib.Path.home().absolute(), ROOT,
                     ROOT.parent, pathlib.Path.cwd().absolute()}
        forbidden.update(ROOT.parents)
        if path in forbidden:
            raise RuntimeError(f"refusing to use {path} as the drill workdir: too dangerous to touch")

    def git(self, repo, *args):
        return subprocess.run(["git", *args], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    # ------------------------------------------------------------- plumbing
    def cli(self, *args, timeout=60, check=True):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env,
                                text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"openswarm {' '.join(map(str, args))} failed: "
                               f"{result.stdout.strip()} {result.stderr.strip()}")
        return result

    def capacity(self, **payload):
        result = subprocess.run([sys.executable, str(CAPACITY), HOST], input=json.dumps(payload),
                                env=self.env, text=True, capture_output=True, timeout=30)
        return json.loads(result.stdout)

    def reservations(self):
        return self.capacity(op="status")["reservations"]

    def holds_slot(self, run_id):
        """Whether the coordinator still holds this run's concurrency slot."""
        return any(row.get("run_id") == run_id for row in self.reservations())

    def probe_capacity(self):
        """A coordinator-side probe: one extra admission while the only slot is held."""
        token = "0" * 32
        answer = self.capacity(op="acquire", token=token, run_id="drill-probe",
                                backend="pi", provider="minimax-cn")
        if answer.get("admitted"):
            self.capacity(op="release", token=token, run_id="drill-probe")
        return answer

    def lane_status(self):
        return json.loads(self.cli("lane", "status").stdout)

    def lane_state(self, name):
        try:
            return json.loads((self.home / "lanes" / name / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def run_status(self, run_id):
        try:
            return json.loads((self.home / "runs" / run_id / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout=45, description="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for {description}")

    def start_lanes(self):
        self.cli("lane", "up", *LANES, timeout=120)
        self.wait_for(lambda: all(self.lane_state(n).get("supervisor_pid") for n in LANES),
                      description="lane supervisors to become ready")

    def assign(self, lane, run_id):
        result = self.cli("lane", "assign", lane, "--prompt", TASK, "--run-id", run_id,
                          "--budget-minutes", "3", "--title", f"drill {run_id}", timeout=120)
        return json.loads(result.stdout)

    def resolve_roles(self, first, second):
        """Decide who holds the only slot; the coordinator decides, not the drill.

        L1 is dispatched first, but L2's supervisor may reach ``reserve_provider_slot``
        first, so the holder is whichever dispatch is really running with the
        reservation while the other one is queued.  Returns ``(holder, waiter)``
        once that is unambiguous, otherwise ``None`` so the caller keeps waiting.
        """
        holder = None
        for run_id in (first, second):
            status = self.run_status(run_id)
            if status.get("state") != "running" or not status.get("fleet_reserved_at"):
                continue
            if holder is not None:
                return None  # 上限是 1：两个都 running 说明状态还没稳，继续等
            holder = run_id
        if holder is None:
            return None
        other = second if holder == first else first
        if self.run_status(other).get("state") != "queued":
            return None
        if not self.holds_slot(holder):
            return None  # 本地写了 reserved，协调器还没真正给名额
        return holder, other

    # ------------------------------------------------------------ scenarios
    def run_scenario(self, scenario):
        record = {"scenario": scenario, "started_at": stamp(), "events": []}
        self.evidence["scenarios"].append(record)
        first, second = f"drill-{scenario}-holder", f"drill-{scenario}-waiter"
        self.start_lanes()
        self.assign("L1", first)
        self.assign("L2", second)
        # 唯一名额归谁由协调器决定：谁真的 running 谁是 holder，另一条排队等名额。
        holder, waiter = self.wait_for(lambda: self.resolve_roles(first, second),
                                       description="one dispatch to hold the only slot and the other to queue")
        holder_lane = self.run_status(holder).get("lane")
        record["holder"] = holder
        record["waiter"] = waiter
        record["holder_lane"] = holder_lane
        record["waiter_lane"] = self.run_status(waiter).get("lane")
        record["before"] = self.snapshot(holder, waiter)
        record["before"]["capacity_probe"] = self.probe_capacity()

        if scenario == "executor":
            pid = self.run_status(holder).get("executor_pid")
            record["events"].append({"at": stamp(), "action": "kill -9 executor",
                                      "pid": pid, "run": holder, "lane": holder_lane})
            os.kill(int(pid), signal.SIGKILL)
        elif scenario == "stop":
            record["events"].append({"at": stamp(), "action": "openswarm stop (既有语义：优雅停止)",
                                      "run": holder, "lane": holder_lane})
            self.cli("stop", holder, timeout=60)
        else:
            pid = self.lane_state(holder_lane).get("supervisor_pid")
            record["events"].append({"at": stamp(), "action": "kill -9 lane supervisor",
                                      "pid": pid, "run": holder, "lane": holder_lane})
            os.kill(int(pid), signal.SIGKILL)
            # 宿主故障后没有存活的 supervisor 回收名额；用既有 `lane up` 语义恢复该 lane。
            started = time.time()
            self.cli("lane", "up", holder_lane, timeout=120)
            record["events"].append({"at": stamp(), "action": f"lane up {holder_lane} (既有语义：重启 supervisor)",
                                      "seconds": round(time.time() - started, 3)})

        killed_at = time.time()
        reclaimed_at = []

        def reclaimed():
            if not self.holds_slot(holder):
                reclaimed_at.append(time.time())
                return True
            return False

        self.wait_for(reclaimed, timeout=90, description="the fleet slot to be reclaimed")
        record["events"].append({"at": stamp(reclaimed_at[0]), "action": "slot reclaimed",
                                  "seconds_after_fault": round(reclaimed_at[0] - killed_at, 3)})
        # 名额回收后，排队任务必须被放行并跑完（可再派）。
        self.wait_for(lambda: self.run_status(waiter).get("state") == "finished", timeout=150,
                      description="the queued dispatch to run after the reclaim")
        record["after"] = self.snapshot(holder, waiter)
        record["reclaim_seconds"] = round(reclaimed_at[0] - killed_at, 3)
        record["redispatch_seconds"] = round(time.time() - killed_at, 3)
        holder_after = record["after"]["runs"][holder]
        waiter_after = record["after"]["runs"][waiter]
        record["release_path"] = ("ttl-recovery" if holder_after.get("fleet_recovered_at")
                                  else "graceful-stop")
        record["finished_at"] = stamp()
        record["ok"] = (not self.holds_slot(holder)
                        and waiter_after.get("state") == "finished"
                        and waiter_after.get("status") == "done"
                        and not holder_after.get("fleet_reservation_error")
                        and bool(holder_after.get("fleet_recovered_at") or holder_after.get("finished")))
        return record

    def slot_view(self, run_id):
        """The slot-relevant fields of a run status, for the evidence table."""
        status = self.run_status(run_id)
        keys = ("lane", "state", "status", "queue_reason", "fleet_host", "fleet_reserved_at",
                "fleet_recovered_at", "fleet_recovery_outcome", "recovered_reason",
                "fleet_reservation_error")
        return {key: status.get(key) for key in keys}

    def snapshot(self, holder, waiter):
        rows = {row["name"]: row for row in self.lane_status()}
        reservations = self.reservations()
        return {
            "at": stamp(),
            "lanes_status": [rows.get(name) for name in LANES],
            "reservations": reservations,
            "slot_holder": next((row["run_id"] for row in reservations), None),
            "runs": {run_id: self.run_status(run_id) for run_id in (holder, waiter)},
            "slot_view": {run_id: self.slot_view(run_id) for run_id in (holder, waiter)},
        }

    # --------------------------------------------------------------- driver
    def run(self, scenarios):
        self.prepare()
        try:
            for scenario in scenarios:
                try:
                    self.run_scenario(scenario)
                except Exception as error:  # noqa: BLE001 - 演练要留下失败证据
                    record = self.evidence["scenarios"][-1]
                    record["ok"] = False
                    record["error"] = f"{type(error).__name__}: {error}"
                    record["finished_at"] = stamp()
                finally:
                    self.cli("lane", "down", *LANES, check=False, timeout=90)
        finally:
            self.evidence["finished_at"] = stamp()
            payload = json.dumps(self.evidence, ensure_ascii=False, indent=2)
            (self.workdir / "evidence.json").write_text(payload)
            # 证据要能留在仓内复核：--evidence-out 指定的路径在沙箱之外，不会被删掉。
            if self.evidence_out:
                if self.workdir in self.evidence_out.parents:
                    raise RuntimeError(f"evidence output must live outside the sandbox: {self.evidence_out}")
                self.evidence_out.parent.mkdir(parents=True, exist_ok=True)
                self.evidence_out.write_text(payload)
            if not self.keep:
                shutil.rmtree(self.workdir, ignore_errors=True)
        return self.evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=["executor", "supervisor", "stop", "all"], default="all")
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR),
                        help="drill sandbox (only a marked/empty directory is ever deleted)")
    parser.add_argument("--evidence-out", default=None,
                        help="copy the evidence JSON to this path (outside the sandbox)")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox for inspection")
    args = parser.parse_args()
    scenarios = ["executor", "supervisor", "stop"] if args.scenario == "all" else [args.scenario]
    drill = Drill(args.workdir, keep=args.keep, evidence_out=args.evidence_out)
    evidence = drill.run(scenarios)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if all(record.get("ok") for record in evidence["scenarios"]) else 1


if __name__ == "__main__":
    sys.exit(main())
