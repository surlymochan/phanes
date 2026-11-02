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
        self.assertEqual(sorted(report), ["applied", "hosts", "leases", "ok", "status", "thresholds"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["status"], "hard")
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 95)
        row = report["hosts"][0]
        self.assertEqual(sorted(row), ["action", "decision", "error", "host", "metrics", "paused",
                                       "reason", "status", "triggers"])
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

    def test_the_lease_numbers_are_part_of_the_health_report(self):
        # a lease that expired and was never reclaimed: a ghost lease
        (self.home / "leases.json").write_text(json.dumps({"version": 1, "leases": {
            "1795": {"issue": 1795, "run_id": "run-a", "lane": "M1",
                     "granted_at": self.now - 5000, "expires_at": self.now - 4400, "ttl_s": 600},
        }}), encoding="utf-8")

        report = self.health()
        leases = report["leases"]
        self.assertEqual(leases["metrics"]["ghost_leases"], 1)
        self.assertEqual(leases["metrics"]["dual_claims"], 0)
        self.assertEqual(len(leases["alerts"]), 1)
        self.assertEqual(leases["alerts"][0]["kind"], "expired_lease")
        self.assertEqual(leases["window_24h"]["window_s"], 86400.0)
        # the host health is not mixed with the lease verdict
        self.assertTrue(report["ok"])
        self.assertFalse(leases["ok"])

    def test_without_a_lease_store_the_health_report_has_no_findings(self):
        report = self.health()
        self.assertEqual(report["leases"]["metrics"]["ghost_leases"], 0)
        self.assertEqual(report["leases"]["alerts"], [])
        self.assertTrue(report["leases"]["ok"])

    def test_the_lease_numbers_are_written_into_the_health_state_file(self):
        (self.home / "leases.json").write_text(json.dumps({"version": 1, "leases": {
            "1795": {"issue": 1795, "run_id": "run-a", "lane": "M1",
                     "granted_at": self.now - 5000, "expires_at": self.now - 4400, "ttl_s": 600},
        }}), encoding="utf-8")

        report = self.health()

        state = json.loads((self.home / "fleet-health.json").read_text(encoding="utf-8"))
        self.assertEqual(state["leases"]["metrics"]["ghost_leases"], 1)
        self.assertEqual(state["leases"]["metrics"], report["leases"]["metrics"])
        self.assertEqual(state["leases"]["alerts"][0]["kind"], "expired_lease")

    def test_every_lease_store_of_the_home_is_read(self):
        # two hosts, two lease stores: the card 1795 was claimed by both of them
        (self.home / "leases.json").write_text(json.dumps({"version": 1, "leases": {
            "1795": {"issue": 1795, "run_id": "run-a", "lane": "M1",
                     "granted_at": self.now - 100, "expires_at": self.now + 500, "ttl_s": 600},
        }}), encoding="utf-8")
        (self.home / "leases-host2.json").write_text(json.dumps({"version": 1, "leases": {
            "1795": {"issue": 1795, "run_id": "run-b", "lane": "M2",
                     "granted_at": self.now - 50, "expires_at": self.now + 550, "ttl_s": 600},
        }}), encoding="utf-8")

        report = self.health()

        self.assertEqual(report["leases"]["metrics"]["dual_claims"], 1)
        self.assertEqual([alert["kind"] for alert in report["leases"]["alerts"]],
                         ["dual_claim"])


if __name__ == "__main__":
    unittest.main()
