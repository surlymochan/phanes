"""Contracts for dispatch host health decisions and remote output parsing."""

import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "health.py"
spec = importlib.util.spec_from_file_location("dispatch_health", MODULE)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)

ENGINE = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "engine.py"
engine_spec = importlib.util.spec_from_file_location("dispatch_engine_under_test", ENGINE)
engine = importlib.util.module_from_spec(engine_spec)
engine_spec.loader.exec_module(engine)

CLI = Path(__file__).resolve().parents[1] / "bin" / "openswarm"
cli_loader = importlib.machinery.SourceFileLoader("openswarm_cli_under_test", str(CLI))
cli_spec = importlib.util.spec_from_loader("openswarm_cli_under_test", cli_loader)
cli = importlib.util.module_from_spec(cli_spec)
cli_loader.exec_module(cli)


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

    def test_zombie_processes_pause_the_host(self):
        """A host that keeps unreaped children cannot start a new git, so it is unhealthy."""
        decision, reason = health.evaluate({"host": "h", "zombie_count": 200})
        self.assertEqual(decision, "pause")
        self.assertIn("zombies", reason)
        self.assertEqual(health.evaluate({"host": "h", "zombie_count": 199})[0], "none")

    def test_zombie_threshold_is_configurable(self):
        limits = health.thresholds_from_policy({"resources": {"health": {"zombie_pause_count": 5}}})
        self.assertEqual(limits["zombie_pause_count"], 5)
        self.assertEqual(health.evaluate({"host": "h", "zombie_count": 6}, limits)[0], "pause")
        self.assertEqual(health.evaluate({"host": "h", "zombie_count": 4}, limits)[0], "none")
        self.assertEqual(health.thresholds_from_policy({})["zombie_pause_count"], 200)


class ProbeTests(unittest.TestCase):
    def test_parses_disk_inode_and_memory_from_injected_runner(self):
        calls = []
        outputs = {
            "df -P /": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 1000 700 300 70% /\n",
            "df -Pi /": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1000 950 50 95% /\n",
            "free -m": "              total used free shared buff/cache available\nMem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n",
            "ps -eo stat=": "Ss\nZ\nZ+\nR+\n",
        }
        def runner(host, command):
            calls.append((host, command))
            return outputs[command]
        result = health.run_probe("node-a", runner)
        self.assertEqual(result, {"host": "node-a", "disk_pct": 70.0, "inode_pct": 95.0,
                                  "mem_avail_mb": 5000, "ssh_fail_streak": 0, "zombie_count": 2})
        self.assertEqual([command for _, command in calls], list(outputs))

    def test_a_host_without_ps_has_no_zombie_measurement(self):
        outputs = {
            "df -P /": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 1000 700 300 70% /\n",
            "df -Pi /": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1000 950 50 95% /\n",
            "free -m": "              total used free shared buff/cache available\nMem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n",
        }
        def runner(host, command):
            if command == "ps -eo stat=":
                raise OSError("ps: command not found")
            return outputs[command]
        result = health.run_probe("node-a", runner)
        self.assertNotIn("zombie_count", result)
        self.assertEqual(result["disk_pct"], 70.0)

    def test_a_runner_without_the_zombie_command_has_no_zombie_measurement(self):
        """An optional measurement never fails the probe of the other pressures."""
        outputs = {
            "df -P /": "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/root 1000 700 300 70% /\n",
            "df -Pi /": "Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/root 1000 950 50 95% /\n",
            "free -m": "              total used free shared buff/cache available\nMem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n",
        }
        def runner(host, command):
            return outputs[command]  # a runner that does not know the command raises KeyError
        self.assertIsNone(health.probe_zombies("node-a", runner))
        result = health.run_probe("node-a", runner)
        self.assertNotIn("zombie_count", result)
        self.assertEqual(result["disk_pct"], 70.0)

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


