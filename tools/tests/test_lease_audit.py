"""Offline fixtures for the lease audit: the findings, the health file and the dedup (Refs #1795).

The tool runs on fixtures inside a temporary directory: a fake lease store, a
fake lane state and a fake run directory.  The real lease store of the fleet
is never read, written or removed -- the tool only reads the leases, it never
grants, renews or releases one.

The contract asserted here:

* a card that was claimed twice (two leases at the same time) and a lease that
  expired and was never reclaimed are found, and a normal sample is quiet;
* the health file carries the numbers of the moment and the numbers of the
  rolling 24 hour window, and the two are not the same number;
* the same finding is reported once: the second round writes no new alert;
* the lease store is untouched by the audit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "lease-audit.py"
NOW = 1_800_000_000.0


def lease(issue, run_id, granted_at, ttl_s=600.0, lane="M1", requested_at=None):
    """One lease record, as the lease store writes it."""
    record = {"issue": issue, "run_id": run_id, "lane": lane,
              "granted_at": granted_at, "expires_at": granted_at + ttl_s, "ttl_s": ttl_s}
    if requested_at is not None:
        record["requested_at"] = requested_at
    return record


class LeaseAuditCliTests(unittest.TestCase):
    """The tool on fixtures: the findings, the health file, the dedup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lease-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "out"

    def write_leases(self, leases):
        path = self.root / "leases.json"
        path.write_text(json.dumps({"version": 1, "leases": leases}, indent=1), encoding="utf-8")
        return path

    def write_store(self, name, leases):
        """One lease store of another host, in the directory of the stores."""
        directory = self.root / "stores"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text(json.dumps({"version": 1, "leases": leases}, indent=1), encoding="utf-8")
        return path

    def write_state(self, state):
        path = self.root / "state.json"
        path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        return path

    def write_run(self, run_id, status):
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")

    def audit(self, leases_path, state_path=None, now=NOW):
        command = [sys.executable, str(SCRIPT), "--leases", str(leases_path),
                   "--state", str(state_path or self.root / "missing-state.json"),
                   "--runs", str(self.root / "missing-runs"),
                   "--out", str(self.out), "--now", str(now), "--json"]
        done = subprocess.run(command, capture_output=True, text=True)
        report = json.loads(done.stdout)
        return done, report

    def test_the_double_claim_and_the_ghost_are_found(self):
        # two hosts, two lease stores: the card 1795 was claimed by both of them
        self.write_store("leases.json", {
            "1795": lease(1795, "run-a", NOW - 100, lane="M1", requested_at=NOW - 160),
            "1800": lease(1800, "run-c", NOW - 5000, ttl_s=600.0, lane="M3"),
        })
        self.write_store("leases-host2.json", {
            "1795": lease(1795, "run-b", NOW - 50, lane="M2"),
        })
        stores = self.root / "stores"

        done, report = self.audit(stores)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        kinds = sorted(alert["kind"] for alert in report["alerts"])
        self.assertEqual(kinds, ["dual_claim", "expired_lease"])
        self.assertEqual(report["leases"]["ghost_leases"], 1)
        self.assertEqual(report["leases"]["dual_claims"], 1)
        self.assertEqual(report["leases"]["dual_executions"], 0)

    def test_a_normal_sample_is_quiet(self):
        leases = self.write_leases({
            "1795": lease(1795, "run-a", NOW - 100, lane="M1", requested_at=NOW - 160),
        })
        self.write_state({"M1": {"issue": 1795, "run": "run-a", "host": "gocc"}})
        self.write_run("run-a", {"state": "running", "started": NOW - 100})

        done, report = self.audit(leases, self.root / "state.json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(report["alerts"], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["leases"]["ghost_leases"], 0)
        self.assertEqual(report["leases"]["wait_avg_s"], 60.0)
        self.assertEqual(report["leases"]["wait_max_s"], 60.0)

    def test_the_health_file_carries_the_snapshot_and_the_window(self):
        leases = self.write_leases({
            "1800": lease(1800, "run-c", NOW - 5000, ttl_s=600.0, lane="M3"),
        })

        self.audit(leases)
        health = json.loads((self.out / "lease-health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["leases"]["ghost_leases"], 1)
        self.assertEqual(health["window_24h"]["ghost_leases"], 1)
        self.assertEqual(health["window_24h"]["window_s"], 86400.0)
        self.assertEqual(health["window_24h"]["rounds"], 1)
        self.assertIn("at", health)
        self.assertIn("updated_at", health)

    def test_the_same_finding_is_written_once(self):
        leases = self.write_leases({
            "1800": lease(1800, "run-c", NOW - 5000, ttl_s=600.0, lane="M3"),
        })

        self.audit(leases)
        log = self.out / "lease-audit-alerts.jsonl"
        self.assertEqual(len(log.read_text(encoding="utf-8").strip().splitlines()), 1)

        # the next round finds the same ghost: no new alert, no flood
        done, report = self.audit(leases, now=NOW + 60)
        self.assertEqual(report["new_alerts"], [])
        self.assertEqual(len(log.read_text(encoding="utf-8").strip().splitlines()), 1)
        state = json.loads((self.out / "lease-audit-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["alerts"]["expired_lease:1800:run-c"]["count"], 2)

    def test_the_lease_store_is_not_touched(self):
        leases = self.write_leases({
            "1795": lease(1795, "run-a", NOW - 100, lane="M1", requested_at=NOW - 160),
            "1800": lease(1800, "run-c", NOW - 5000, ttl_s=600.0, lane="M3"),
        })
        before = leases.read_text(encoding="utf-8")
        stamp = leases.stat().st_mtime

        self.audit(leases)

        self.assertEqual(leases.read_text(encoding="utf-8"), before)
        self.assertEqual(leases.stat().st_mtime, stamp)

    def test_the_stores_of_the_home_are_audited_without_arguments(self):
        # the home of the fleet holds one store per host: all of them are read
        previous = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.root)
        self.addCleanup(self.restore_home, previous)
        (self.root / "leases.json").write_text(json.dumps({"version": 1, "leases": {
            "1795": lease(1795, "run-a", NOW - 100, lane="M1"),
        }}), encoding="utf-8")
        (self.root / "leases-host2.json").write_text(json.dumps({"version": 1, "leases": {
            "1795": lease(1795, "run-b", NOW - 50, lane="M2"),
        }}), encoding="utf-8")

        command = [sys.executable, str(SCRIPT), "--now", str(NOW), "--json"]
        done = subprocess.run(command, capture_output=True, text=True)
        report = json.loads(done.stdout)

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertEqual([alert["kind"] for alert in report["alerts"]], ["dual_claim"])
        # the health file of the round lands in the home of the fleet
        health = json.loads((self.root / "lease-health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["leases"]["dual_claims"], 1)

    def restore_home(self, previous):
        if previous is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
