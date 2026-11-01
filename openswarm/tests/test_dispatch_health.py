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


if __name__ == "__main__":
    unittest.main()
