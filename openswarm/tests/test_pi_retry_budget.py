import os
import pathlib
import sys
import unittest
from unittest import mock

# Keep _load's temporary module home inside this dedicated worktree.
os.environ["TMPDIR"] = str(pathlib.Path(__file__).resolve().parents[2] / ".openswarm-tmp")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _load

xs = _load.load()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds


class PiRetryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def exhaust(self, last_error):
        budget = xs.PiRetryBudget(10)
        with mock.patch.object(xs.random, "uniform", return_value=1.0):
            while budget.waited_s < budget.max_wait_s:
                delay = budget.schedule(4, now=self.clock.now)
                self.clock.advance(delay)
                budget.resumed(now=self.clock.now, expired=True)
        return xs.pi_retry_budget_error(last_error, budget.waited_s)

    def test_continuous_429_ends_at_budget_as_rate_limit(self):
        error_class, summary = self.exhaust("429 rate_limit_error")
        self.assertEqual(error_class, "rate_limit")
        self.assertIn("10.0s", summary)
        self.assertIn("429 rate_limit_error", summary)

    def test_5xx_budget_exhaustion_is_exec_error(self):
        error_class, summary = self.exhaust("HTTP 503 Service Unavailable")
        self.assertEqual(error_class, "exec_error")
        self.assertIn("HTTP 503", summary)

    def test_recovery_before_budget_keeps_budget_available(self):
        budget = xs.PiRetryBudget(10)
        with mock.patch.object(xs.random, "uniform", return_value=1.0):
            delay = budget.schedule(4, now=self.clock.now)
            self.clock.advance(1)
            budget.resumed(now=self.clock.now)
            self.assertEqual(budget.waited_s, 1)
            self.assertEqual(budget.schedule(4, now=self.clock.now), 4)
            self.assertLess(budget.waited_s, budget.max_wait_s)

    def test_jitter_is_zero_to_twenty_percent_and_clipped_to_remaining(self):
        budget = xs.PiRetryBudget(10)
        with mock.patch.object(xs.random, "uniform", side_effect=[1.0, 1.2]):
            self.assertEqual(budget.schedule(5, now=self.clock.now), 5)
            budget.resumed(now=5)
            self.clock.advance(5)
            self.assertEqual(budget.schedule(5, now=self.clock.now), 5)
            budget.resumed(now=10, expired=True)
            self.assertEqual(budget.waited_s, 10)

    def test_environment_overrides_policy_maximum(self):
        with mock.patch.dict(os.environ, {"PI_RETRY_MAX_WAIT_S": "12"}):
            self.assertEqual(xs.pi_retry_max_wait_s({"pi": {"retry_max_wait_s": 3}}), 12)


if __name__ == "__main__":
    unittest.main()
