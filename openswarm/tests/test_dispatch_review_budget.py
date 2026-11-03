"""Regression tests for review-specific budgets and backend retries."""
from tests.test_dispatch_engine import Base, SHA, pr


class ReviewBudget(Base):
    def test_review_assign_uses_review_budget(self):
        d = self.make()
        commands = []
        d.local_hosts = set()
        d.sh = lambda cmd, **kwargs: (commands.append(" ".join(cmd)) or (0, '{"id":"run-1"}', ""))
        d.dispatch("M1", {"host": "box"}, {"n": 10, "title": "review", "body": "", "review": True}, SHA)
        self.assertTrue(any("--budget-minutes 20" in cmd for cmd in commands), commands)

    def test_budget_exceeded_records_backend_without_incrementing_fails(self):
        d = self.make()
        st = {"review_pr": 11, "review_sha": SHA}
        d.finish_review("M1", {"backend": "pi"}, st,
                        {"status": "failed", "error_class": "budget_exceeded", "backend": "pi"}, {})
        review = d.load_reviews()["11"]
        self.assertEqual(review.get("fails", 0), 0)
        self.assertEqual(review["budget_backends"], ["pi"])

    def test_dispatch_reviews_uses_non_timed_out_backend_and_falls_back(self):
        p = pr(11, 101)
        d = self.make()
        d.review_candidates = lambda *args: [p]
        d.main_sha = lambda: SHA
        d.review_item = lambda item: {"n": "PR11", "title": "review", "body": "", "review": True}
        chosen = []
        d.dispatch = lambda lane, *args: (chosen.append(lane) or ("run-1", None))
        lanes = {"M1": {"host": "box", "backend": "pi"}, "M2": {"host": "box", "backend": "codex"}}
        state = {}
        d.put_review(11, {"budget_backends": ["pi"]})
        d.dispatch_reviews(lanes, {"M1": {"lane": "idle"}, "M2": {"lane": "idle"}}, state, [])
        self.assertEqual(chosen, ["M2"])

        d.put_review(11, {"budget_backends": ["pi", "codex"]})
        state.clear()
        chosen.clear()
        d.dispatch_reviews(lanes, {"M1": {"lane": "idle"}, "M2": {"lane": "idle"}}, state, [])
        self.assertEqual(len(chosen), 1)

    def test_large_diff_has_diffstat_truncated_excerpt_and_git_diff_hint(self):
        d = self.make()
        d.R["review_diff_max_kb"] = 1
        content = "diff --git a/file.txt b/file.txt\n" + "+x\n" * 800
        d.gh = lambda args, **kwargs: (0, content, "")
        item = d.review_item(pr(12, 102))
        self.assertIn("Diffstat:", item["body"])
        self.assertIn("git diff", item["body"])
        self.assertIn("diff excerpt truncated", item["body"])
        # the body is the truncated excerpt plus the fixed evidence-pack instruction (Refs #1788)
        self.assertLess(len(item["body"]), len(content) + 3000)
