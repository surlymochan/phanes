"""`openswarm fleet health`: probe the fleet hosts and pause the unhealthy ones."""
import argparse
import contextlib
import importlib.util
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

_engine_spec = importlib.util.spec_from_file_location(
    "dispatch_engine_under_test", ROOT / "lib" / "dispatch" / "engine.py")
E = importlib.util.module_from_spec(_engine_spec)
_engine_spec.loader.exec_module(E)

DISK = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/root 1000 700 300 70% /\n")
MEMORY = ("              total used free shared buff/cache available\n"
          "Mem: 8000 3000 2000 100 3000 5000\nSwap: 0 0 0\n")


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
        (self.home / "runs" / "failed-1").mkdir(parents=True)
        (self.home / "runs" / "failed-1" / "result.json").write_text(
            json.dumps({"status": "failed", "failure_reason": "spawn-failed"}), encoding="utf-8")
        self.now = 1700000000.0
        self.store = xs.dispatch_pause_module().PauseStore(self.home)

    # ---------------------------------------------------------------- fixtures

    def runner(self, inode=10, failing=()):
        """A fake probe runner: fixed outputs per command, transport failures per host."""
        outputs = {"df -P /": DISK, "df -Pi /": inode_output(inode), "free -m": MEMORY,
                   "date +%s.%N": str(self.now)}

        def run(host, command):
            if host in failing:
                raise OSError("ssh: connect to host %s port 22: Connection refused" % host)
            return outputs[command]
        return run

    def clock_runner(self, offset_s, inode=10):
        """A probe runner whose host clock is `offset_s` seconds off the control plane."""
        runner = self.runner(inode=inode)

        def run(host, command):
            if command == "date +%s.%N":
                return str(self.now + offset_s)
            return runner(host, command)
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

    def restore_dispatch_config(self, previous):
        if previous is None:
            os.environ.pop("OPENSWARM_DISPATCH_CONFIG", None)
        else:
            os.environ["OPENSWARM_DISPATCH_CONFIG"] = previous

    def health(self, runner=None, hosts=("worker-a",), apply=False, json_out=True,
               policy=None, lanes=("worker-a",), brain_log=None, text=False):
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
                                         now=self.now, home=self.home, brain_log=brain_log)
        if json_out:
            self.assertEqual(json.loads(output.getvalue()), report)
        if text:
            self.text = output.getvalue()
        return report

    def pauses(self):
        return {row["scope"]: row for row in self.store.active(self.now)}

    def dispatcher(self):
        """A dispatcher that reads the health state this probe writes."""
        cfg_path = self.root / "cfg.json"
        cfg_path.write_text(json.dumps({
            "repo": "acme/project", "local_repo": str(self.root),
            "state_dir": str(self.root / "state"),
            "fleet_health_file": str(self.home / "fleet-health.json"),
        }), encoding="utf-8")
        return E.Dispatcher(E.load_config(str(cfg_path)),
                            runner=lambda *args, **kwargs: (0, "", ""), out=io.StringIO())

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
        self.assertEqual(sorted(report), ["applied", "hosts", "ok", "recent_failure_reasons", "status", "thresholds"])
        self.assertEqual(report["recent_failure_reasons"], {"spawn-failed": 1})
        self.assertFalse(report["applied"])
        self.assertEqual(report["status"], "hard")
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 95)
        row = report["hosts"][0]
        self.assertEqual(sorted(row), ["action", "clock", "decision", "error", "host", "metrics", "paused",
                                       "reason", "status", "triggers"])
        self.assertEqual(row["host"], "worker-a")
        self.assertFalse(row["paused"])
        self.assertFalse(row["clock"]["unreliable"])

    def test_fast_clock_is_reported_and_time_is_unreliable(self):
        runner = self.runner()
        def fast(host, command):
            return str(self.now + 300) if command == "date +%s.%N" else runner(host, command)
        row = self.health(runner=fast)["hosts"][0]
        self.assertEqual(row["clock"]["offset_s"], 300.0)
        self.assertTrue(row["clock"]["unreliable"])
        self.assertIn("fast by 300.0s", row["clock"]["warning"])

    def test_a_host_five_minutes_fast_is_alarmed_and_its_time_is_not_trusted(self):
        """The 5-minute simulation of the issue: the host is fast, the fleet is not ok."""
        row = self.health(runner=self.clock_runner(300.0))["hosts"][0]
        self.assertEqual(row["status"], "soft")
        self.assertEqual(row["clock"]["offset_s"], 300.0)
        self.assertEqual(row["clock"]["direction"], "fast")
        self.assertTrue(row["clock"]["unreliable"])
        report = self.health(runner=self.clock_runner(300.0))
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "soft")

    def test_the_text_output_names_the_direction_the_seconds_and_the_repair(self):
        self.health(runner=self.clock_runner(-300.0), json_out=False, text=True)
        self.assertIn("clock: clock slow by 300.0s", self.text)
        self.assertIn("timedatectl set-ntp true", self.text)

    def test_the_measured_clock_is_remembered_per_host(self):
        self.health(runner=self.clock_runner(300.0))
        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        self.assertEqual(state["hosts"]["worker-a"]["clock_offset_s"], 300.0)
        self.assertTrue(state["hosts"]["worker-a"]["clock_unreliable"])

    def test_a_clock_in_range_is_remembered_as_trusted(self):
        self.health(runner=self.clock_runner(5.0))
        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        self.assertEqual(state["hosts"]["worker-a"]["clock_offset_s"], 5.0)
        self.assertFalse(state["hosts"]["worker-a"]["clock_unreliable"])

    def test_the_first_skew_is_written_into_the_brain_log(self):
        brain = self.root / "brain.md"
        self.health(runner=self.clock_runner(300.0), brain_log=str(brain))
        lines = brain.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("worker-a", lines[0])
        self.assertIn("clock fast by 300.0s", lines[0])
        self.assertIn("timedatectl set-ntp true", lines[0])

    def test_a_steady_skew_is_not_written_again(self):
        brain = self.root / "brain.md"
        for _ in range(3):
            self.health(runner=self.clock_runner(300.0), brain_log=str(brain))
        self.assertEqual(len(brain.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_a_growing_skew_is_written_again(self):
        brain = self.root / "brain.md"
        self.health(runner=self.clock_runner(300.0), brain_log=str(brain))
        self.health(runner=self.clock_runner(900.0), brain_log=str(brain))
        lines = brain.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("clock fast by 900.0s", lines[1])

    def test_a_skew_after_a_recovery_is_written_again(self):
        brain = self.root / "brain.md"
        self.health(runner=self.clock_runner(300.0), brain_log=str(brain))
        self.health(runner=self.clock_runner(0.0), brain_log=str(brain))
        self.health(runner=self.clock_runner(300.0), brain_log=str(brain))
        lines = brain.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("clock fast by 300.0s", lines[1])

    def test_an_unreachable_host_does_not_report_a_clock_alert(self):
        brain = self.root / "brain.md"
        self.health(runner=self.runner(failing=("worker-a",)), brain_log=str(brain))
        self.assertFalse(brain.exists())
        # the last known clock of the host is kept, not replaced by a guess
        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        self.assertNotIn("clock_offset_s", state["hosts"]["worker-a"])

    def test_the_brain_log_of_the_dispatch_configuration_is_used(self):
        """The health probe writes into the brain log the dispatcher writes into."""
        brain = self.root / "brain.md"
        config = self.root / "dispatch.json"
        config.write_text(json.dumps({"repo": "acme/project", "brain_log": str(brain)}), encoding="utf-8")
        previous = os.environ.get("OPENSWARM_DISPATCH_CONFIG")
        os.environ["OPENSWARM_DISPATCH_CONFIG"] = str(config)
        self.addCleanup(self.restore_dispatch_config, previous)
        self.health(runner=self.clock_runner(300.0))
        self.assertIn("clock fast by 300.0s", brain.read_text(encoding="utf-8"))

    def test_the_harvest_failure_is_counted(self):
        (self.home / "runs" / "harvest-1").mkdir(parents=True)
        (self.home / "runs" / "harvest-1" / "result.json").write_text(
            json.dumps({"status": "done", "harvest_failure_reason": "fetch-failed",
                        "harvest_failure_error": "Permission denied"}), encoding="utf-8")
        report = self.health(runner=self.runner())
        self.assertEqual(report["recent_failure_reasons"], {"spawn-failed": 1, "fetch-failed": 1})

    def test_a_failed_run_without_a_reason_is_counted_as_unknown(self):
        (self.home / "runs" / "failed-2").mkdir(parents=True)
        (self.home / "runs" / "failed-2" / "result.json").write_text(
            json.dumps({"status": "failed", "error_class": "brand_new_class",
                        "error": "something odd happened"}), encoding="utf-8")
        report = self.health(runner=self.runner())
        self.assertEqual(report["recent_failure_reasons"], {"spawn-failed": 1, "unknown": 1})
        for reason in report["recent_failure_reasons"]:
            self.assertNotEqual(reason, "")
            self.assertIsNotNone(reason)

    def test_a_run_is_counted_once(self):
        # the run itself failed and the harvest broke the ending: one run, one count
        (self.home / "runs" / "failed-3").mkdir(parents=True)
        (self.home / "runs" / "failed-3" / "result.json").write_text(
            json.dumps({"status": "failed", "failure_reason": "exec-error",
                        "failure_error": "child exited with code 1",
                        "harvest_failure_reason": "exec-error",
                        "harvest_failure_error": "pr create failed: HTTP 502"}), encoding="utf-8")
        report = self.health(runner=self.runner())
        self.assertEqual(report["recent_failure_reasons"], {"spawn-failed": 1, "exec-error": 1})

    def test_the_harvest_failure_of_a_failed_run_is_counted_too(self):
        # the run failed and the harvest fetch gave up: both causes stay visible
        (self.home / "runs" / "failed-4").mkdir(parents=True)
        (self.home / "runs" / "failed-4" / "result.json").write_text(
            json.dumps({"status": "failed", "failure_reason": "exec-error",
                        "failure_error": "child exited with code 1",
                        "harvest_failure_reason": "fetch-failed",
                        "harvest_failure_error": "Permission denied"}), encoding="utf-8")
        report = self.health(runner=self.runner())
        self.assertEqual(report["recent_failure_reasons"],
                         {"spawn-failed": 1, "exec-error": 1, "fetch-failed": 1})

    def test_a_clean_run_with_a_commit_is_not_counted(self):
        (self.home / "runs" / "done-1").mkdir(parents=True)
        (self.home / "runs" / "done-1" / "result.json").write_text(
            json.dumps({"status": "done", "branch": "lane/x", "commit": "c"}), encoding="utf-8")
        report = self.health(runner=self.runner())
        self.assertEqual(report["recent_failure_reasons"], {"spawn-failed": 1})

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

    def test_the_state_the_probe_writes_blocks_the_dispatch(self):
        """The dispatch gate reads the file the probe writes (Refs #1674)."""
        for _ in range(3):                     # three failing probes reach the pause threshold
            self.health(runner=self.runner(failing=("worker-a",)), apply=True)
        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        self.assertEqual(state["hosts"]["worker-a"]["ssh_fail_streak"], 3)
        self.assertTrue(self.dispatcher().ssh_health_blocked("worker-a"))
        # a reachable host resets the streak and the gate lets it pass again
        self.health(runner=self.runner(), apply=True)
        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        self.assertEqual(state["hosts"]["worker-a"]["ssh_fail_streak"], 0)
        self.assertFalse(self.dispatcher().ssh_health_blocked("worker-a"))


if __name__ == "__main__":
    unittest.main()
