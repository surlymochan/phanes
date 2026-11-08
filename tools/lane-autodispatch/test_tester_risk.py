"""Acceptance tests for the tester evidence review risk classifier (#1988)."""
import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("tester_risk.py")
spec = importlib.util.spec_from_file_location("tester_risk", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TesterRiskTests(unittest.TestCase):
    def test_omitted_history_requires_full_read(self):
        result = module.classify(
            {"files_changed": 1, "additions": 2, "deletions": 1},
            pr_number=1988,
            sample_rate=0,
        )

        self.assertEqual(result["decision"], "full_read")
        self.assertIn("history", result["reason"])

    def test_explicit_empty_history_allows_routine_sampling(self):
        diff = {"files_changed": 1, "additions": 2, "deletions": 1}
        for rate, decision in ((0, "sampled"), (1, "full_read")):
            with self.subTest(rate=rate):
                result = module.classify(diff, [], pr_number=1988, sample_rate=rate)
                self.assertEqual(result["decision"], decision)

    def test_large_diff_thresholds_require_full_read_with_sampling_disabled(self):
        for diff in (
            {"files_changed": 20, "additions": 0, "deletions": 0},
            {"files_changed": 1, "additions": 500, "deletions": 500},
        ):
            with self.subTest(diff=diff):
                result = module.classify(diff, [], pr_number=1988, sample_rate=0)
                self.assertEqual(result["decision"], "full_read")

    def test_sampling_uses_pr_and_seed_without_diff_order_dependence(self):
        diff = {"files_changed": 1, "additions": 2, "deletions": 1}
        decisions = []
        for pr in range(1, 31):
            first = module.classify(diff, [], pr_number=pr, seed="fixed")
            repeated = module.classify(dict(reversed(list(diff.items()))), [],
                                       pr_number=pr, seed="fixed")
            self.assertEqual(first, repeated)
            decisions.append(first["decision"])
        self.assertEqual(set(decisions), {"full_read", "sampled"})

    def classify(self, diff_stats, needs_human_history=(), **config):
        return module.classify(
            diff_stats=diff_stats,
            needs_human_history=needs_human_history,
            pr_number=1988,
            sample_rate=0.2,
            seed="risk-v1",
            **config,
        )

    def test_large_diff_always_requires_full_read(self):
        result = self.classify({"files_changed": 100, "additions": 5000, "deletions": 3000})

        self.assertEqual(result["decision"], "full_read")
        self.assertTrue(result["reason"])

    def test_any_needs_human_history_always_requires_full_read(self):
        result = self.classify(
            {"files_changed": 1, "additions": 2, "deletions": 1},
            needs_human_history=[False, True, False],
        )

        self.assertEqual(result["decision"], "full_read")
        self.assertTrue(result["reason"])

    def test_routine_card_sampling_is_stable_for_same_pr_and_seed(self):
        diff = {"files_changed": 2, "additions": 12, "deletions": 3}

        first = self.classify(diff)
        second = self.classify(diff)

        self.assertEqual(first, second)
        self.assertIn(first["decision"], {"full_read", "sampled"})
        self.assertTrue(first["reason"])

    def test_missing_or_malformed_inputs_fail_closed_to_full_read(self):
        cases = (
            (None, ()),
            ({"files_changed": "many", "additions": 1, "deletions": 1}, ()),
            ({"files_changed": 1, "additions": 1}, ()),
            ({"files_changed": 1, "additions": 1, "deletions": 1}, None),
            ({"files_changed": 1, "additions": 1, "deletions": 1}, "unknown"),
        )
        for diff_stats, history in cases:
            with self.subTest(diff_stats=diff_stats, history=history):
                result = self.classify(diff_stats, needs_human_history=history)
                self.assertEqual(result["decision"], "full_read")
                self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
