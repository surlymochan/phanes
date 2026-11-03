"""The dispatch engine hands the review lane the raw evidence and checks the pack (Refs #1788)."""
import json
import os
import unittest

from tests.test_dispatch_engine import Base, SHA, pr

PACK = """# Evidence pack — PR 20

## 1. CI results (CI 结论逐项)
CI: OpenSwarm tests | pass | https://example.test/runs/1

## 2. Test log summary and links (测试日志摘要与链接)
TEST: python -m unittest discover -s tests | 42 tests, all passed | https://example.test/runs/1/logs
LOG: Ran 42 tests in 1.234s
LOG: OK

## 3. Diff statistics (diff 统计)
DIFFSTAT: files=3 | insertions=120 | deletions=12 | effective_lines=95
RULE: effective_lines = added + deleted lines of code and test files; documentation is excluded

## 4. Acceptance checks (验收项逐条原始核查)
CHECK: acceptance item 1 | pass | `python -m unittest` → OK

## 5. Preliminary opinion (初步意见 · 参考，不算判定)
OPINION: looks fine to me
"""

# The same pack without the acceptance checks section: a section is missing.
PACK_WITHOUT_CHECKS = PACK.split("## 4. Acceptance checks")[0]

# The diff of a one-file change, used as the diff of the local git objects.
LOCAL_DIFF = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
              "+one\n+two\n")


def local_head(commit=SHA, diff=LOCAL_DIFF):
    """The git rules of a local clone that holds the head of a PR."""
    return [("rev-parse --verify --quiet refs/heads/rebase-pr-12", (0, commit + "\n", "")),
            ("merge-base", (0, "c" * 40 + "\n", "")),
            ("diff --no-color", (0, diff, ""))]


def git_rules(verdict="VERDICT: APPROVE\nSUMMARY: ok\n", pack=PACK):
    """Answer the two `git show` calls of a review: the verdict file and the evidence file."""
    def show(text):
        if "-evidence.md" in text:
            return (0, pack, "")
        return (0, verdict, "")
    return [("git -C", show)]


class ReviewTaskTests(Base):
    """The review task asks for the raw evidence pack and hands the raw numbers over."""

    def test_the_task_names_the_evidence_file_and_the_five_sections(self):
        d = self.make(local_head())
        d.gh = lambda args, **kwargs: (0, "", "")

        body = d.review_item(pr(12, 102))["body"]

        self.assertIn(".lane-review/PR-12-evidence.md", body)
        for title in ("CI results", "Test log summary and links", "Diff statistics",
                      "Acceptance checks", "Preliminary opinion"):
            self.assertIn(title, body)

    def test_the_task_hands_the_raw_ci_conclusions_over(self):
        d = self.make(local_head())
        d.gh = lambda args, **kwargs: (0, "", "")

        body = d.review_item(pr(12, 102))["body"]

        self.assertIn("CI: linux-0 | SUCCESS | ", body)
        self.assertIn("judgement of this review belongs to the tester", body)

    def test_the_task_hands_the_raw_diff_numbers_over(self):
        d = self.make()
        diff = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
                "+one\n+two\n")
        d.gh = lambda args, **kwargs: (0, diff, "")

        body = d.review_item(pr(12, 102))["body"]

        self.assertIn("DIFFSTAT: files=1 | insertions=2 | deletions=0 | effective_lines=2", body)


class DiffstatOfTests(Base):
    """The raw diff statistics are read from the unified diff itself."""

    def test_a_deleted_file_counts_under_its_own_name(self):
        d = self.make()
        diff = ("diff --git a/openswarm/lib/gone.py b/openswarm/lib/gone.py\n"
                "deleted file mode 100644\n"
                "--- a/openswarm/lib/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-one\n-two\n")

        stats = d.diffstat_of(diff)

        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["deletions"], 2)
        self.assertEqual(stats["effective_lines"], 2)

    def test_a_removed_line_that_looks_like_a_header_is_a_removed_line(self):
        d = self.make()
        diff = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
                "@@ -1,2 +1,1 @@\n--- not a header\n+new\n")

        stats = d.diffstat_of(diff)

        self.assertEqual(stats["insertions"], 1)
        self.assertEqual(stats["deletions"], 1)