class SshFailStreakTests(unittest.TestCase):
    """The remembered SSH failure streaks of the fleet-health state (Refs #1674)."""

    def test_the_streak_sits_on_the_host_record_itself(self):
        state = {"hosts": {"box": {"ssh_fail_streak": 3, "status": "unreachable",
                                   "updated_at": 1700000000.0},
                           "pi": {"ssh_fail_streak": 0, "status": "ok", "updated_at": 1700000000.0}},
                 "updated_at": 1700000000.0}
        self.assertEqual(health.ssh_fail_streaks(state), {"box": 3, "pi": 0})

    def test_a_damaged_state_is_never_evidence_of_a_failure(self):
        for state in ({}, {"hosts": None}, {"hosts": {"box": "broken"}},
                      {"hosts": {"box": {"ssh_fail_streak": "x"}}}, None, []):
            with self.subTest(state=state):
                self.assertEqual(health.ssh_fail_streaks(state), {})

    def test_a_hosts_entry_that_is_not_an_object_is_never_evidence_of_a_failure(self):
        for hosts in (["box"], "box", 3, [1, 2]):
            with self.subTest(hosts=hosts):
                self.assertEqual(health.ssh_fail_streaks({"hosts": hosts}), {})

    def test_the_record_is_written_the_way_the_gate_reads_it(self):
        record = health.ssh_fail_record(3, "unreachable", 1700000000.0)
        self.assertEqual(record, {"ssh_fail_streak": 3, "status": "unreachable",
                                  "updated_at": 1700000000.0})
        self.assertEqual(health.ssh_fail_streaks({"hosts": {"box": record}}), {"box": 3})

    def test_a_damaged_streak_is_counted_as_zero(self):
        self.assertEqual(health.ssh_fail_record("x", "unreachable", 1)["ssh_fail_streak"], 0)
        self.assertEqual(health.ssh_fail_record(None, "ok", 1)["ssh_fail_streak"], 0)

    def test_the_threshold_decides_whether_a_host_is_blocked(self):
        self.assertFalse(health.ssh_fail_blocked(2))
        self.assertTrue(health.ssh_fail_blocked(3))
        self.assertTrue(health.ssh_fail_blocked(4))
        self.assertFalse(health.ssh_fail_blocked(None))
        self.assertFalse(health.ssh_fail_blocked("broken"))

    def test_a_configured_threshold_wins(self):
        self.assertFalse(health.ssh_fail_blocked(4, {"ssh_fail_pause_streak": 5}))
        self.assertTrue(health.ssh_fail_blocked(5, {"ssh_fail_pause_streak": 5}))

    def test_a_damaged_threshold_falls_back_to_the_default(self):
        self.assertTrue(health.ssh_fail_blocked(3, {"ssh_fail_pause_streak": "x"}))
        self.assertFalse(health.ssh_fail_blocked(2, {"ssh_fail_pause_streak": "x"}))


class TickRunner:
    """Answers the GitHub and git commands of one dispatch tick and records every call."""

    def __init__(self, issues=()):
        self.issues = list(issues)
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        if "issue list" in text and "lane-ready" in text:
            return 0, json.dumps(self.issues), ""
        if "issue list" in text:
            return 0, "[]", ""
        if "issue view" in text:
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-ready"}]}), ""
        if "ls-remote" in text:
            return 0, "b" * 40 + "\n", ""
        if "lane assign" in text:
            return 0, '{"id": "run-1"}', ""
        return 0, "", ""

    def called(self, needle):
        return [call for call in self.calls if needle in call]


