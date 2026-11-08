"""`openswarm/lib/dispatch/lease_sweep.py`: the decisions of the in-progress leak sweep.

The sweep is lease based (Refs #1364): the lease is taken before the label, the
lane heartbeat renews it, and an expired lease is reclaimed only when the run
it points at is gone.  A label written before the upgrade holds no lease and is
reclaimed with the original grace rule.

The modules are loaded by path: `openswarm/lib/dispatch/` is a namespace
package without an `__init__.py`, so they must not be imported as a package.
"""
import importlib.util
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
LIB = HERE.parent / "lib" / "dispatch"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lease = load("dispatch_lease", LIB / "lease.py")
sweep = load("dispatch_lease_sweep", LIB / "lease_sweep.py")


class LeaseSweepDecisionTests(unittest.TestCase):
    """One decision per labelled issue, with the reason it was kept or reclaimed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lease-sweep-")
        self.addCleanup(self.tmp.cleanup)
        self.store = lease.LeaseStore(self.tmp.name)
        self.label = {"number": 1400, "updatedAt": 1000.0}

    def decide(self, labelled=None, **kwargs):
        decisions = sweep.decide(
            [self.label] if labelled is None else labelled,
            kwargs.pop("leases", None),
            **kwargs,
        )
        return decisions[0]

    def test_a_live_lease_keeps_the_label(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        decision = self.decide(leases=self.store.load(), now=1400.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "lease_alive"))

    def test_an_expired_lease_is_reclaimed_when_the_run_is_gone(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        decision = self.decide(leases=self.store.load(), now=1600.0)

        self.assertEqual((decision["action"], decision["reason"]), ("reclaim", "lease_expired"))

    def test_an_expired_lease_is_kept_while_the_run_still_lives(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        decision = self.decide(leases=self.store.load(), running_runs=["run-1"], now=1600.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "run_alive"))

    def test_a_pending_lease_is_kept_while_the_dispatch_is_in_flight(self):
        """The lease is taken before the dispatch: the issue is held until the run exists."""
        self.store.grant(1400, "", "M12", 600, 1000.0)

        held = self.decide(leases=self.store.load(), held=[1400], now=1600.0)
        free = self.decide(leases=self.store.load(), now=1600.0)

        self.assertEqual((held["action"], held["reason"]), ("keep", "held"))
        self.assertEqual((free["action"], free["reason"]), ("reclaim", "lease_expired"))

    def test_a_label_without_a_lease_is_reclaimed_after_the_grace(self):
        """The labels written before the upgrade hold no lease: the grace rule applies."""
        decision = self.decide(now=1000.0 + 901.0)

        self.assertEqual((decision["action"], decision["reason"]), ("reclaim", "no_lease"))

    def test_a_fresh_label_without_a_lease_is_kept(self):
        decision = self.decide(now=1000.0 + 899.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "within_grace"))

    def test_a_label_without_a_lease_is_kept_while_a_lane_holds_the_issue(self):
        decision = self.decide(held=[1400], now=1000.0 + 901.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "held"))

    def test_a_label_without_a_timestamp_is_kept(self):
        decision = self.decide(labelled=[{"number": 1400}], now=1000.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "no_timestamp"))

    def test_a_label_from_the_future_is_kept(self):
        decision = self.decide(labelled=[{"number": 1400, "updatedAt": 2000.0}], now=1000.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "clock_rollback"))

    def test_an_unreadable_row_is_kept(self):
        decision = self.decide(labelled=[{}], now=1000.0)

        self.assertEqual((decision["action"], decision["reason"]), ("keep", "invalid_issue"))

    def test_a_github_timestamp_is_read_as_utc(self):
        decision = self.decide(labelled=[{"number": 1400, "updatedAt": "1970-01-01T01:00:00Z"}], now=4501.0)

        self.assertEqual((decision["action"], decision["reason"]), ("reclaim", "no_lease"))

    def test_reclaimable_lists_the_issues_only(self):
        self.store.grant(1400, "run-1", "M12", 1200, 1000.0)
        labels = [{"number": 1400, "updatedAt": 1000.0}, {"number": 1401, "updatedAt": 100.0}]

        issues = sweep.reclaimable(labels, self.store.load(), now=1600.0)

        # 1400 keeps its live lease, 1401 has no lease and is older than the grace
        self.assertEqual(issues, [1401])


class LeaseStorePendingLeaseTests(unittest.TestCase):
    """The lease may be taken before the run exists (Refs #1364)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lease-pending-")
        self.addCleanup(self.tmp.cleanup)
        self.store = lease.LeaseStore(self.tmp.name)

    def test_a_lease_without_a_run_is_taken_and_reclaimable(self):
        record = self.store.grant(1400, "", "M12", 600, 1000.0)

        self.assertEqual(record["run_id"], "")
        self.assertEqual(record["expires_at"], 1600.0)
        self.assertEqual(self.store.expired(1600.0, []), [1400])

    def test_a_pending_lease_is_only_renewed_by_its_holder(self):
        self.store.grant(1400, "", "M12", 600, 1000.0)

        self.assertIsNone(self.store.renew(1400, "run-1", 1200.0))
        renewed = self.store.renew(1400, "", 1200.0)

        self.assertIsNotNone(renewed)
        self.assertEqual(renewed["expires_at"], 1800.0)

    def test_an_empty_run_id_never_keeps_a_lease_alive(self):
        self.store.grant(1400, "", "M12", 600, 1000.0)

        self.assertEqual(self.store.expired(1600.0, [""]), [1400])


if __name__ == "__main__":
    unittest.main()
