"""The review lane reads the PR diff from the local clone, not from the prompt (Refs #1828).

The review sandbox has no network: the PR head is seeded into the lane repository as
`rebase-pr-<n>` and the reviewer reads the whole diff from there. A review that could not
read the diff is evidence-insufficient (TOOL_FAIL): it approves nothing, it rejects
nothing, and it must not carry findings about the code.
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import unittest

from tests.test_dispatch_engine import Base, SHA, pr

_spec = importlib.util.spec_from_file_location(
    "review_status", pathlib.Path(__file__).resolve().parent.parent / "lib" / "review" / "status.py")
status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status)

DIFF = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "+one\n+two\n")

# A review that could not read the diff: the SUMMARY says so, the findings carry no
# content blocker, so the verdict is not released.
APPROVE_WITHOUT_DIFF = "VERDICT: APPROVE\nSUMMARY: 无法获取 diff\nBLOCKERS: 0\n## Findings\n- [BLOCKER] 无\n"
CHANGES_WITHOUT_DIFF = "VERDICT: CHANGES\nSUMMARY: 无法获取 diff\nBLOCKERS: 1\n## Findings\n- [BLOCKER] 无法核对\n"
# The same wording, but with a real finding that names a place in the code: this is a
# content verdict and must not be swallowed.
CHANGES_WITH_A_BLOCKER = ("VERDICT: CHANGES\nSUMMARY: 无法获取 diff\nBLOCKERS: 1\n## Findings\n"
                          "- [BLOCKER] tools/lease-audit.py:103: the check never runs\n")
NORMAL_REVIEW = "VERDICT: APPROVE\nSUMMARY: 缺少测试覆盖，实现不满足验收\n"


class ReviewTaskTests(Base):
    """The review task names the local branch and the commands, and carries no diff."""

    def task(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, DIFF, "")
        return d.review_item(pr(12, 102))["body"]

    def test_the_task_carries_the_local_branch_and_the_commands(self):
        body = self.task()

        self.assertIn("rebase-pr-12", body)
        self.assertIn("git diff --stat HEAD...rebase-pr-12", body)
        self.assertIn("git diff HEAD...rebase-pr-12", body)

    def test_the_task_carries_no_diff(self):
        body = self.task()

        self.assertNotIn("+two", body)
        self.assertNotIn("diff --git a/src/app.py", body)

    def test_the_task_names_the_reviewed_head(self):
        body = self.task()

        self.assertIn(SHA, body)

    def test_the_task_tells_the_reviewer_what_to_do_without_the_diff(self):
        body = self.task()

        self.assertIn("SUMMARY: 无法获取 diff", body)
        self.assertIn("TOOL_FAIL", body)

    def test_a_huge_diff_is_not_cut_in_the_task(self):
        d = self.make()
        content = "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n" + "+x\n" * 5000
        d.gh = lambda args, **kwargs: (0, content, "")

        body = d.review_item(pr(12, 102))["body"]

        self.assertNotIn("+x\n" * 10, body)
        self.assertIn("4000 lines", body)


class ReviewSeedTests(Base):
    """The PR head is seeded into the lane repository before the review is dispatched."""

    ITEM = {"n": 12, "title": "review", "body": "", "review": True, "head_sha": SHA}

    def dispatcher(self, head_sha=SHA, commands=None, fetch_fails=False):
        commands = commands if commands is not None else []

        def fake_sh(cmd, **kwargs):
            text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            commands.append(text)
            if "lane assign" in text:
                return (0, '{"id":"run-1"}', "")
            if fetch_fails and "pull/12/head" in text:
                return (1, "", "could not resolve host: github.com")
            if "rev-parse" in text and "refs/ad/pr-12" in text:
                return (0, head_sha + "\n", "")
            return (0, "", "")

        d = self.make()
        d.local_hosts = set()
        d.sh = fake_sh
        return d, commands

    def test_the_box_fetches_the_head_and_pushes_it_to_the_review_host(self):
        d, commands = self.dispatcher()

        d.dispatch("M1", {"host": "box", "seed_from_box": True}, self.ITEM, SHA)

        self.assertTrue(any("+pull/12/head:refs/ad/pr-12" in c for c in commands), commands)
        self.assertTrue(any("+refs/ad/pr-12:refs/heads/rebase-pr-12" in c for c in commands), commands)

    def test_a_host_with_network_fetches_the_head_itself(self):
        d, commands = self.dispatcher()

        d.dispatch("M1", {"host": "box"}, self.ITEM, SHA)

        self.assertTrue(any("+pull/12/head:refs/heads/rebase-pr-12" in c for c in commands), commands)

    def test_the_seeded_head_has_to_be_the_reviewed_head(self):
        d, commands = self.dispatcher()

        d.dispatch("M1", {"host": "box"}, self.ITEM, SHA)

        assign = [c for c in commands if "lane assign" in c][0]
        self.assertIn("rev-parse --verify --quiet refs/heads/rebase-pr-12", assign)
        self.assertIn("grep -qx %s" % SHA, assign)
        self.assertIn("update-ref -d refs/heads/rebase-pr-12", assign)

    def test_a_head_that_moved_is_not_reviewed(self):
        d, commands = self.dispatcher(head_sha="b" * 40)
        d.review_candidates = lambda *args: [pr(12, 102)]
        d.main_sha = lambda: SHA
        d.review_item = lambda p: dict(self.ITEM)
        lanes = {"M1": {"host": "box", "backend": "pi", "seed_from_box": True}}
        state = {}

        d.dispatch_reviews(lanes, {"M1": {"lane": "idle"}}, state, [])

        self.assertEqual(state, {})
        self.assertFalse(any("lane assign" in c for c in commands), commands)

    def test_a_failed_head_fetch_still_dispatches_the_review(self):
        # The fetch of the PR head failed (no network): the review is dispatched anyway, the
        # reviewer finds no branch and reports the missing diff (TOOL_FAIL).
        d, commands = self.dispatcher(fetch_fails=True)

        run, _ = d.dispatch("M1", {"host": "box", "seed_from_box": True}, self.ITEM, SHA)

        self.assertEqual(run, "run-1")
        self.assertFalse(any("+refs/ad/pr-12:refs/heads/rebase-pr-12" in c for c in commands), commands)


class BigDiffReviewTests(Base):
    """A PR with more than 1500 lines: the reviewer reads the whole diff (Refs #1828)."""

    def diff(self, lines=2000):
        body = "".join("+%s\n" % ("x" * 60) for _ in range(lines))
        return ("diff --git a/tools/lease-audit.py b/tools/lease-audit.py\n"
                "--- a/tools/lease-audit.py\n+++ b/tools/lease-audit.py\n" + body)

    def test_the_task_does_not_cut_a_big_diff(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, self.diff(), "")

        body = d.review_item(pr(12, 102))["body"]

        self.assertNotIn("+" + "x" * 60, body)
        self.assertIn("git diff HEAD...rebase-pr-12", body)

    def test_a_finding_in_the_second_half_of_the_diff_is_not_swallowed(self):
        # The reviewer read the whole diff and quotes a line of its second half: the
        # content blocker is a real finding, not a failure of the evidence gathering.
        text = ("VERDICT: CHANGES\nSUMMARY: 无法获取 diff\nBLOCKERS: 1\n## Findings\n"
                "- [BLOCKER] tools/lease-audit.py:1203: the lease check never runs\n")

        self.assertFalse(status.tool_fail(text))

    def test_real_local_diff_contains_second_half(self):
        repo = pathlib.Path(self.tmp) / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True)
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (repo / "file.py").write_text("base\n")
        git("add", "file.py")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD").strip()
        (repo / "file.py").write_text("".join(
            "DEFECT: lease check disabled\n" if i == 1899 else "line %d\n" % i
            for i in range(2000)))
        git("commit", "-qam", "head")
        git("branch", "rebase-pr-12")
        git("checkout", "-q", "--detach", base)
        diff = git("diff", "HEAD...rebase-pr-12")
        self.assertIn("+line 1999", diff)
        self.assertGreater(len(diff.splitlines()), 1500)

        # A deterministic reviewer reads the actual unified diff and derives the
        # finding location from its hunk, as a sandboxed reviewer would.
        filename, line, finding = None, None, None
        for row in diff.splitlines():
            if row.startswith("+++ b/"):
                filename = row[6:]
            elif row.startswith("@@"):
                line = int(re.search(r"\+(\d+)", row).group(1))
            elif line is not None and row.startswith("+"):
                if "DEFECT:" in row:
                    finding = "- [BLOCKER] %s:%d: %s" % (filename, line, row[1:])
                line += 1
            elif line is not None and row.startswith(" "):
                line += 1
        self.assertEqual(finding, "- [BLOCKER] file.py:1900: DEFECT: lease check disabled")
        review = "VERDICT: CHANGES\nSUMMARY: 发现缺陷\nBLOCKERS: 1\n## Findings\n" + finding + "\n"
        rules = [("git -C", lambda text: (0, "" if "-evidence.md" in text else review, "")),
                 ("pr view", (0, json.dumps({"headRefOid": SHA, "state": "OPEN",
                                             "headRefName": "fleet/M5-issue200-r",
                                             "body": "Closes #200"}), ""))]
        d = self.make(rules)
        state = {"issue": "PR20", "run": "r1", "review_pr": 20, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, state, info, {}))
        self.assertEqual(d.load_reviews()["20"]["verdict"], "CHANGES")
        self.assertIn("state=failure", self.runner.called("statuses/")[0])