class ZombiePressureDispatchTickTests(unittest.TestCase):
    """The dispatch tick of a host under zombie pressure (Refs #1817).

    A host that keeps unreaped children cannot start a new `git` (it fails with
    "unable to create threaded lstat"), so the health probe pauses the host and the
    dispatch tick gives that host no lane any more.  When the pressure is gone the
    probe releases the host and the tick dispatches again.

    Both parts are the shipped ones: the health probe is the real `fleet health` of the
    CLI (it writes the pause into the pause store of the openswarm home) and the tick is
    the real dispatch tick.  Only the ssh probe of the lane and the GitHub commands are
    stubbed.
    """

    HOST = "h1"
    LANE = {"L1": {"host": "h1", "backend": "pi"}}
    THRESHOLD = 200

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-zombie-tick-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "repo").mkdir()
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    # ---------------------------------------------------------------- fixtures

    def probe_runner(self, zombies):
        """The runner of the health probe: the host holds `zombies` zombie processes."""
        def runner(host, command):
            if command == health.ZOMBIE_PROBE_COMMAND:
                return "".join("Z\n" for _ in range(zombies))
            if command.startswith("df -Pi"):
                return ("Filesystem Inodes IUsed IFree IUse% Mounted on\n"
                        "/dev/root 1000 100 900 10% /\n")
            if command.startswith("df -P"):
                return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                        "/dev/root 1000 700 300 70% /\n")
            if command.startswith("free"):
                return ("              total used free shared buff/cache available\n"
                        "Mem: 8000 3000 2000 100 3000 5000\n")
            raise OSError("unknown command: " + command)
        return runner

    def health_probe(self, zombies):
        """Run the real health probe with --apply and return its report."""
        args = argparse.Namespace(hosts=self.HOST, apply=True, json=False)
        policy = {"resources": {"health": {"zombie_pause_count": self.THRESHOLD}}}
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.cmd_fleet_health(args, policy=policy, runner=self.probe_runner(zombies),
                                        hosts={self.HOST: {}}, home=str(self.home))

    def pause_records(self):
        """The pause records of the openswarm home."""
        store = self.home / "pauses"
        if not store.exists():
            return {}
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in store.glob("*.json")}

    def dispatcher(self, runner):
        """The real dispatcher; only the ssh probe of the lanes is stubbed."""
        cfg = {"repo": "acme/project", "local_repo": str(self.root / "repo"),
               "state_dir": str(self.root / "state"), "remote_home": str(self.root),
               "ssh": {"control_dir": str(self.root / "cm")}, "local_hosts": [],
               "hot_files": {"limits": {}, "union": []}}
        path = self.root / "cfg.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        d = engine.Dispatcher(engine.load_config(str(path)), runner=runner)
        d.probe = lambda lanes, state: {name: {"lane": "idle"} for name in lanes}
        return d

    def tick(self, runner):
        """Run one real dispatch tick and return its state."""
        d = self.dispatcher(runner)
        state = {}
        d.tick(dict(self.LANE), state)
        return state

    def runner(self):
        return TickRunner([{"number": 7, "title": "card 7", "body": "", "labels": [{"name": "lane-ready"}]}])

    # ---------------------------------------------------------------- tests

    def test_the_zombie_pressure_pauses_the_host_and_stops_the_dispatch(self):
        report = self.health_probe(self.THRESHOLD + 100)

        self.assertEqual(report["hosts"][0]["decision"], "pause")
        self.assertIn("zombies", report["hosts"][0]["reason"])
        self.assertEqual(report["hosts"][0]["action"], "paused")
        # The probe really paused the host in the pause store of the openswarm home.
        self.assertIn("host_h1", self.pause_records())
        runner = self.runner()

        state = self.tick(runner)

        self.assertNotIn("L1", state)
        self.assertFalse(runner.called("lane assign"))
    def test_the_recovered_host_is_dispatched_again(self):
        self.health_probe(self.THRESHOLD + 100)
        runner = self.runner()
        self.assertNotIn("L1", self.tick(runner))

        report = self.health_probe(0)

        self.assertEqual(report["hosts"][0]["decision"], "resume")
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertEqual(self.pause_records(), {})
        runner = self.runner()

        state = self.tick(runner)

        self.assertEqual(state["L1"]["issue"], 7)
        self.assertTrue(runner.called("lane assign"))

    def test_a_host_below_the_threshold_is_never_paused(self):
        report = self.health_probe(self.THRESHOLD - 1)

        self.assertEqual(report["hosts"][0]["decision"], "none")
        self.assertEqual(report["hosts"][0]["action"], "none")
        self.assertEqual(self.pause_records(), {})
        runner = self.runner()

        state = self.tick(runner)

        self.assertEqual(state["L1"]["issue"], 7)

    def test_the_threshold_of_the_policy_decides_the_pause(self):
        """The threshold is configurable, so a host with fewer zombies can be paused too."""
        args = argparse.Namespace(hosts=self.HOST, apply=True, json=False)
        policy = {"resources": {"health": {"zombie_pause_count": 5}}}
        with contextlib.redirect_stdout(io.StringIO()):
            report = cli.cmd_fleet_health(args, policy=policy, runner=self.probe_runner(6),
                                          hosts={self.HOST: {}}, home=str(self.home))

        self.assertEqual(report["hosts"][0]["decision"], "pause")
        self.assertIn("host_h1", self.pause_records())


if __name__ == "__main__":
    unittest.main()
