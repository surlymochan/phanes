"""`openswarm fleet health`: probe the fleet hosts and pause the unhealthy ones."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

DISK = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/root 1000 700 300 70% /\n")
MEMORY = ("              total used free shared buff/cache available\n"
          "Mem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n")
# The dispatcher snapshot of the 2026-11-02 22:13 stall (Refs #1697).
STALLED_AT = "2026-11-02 22:13:20"
STALLED_NOW = "2026-11-02 22:18:20"


def inode_output(percent):
    return ("Filesystem Inodes IUsed IFree IUse% Mounted on\n"
            f"/dev/root 1000 950 50 {percent}% /\n")


class FleetHealthCliTests(unittest.TestCase):
    """The health check runs against a fake runner and a temporary openswarm home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-health-")
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.root / "home"
        self.home.mkdir()
        self.now = 1700000000.0
        self.store = xs.dispatch_pause_module().PauseStore(self.home)
        # the dispatcher's pid file lives in the control-plane state dir, never in the real one
        self.ctl_state = self.root / "ctl-state"
        self.ctl_state.mkdir()
        patcher = mock.patch.dict(os.environ, {"OPENSWARM_CTL_STATE_DIR": str(self.ctl_state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---------------------------------------------------------------- fixtures

    def runner(self, inode=10, failing=()):
        """A fake probe runner: fixed outputs per command, transport failures per host."""
        outputs = {"df -P /": DISK, "df -Pi /": inode_output(inode), "free -m": MEMORY}

        def run(host, command):
            if host in failing:
                raise OSError("ssh: connect to host %s port 22: Connection refused" % host)
            return outputs[command]
        return run

    def lanes(self, hosts=("worker-a",)):
        """Write the lanes.json the default host list is read from."""
        path = self.home / "lanes.json"
        lanes = [{"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                  "repo": str(self.root), "host": host} for host in hosts]
        path.write_text(json.dumps({"lanes": lanes}), encoding="utf-8")
        previous = os.environ.get("OPENSWARM_LANES_FILE")
        os.environ["OPENSWARM_LANES_FILE"] = str(path)
        self.addCleanup(self.restore_lanes, previous)
        return path

    def restore_lanes(self, previous):
        if previous is None:
            os.environ.pop("OPENSWARM_LANES_FILE", None)
        else:
            os.environ["OPENSWARM_LANES_FILE"] = previous

    def health(self, runner=None, hosts=("worker-a",), apply=False, json_out=True,
               policy=None, lanes=("worker-a",)):
        """Run `fleet health` against the fake runner and return its report."""
        if lanes:
            self.lanes(lanes)
        args = argparse.Namespace(hosts=",".join(hosts) if hosts else None,
                                  json=json_out, apply=apply)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = xs.cmd_fleet_health(args, policy=policy,
                                         runner=runner or self.runner(),
                                         hosts={name: {"name": name, "kind": "local"}
                                                for name in (lanes or ())},
                                         now=self.now, home=self.home)
        self.assertEqual(json.loads(output.getvalue()) if json_out else {}, report if json_out else {})
        return report

    def pauses(self):
        return {row["scope"]: row for row in self.store.active(self.now)}

    # ---------------------------------------------------------------- the dispatcher snapshot

    def snapshot_dir(self):
        """The directory the dispatcher publishes its snapshot in."""
        return self.home / "phanes-ctl" / "autodispatch"

    def snapshot(self, stamp, **extra):
        """Write the lanes-status snapshot the dispatcher publishes."""
        directory = self.snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True)
        body = {"_at": stamp, "_busy": 1, "_ready": 4, "_blocked": 0}
        body.update(extra)
        (directory / "lanes-status.json").write_text(json.dumps(body), encoding="utf-8")
        return directory / "lanes-status.json"

    def heartbeat(self, stamp):
        """Write the heartbeat the dispatcher refreshes on every tick."""
        directory = self.snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "heartbeat").write_text(stamp + "\n", encoding="utf-8")

    def stamp(self, text):
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))

    # ---------------------------------------------------------------- tests

    def test_inode_pressure_pauses_the_host(self):
        report = self.health(runner=self.runner(inode=96), apply=True)
        row = report["hosts"][0]
        self.assertEqual(row["status"], "hard")
        self.assertEqual(row["decision"], "pause")
        self.assertEqual(row["triggers"], ["inode_pct"])
        self.assertEqual(row["metrics"]["inode_pct"], 96.0)
        self.assertEqual(row["action"], "paused")
        self.assertFalse(report["ok"])
        record = self.pauses()["host:worker-a"]
        self.assertTrue(record["reason"].startswith("auto:health"))
        self.assertIn("inodes", record["reason"])
        self.assertEqual(record["expires_at"], self.now + 1800)

    def test_pressure_below_the_pause_watermark_does_not_pause(self):
        report = self.health(runner=self.runner(inode=93), apply=True)
        row = report["hosts"][0]
        self.assertEqual(row["status"], "soft")
        self.assertEqual(row["decision"], "none")
        self.assertEqual(self.pauses(), {})

    def test_paused_host_keeps_its_pause_until_the_recovery_watermark(self):
        self.health(runner=self.runner(inode=96), apply=True)
        report = self.health(runner=self.runner(inode=93), apply=True)
        self.assertEqual(report["hosts"][0]["decision"], "none")
        self.assertEqual(report["hosts"][0]["status"], "soft")
        self.assertIn("host:worker-a", self.pauses())

    def test_recovered_host_releases_the_health_pause(self):
        self.health(runner=self.runner(inode=96), apply=True)
        report = self.health(runner=self.runner(inode=91), apply=True)
        self.assertEqual(report["hosts"][0]["decision"], "resume")
        self.assertEqual(report["hosts"][0]["status"], "ok")
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertEqual(self.pauses(), {})

    def test_manual_pause_is_never_cleared(self):
        self.store.pause("host:worker-a", reason="operator: maintenance", now=self.now)
        report = self.health(runner=self.runner(inode=91), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "kept")
        record = self.pauses()["host:worker-a"]
        self.assertEqual(record["reason"], "operator: maintenance")

    def test_manual_pause_is_not_rewritten_by_a_health_pause(self):
        self.store.pause("host:worker-a", reason="operator: maintenance", now=self.now)
        report = self.health(runner=self.runner(inode=96), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "paused-manual")
        self.assertEqual(self.pauses()["host:worker-a"]["reason"], "operator: maintenance")

    def test_ssh_failures_pause_only_after_the_streak(self):
        for attempt in (1, 2):
            report = self.health(runner=self.runner(failing=("worker-a",)), apply=True)
            row = report["hosts"][0]
            self.assertEqual(row["status"], "unreachable")
            self.assertEqual(row["metrics"]["ssh_fail_streak"], attempt)
            self.assertEqual(row["decision"], "none")
            self.assertEqual(self.pauses(), {})
        report = self.health(runner=self.runner(failing=("worker-a",)), apply=True)
        row = report["hosts"][0]
        self.assertEqual(row["metrics"]["ssh_fail_streak"], 3)
        self.assertEqual(row["decision"], "pause")
        self.assertEqual(row["action"], "paused")
        self.assertIn("SSH failure streak", self.pauses()["host:worker-a"]["reason"])

    def test_a_reachable_host_resets_the_failure_streak(self):
        self.health(runner=self.runner(failing=("worker-a",)), apply=True)
        report = self.health(runner=self.runner(), apply=True)
        self.assertEqual(report["hosts"][0]["status"], "ok")
        self.assertEqual(report["hosts"][0]["metrics"]["ssh_fail_streak"], 0)

    def test_json_report_structure(self):
        report = self.health(runner=self.runner(inode=96))
        self.assertEqual(sorted(report), ["alerts", "applied", "hosts", "lanes_snapshot", "ok",
                                          "status", "thresholds", "writer"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["status"], "hard")
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 95)
        row = report["hosts"][0]
        self.assertEqual(sorted(row), ["action", "decision", "error", "host", "metrics", "paused",
                                       "reason", "status", "triggers"])
        self.assertEqual(row["host"], "worker-a")
        self.assertFalse(row["paused"])

    # ---------------------------------------------------------------- the snapshot freshness (Refs #1697)

    def test_the_snapshot_path_is_the_one_the_dispatcher_writes(self):
        default = os.path.join(os.path.expanduser("~"), "phanes-ctl", "autodispatch", "lanes-status.json")
        self.assertEqual(xs.fleet_lanes_snapshot_path(), default)
        self.assertEqual(xs.fleet_lanes_snapshot_path(self.home),
                         str(self.snapshot_dir() / "lanes-status.json"))
        with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE_DIR": str(self.snapshot_dir())}):
            self.assertEqual(xs.fleet_lanes_snapshot_path(), str(self.snapshot_dir() / "lanes-status.json"))
        with mock.patch.dict(os.environ, {"OPENSWARM_LANES_STATUS_FILE": str(self.snapshot_dir() / "other.json")}):
            self.assertEqual(xs.fleet_lanes_snapshot_path(), str(self.snapshot_dir() / "other.json"))

    def test_stalled_2213_snapshot_is_reported_stale(self):
        """The 2026-11-02 22:13 stall replayed: the snapshot must be marked expired."""
        self.snapshot(STALLED_AT)
        self.now = self.stamp(STALLED_NOW)
        report = self.health()
        snapshot = report["lanes_snapshot"]
        self.assertEqual(snapshot["status"], "stale")
        self.assertEqual(snapshot["at"], STALLED_AT)
        self.assertEqual(snapshot["age_seconds"], 300)
        self.assertEqual(snapshot["stale_after_seconds"], 180)
        self.assertEqual(snapshot["ready"], 4)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "stale")
        self.assertEqual([alert["kind"] for alert in report["alerts"]], ["lanes-snapshot-stale"])
        self.assertIn(STALLED_AT, report["alerts"][0]["message"])

    def test_a_fresh_snapshot_keeps_the_health_ok(self):
        self.snapshot(STALLED_AT)
        self.now = self.stamp("2026-11-02 22:14:20")
        report = self.health()
        self.assertEqual(report["lanes_snapshot"]["status"], "fresh")
        self.assertEqual(report["lanes_snapshot"]["age_seconds"], 60)
        self.assertEqual(report["alerts"], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "ok")

    def test_a_missing_snapshot_is_reported_without_failing_the_health(self):
        report = self.health()
        self.assertEqual(report["lanes_snapshot"]["status"], "missing")
        self.assertEqual(report["alerts"], [])
        self.assertTrue(report["ok"])

    def test_an_unreadable_snapshot_is_an_alarm(self):
        directory = self.snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "lanes-status.json").write_text("{not json", encoding="utf-8")
        report = self.health()
        self.assertEqual(report["lanes_snapshot"]["status"], "unreadable")
        self.assertEqual([alert["kind"] for alert in report["alerts"]], ["lanes-snapshot-unreadable"])
        self.assertFalse(report["ok"])

    def test_the_default_output_warns_about_the_stale_snapshot(self):
        self.snapshot(STALLED_AT)
        self.now = self.stamp(STALLED_NOW)
        args = argparse.Namespace(hosts="worker-a", json=False, apply=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = xs.cmd_fleet_health(args, runner=self.runner(),
                                         hosts={"worker-a": {"name": "worker-a", "kind": "local"}},
                                         now=self.now, home=self.home)
        printed = output.getvalue()
        self.assertIn("lanes-status  stale", printed)
        self.assertIn(STALLED_AT, printed)
        self.assertIn("WARNING", printed)
        self.assertFalse(report["ok"])

    # ---------------------------------------------------------------- the writer liveness (Refs #1697)

    def test_a_fresh_heartbeat_means_the_writer_is_alive(self):
        self.snapshot("2026-11-02 22:17:20")
        self.heartbeat("2026-11-02 22:17:20")
        self.now = self.stamp(STALLED_NOW)
        report = self.health()
        self.assertEqual(report["lanes_snapshot"]["status"], "fresh")
        self.assertEqual(report["writer"]["status"], "alive")
        self.assertEqual(report["writer"]["heartbeat_age_seconds"], 60)
        self.assertEqual(report["alerts"], [])

    def test_a_silent_heartbeat_means_the_writer_is_stalled(self):
        self.snapshot(STALLED_AT)
        self.heartbeat(STALLED_AT)
        self.now = self.stamp("2026-11-02 22:33:20")  # 20 minutes of silence
        report = self.health()
        self.assertEqual(report["writer"]["status"], "stalled")
        self.assertEqual([alert["kind"] for alert in report["alerts"]],
                         ["lanes-snapshot-stale", "autodispatch-writer-stalled"])
        self.assertFalse(report["ok"])

    def test_a_dead_writer_process_is_reported(self):
        self.snapshot(STALLED_AT)
        state = self.ctl_state / "autodispatch.pid"
        state.write_text(json.dumps({"pid": 999999, "started_at": 1}), encoding="utf-8")
        self.now = self.stamp(STALLED_NOW)
        report = self.health()
        self.assertEqual(report["writer"]["status"], "dead")
        self.assertFalse(report["writer"]["pid_alive"])
        self.assertIn("autodispatch-writer-dead", [alert["kind"] for alert in report["alerts"]])

    def test_a_dead_writer_process_is_reported_even_with_a_fresh_heartbeat(self):
        """The pid file survives the process it names: a fresh heartbeat is not proof of life."""
        self.snapshot("2026-11-02 22:17:20")
        self.heartbeat("2026-11-02 22:17:20")  # refreshed a minute ago
        state = self.ctl_state / "autodispatch.pid"
        state.write_text(json.dumps({"pid": 999999, "started_at": 1}), encoding="utf-8")
        self.now = self.stamp(STALLED_NOW)
        report = self.health()
        self.assertEqual(report["lanes_snapshot"]["status"], "fresh")
        self.assertEqual(report["writer"]["heartbeat_age_seconds"], 60)
        self.assertFalse(report["writer"]["pid_alive"])
        self.assertEqual(report["writer"]["status"], "dead")
        self.assertEqual([alert["kind"] for alert in report["alerts"]], ["autodispatch-writer-dead"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "stale")

    def test_a_live_writer_process_is_reported_as_alive(self):
        self.snapshot(STALLED_AT)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        state = self.ctl_state / "autodispatch.pid"
        state.write_text(json.dumps({"pid": process.pid, "started_at": time.time()}), encoding="utf-8")
        self.now = self.stamp(STALLED_NOW)
        report = self.health()
        self.assertTrue(report["writer"]["pid_alive"])
        self.assertEqual(report["writer"]["status"], "alive")

    def test_the_writer_check_is_reported_even_without_a_snapshot(self):
        report = self.health()
        self.assertEqual(report["writer"]["status"], "unknown")
        self.assertIn("checked_at", report["writer"])
        self.assertIn("heartbeat_path", report["writer"])

    def test_policy_thresholds_override_the_defaults(self):
        policy = {"resources": {"health": {"inode_pause_pct": 50, "inode_resume_pct": 40}}}
        report = self.health(runner=self.runner(inode=60), policy=policy)
        self.assertEqual(report["hosts"][0]["status"], "hard")
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 50)

    def test_the_default_hosts_come_from_the_lanes_file(self):
        self.lanes(("worker-a", "worker-b"))
        args = argparse.Namespace(hosts=None, json=True, apply=False)
        report = xs.cmd_fleet_health(args, runner=self.runner(),
                                     hosts={"worker-a": {"name": "worker-a", "kind": "local"},
                                            "worker-b": {"name": "worker-b", "kind": "local"}},
                                     now=self.now, home=self.home)
        self.assertEqual([row["host"] for row in report["hosts"]], ["worker-a", "worker-b"])

    def test_an_unknown_host_is_reported_as_unreachable(self):
        report = self.health(hosts=("worker-a", "worker-b"))
        row = report["hosts"][1]
        self.assertEqual(row["host"], "worker-b")
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(row["error"], "unknown fleet host")


if __name__ == "__main__":
    unittest.main()
