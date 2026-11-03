"""`openswarm fleet health`: probe the fleet hosts and pause the unhealthy ones."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

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
RETENTION_COMMAND = xs.dispatch_health_module().RETENTION_PROBE_COMMAND


def home_disk(percent):
    """The df output of the filesystem that holds the executor home."""
    return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"/dev/root 1000 {percent} {100 - percent} {percent}% /\n")


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

    # ---------------------------------------------------------------- fixtures

    def runner(self, inode=10, failing=(), home=70, no_retention=False):
        """A fake probe runner: fixed outputs per command, transport failures per host."""
        outputs = {"df -P /": DISK, "df -Pi /": inode_output(inode), "free -m": MEMORY,
                   RETENTION_COMMAND: home_disk(home)}

        def run(host, command):
            if host in failing:
                raise OSError("ssh: connect to host %s port 22: Connection refused" % host)
            if no_retention and command == RETENTION_COMMAND:
                raise OSError("df: cannot read $HOME")
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
        self.assertEqual(sorted(report), ["applied", "hosts", "ok", "retention_alerts",
                                          "retention_thresholds", "status", "thresholds"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["status"], "hard")
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 95)
        self.assertEqual(report["retention_thresholds"]["disk_alert_pct"], 85)
        row = report["hosts"][0]
        self.assertEqual(sorted(row), ["action", "alerts", "decision", "error", "host", "metrics",
                                       "paused", "reason", "retention", "status", "triggers"])
        self.assertEqual(row["host"], "worker-a")
        self.assertFalse(row["paused"])

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

    def test_a_full_executor_home_warns_and_raises_the_cleanup_priority(self):
        report = self.health(runner=self.runner(home=91.7))
        row = report["hosts"][0]
        self.assertTrue(row["retention"]["alert"])
        self.assertEqual(row["retention"]["priority"], "high")
        self.assertEqual(row["retention"]["disk_used_pct"], 91.7)
        self.assertEqual(row["alerts"], ["workspace retention: disk 91.7% used "
                                          "(alert 85%), cleanup priority high"])
        self.assertEqual(report["retention_alerts"], ["worker-a"])
        self.assertEqual(row["status"], "soft")
        self.assertEqual(row["decision"], "none")
        self.assertFalse(report["ok"])

    def test_the_alert_is_printed_in_the_health_output(self):
        args = argparse.Namespace(hosts=None, json=False, apply=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            xs.cmd_fleet_health(args, policy={}, runner=self.runner(home=91.7),
                                hosts={"worker-a": {"name": "worker-a", "kind": "local"}},
                                now=self.now, home=self.home)
        printed = output.getvalue()
        self.assertIn("alert: workspace retention", printed)
        self.assertIn("cleanup priority high", printed)

    def test_a_quiet_executor_home_does_not_warn(self):
        report = self.health(runner=self.runner(home=42.0))
        row = report["hosts"][0]
        self.assertFalse(row["retention"]["alert"])
        self.assertEqual(row["retention"]["priority"], "normal")
        self.assertEqual(row["alerts"], [])
        self.assertEqual(report["retention_alerts"], [])
        self.assertEqual(row["status"], "ok")

    def test_the_retention_alert_watermark_is_configurable(self):
        policy = {"executor": {"retention": {"disk_alert_pct": 50}}}
        report = self.health(runner=self.runner(home=60.0), policy=policy)
        self.assertTrue(report["hosts"][0]["retention"]["alert"])
        self.assertEqual(report["retention_thresholds"]["disk_alert_pct"], 50)

    def test_a_host_that_cannot_answer_the_retention_probe_is_not_unhealthy(self):
        report = self.health(runner=self.runner(no_retention=True))
        row = report["hosts"][0]
        self.assertFalse(row["retention"]["alert"])
        self.assertIsNone(row["retention"]["disk_used_pct"])
        self.assertEqual(row["status"], "ok")


if __name__ == "__main__":
    unittest.main()