class OfflineDiffTests(Base):
    """The review lane gets the diff even when GitHub is not reachable."""

    DIFF = LOCAL_DIFF

    def local(self, commit=SHA):
        return local_head(commit, self.DIFF)

    def test_the_diff_comes_from_the_local_objects_when_github_is_down(self):
        d = self.make(self.local())
        d.gh = lambda args, **kwargs: (1, "", "could not resolve host: github.com")

        body = d.review_item(pr(12, 102))["body"]

        self.assertIn("+two", body)
        self.assertIn("local git objects", body)
        self.assertIn("DIFFSTAT: files=1 | insertions=2 | deletions=0 | effective_lines=2", body)

    def test_the_local_diff_is_not_used_when_github_answers(self):
        d = self.make(self.local())
        d.gh = lambda args, **kwargs: (0, self.DIFF, "")

        body = d.review_item(pr(12, 102))["body"]

        self.assertNotIn("local git objects", body)

    def test_without_a_local_head_there_is_no_diff(self):
        d = self.make([("rev-parse", (1, "", "not found"))])
        d.gh = lambda args, **kwargs: (1, "", "could not resolve host: github.com")

        self.assertIsNone(d.review_item(pr(12, 102)))

    def test_a_local_head_that_is_not_the_pr_head_blocks_the_review(self):
        d = self.make(self.local(commit="e" * 40))
        d.gh = lambda args, **kwargs: (1, "", "could not resolve host: github.com")

        self.assertIsNone(d.review_item(pr(12, 102)))


class HeadBoundDiffTests(Base):
    """The diff of a review is bound to the head the dispatcher knows (Refs #1788).

    An older local ref must never be reviewed as the current head: the missing
    changes of the head would pass unnoticed.
    """

    OLD = "e" * 40

    def test_an_older_local_ref_is_not_used_as_the_head(self):
        d = self.make(local_head(commit=self.OLD))
        d.gh = lambda args, **kwargs: (1, "", "could not resolve host: github.com")

        self.assertIsNone(d.review_item(pr(12, 102)))

    def test_the_local_ref_of_another_commit_is_never_returned(self):
        d = self.make(local_head(commit=self.OLD))

        self.assertIsNone(d.local_head_ref(12, "fleet/M5-issue102-r1", SHA))

    def test_the_local_ref_of_the_head_is_returned(self):
        d = self.make(local_head(commit=SHA))

        self.assertEqual(d.local_head_ref(12, "fleet/M5-issue102-r1", SHA), "refs/heads/rebase-pr-12")

    def test_the_block_is_written_to_the_log(self):
        d = self.make(local_head(commit=self.OLD))
        d.gh = lambda args, **kwargs: (1, "", "could not resolve host: github.com")

        d.review_item(pr(12, 102))

        with open(d.path(d.cfg["log_file"]), encoding="utf-8") as stream:
            log = stream.read()
        self.assertIn("blocked", log)
        self.assertIn("an older diff is never reviewed as the current head", log)

    def test_no_review_is_dispatched_for_a_head_without_a_local_commit(self):
        d = self.make(local_head(commit=self.OLD))
        d.gh = lambda args, **kwargs: (1, "", "could not resolve host: github.com")
        d.review_candidates = lambda *args: [pr(12, 102)]
        d.main_sha = lambda: SHA
        dispatched = []
        d.dispatch = lambda lane, *args: (dispatched.append(lane) or ("run-1", None))
        lanes = {"M1": {"host": "box", "backend": "pi"}}
        state = {}

        d.dispatch_reviews(lanes, {"M1": {"lane": "idle"}}, state, [])

        self.assertEqual(dispatched, [])
        self.assertNotIn("M1", state)


class FinishReviewEvidenceTests(Base):
    """`finish_review` checks the format of the pack and records what it found."""

    def state(self, pr_number=20):
        return {"issue": "PR%s" % pr_number, "run": "r1", "review_pr": pr_number, "review_sha": SHA}

    def pr_view(self, pr_number=20):
        return ("pr view %s" % pr_number,
                (0, json.dumps({"headRefOid": SHA, "state": "OPEN",
                                "headRefName": "fleet/M5-issue200-r", "body": "Closes #200"}), ""))

    def test_a_complete_pack_is_recorded(self):
        d = self.make(git_rules() + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {}))

        evidence = d.load_reviews()["20"]["evidence"]
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["missing"], [])
        self.assertEqual(evidence["errors"], [])
        self.assertEqual(evidence["ci"], 1)
        self.assertEqual(evidence["tests"], 1)
        self.assertEqual(evidence["checks"], 1)
        self.assertEqual(evidence["effective_lines"], 95)

    def test_a_pack_without_a_section_is_recorded_as_incomplete(self):
        d = self.make(git_rules(pack=PACK_WITHOUT_CHECKS) + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {}))

        evidence = d.load_reviews()["20"]["evidence"]
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["missing"], ["checks"])
        self.assertEqual(evidence["errors"], [])

    def test_a_missing_pack_is_recorded_as_incomplete(self):
        d = self.make(git_rules(pack="") + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {}))

        evidence = d.load_reviews()["20"]["evidence"]
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["missing"], ["ci", "testlog", "diffstat", "checks"])

    def test_an_incomplete_pack_does_not_change_the_judgement(self):
        d = self.make(git_rules(pack=PACK_WITHOUT_CHECKS) + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {})

        self.assertEqual(d.load_reviews()["20"]["verdict"], "APPROVE")
        self.assertIn("state=success", self.runner.called(f"statuses/{SHA}")[0])

    def test_a_verdict_in_the_pack_is_refused_and_recorded(self):
        d = self.make(git_rules(pack=PACK + "\nVERDICT: CHANGES\n") + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {})

        evidence = d.load_reviews()["20"]["evidence"]
        self.assertFalse(evidence["complete"])
        self.assertIn("verdict-in-pack", evidence["errors"])

    def test_the_comment_carries_the_raw_pack_when_it_is_complete(self):
        d = self.make(git_rules() + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {})

        comment = self.runner.called("pr comment 20")[0]
        self.assertIn("Raw evidence pack", comment)
        self.assertIn("CI: OpenSwarm tests | pass | https://example.test/runs/1", comment)
        self.assertIn("CHECK: acceptance item 1 | pass | `python -m unittest` → OK", comment)

    def test_the_comment_carries_the_note_when_the_pack_is_incomplete(self):
        d = self.make(git_rules(pack="") + [self.pr_view()])
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}

        d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {})

        comment = self.runner.called("pr comment 20")[0]
        self.assertIn("no evidence pack in the lane branch", comment)
        self.assertIn("missing: ci, testlog, diffstat, checks", comment)


