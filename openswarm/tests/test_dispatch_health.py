"""Contracts for dispatch host health decisions and remote output parsing."""

import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "health.py"
spec = importlib.util.spec_from_file_location("dispatch_health", MODULE)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class EvaluateTests(unittest.TestCase):
    def test_each_pause_threshold(self):
        cases = (("disk_pct", 95), ("inode_pct", 95), ("mem_avail_mb", 512))
        for field, value in cases:
            with self.subTest(field=field):
                self.assertEqual(health.evaluate({"host": "h", field: value})[0], "pause")

    def test_memory_pause_boundary(self):
        self.assertEqual(health.evaluate({"host": "h", "mem_avail_mb": 512})[0], "pause")
        self.assertEqual(health.evaluate({"host": "h", "mem_avail_mb": 513})[0], "none")

    def test_hysteresis_resumes_only_after_recovery(self):
        self.assertEqual(health.evaluate({"host": "h", "paused": True, "inode_pct": 94})[0], "none")
        self.assertEqual(health.evaluate({"host": "h", "paused": True, "inode_pct": 92})[0], "resume")
        self.assertEqual(health.evaluate({"host": "h", "inode_pct": 94})[0], "none")
        self.assertEqual(health.evaluate({"host": "h", "inode_pct": 95})[0], "pause")
        self.assertEqual(health.evaluate({"host": "h", "paused": True, "mem_avail_mb": 640})[0], "resume")

    def test_resume_waits_for_every_reported_pressure(self):
        for probe in ({"disk_pct": 99, "mem_avail_mb": 1000},
                      {"disk_pct": 50, "mem_avail_mb": 100}):
            decision, reason = health.evaluate({"host": "h", "paused": True, **probe})
            self.assertEqual(decision, "none")
            self.assertIn("still paused", reason)

    def test_disk_resume_threshold(self):
        self.assertEqual(health.evaluate({"host": "h", "paused": True, "disk_pct": 92})[0], "resume")
        self.assertEqual(health.evaluate({"host": "h", "paused": True, "disk_pct": 93})[0], "none")

    def test_ssh_failure_streak_pauses_and_recovery_is_caller_owned(self):
        self.assertEqual(health.evaluate({"host": "h", "ssh_fail_streak": 2})[0], "none")
        decision, reason = health.evaluate({"host": "h", "ssh_fail_streak": 3})
        self.assertEqual(decision, "pause")
        self.assertIn("SSH failure streak", reason)
        decision, reason = health.evaluate({"host": "h", "paused": True, "ssh_fail_streak": 0})
        self.assertEqual(decision, "none")
        self.assertIn("no valid measurements", reason)

    def test_paused_without_valid_measurement_does_not_resume(self):
        for probe in ({"host": "h", "paused": True},
                      {"host": "h", "paused": True, "disk_pct": "95"},
                      {"host": "h", "paused": True, "disk_pct": "95", "mem_avail_mb": None}):
            with self.subTest(probe=probe):
                decision, reason = health.evaluate(probe)
                self.assertEqual(decision, "none")
                self.assertIn("no valid measurements", reason)

    def test_invalid_measurement_is_ignored_for_pauses(self):
        self.assertEqual(health.evaluate({"host": "h", "disk_pct": "95"})[0], "none")
        self.assertEqual(health.evaluate({"host": "h", "disk_pct": "95", "inode_pct": 99})[0], "pause")

    def test_custom_thresholds_and_healthy_probe(self):
        self.assertEqual(health.evaluate({"host": "h", "disk_pct": 80}, {"disk_pause_pct": 80})[0], "pause")
        self.assertEqual(health.evaluate({"host": "h", "disk_pct": 20, "inode_pct": 30, "mem_avail_mb": 900})[0], "none")

    def test_paused_reason_names_pending_pressure(self):
        decision, reason = health.evaluate({"host": "h", "paused": True, "inode_pct": 99})
        self.assertEqual(decision, "none")
        self.assertIn("still paused", reason)
        self.assertIn("inodes", reason)


