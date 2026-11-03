import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tester-risk.py"
spec = importlib.util.spec_from_file_location("tester_risk", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def pr(**changes):
    result = {"number": 1791, "files": 3, "additions": 20, "deletions": 10,
              "reviews": [{"id": 1}], "labels": []}
    result.update(changes)
    return result


class TesterRiskTests(unittest.TestCase):
    def test_large_diff_by_file_count(self):
        result = module.classify(pr(files=20), {})
        self.assertEqual(result["level"], "全读")
        self.assertIn("大 diff", result["reasons"][0])

    def test_large_diff_by_churn_boundary(self):
        result = module.classify(pr(additions=500, deletions=500), {})
        self.assertEqual(result["level"], "全读")

    def test_review_history_boundary(self):
        result = module.classify(pr(reviews=[{}] * 4), {})
        self.assertEqual(result["level"], "全读")
        self.assertIn("review 轮次 4", result["reasons"][0])

    def test_needs_human_and_issue_failure_are_full_read(self):
        self.assertEqual(module.classify(pr(labels=[{"name": "needs-human"}]), {})["level"], "全读")
        result = module.classify(pr(), {"labels": [{"name": "failure:timeout"}]})
        self.assertEqual(result["level"], "全读")
        self.assertIn("failure:timeout", result["reasons"][0])

    def test_regular_pr_is_sampled(self):
        result = module.classify(pr(), {"labels": []})
        self.assertEqual(result["level"], "抽检")
        self.assertEqual(result["sampling"], "20%")

    def test_missing_history_is_sampled_and_gap_is_reported(self):
        result = module.classify(pr(reviews=None), None)
        self.assertEqual(result["level"], "抽检")
        self.assertTrue(any("缺失" in reason for reason in result["reasons"]))

    def test_thresholds_are_inclusive(self):
        self.assertEqual(module.classify(pr(files=19, additions=499, deletions=500), {})["level"], "抽检")
        self.assertEqual(module.classify(pr(reviews=[{}] * 3), {})["level"], "抽检")


if __name__ == "__main__":
    unittest.main()