class ReviewTerminalIntegrationTests(Base):
    """The real review terminal path of a tick reads the pack and persists it (Refs #1788).

    `finish_review` is not called here directly: the tick probes the lane, harvests
    the finished run and ends in the review terminal state, so the test proves that
    the pack of the lane branch is really read and really persisted.
    """

    LANE = "M5"

    def lane_status(self):
        """The status of a lane whose review run has finished with a commit."""
        return {"lane": "idle",
                "run": {"status": "success", "timestamp": 1.0, "branch": "lane/x", "commit": "c",
                        "repo": "/repo", "backend": "pi", "model": "m", "error_class": None,
                        "summary": "reviewed", "seconds": 1}}

    def rules(self, pack):
        def show(text):
            if "-evidence.md" in text:
                return (0, pack, "")
            return (0, "VERDICT: APPROVE\nSUMMARY: ok\n", "")
        return [("python3 -", (0, json.dumps(self.lane_status()), "")),
                ("git -C", show),
                ("pr view", (0, json.dumps({"headRefOid": SHA, "state": "OPEN",
                                            "headRefName": "fleet/M5-issue200-r",
                                            "body": "Closes #200"}), "")),
                ("issue list", (0, "[]", "")),
                ("pr list", (0, "[]", ""))]

    def tick_once(self, pack):
        """Run one tick with a review in flight and return the dispatcher."""
        d = self.make(self.rules(pack))
        lanes = {self.LANE: {"host": "box", "backend": "pi"}}
        state = {self.LANE: {"issue": "PR20", "run": "r1", "host": "box", "base": SHA,
                             "at": "2026-11-03 00:00:00", "review_pr": 20, "review_sha": SHA}}

        d.tick(lanes, state)

        return d

    def test_a_complete_pack_is_read_and_persisted_by_the_tick(self):
        d = self.tick_once(PACK)

        review = d.load_reviews()["20"]
        self.assertEqual(review["verdict"], "APPROVE")
        self.assertTrue(review["evidence"]["complete"])
        self.assertEqual(review["evidence"]["missing"], [])
        self.assertEqual(review["evidence"]["errors"], [])
        self.assertEqual(review["evidence"]["ci"], 1)
        self.assertEqual(review["evidence"]["tests"], 1)
        self.assertEqual(review["evidence"]["checks"], 1)
        self.assertEqual(review["evidence"]["effective_lines"], 95)

    def test_a_pack_without_a_section_is_persisted_as_incomplete_by_the_tick(self):
        d = self.tick_once(PACK_WITHOUT_CHECKS)

        review = d.load_reviews()["20"]
        self.assertEqual(review["verdict"], "APPROVE")
        self.assertFalse(review["evidence"]["complete"])
        self.assertEqual(review["evidence"]["missing"], ["checks"])

    def test_a_missing_pack_is_persisted_as_incomplete_by_the_tick(self):
        d = self.tick_once("")

        review = d.load_reviews()["20"]
        self.assertFalse(review["evidence"]["complete"])
        self.assertEqual(review["evidence"]["missing"], ["ci", "testlog", "diffstat", "checks"])

    def test_the_incomplete_pack_is_written_to_the_log_of_the_tick(self):
        d = self.tick_once(PACK_WITHOUT_CHECKS)

        with open(d.path(d.cfg["log_file"]), encoding="utf-8") as stream:
            log = stream.read()
        self.assertIn("evidence pack INCOMPLETE", log)
        self.assertIn("missing: checks", log)


if __name__ == "__main__":
    unittest.main()
