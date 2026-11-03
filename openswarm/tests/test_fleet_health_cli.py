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

    def runner(self, inode=10, failing=(), disk=DISK):
        """A fake probe runner: fixed outputs per command, transport failures per host."""
        outputs = {"df -P /": disk, "df -Pi /": inode_output(inode), "free -m": MEMORY}

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
               policy=None, lanes=("worker-a",), now=None):
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
                                         now=self.now if now is None else now, home=self.home)
        self.assertEqual(json.loads(output.getvalue()) if json_out else {}, report if json_out else {})
        return report

    def pauses(self, now=None):
        return {row["scope"]: row for row in self.store.active(self.now if now is None else now)}

    def marker(self, host="worker-a"):
        """The host marker of the health check (the health marker dir), or None."""
        path = self.home / "health" / ("PAUSE-HOST-" + host)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def shared_marker(self, host="worker-a"):
        """The host marker of the shared pause dir (another program's), or None."""
        path = self.home / ("PAUSE-HOST-" + host)
        return path.read_text(encoding="utf-8") if path.exists() else None

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
        self.assertIsNone(record["expires_at"])

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
        # The operator's pause keeps the host scope; the health pause takes a
        # scope of its own, so neither record overwrites the other.
        self.store.pause("host:worker-a", reason="operator: maintenance", now=self.now)
        report = self.health(runner=self.runner(inode=96), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertEqual(self.pauses()["host:worker-a"]["reason"], "operator: maintenance")
        record = self.pauses()["host:worker-a@health"]
        self.assertTrue(record["reason"].startswith("auto:health"))
        self.assertIsNone(record["expires_at"])
        self.assertTrue(self.marker())

    def test_the_health_hold_is_reported_after_the_manual_pause_is_removed(self):
        # The operator pauses the host and the health check finds the host
        # sick: the health check keeps a hold of its own.  The operator
        # removes their pause: the pause store still reports the host as
        # paused until the host recovers (Refs #1367).
        self.store.pause("host:worker-a", reason="operator: maintenance", now=self.now)
        report = self.health(runner=self.runner(inode=96), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertEqual(self.pauses()["host:worker-a"]["reason"], "operator: maintenance")
        self.assertTrue(self.pauses()["host:worker-a@health"]["reason"].startswith("auto:health"))
        # The operator releases their own pause: the health hold stays.
        self.store.resume("host:worker-a")
        self.assertNotIn("host:worker-a", self.pauses())
        self.assertTrue(self.store.is_paused(host="worker-a", now=self.now))
        self.assertTrue(self.marker())
        # The host recovered: the health check releases its own hold.
        report = self.health(runner=self.runner(inode=91), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(self.store.is_paused(host="worker-a", now=self.now))
        self.assertNotIn("host:worker-a@health", self.pauses())
        self.assertIsNone(self.marker())

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
        self.assertEqual(sorted(report), ["applied", "hosts", "ok", "status", "thresholds"])
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

    # ------------------------------------------------- the host-level pause markers

    def test_apply_writes_the_host_pause_marker(self):
        report = self.health(runner=self.runner(inode=96), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        marker = self.marker()
        self.assertIsNotNone(marker)
        self.assertTrue(marker.startswith("auto:health"), marker)
        self.assertIn("inodes", marker)

    def test_the_marker_is_released_when_the_host_recovers(self):
        self.health(runner=self.runner(inode=96), apply=True)
        report = self.health(runner=self.runner(inode=91), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertIsNone(self.marker())

    def test_a_marker_written_by_another_health_check_is_kept(self):
        # The box health check holds a host with the same marker file and no
        # pause-store record: the hold belongs to that check, so the fleet
        # health neither rewrites nor releases it.
        (self.home / "PAUSE-HOST-worker-a").write_text("auto: disk 97% used\n", encoding="utf-8")
        report = self.health(runner=self.runner(), apply=True)
        self.assertEqual(report["hosts"][0]["paused"], True)
        self.assertEqual(report["hosts"][0]["action"], "kept")
        self.assertEqual(self.shared_marker(), "auto: disk 97% used\n")
        self.assertEqual(self.pauses(), {})
        self.assertIsNone(self.marker())

    def test_a_manual_marker_is_never_cleared_or_rewritten(self):
        marker = self.home / "PAUSE-HOST-worker-a"
        marker.write_text("operator hold\n", encoding="utf-8")
        report = self.health(runner=self.runner(inode=96), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertEqual(marker.read_text(encoding="utf-8"), "operator hold\n")
        # The health check holds the host on its own, next to the marker of
        # the operator: the hold of one program never replaces the other.
        self.assertTrue(self.marker().startswith("auto:health"))
        self.assertTrue(self.pauses()["host:worker-a"]["reason"].startswith("auto:health"))

    def test_a_foreign_marker_does_not_hide_a_health_fault(self):
        # The host is held by another program and its disk is over the pause
        # watermark: the health check takes a hold of its own.
        (self.home / "PAUSE-HOST-worker-a").write_text("operator hold\n", encoding="utf-8")
        report = self.health(runner=self.runner(inode=96), apply=True)
        row = report["hosts"][0]
        self.assertEqual(row["status"], "hard")
        self.assertEqual(row["decision"], "pause")
        self.assertEqual(row["action"], "paused")
        self.assertEqual(self.shared_marker(), "operator hold\n")
        self.assertTrue(self.marker().startswith("auto:health"))
        self.assertTrue(self.pauses()["host:worker-a"]["reason"].startswith("auto:health"))

    def test_the_health_hold_survives_the_removal_of_a_foreign_marker(self):
        # The operator removes their own marker while the host is still sick:
        # the health hold keeps the host out of the dispatch until it recovers.
        (self.home / "PAUSE-HOST-worker-a").write_text("operator hold\n", encoding="utf-8")
        self.health(runner=self.runner(inode=96), apply=True)
        (self.home / "PAUSE-HOST-worker-a").unlink()
        self.assertIsNone(self.shared_marker())
        # The health hold is still there (no health check ran in between).
        self.assertTrue(self.marker().startswith("auto:health"))
        self.assertIn("host:worker-a", self.pauses())
        # The host recovered: the health hold ends, the operator's hold is
        # not touched (it is already gone here).
        report = self.health(runner=self.runner(inode=91), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertIsNone(self.marker())
        self.assertEqual(self.pauses(), {})

    def test_the_health_hold_of_an_operator_pause_is_released_on_recovery(self):
        self.store.pause("host:worker-a", reason="operator: maintenance", now=self.now)
        self.health(runner=self.runner(inode=96), apply=True)
        self.assertIn("host:worker-a@health", self.pauses())
        report = self.health(runner=self.runner(inode=91), apply=True)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertNotIn("host:worker-a@health", self.pauses())
        # The operator's pause stays untouched.
        self.assertEqual(self.pauses()["host:worker-a"]["reason"], "operator: maintenance")
        self.assertIsNone(self.marker())

    def test_the_marker_directory_can_be_moved_with_the_pause_dir_env(self):
        # The lane autodispatcher reads the host markers from the same place:
        # OPENSWARM_PAUSE_DIR names the directory for both programs.
        with tempfile.TemporaryDirectory() as directory:
            previous = os.environ.get("OPENSWARM_PAUSE_DIR")
            os.environ["OPENSWARM_PAUSE_DIR"] = directory
            try:
                report = self.health(runner=self.runner(inode=96), apply=True)
                self.assertEqual(report["hosts"][0]["action"], "paused")
                self.assertTrue(pathlib.Path(directory, "health", "PAUSE-HOST-worker-a").exists())
                self.assertIsNone(self.marker())
            finally:
                if previous is None:
                    os.environ.pop("OPENSWARM_PAUSE_DIR", None)
                else:
                    os.environ["OPENSWARM_PAUSE_DIR"] = previous

    # ------------------------------------------------- the pause outlives its TTL

    def test_the_pause_is_renewed_while_the_host_did_not_recover(self):
        self.health(runner=self.runner(inode=96), apply=True)
        later = self.now + 3600  # the host is still sick at the next check
        report = self.health(runner=self.runner(inode=96), apply=True, now=later)
        row = report["hosts"][0]
        self.assertEqual(row["decision"], "none")
        self.assertEqual(row["status"], "hard")
        self.assertEqual(row["action"], "renewed")
        record = self.pauses(now=later)["host:worker-a"]
        self.assertIsNone(record["expires_at"])
        self.assertTrue(self.marker())

    def test_the_pause_does_not_expire_before_the_host_recovers(self):
        # The health check owns the pause: the record has no expiry, so the
        # pause cannot lapse while the host is still sick - not even when the
        # health check does not run again.
        self.health(runner=self.runner(inode=96), apply=True)
        later = self.now + 30 * 24 * 3600
        self.assertTrue(self.store.is_paused(host="worker-a", now=later))
        self.assertIn("host:worker-a", self.pauses(now=later))
        self.assertTrue(self.marker())
        # The host recovered: the pause ends.
        report = self.health(runner=self.runner(inode=91), apply=True, now=later)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(self.store.is_paused(host="worker-a", now=later))
        self.assertEqual(self.pauses(now=later), {})
        self.assertIsNone(self.marker())

    def test_the_pause_ends_when_the_host_recovers_after_the_ttl(self):
        self.health(runner=self.runner(inode=96), apply=True)
        later = self.now + 3600
        self.health(runner=self.runner(inode=96), apply=True, now=later)
        recovered = later + 3600
        report = self.health(runner=self.runner(inode=91), apply=True, now=recovered)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertEqual(self.pauses(now=recovered), {})
        self.assertIsNone(self.marker())

    # ------------------------------------------------- the thresholds in the policy

    def test_the_shipped_policy_carries_the_health_thresholds(self):
        policy = json.loads((ROOT / "policy.json").read_text(encoding="utf-8"))
        health = policy["resources"]["health"]
        self.assertEqual(health["disk_pause_pct"], 95)
        self.assertEqual(health["mem_pause_mb"], 512)
        self.assertEqual(health["ssh_fail_pause_streak"], 3)
        limits = xs.dispatch_health_module().thresholds_from_policy(policy)
        self.assertEqual(limits["inode_resume_pct"], health["inode_resume_pct"])

    def test_a_policy_threshold_that_is_not_a_number_falls_back_to_the_default(self):
        policy = {"resources": {"health": {"inode_pause_pct": "96"}}}
        report = self.health(runner=self.runner(inode=96), policy=policy)
        self.assertEqual(report["thresholds"]["inode_pause_pct"], 95)
        self.assertEqual(report["hosts"][0]["status"], "hard")


if __name__ == "__main__":
    unittest.main()
