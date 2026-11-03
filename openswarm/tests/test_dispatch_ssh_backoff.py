"""Tiered SSH backoff of flaky hosts (lib/dispatch/ssh_backoff.py, Refs #1689)."""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "dispatch_ssh_backoff_under_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "ssh_backoff.py"))
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


class TierTests(unittest.TestCase):
    """The streak decides the tier: short retries below the limit, long ones from it on."""

    def test_tier_switches_at_the_soft_limit(self):
        self.assertEqual([B.tier(n) for n in (0, 1, 2)], ["soft", "soft", "soft"])
        self.assertEqual([B.tier(n) for n in (3, 4, 58)], ["hard", "hard", "hard"])

    def test_the_soft_limit_is_configurable(self):
        self.assertEqual(B.tier(2, soft_limit=2), "hard")
        self.assertEqual(B.tier(1, soft_limit=2), "soft")

    def test_a_broken_streak_counts_as_no_failure(self):
        self.assertEqual(B.tier("nonsense"), "soft")
        self.assertEqual(B.backoff_seconds("nonsense"), 0)


class BackoffLadderTests(unittest.TestCase):
    """The wait of a tier: short in the soft tier, long (and capped) in the hard one."""

    def test_soft_tier_waits_a_short_interval(self):
        self.assertEqual([B.backoff_seconds(n) for n in (1, 2)], [30, 60])

    def test_hard_tier_waits_a_long_interval(self):
        self.assertEqual([B.backoff_seconds(n) for n in (3, 4, 5)], [240, 480, 600])

    def test_the_hard_tier_is_capped(self):
        self.assertEqual(B.backoff_seconds(58), 600)
        self.assertEqual(B.backoff_seconds(1000), 600)

    def test_the_limits_can_be_overridden(self):
        limits = {"soft_limit": 2, "soft_base_s": 10, "soft_max_s": 15,
                  "hard_base_s": 100, "hard_max_s": 200}
        self.assertEqual([B.backoff_seconds(n, limits) for n in (1, 2, 3, 4)], [10, 100, 200, 200])

    def test_a_broken_override_keeps_the_default(self):
        limits = B.limits({"soft_limit": 0, "soft_base_s": "x", "unknown": 5})
        self.assertEqual(limits["soft_limit"], B.DEFAULT_SOFT_LIMIT)
        self.assertEqual(limits["soft_base_s"], B.DEFAULT_SOFT_BASE_S)
        self.assertNotIn("unknown", limits)


class StreakTests(unittest.TestCase):
    """The streak counts consecutive failures; one success clears it."""

    def test_every_failure_counts_once(self):
        state = {}
        for expected in (1, 2, 3):
            state = B.record_failure(state, 1000.0)
            self.assertEqual(state["n"], expected)
        self.assertEqual(state["tier"], "hard")

    def test_one_success_clears_the_streak(self):
        state = B.record_failure(B.record_failure(B.record_failure({}, 1000.0), 1010.0), 1020.0)
        self.assertEqual(state["n"], 3)
        self.assertEqual(B.record_success(state), {})
        # the next failure starts the ladder from the beginning again
        again = B.record_failure(B.record_success(state), 1030.0)
        self.assertEqual(again["n"], 1)
        self.assertEqual(again["tier"], "soft")
        self.assertEqual(again["wait"], 30)

    def test_the_failure_state_carries_the_gates(self):
        state = B.record_failure({}, 1000.0)
        self.assertEqual(state["until"], 1030.0)
        self.assertEqual(state["probe_at"], 1030.0)
        self.assertEqual(state["wait"], 30)


class GateTests(unittest.TestCase):
    """The probe gate and the dispatch gate of a tier."""

    def test_a_host_without_backoff_is_always_due(self):
        self.assertTrue(B.probe_due(None, 0))
        self.assertTrue(B.dispatchable({}, 0))

    def test_the_soft_tier_opens_after_its_short_backoff(self):
        state = B.record_failure({}, 1000.0)
        self.assertFalse(B.probe_due(state, 1029))
        self.assertFalse(B.dispatchable(state, 1029))
        self.assertTrue(B.probe_due(state, 1030))
        self.assertTrue(B.dispatchable(state, 1030))

    def test_the_hard_tier_keeps_a_probe_but_no_dispatch(self):
        state = B.record_failure(B.record_failure(B.record_failure({}, 1000.0), 1010.0), 1020.0)
        self.assertEqual(state["tier"], "hard")
        self.assertFalse(B.probe_due(state, 1259))
        # the low-frequency probe of the hard tier: once per backoff window
        self.assertTrue(B.probe_due(state, 1260))
        # a probe alone does not open the dispatch: only a success (streak cleared) does
        self.assertFalse(B.dispatchable(state, 1260))
        self.assertTrue(B.dispatchable(B.record_success(state), 1260))

    def test_a_broken_state_is_treated_as_no_backoff(self):
        self.assertTrue(B.probe_due({"n": "x"}, 0))
        self.assertTrue(B.dispatchable({"n": "x", "until": "x"}, 0))


class HostVerdictTests(unittest.TestCase):
    """The host is judged by its ssh results: one answer is enough (Refs #1689)."""

    def test_a_lane_that_answered_means_the_host_answered(self):
        self.assertTrue(B.host_reachable({"A1": {"lane": "idle"}, "A2": {"error": "Connection closed"}}))

    def test_every_lane_failing_with_an_ssh_error_means_the_host_is_down(self):
        self.assertFalse(B.host_reachable({"A1": {"error": "Connection closed"},
                                           "A2": {"error": "ssh status timeout"}}))

    def test_a_lane_level_error_means_the_ssh_session_worked(self):
        self.assertTrue(B.lane_error("remote status exc: boom"))
        self.assertFalse(B.lane_error("Connection closed"))
        self.assertTrue(B.host_reachable({"A1": {"error": "remote status exc: boom"}}))

    def test_a_lane_that_was_not_probed_is_no_evidence(self):
        self.assertTrue(B.host_reachable({"A1": {"error": "host in backoff", "skip": True}}))
        self.assertTrue(B.host_reachable({}))

    def test_a_broken_result_is_no_evidence(self):
        self.assertTrue(B.host_reachable({"A1": "nonsense"}))


class DescribeTests(unittest.TestCase):
    """The observable backoff row of a host."""

    def test_the_row_reports_the_tier_the_streak_and_the_next_probe(self):
        state = B.record_failure({}, 1000.0)
        row = B.describe(state, 1010.0)
        self.assertEqual(row["streak"], 1)
        self.assertEqual(row["tier"], "soft")
        self.assertEqual(row["wait_s"], 30)
        self.assertEqual(row["next_probe_at"], 1030.0)
        self.assertEqual(row["probe_due_in_s"], 20)
        self.assertTrue(row["dispatchable"] is False)

    def test_a_host_without_backoff_has_no_row(self):
        self.assertEqual(B.describe({}, 0), {})

    def test_the_snapshot_lists_the_backing_off_hosts(self):
        states = {"box": B.record_failure({}, 1000.0), "ib1": {}}
        rows = B.snapshot(states, 1000.0)
        self.assertEqual(list(rows), ["box"])
        self.assertEqual(rows["box"]["tier"], "soft")


if __name__ == "__main__":
    unittest.main()