class ReviewToolFailTests(Base):
    """A review that could not read the diff is not released (Refs #1828)."""

    def state(self, pr_number=20):
        return {"issue": "PR%s" % pr_number, "run": "r1", "review_pr": pr_number, "review_sha": SHA}

    def rules(self, verdict):
        def show(text):
            if "-evidence.md" in text:
                return (0, "", "")
            return (0, verdict, "")

        return [("git -C", show),
                ("pr view", (0, json.dumps({"headRefOid": SHA, "state": "OPEN",
                                            "headRefName": "fleet/M5-issue200-r",
                                            "body": "Closes #200"}), ""))]

    def finish(self, verdict):
        d = self.make(self.rules(verdict))
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        done = d.finish_review("M5", {"host": "box", "backend": "pi"}, self.state(), info, {})
        return d, done

    def test_an_approval_without_the_diff_is_not_released(self):
        d, done = self.finish(APPROVE_WITHOUT_DIFF)

        self.assertTrue(done)
        self.assertEqual(self.runner.called("statuses/"), [])
        self.assertEqual(self.runner.called("--add-label lane-reviewed"), [])
        review = d.load_reviews()["20"]
        self.assertNotIn("verdict", review)
        self.assertEqual(review["fails"], 1)
        self.assertEqual(review["fail_classes"], ["tool_fail"])

    def test_a_change_without_the_diff_is_not_released(self):
        d, done = self.finish(CHANGES_WITHOUT_DIFF)

        self.assertTrue(done)
        self.assertEqual(self.runner.called("statuses/"), [])
        self.assertEqual(self.runner.called("--add-label needs-changes"), [])
        review = d.load_reviews()["20"]
        self.assertNotIn("verdict", review)
        self.assertEqual(review["fails"], 1)

    def test_a_change_with_a_real_blocker_is_not_swallowed(self):
        d, done = self.finish(CHANGES_WITH_A_BLOCKER)

        self.assertTrue(done)
        self.assertIn("state=failure", self.runner.called("statuses/")[0])
        self.assertEqual(d.load_reviews()["20"]["verdict"], "CHANGES")
        self.assertTrue(self.runner.called("--add-label needs-changes"))

    def test_a_normal_review_is_released(self):
        d, done = self.finish(NORMAL_REVIEW)

        self.assertTrue(done)
        self.assertIn("state=success", self.runner.called("statuses/")[0])
        self.assertEqual(d.load_reviews()["20"]["verdict"], "APPROVE")
        self.assertTrue(self.runner.called("--add-label lane-reviewed"))

    def test_followup_with_blocker_word_is_not_a_blocker(self):
        d, done = self.finish(APPROVE_WITHOUT_DIFF + "- [FOLLOW-UP] foo.py:12 无 blocker\n")
        self.assertTrue(done)
        self.assertEqual(d.load_reviews()["20"]["fail_classes"], ["tool_fail"])
        self.assertEqual(self.runner.called("statuses/"), [])

    def test_failed_fetch_dispatches_and_records_tool_fail(self):
        d = self.make(self.rules(APPROVE_WITHOUT_DIFF))
        original_gh, original_sh = d.gh, d.sh
        d.gh = lambda args, **kw: ((1, "", "network failure") if args[:2] == ["pr", "diff"]
                                   else original_gh(args, **kw))
        def sh(cmd, **kw):
            text = " ".join(cmd)
            if "pull/20/head" in text:
                return 1, "", "network failure"
            if "lane assign" in text:
                return 0, '{"id":"r1"}', ""
            return original_sh(cmd, **kw)
        d.sh = sh
        d.local_hosts = set()
        d.local_diff = lambda *args: (None, "")
        d.review_candidates = lambda *args: [pr(20, 200)]
        d.main_sha = lambda: SHA
        lanes = {"M1": {"host": "box", "backend": "pi", "seed_from_box": True}}
        state = {}
        d.dispatch_reviews(lanes, {"M1": {"lane": "idle"}}, state, [])
        self.assertIn("M1", state)
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(d.finish_review("M1", lanes["M1"], state["M1"], info, state))
        self.assertEqual(d.load_reviews()["20"]["fail_classes"], ["tool_fail"])
        self.assertEqual(self.runner.called("statuses/"), [])

    def test_the_tool_fail_is_written_to_the_log(self):
        d, done = self.finish(APPROVE_WITHOUT_DIFF)

        with open(d.path(d.cfg["log_file"]), encoding="utf-8") as stream:
            log = stream.read()
        self.assertIn("TOOL_FAIL", log)


if __name__ == "__main__":
    unittest.main()