class ProbeTests(unittest.TestCase):
    def test_parses_disk_inode_and_memory_from_injected_runner(self):
        calls = []
        outputs = {
            "df -P /": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 1000 700 300 70% /\n",
            "df -Pi /": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1000 950 50 95% /\n",
            "free -m": "              total used free shared buff/cache available\nMem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n",
        }
        def runner(host, command):
            calls.append((host, command))
            return outputs[command]
        result = health.run_probe("node-a", runner)
        self.assertEqual(result, {"host": "node-a", "disk_pct": 70.0, "inode_pct": 95.0,
                                  "mem_avail_mb": 5000, "ssh_fail_streak": 0})
        self.assertEqual([command for _, command in calls], list(outputs))

    def test_parses_mac_inode_column_and_selects_root_mount(self):
        disk = "Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/root 1000 700 300 70% /\n/dev/data 100 99 1 99% /data\n"
        inode = "Filesystem 512-blocks Used Available Capacity iused ifree %iused Mounted on\n/dev/root 1000 700 300 70% 10 990 1% /\n"
        self.assertEqual(health._df_percent(disk, "Use%"), 70.0)
        self.assertEqual(health._df_percent(inode, "IUse%"), 1.0)

    def test_bad_output_raises_value_error(self):
        with self.assertRaises(ValueError):
            health.run_probe("h", lambda _host, _command: "unexpected output")

    def test_runner_ssh_error_propagates(self):
        def failed(_host, _command):
            raise OSError("ssh unavailable")
        with self.assertRaisesRegex(OSError, "ssh unavailable"):
            health.run_probe("h", failed)


class RetentionTests(unittest.TestCase):
    """The workspace retention of the executor home is part of the host health."""

    def test_the_retention_probe_measures_the_home_filesystem(self):
        calls = []
        def runner(host, command):
            calls.append((host, command))
            return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 1000 917 83 92% /\n")
        result = health.run_retention_probe("node-a", runner)
        self.assertEqual(result, {"home_disk_pct": 92.0})
        self.assertEqual(calls, [("node-a", health.RETENTION_PROBE_COMMAND)])

    def test_the_home_mountpoint_is_not_required_to_be_root(self):
        output = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                  "/dev/data 1000 90 10 90% /home/worker\n")
        self.assertEqual(health._df_last_percent(output, "Use%"), 90.0)

    def test_a_full_disk_raises_the_cleanup_priority(self):
        state = health.retention_state(91.7)
        self.assertTrue(state["alert"])
        self.assertEqual(state["priority"], "high")
        self.assertEqual(state["alert_pct"], 85)

    def test_a_quiet_disk_keeps_the_normal_priority(self):
        state = health.retention_state(42.0)
        self.assertFalse(state["alert"])
        self.assertEqual(state["priority"], "normal")

    def test_an_unmeasured_disk_never_warns(self):
        state = health.retention_state(None)
        self.assertFalse(state["alert"])
        self.assertIsNone(state["disk_used_pct"])

    def test_the_alert_watermark_comes_from_the_policy(self):
        limits = health.retention_thresholds_from_policy(
            {"executor": {"retention": {"disk_alert_pct": 50, "disk_threshold_pct": 60}}})
        self.assertEqual(limits["disk_alert_pct"], 50)
        self.assertEqual(limits["disk_threshold_pct"], 60)
        self.assertTrue(health.retention_state(55.0, limits)["alert"])
        self.assertFalse(health.retention_state(45.0, limits)["alert"])

    def test_the_shipped_policy_alerts_at_85_percent(self):
        import json
        import pathlib
        policy = json.loads((pathlib.Path(__file__).resolve().parents[1] / "policy.json")
                             .read_text(encoding="utf-8"))
        limits = health.retention_thresholds_from_policy(policy)
        self.assertEqual(limits["disk_alert_pct"], 85)

    def test_a_host_row_carries_the_retention_alert(self):
        def runner(_host, command):
            if command == health.RETENTION_PROBE_COMMAND:
                return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                        "/dev/root 1000 917 83 92% /\n")
            if command == "df -P /":
                return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                        "/dev/root 1000 700 300 70% /\n")
            if command == "df -Pi /":
                return ("Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                        "/dev/root 1000 950 50 10% /\n")
            return ("              total used free shared buff/cache available\n"
                    "Mem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n")
        row = health.check_host("node-a", runner)
        self.assertTrue(row["retention"]["alert"])
        self.assertEqual(row["retention"]["priority"], "high")
        self.assertEqual(row["alerts"], ["workspace retention: disk 92% used "
                                          "(alert 85%), cleanup priority high"])
        self.assertEqual(row["status"], health.STATUS_SOFT)


if __name__ == "__main__":
    unittest.main()
