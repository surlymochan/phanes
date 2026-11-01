"""Tests for pure dispatch task sources (Refs #1399)."""
import importlib.util
import pathlib
import unittest

MODULE = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "sources.py"
spec = importlib.util.spec_from_file_location("dispatch_sources", MODULE)
sources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sources)


class DispatchSourcesTests(unittest.TestCase):
    def setUp(self):
        # This matches the fields requested by `gh pr list --json`.
        self.prs = [
            {"number": 41, "headRefName": "fix/issue-9", "mergeable": "MERGEABLE",
             "statusCheckRollup": [{"state": "FAILURE"}], "body": "Closes #9"},
            {"number": 42, "headRefName": "feature/issue-10", "mergeable": "CONFLICTING",
             "statusCheckRollup": [{"state": "SUCCESS"}], "body": "Resolves #10"},
            {"number": 43, "headRefName": "healthy", "mergeable": "MERGEABLE",
             "statusCheckRollup": [{"state": "SUCCESS"}], "body": "Closes #11"},
        ]

    def test_ci_red_produces_task(self):
        self.assertEqual(sources.ci_fix_source(self.prs), [{
            "issue": 9, "kind": "ci_fix", "pr": 41,
            "head_sha": "fix/issue-9", "hot_files": [],
        }])

    def test_conflicting_pr_produces_rebase_task(self):
        task, = sources.rebase_source(self.prs)
        self.assertEqual((task["issue"], task["kind"], task["pr"], task["head_sha"]),
                         (10, "rebase", 42, "feature/issue-10"))

    def test_inflight_and_duplicate_heads_are_not_queued_twice(self):
        same_head = dict(self.prs[0])
        inflight = [{"kind": "ci_fix", "pr": 41, "head_sha": "fix/issue-9"}]
        self.assertEqual(sources.ci_fix_source(self.prs + [same_head], inflight), [])
        self.assertEqual(sources.ci_fix_source(self.prs + [same_head]),
                         sources.ci_fix_source(self.prs))

    def test_closes_issue_and_hot_file_declaration_are_parsed(self):
        self.assertEqual(sources.labels_source([{
            "number": 9, "body": "Closes #9\n热文件：`src/a.py`，src/b.py\n热文件：src/a.py"
        }]), [{"issue": 9, "kind": "labels", "pr": None,
               "head_sha": None, "hot_files": ["src/a.py", "src/b.py"]}])
        task, = sources.ci_fix_source([self.prs[0]])
        self.assertEqual(task["issue"], 9)  # Parsed from the PR's Closes #N body.


if __name__ == "__main__":
    unittest.main()
