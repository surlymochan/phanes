"""Review failure limits are scoped to a PR head SHA (Refs #1590).

The failure counter lives on the head sha: a PR that burned max_fails on one head is skipped
on that head only, a new commit starts the counter over and the `needs-human` label that the
failure limit put on the PR is lifted again (a CHANGES-round escalation stays untouched).

Legacy rows without `fails_sha` keep counting on the current head, so a PR that already sits at
max_fails still needs one manual reset of its review record.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_fails_test",
                                               os.path.join(HERE, "..", "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

SHA = "a" * 40
SHA2 = "b" * 40


def pr(n, issue_n, labels=(), head=SHA):
    return {"number": n, "title": f"pr {n}", "headRefName": f"fleet/M5-issue{issue_n}-r1", "headRefOid": head,
            "isDraft": False, "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "linux-0", "conclusion": "SUCCESS"},
                                  {"name": "OpenSwarm tests (macos-latest)", "conclusion": "FAILURE"}],
            "labels": [{"name": l} for l in labels], "body": f"Closes #{issue_n}", "createdAt": "2026-11-02T00:00:00Z"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-fails-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"]}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(path)

    def make(self, rules=None, dry_run=False):
        self.calls = []

        def runner(cmd, **kwargs):
            text = " ".join(str(c) for c in cmd)
            self.calls.append(text)
            for needle, result in rules or []:
                if needle in text:
                    return result(text) if callable(result) else result
            return 0, "", ""

        d = E.Dispatcher(self.cfg, runner=runner, dry_run=dry_run, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        return d

    def called(self, needle):
        return [c for c in self.calls if needle in c]

    @staticmethod
    def pr_list(*prs):
        """A live `gh pr list` answer: the label changes of the dispatcher show up in the next tick."""
        return ("pr list", lambda text: (0, json.dumps(list(prs)), ""))

    def write_reviews(self, d, reviews):
        os.makedirs(d.path("."), exist_ok=True)
        with open(d.path("reviews.json"), "w") as f:
            json.dump(reviews, f)


class ReviewFailureSha(Base):
    def dispatch_candidate(self, d, candidate):
        """dispatch_reviews with a stubbed candidate list (the real filter has its own test below)."""
        d.idle_lanes = lambda *args: ["M1"]
        d.main_sha = lambda: SHA
        d.dispatch = lambda *args: ("run", "verify")
        d.review_item = lambda item: {"n": item["number"], "title": "review", "body": ""}
        state = {}
        d.review_candidates = lambda *args: [candidate]
        d.dispatch_reviews({"M1": {"host": "box", "backend": "pi"}}, {"M1": {"lane": "idle"}}, state, [])
        return state

    def dispatch_real(self, d, candidate):
        """dispatch_reviews with the real review_candidates (needs `pr list` to answer)."""
        d.idle_lanes = lambda *args: ["M1"]
        d.main_sha = lambda: SHA
        d.dispatch = lambda *args: ("run", "verify")
        d.review_item = lambda item: {"n": item["number"], "title": "review", "body": ""}
        state = {}
        d.dispatch_reviews({"M1": {"host": "box", "backend": "pi"}}, {"M1": {"lane": "idle"}}, state, [])
        return state

    def test_failures_count_and_skip_only_on_same_sha(self):
        candidate = pr(1590, 159)
        d = self.make()
        for _ in range(d.R["max_fails"]):
            d.finish_review("M1", {"backend": "pi"}, {"review_pr": 1590, "review_sha": SHA},
                            {"status": "failed", "error_class": "exec_error"}, {})
        review = d.load_reviews()["1590"]
        self.assertEqual(review["fails"], d.R["max_fails"])
        self.assertEqual(review["fails_sha"], SHA)
        self.assertEqual(review["fail_classes"], ["exec_error"] * 4)
        self.assertEqual(self.dispatch_candidate(d, candidate), {})
        self.assertFalse(self.called("dispatch"))

    def test_fail_classes_keep_only_the_last_four(self):
        d = self.make()
        for i in range(6):
            d.finish_review("M1", {"backend": "pi"}, {"review_pr": 1596, "review_sha": SHA},
                            {"status": "failed", "error_class": f"err{i}"}, {})
        review = d.load_reviews()["1596"]
        self.assertEqual(review["fails"], 6)
        self.assertEqual(len(review["fail_classes"]), 4)
        self.assertEqual(review["fail_classes"], ["err2", "err3", "err4", "err5"])

    def test_new_sha_retries_and_failure_counter_resets(self):
        candidate = pr(1591, 159, head=SHA2)
        d = self.make()
        d.put_review(1591, {"fails": 4, "fails_sha": SHA, "fail_classes": ["exec_error"] * 4})
        state = self.dispatch_candidate(d, candidate)
        self.assertEqual(state["M1"]["review_sha"], SHA2)
        d.finish_review("M1", {"backend": "pi"}, {"review_pr": 1591, "review_sha": SHA2},
                        {"status": "failed", "error_class": "timeout"}, {})
        review = d.load_reviews()["1591"]
        self.assertEqual((review["fails"], review["fails_sha"], review["fail_classes"]), (1, SHA2, ["timeout"]))

    def test_limit_comment_and_needs_human_are_once_per_sha(self):
        candidate = pr(1592, 159)
        d = self.make()
        d.put_review(1592, {"fails": 4, "fails_sha": SHA, "fail_classes": ["timeout", "exec_error"]})
        self.dispatch_candidate(d, candidate)
        self.dispatch_candidate(d, candidate)
        comments = self.called("pr comment 1592")
        self.assertEqual(len(comments), 1)
        self.assertIn("timeout, exec_error", comments[0])
        self.assertEqual(len(self.called("--add-label needs-human")), 1)
        review = d.load_reviews()["1592"]
        self.assertEqual(review["fails_notified_sha"], SHA)
        self.assertEqual(review["needs_human_sha"], SHA)
        self.assertEqual(review["needs_human_reason"], "fails")

    def test_legacy_failure_record_without_sha_applies_to_current_head(self):
        candidate = pr(1593, 159)
        d = self.make([self.pr_list(candidate)])
        d.put_review(1593, {"fails": 4})
        self.assertEqual(self.dispatch_candidate(d, candidate), {})
        self.assertFalse(self.called("dispatch"))
        self.assertEqual(len(self.called("pr comment 1593")), 1)

    def test_comment_is_not_repeated_when_the_label_write_fails(self):
        candidate = pr(1597, 159)
        d = self.make([("--add-label", (1, "", "boom"))])
        d.put_review(1597, {"fails": 4, "fails_sha": SHA, "fail_classes": ["timeout"]})
        self.dispatch_candidate(d, candidate)
        self.dispatch_candidate(d, candidate)
        self.assertEqual(len(self.called("pr comment 1597")), 1)          # told once ...
        self.assertEqual(len(self.called("--add-label needs-human")), 2)   # ... label retried
        self.assertNotIn("needs_human_sha", d.load_reviews()["1597"])      # not parked yet

    def test_dry_run_writes_no_review_record(self):
        candidate = pr(1598, 159)
        d = self.make([self.pr_list(candidate)], dry_run=True)
        self.write_reviews(d, {"1598": {"fails": 4, "fails_sha": SHA, "fail_classes": ["timeout"]}})
        self.dispatch_candidate(d, candidate)
        self.assertEqual(d.load_reviews()["1598"], {"fails": 4, "fails_sha": SHA, "fail_classes": ["timeout"]})
        self.assertTrue(any("pr comment 1598" in p.get("cmd", "") for p in d.plan))

    def test_real_candidates_remove_needs_human_after_head_moves(self):
        """The failure limit parked this PR; a new head lifts the label and the review runs again."""
        candidate = pr(1594, 159, labels=("needs-human",), head=SHA2)
        d = self.make([self.pr_list(candidate)])
        d.put_review(1594, {"fails": 4, "fails_sha": SHA, "fails_notified_sha": SHA,
                            "needs_human_sha": SHA, "needs_human_reason": "fails"})
        state = self.dispatch_real(d, candidate)
        self.assertIn("--remove-label needs-human", " ".join(self.calls))
        self.assertEqual(state["M1"]["review_sha"], SHA2)
        review = d.load_reviews()["1594"]
        self.assertIsNone(review["needs_human_sha"])
        self.assertIsNone(review["needs_human_reason"])

    def test_needs_human_from_changes_rounds_is_kept(self):
        """A CHANGES-round escalation is for a human: it must not be lifted by the head moving."""
        candidate = pr(1595, 159, labels=("needs-human",), head=SHA2)
        d = self.make([self.pr_list(candidate)])
        d.put_review(1595, {"fails_sha": SHA, "rounds": 4, "needs_human_sha": SHA, "needs_human_reason": "rounds"})
        state = self.dispatch_real(d, candidate)
        self.assertNotIn("--remove-label needs-human", " ".join(self.calls))
        self.assertEqual(state, {})
        self.assertEqual(d.load_reviews()["1595"]["needs_human_reason"], "rounds")

    def test_parked_pr_is_parked_again_on_the_new_head(self):
        """After the label is lifted, a new failure streak on the new head parks the PR again."""
        candidate = pr(1599, 159, labels=("needs-human",), head=SHA2)
        d = self.make([self.pr_list(candidate)])
        d.put_review(1599, {"fails": 4, "fails_sha": SHA, "fails_notified_sha": SHA,
                            "needs_human_sha": SHA, "needs_human_reason": "fails"})
        state = self.dispatch_real(d, candidate)          # head moved: the label is lifted, review runs again
        self.assertEqual(state["M1"]["review_sha"], SHA2)
        for _ in range(d.R["max_fails"]):                  # the review on the new head fails again
            d.finish_review("M1", {"backend": "pi"}, {"review_pr": 1599, "review_sha": SHA2},
                            {"status": "failed", "error_class": "exec_error"}, {})
        candidate["labels"] = []                          # the label is gone from the PR (it was lifted)
        state = self.dispatch_real(d, candidate)
        self.assertEqual(state, {})                      # the limit parks it instead of dispatching
        review = d.load_reviews()["1599"]
        self.assertEqual(review["needs_human_sha"], SHA2)
        self.assertEqual(review["needs_human_reason"], "fails")
        self.assertEqual(review["fails_notified_sha"], SHA2)
        self.assertEqual(len(self.called("--add-label needs-human")), 1)
