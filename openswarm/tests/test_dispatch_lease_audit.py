"""`openswarm/lib/dispatch/lease_audit.py`: the lease audit and its health numbers (Refs #1795, Epic #1785).

The lease system (P2-1/P2-2) needs an outside look: a card that was claimed
twice, a card two runs work at the same time and a lease that expired and was
never reclaimed can not be seen from the lease file alone.  This suite builds
the three cases and asserts that the audit finds exactly them -- and that a
normal sample stays quiet.

The module is loaded by path: `openswarm/lib/dispatch/` is a namespace package
without an `__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import json
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
AUDIT_MODULE = HERE.parent / "lib" / "dispatch" / "lease_audit.py"
LEASE_MODULE = HERE.parent / "lib" / "dispatch" / "lease.py"

spec = importlib.util.spec_from_file_location("dispatch_lease_audit", AUDIT_MODULE)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)

lease_spec = importlib.util.spec_from_file_location("dispatch_lease", LEASE_MODULE)
lease_module = importlib.util.module_from_spec(lease_spec)
lease_spec.loader.exec_module(lease_module)

NOW = 1_800_000_000.0


def lease(issue, run_id, granted_at, ttl_s=600.0, lane="M1", requested_at=None, source=None):
    """One lease record, as the lease store writes it."""
    record = {"issue": issue, "run_id": run_id, "lane": lane,
              "granted_at": granted_at, "expires_at": granted_at + ttl_s, "ttl_s": ttl_s}
    if requested_at is not None:
        record["requested_at"] = requested_at
    if source is not None:
        record["source"] = source
    return record


def run(run_id, issue, state="running", started=None, finished=None, commit=None, branch=None, lane=None):
    """One run record."""
    record = {"run_id": run_id, "issue": issue, "state": state}
    if started is not None:
        record["started"] = started
    if finished is not None:
        record["finished"] = finished
    if commit is not None:
        record["commit"] = commit
    if branch is not None:
        record["branch"] = branch
    if lane is not None:
        record["lane"] = lane
    return record


def kinds(alerts):
    """The kinds of the findings, sorted."""
    return sorted(alert["kind"] for alert in alerts)


class AuditTests(unittest.TestCase):
    """The three failures the audit has to see."""

    def test_a_normal_sample_is_quiet(self):
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 10, requested_at=NOW - 100)],
            [run("run-a", 1795, started=NOW - 10)],
            NOW)

        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["metrics"]["ghost_leases"], 0)
        self.assertEqual(result["metrics"]["dual_claims"], 0)
        self.assertEqual(result["metrics"]["dual_executions"], 0)
        self.assertEqual(result["metrics"]["active_leases"], 1)
        self.assertTrue(result["metrics"]["ok"])

    def test_two_leases_of_one_card_are_a_double_claim(self):
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 100, lane="M1"),
             lease(1795, "run-b", NOW - 50, lane="M2")],
            [],
            NOW)

        self.assertEqual(kinds(result["alerts"]), ["dual_claim"])
        alert = result["alerts"][0]
        self.assertEqual(alert["issue"], "1795")
        self.assertEqual(alert["severity"], "high")
        self.assertIn("run-a", alert["summary"])
        self.assertIn("run-b", alert["summary"])
        self.assertEqual(alert["fingerprint"], "dual_claim:1795:run-a|run-b")
        self.assertEqual(result["metrics"]["dual_claims"], 1)

    def test_a_lease_of_one_card_that_was_never_reclaimed_is_a_ghost(self):
        result = audit_module.audit(
            [lease(1800, "run-c", NOW - 5000, ttl_s=600.0)],
            [],
            NOW)

        self.assertEqual(kinds(result["alerts"]), ["expired_lease"])
        alert = result["alerts"][0]
        self.assertEqual(alert["issue"], "1800")
        self.assertEqual(alert["fingerprint"], "expired_lease:1800:run-c")
        self.assertEqual(result["metrics"]["ghost_leases"], 1)
        self.assertEqual(result["metrics"]["expired_leases"], 1)

    def test_an_expired_lease_of_a_live_run_is_no_ghost(self):
        # the lease is over, but the run still lives: the card must not be handed out again
        result = audit_module.audit(
            [lease(1800, "run-c", NOW - 5000, ttl_s=600.0)],
            [run("run-c", 1800, state="running")],
            NOW)

        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["metrics"]["ghost_leases"], 0)
        self.assertEqual(result["metrics"]["expired_held_leases"], 1)

    def test_a_lane_state_entry_does_not_prove_that_the_run_is_alive(self):
        # the dispatcher still files the card on its lane, but nothing says the run lives
        result = audit_module.audit(
            [lease(1800, "run-c", NOW - 5000, ttl_s=600.0)],
            [{"run_id": "run-c", "issue": 1800, "lane": "M3"}],
            NOW)

        self.assertEqual(kinds(result["alerts"]), ["expired_lease"])
        self.assertEqual(result["alerts"][0]["detail"]["confidence"], "suspected")

    def test_the_ghost_of_a_finished_run_is_confirmed(self):
        result = audit_module.audit(
            [lease(1800, "run-c", NOW - 5000, ttl_s=600.0)],
            [run("run-c", 1800, state="finished", finished=NOW - 1000)],
            NOW)

        self.assertEqual(kinds(result["alerts"]), ["expired_lease"])
        self.assertEqual(result["alerts"][0]["detail"]["confidence"], "confirmed")

    def test_two_active_runs_of_one_card_are_a_double_execution(self):
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 100)],
            [run("run-a", 1795, lane="M1"), run("run-b", 1795, lane="M2")],
            NOW)

        self.assertIn("dual_execution", kinds(result["alerts"]))
        alert = [row for row in result["alerts"] if row["kind"] == "dual_execution"][0]
        self.assertEqual(alert["severity"], "critical")
        self.assertEqual(alert["detail"]["reason"], "concurrent")
        self.assertEqual(result["metrics"]["dual_executions"], 1)

    def test_two_runs_that_wrote_the_same_card_back_are_a_double_execution(self):
        result = audit_module.audit(
            [],
            [run("run-a", 1795, state="finished", commit="abc123"),
             run("run-b", 1795, state="finished", commit="abc123")],
            NOW)

        self.assertEqual(kinds(result["alerts"]), ["dual_execution"])
        alert = result["alerts"][0]
        self.assertEqual(alert["detail"]["reason"], "write_back")
        self.assertEqual(alert["detail"]["shared"], "commit")

    def test_the_same_run_holding_two_leases_is_a_renewal_not_a_double_claim(self):
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 100, lane="M1"),
             lease(1795, "run-a", NOW - 50, lane="M1")],
            [run("run-a", 1795)],
            NOW)

        self.assertEqual(result["alerts"], [])

    def test_two_leases_of_one_card_that_do_not_overlap_are_no_double_claim(self):
        # the card was dispatched, finished and dispatched again: that is normal
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 5000, ttl_s=600.0),
             lease(1795, "run-b", NOW - 100, ttl_s=600.0)],
            [run("run-b", 1795)],
            NOW)

        self.assertEqual([alert for alert in result["alerts"]
                          if alert["kind"] == "dual_claim"], [])

    def test_a_lease_that_ends_when_the_next_one_starts_is_no_double_claim(self):
        # the card was handed out again the moment the first lease ran out
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 600, ttl_s=600.0),
             lease(1795, "run-b", NOW, ttl_s=600.0)],
            [run("run-b", 1795)],
            NOW)

        self.assertEqual([alert for alert in result["alerts"]
                          if alert["kind"] == "dual_claim"], [])

    def test_two_runs_of_one_card_on_the_same_branch_are_no_double_execution(self):
        # the card was dispatched to the same lane twice in a row: that is normal
        result = audit_module.audit(
            [],
            [run("run-a", 1795, state="finished", commit="aaa111", branch="fleet/M1"),
             run("run-b", 1795, state="finished", commit="bbb222", branch="fleet/M1")],
            NOW)

        self.assertEqual(result["alerts"], [])


class ReaderTests(unittest.TestCase):
    """The readers of the stores: the tool and the health report read the same files."""

    def test_every_lease_store_of_a_directory_is_read(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-lease-audit-") as tmp:
            root = pathlib.Path(tmp)
            (root / "leases.json").write_text(json.dumps(
                {"version": 1, "leases": {"1795": lease(1795, "run-a", NOW - 10)}}), encoding="utf-8")
            (root / "leases-host2.json").write_text(json.dumps(
                {"version": 1, "leases": {"1800": lease(1800, "run-c", NOW - 10)}}), encoding="utf-8")
            (root / "lease-health.json").write_text("{}", encoding="utf-8")

            records = audit_module.read_leases(root)

            self.assertEqual(sorted(str(row["issue"]) for row in records), ["1795", "1800"])
            self.assertTrue(all(row["source"] for row in records))

    def test_a_lane_state_and_a_run_directory_are_read(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-lease-audit-") as tmp:
            root = pathlib.Path(tmp)
            (root / "state.json").write_text(json.dumps(
                {"M1": {"issue": 1795, "run": "run-a"}, "_done": [1, 2]}), encoding="utf-8")
            run_dir = root / "runs" / "run-a"
            run_dir.mkdir(parents=True)
            (run_dir / "status.json").write_text(json.dumps(
                {"state": "running", "issue": 1795}), encoding="utf-8")

            lanes = audit_module.read_lane_state(root / "state.json")
            runs = audit_module.read_runs(root / "runs")

            self.assertEqual([row["run"] for row in lanes], ["run-a"])
            self.assertEqual([row["run_id"] for row in runs], ["run-a"])
            self.assertEqual(lanes[0]["lane"], "M1")


class HealthNumberTests(unittest.TestCase):
    """The health numbers: the snapshot of the moment and the rolling window."""

    def test_the_wait_numbers_match_the_sample(self):
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 10, requested_at=NOW - 70),
             lease(1796, "run-b", NOW - 10, requested_at=NOW - 130),
             lease(1797, "run-c", NOW - 10)],
            [],
            NOW)

        metrics = result["metrics"]
        self.assertEqual(metrics["wait_samples"], 2)
        self.assertEqual(metrics["wait_unknown"], 1)
        self.assertEqual(metrics["wait_avg_s"], 90.0)
        self.assertEqual(metrics["wait_max_s"], 120.0)
        self.assertEqual(metrics["wait_min_s"], 60.0)

    def test_the_health_report_carries_the_snapshot_and_the_window(self):
        result = audit_module.audit(
            [lease(1795, "run-a", NOW - 10, requested_at=NOW - 70),
             lease(1800, "run-c", NOW - 5000, ttl_s=600.0)],
            [],
            NOW)
        state = audit_module.apply_round({}, result, NOW)
        report = audit_module.health_report(result, state, NOW, at_label="2026-11-03 04:26:00")

        self.assertEqual(report["at"], "2026-11-03 04:26:00")
        self.assertEqual(report["leases"]["ghost_leases"], 1)
        self.assertEqual(report["window_24h"]["ghost_leases"], 1)
        self.assertEqual(report["window_24h"]["window_s"], 86400.0)
        self.assertFalse(report["ok"])

    def test_the_window_counts_the_findings_that_appeared_inside_it(self):
        old = audit_module.audit([lease(1800, "run-c", NOW - 5000, ttl_s=600.0)], [], NOW)
        state = audit_module.apply_round({}, old, NOW - 90000)
        fresh = audit_module.audit([lease(1801, "run-d", NOW - 5000, ttl_s=600.0)], [], NOW)
        state = audit_module.apply_round(state, fresh, NOW)

        window = audit_module.window_metrics(state, NOW)
        # the ghost of two days ago is not an event of the last day ...
        self.assertEqual(window["ghost_leases"], 1)
        # ... but the live snapshot still sees the one that is there now
        live = audit_module.audit([lease(1800, "run-c", NOW - 5000, ttl_s=600.0),
                                   lease(1801, "run-d", NOW - 5000, ttl_s=600.0)], [], NOW)
        self.assertEqual(live["metrics"]["ghost_leases"], 2)

    def test_a_finding_that_is_gone_falls_out_of_the_window(self):
        found = audit_module.audit([lease(1800, "run-c", NOW - 5000, ttl_s=600.0)], [], NOW)
        state = audit_module.apply_round({}, found, NOW)
        self.assertEqual(len(state["alerts"]), 1)

        gone = audit_module.audit([], [], NOW)
        state = audit_module.apply_round(state, gone, NOW + 10)
        # it is still there: the window has to count the round it happened in
        self.assertEqual(len(state["alerts"]), 1)
        self.assertEqual(audit_module.window_metrics(state, NOW + 10)["ghost_leases"], 1)

        # two days later it is forgotten
        state = audit_module.apply_round(state, gone, NOW + 200000)
        self.assertEqual(len(state["alerts"]), 0)

    def test_the_wait_samples_of_the_window_survive_the_round(self):
        first = audit_module.audit([lease(1795, "run-a", NOW - 10, requested_at=NOW - 70)], [], NOW)
        state = audit_module.apply_round({}, first, NOW)
        # the lease is gone in the next round: the sample of the wait stays
        second = audit_module.audit([], [], NOW + 10)
        state = audit_module.apply_round(state, second, NOW + 10)

        window = audit_module.window_metrics(state, NOW + 10)
        self.assertEqual(window["wait_samples"], 1)
        self.assertEqual(window["wait_avg_s"], 60.0)
        self.assertEqual(window["wait_max_s"], 60.0)


class DedupeTests(unittest.TestCase):
    """The same anomaly must not be reported twice."""

    def test_the_same_anomaly_is_new_only_once(self):
        leases = [lease(1800, "run-c", NOW - 5000, ttl_s=600.0)]
        result = audit_module.audit(leases, [], NOW)

        state = audit_module.apply_round({}, result, NOW)
        self.assertEqual(len(state["new_alerts"]), 1)
        self.assertEqual(state["alerts"]["expired_lease:1800:run-c"]["count"], 1)

        again = audit_module.audit(leases, [], NOW + 60)
        state = audit_module.apply_round(state, again, NOW + 60)
        self.assertEqual(state["new_alerts"], [])
        self.assertEqual(state["alerts"]["expired_lease:1800:run-c"]["count"], 2)
        self.assertEqual(state["alerts"]["expired_lease:1800:run-c"]["first_seen"], NOW)


class ReadOnlyTests(unittest.TestCase):
    """The audit must never become the second writer of the lease file."""

    def test_the_lease_file_is_not_touched_by_the_audit(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-lease-audit-") as tmp:
            store = lease_module.LeaseStore(tmp)
            store.grant(1795, "run-a", "M1", 600, NOW - 10)
            path = pathlib.Path(tmp) / "leases.json"
            before = path.read_text(encoding="utf-8")
            stamp = path.stat().st_mtime

            result = audit_module.audit(store.load(), [], NOW)
            state = audit_module.apply_round({}, result, NOW)

            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(path.stat().st_mtime, stamp)
            self.assertEqual(json.loads(before)["leases"]["1795"]["run_id"], "run-a")
            self.assertEqual(state["version"], 1)


if __name__ == "__main__":
    unittest.main()
