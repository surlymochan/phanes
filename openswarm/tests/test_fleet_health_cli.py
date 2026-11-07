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

    def topology(self, limits, hosts=("worker-a",)):
        """Write the capacity topology the coordinator admits turns against."""
        directory = self.root / "capacity"
        directory.mkdir(exist_ok=True)
        (directory / "topology.json").write_text(json.dumps({"hosts": list(hosts), "limits": limits}),
                                                 encoding="utf-8")
        previous = os.environ.get("OPENSWARM_FLEET_CAPACITY_DIR")
        os.environ["OPENSWARM_FLEET_CAPACITY_DIR"] = str(directory)
        self.addCleanup(self.restore_env, "OPENSWARM_FLEET_CAPACITY_DIR", previous)
        return directory

    def brain_log(self):
        """Point the brain log at a file inside the temporary home."""
        path = self.home / "brain.log"
        previous = os.environ.get("OPENSWARM_BRAIN_LOG")
        os.environ["OPENSWARM_BRAIN_LOG"] = str(path)
        self.addCleanup(self.restore_env, "OPENSWARM_BRAIN_LOG", previous)
        return path

    def restore_env(self, name, previous):
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

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
        self.assertEqual(sorted(report), ["applied", "capacity", "hosts", "ok",
                                          "recent_failure_reasons", "status", "thresholds"])
        self.assertEqual(report["recent_failure_reasons"], {"spawn-failed": 1})
        self.assertFalse(report["applied"])
        self.assertEqual(report["status"], "hard")
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 95)
        row = report["hosts"][0]
        self.assertEqual(sorted(row), ["action", "decision", "error", "host", "metrics", "paused",
                                       "reason", "status", "triggers"])
        self.assertEqual(row["host"], "worker-a")
        self.assertFalse(row["paused"])

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

    def test_the_capacity_section_flags_a_limit_behind_the_lane_count(self):
        """The lane count of the lanes.json is compared with the fleet limits."""
        self.topology({"total": 4, "pi": 11, "minimax-cn": 11, "codex": 1, "host:worker-a": 2},
                      hosts=("worker-a", "worker-b"))
        report = self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        capacity = report["capacity"]
        self.assertEqual(capacity["status"], "alert")
        self.assertFalse(report["ok"])
        rows = {row["limit"]: row for row in capacity["alerts"]}
        self.assertEqual(sorted(rows), ["codex"])
        self.assertEqual(rows["codex"]["limit_value"], 1)
        self.assertEqual(rows["codex"]["lanes"], 2)
        self.assertEqual(rows["codex"]["deficit"], 1)
        self.assertEqual(rows["codex"]["suggested"], 3)
        self.assertEqual(capacity["lane_total"], 2)
        self.assertEqual(capacity["by_backend"], {"codex": 2})
        self.assertEqual(capacity["by_host"], {"worker-a": 1, "worker-b": 1})

    def test_capacity_counts_control_plane_and_all_host_lane_files_once(self):
        control = self.home / "lanes.json"
        control.write_text(json.dumps({"lanes": [
            {"name": "box-pi-%d" % i, "backend": "pi", "provider": "minimax-cn", "host": "box"}
            for i in range(7)]}), encoding="utf-8")
        fleet_hosts = {"box": {"name": "box", "kind": "local", "lanes_file": str(control)}}
        topology_hosts = ["box"]
        for host in ("ib1", "ib2", "ib3", "ib4"):
            path = self.home / (host + "-lanes.json")
            path.write_text(json.dumps({"lanes": [
                {"name": "%s-pi-%d" % (host, i), "backend": "pi", "provider": "minimax-cn"}
                for i in range(2)]}), encoding="utf-8")
            fleet_hosts[host] = {"name": host, "kind": "local", "lanes_file": str(path)}
            topology_hosts.append(host)
        self.topology({"total": 20, "pi": 11, "minimax-cn": 11}, hosts=topology_hosts)
        capacity = xs.fleet_health_capacity(self.home, fleet_hosts)
        self.assertEqual(capacity["status"], "alert")
        self.assertEqual(capacity["lane_total"], 15)
        rows = {row["limit"]: row for row in capacity["alerts"]}
        self.assertEqual(sorted(rows), ["minimax-cn", "pi"])
        self.assertEqual((rows["pi"]["limit_value"], rows["pi"]["lanes"], rows["pi"]["deficit"]),
                         (11, 15, 4))
        self.assertEqual((rows["minimax-cn"]["limit_value"], rows["minimax-cn"]["lanes"],
                          rows["minimax-cn"]["deficit"]), (11, 15, 4))

    def test_a_host_that_is_not_in_the_topology_is_still_counted(self):
        """A lane that moved to a host the topology does not list yet is counted all the same."""
        control = self.home / "lanes.json"
        control.write_text(json.dumps({"lanes": [
            {"name": "box-pi-%d" % i, "backend": "pi", "provider": "minimax-cn", "host": "box"}
            for i in range(7)]}), encoding="utf-8")
        previous = os.environ.get("OPENSWARM_LANES_FILE")
        os.environ["OPENSWARM_LANES_FILE"] = str(control)
        self.addCleanup(self.restore_lanes, previous)
        new_host = self.home / "ib5-lanes.json"
        new_host.write_text(json.dumps({"lanes": [
            {"name": "ib5-pi-%d" % i, "backend": "pi", "provider": "minimax-cn"}
            for i in range(8)]}), encoding="utf-8")
        fleet_hosts = {"box": {"name": "box", "kind": "local", "lanes_file": str(control)},
                       "ib5": {"name": "ib5", "kind": "local", "lanes_file": str(new_host)}}
        self.topology({"total": 20, "pi": 11, "minimax-cn": 11}, hosts=["box"])
        capacity = xs.fleet_health_capacity(self.home, fleet_hosts)
        self.assertEqual(capacity["status"], "alert")
        self.assertEqual(capacity["lane_total"], 15)
        self.assertEqual(capacity["by_host"], {"box": 7, "ib5": 8})
        rows = {row["limit"]: row for row in capacity["alerts"]}
        self.assertEqual(sorted(rows), ["minimax-cn", "pi"])
        self.assertEqual((rows["pi"]["limit_value"], rows["pi"]["lanes"], rows["pi"]["deficit"]),
                         (11, 15, 4))
        self.assertEqual((rows["minimax-cn"]["limit_value"], rows["minimax-cn"]["lanes"],
                          rows["minimax-cn"]["deficit"]), (11, 15, 4))

    def test_a_host_whose_lanes_cannot_be_read_is_reported_as_unverified(self):
        """A partial lane count is never presented as a verified fleet."""
        control = self.home / "lanes.json"
        control.write_text(json.dumps({"lanes": [
            {"name": "box-pi-%d" % i, "backend": "pi", "provider": "minimax-cn", "host": "box"}
            for i in range(7)]}), encoding="utf-8")
        previous = os.environ.get("OPENSWARM_LANES_FILE")
        os.environ["OPENSWARM_LANES_FILE"] = str(control)
        self.addCleanup(self.restore_lanes, previous)
        fleet_hosts = {"box": {"name": "box", "kind": "local", "lanes_file": str(control)},
                       "ib5": {"name": "ib5", "kind": "local",
                               "lanes_file": str(self.home / "missing-lanes.json")}}
        self.topology({"total": 20, "pi": 11, "minimax-cn": 11}, hosts=["box"])
        capacity = xs.fleet_health_capacity(self.home, fleet_hosts)
        self.assertEqual(capacity["status"], "unverified")
        self.assertFalse(capacity["verified"])
        self.assertEqual(capacity["alerts"], [])
        self.assertIn("未核实", capacity["message"])
        self.assertIn("ib5", capacity["error"])

    def test_the_state_file_carries_the_capacity_reconciliation(self):
        """The dispatcher reads the fleet-wide result from the health state file."""
        self.topology({"total": 4, "codex": 1, "host:worker-a": 2}, hosts=("worker-a", "worker-b"))
        self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        capacity = state["capacity"]
        self.assertEqual(capacity["status"], "alert")
        self.assertEqual(capacity["signature"], "codex:1/2")
        self.assertEqual(capacity["report"]["lane_total"], 2)
        self.assertEqual(capacity["report"]["by_host"], {"worker-a": 1, "worker-b": 1})

    def test_a_host_limit_below_its_lane_count_is_flagged(self):
        self.topology({"total": 4, "codex": 4, "host:worker-a": 0}, hosts=("worker-a",))
        report = self.health(hosts=("worker-a",))
        rows = {row["limit"]: row for row in report["capacity"]["alerts"]}
        self.assertEqual(sorted(rows), ["host:worker-a"])
        self.assertEqual(rows["host:worker-a"]["lanes"], 1)
        self.assertEqual(rows["host:worker-a"]["deficit"], 1)
        self.assertEqual(rows["host:worker-a"]["suggested"], 2)

    def test_sufficient_limits_report_no_capacity_alert(self):
        self.topology({"total": 4, "codex": 2, "host:worker-a": 1}, hosts=("worker-a",))
        report = self.health(hosts=("worker-a",))
        self.assertEqual(report["capacity"]["status"], "ok")
        self.assertEqual(report["capacity"]["alerts"], [])
        self.assertTrue(report["ok"])

    def test_a_missing_topology_is_reported_as_unverified(self):
        self.lanes(("worker-a",))
        report = self.health(hosts=("worker-a",))
        capacity = report["capacity"]
        self.assertEqual(capacity["status"], "unverified")
        self.assertFalse(capacity["verified"])
        self.assertEqual(capacity["alerts"], [])
        self.assertIn("未核实", capacity["message"])

    def test_the_capacity_alert_is_written_to_the_brain_log(self):
        log = self.brain_log()
        self.topology({"total": 4, "codex": 1, "host:worker-a": 2}, hosts=("worker-a", "worker-b"))
        self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        text = log.read_text(encoding="utf-8")
        self.assertIn("fleet-health", text)
        self.assertIn("codex", text)
        self.assertIn("deficit 1", text)

    def test_the_brain_log_is_not_filled_with_the_same_alert(self):
        log = self.brain_log()
        self.topology({"total": 4, "codex": 1, "host:worker-a": 2}, hosts=("worker-a", "worker-b"))
        self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)

    def test_brain_log_failure_does_not_suppress_retry(self):
        log = self.brain_log()
        missing = self.home / "missing" / "brain.log"
        os.environ["OPENSWARM_BRAIN_LOG"] = str(missing)
        self.topology({"total": 4, "codex": 1, "host:worker-a": 2}, hosts=("worker-a", "worker-b"))
        self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        self.assertFalse(missing.exists())
        os.environ["OPENSWARM_BRAIN_LOG"] = str(log)
        self.health(hosts=("worker-a", "worker-b"), lanes=("worker-a", "worker-b"))
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)

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
