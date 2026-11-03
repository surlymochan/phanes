"""A review task must carry the PR diff even when GitHub does not answer (Refs #1731).

The second review round of PR #1804 could not be done at all: the review lane could not get the
diff (github.com did not resolve, `gh` was not installed there), so the round had nothing to
review.  The dispatcher therefore rebuilds the diff from the objects it holds locally (the PR head
ref and the base branch) when `gh pr diff` comes back empty.
"""
from tests.test_dispatch_engine import Base, SHA, pr

LOCAL_DIFF = ("diff --git a/lib/x.py b/lib/x.py\n--- a/lib/x.py\n+++ b/lib/x.py\n"
              "@@ -1 +1 @@\n-old\n+new\n")


class ReviewDiff(Base):
    """The diff of the review task: GitHub first, the local objects as the fallback."""

    def make_with(self, gh_diff=(1, "", "could not resolve host: github.com"), head=True, base=True):
        rules = [("gh pr diff", gh_diff)]
        if head:
            rules.append(("refs/ad/pr-12", (0, SHA, "")))
        if base:
            rules.append(("refs/remotes/origin/main", (0, "c" * 40, "")))
        rules.append(("git -C", lambda text: (0, LOCAL_DIFF, "") if " diff " in text else (0, "", "")))
        return self.make(rules)

    def test_the_diff_of_github_is_used_when_it_answers(self):
        d = self.make_with(gh_diff=(0, "diff --git a/x b/x\n+from github\n", ""))
        item = d.review_item(pr(12, 102))
        self.assertIn("+from github", item["body"])
        self.assertFalse(self.runner.called("git -C"))    # no local lookup needed

    def test_the_diff_is_rebuilt_from_the_local_objects_when_github_is_unreachable(self):
        d = self.make_with()
        item = d.review_item(pr(12, 102))
        self.assertIn("+new", item["body"])               # the diff of the local objects
        self.assertNotIn("+from github", item["body"])
        self.assertTrue(self.runner.called("diff --no-color"))

    def test_the_rebuilt_diff_is_logged(self):
        d = self.make_with()
        d.review_item(pr(12, 102))
        with open(d.path("dispatch.log")) as stream:
            log = stream.read()
        self.assertIn("GitHub gave no diff", log)
        self.assertIn("rebuilt it from the local objects", log)

    def test_the_reported_head_sha_is_used_when_no_pr_ref_exists(self):
        d = self.make_with(head=False)
        d.runner.rules.insert(1, ("cat-file", (0, "", "")))     # the head sha of the PR list is local
        item = d.review_item(pr(12, 102))
        self.assertIn("+new", item["body"])

    def test_without_any_diff_the_task_says_so_instead_of_pretending(self):
        d = self.make_with(head=False, base=False)
        item = d.review_item(pr(12, 102))
        self.assertIsNone(item)                       # no diff: the review is not dispatched
        with open(d.path("dispatch.log")) as stream:
            log = stream.read()
        self.assertIn("no diff at all", log)
        self.assertIn("the review is not dispatched", log)

    def test_a_stale_local_ref_is_not_reviewed(self):
        d = self.make_with(head=False)
        d.runner.rules.insert(1, ("refs/ad/pr-12", (0, "c" * 40, "")))       # an older head
        d.runner.rules.insert(2, ("cat-file", (1, "", "unknown revision")))   # the current head is not local
        item = d.review_item(pr(12, 102))
        self.assertIsNone(item)                       # the diff of the old head is never reviewed
        with open(d.path("dispatch.log")) as stream:
            log = stream.read()
        self.assertIn("is stale", log)
        self.assertIn("the review is not dispatched", log)

    def test_a_missing_issue_text_is_logged_as_well(self):
        d = self.make_with()
        p = pr(12, 102)
        p["issue"] = 102
        d.review_item(p)
        with open(d.path("dispatch.log")) as stream:
            log = stream.read()
        self.assertIn("the issue text is missing", log)

    def test_the_pre_review_task_carries_the_diff_as_well(self):
        d = self.make_with()
        item = d.pre_review_item(pr(12, 102))
        self.assertTrue(item["pre_review"])
        self.assertIn("+new", item["body"])               # the pre-review lane is not blind either
