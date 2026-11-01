import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
from external_lane import contract as C  # noqa: E402
from fakes import issue  # noqa: E402


class ContractTest(unittest.TestCase):
    def test_branch_round_trip(self):
        b = C.branch_name("chatgpt-agent", 42, now=0)
        self.assertEqual(b, "fleet/chatgpt-agent-issue42-19700101-000000")
        self.assertEqual(C.parse_branch(b), ("chatgpt-agent", 42))
        self.assertIsNone(C.parse_branch("fleet/M5-issue42-20261101-1"))  # fleet lanes are upper-case, not external
        self.assertIsNone(C.parse_branch("feature/x"))

    def test_agent_name_validated(self):
        for bad in ("", "Has Space", "UPPER", "a" * 60, "-lead"):
            with self.assertRaises(ValueError):
                C.backend_label(bad)
        self.assertEqual(C.backend_label("dot"), "backend:dot")
        self.assertEqual(C.claim_comment("dot"), "claimed by dot")

    def test_linked_issue_prefers_closes_then_branch(self):
        self.assertEqual(C.linked_issue("Fixes #7 and more", "fleet/dot-issue9-1-2"), 7)
        self.assertEqual(C.linked_issue("no link", "fleet/dot-issue9-20261102-010203"), 9)
        self.assertIsNone(C.linked_issue("Refs #3", "fleet/dot-x"))

    def test_claimable(self):
        self.assertTrue(C.claimable(issue(1, ["lane-ready", "backend:dot", "p1"]), "dot"))
        self.assertFalse(C.claimable(issue(1, ["lane-ready", "backend:other"]), "dot"))
        self.assertFalse(C.claimable(issue(1, ["lane-ready", "backend:dot", "lane-claimed"]), "dot"))
        self.assertFalse(C.claimable(issue(1, ["lane-ready", "backend:dot", "in-progress"]), "dot"))
        self.assertFalse(C.claimable(issue(1, ["lane-ready", "backend:dot"], state="closed"), "dot"))
        pr = issue(1, ["lane-ready", "backend:dot"]); pr["pull_request"] = {}
        self.assertFalse(C.claimable(pr, "dot"))

    def test_label_transitions(self):
        base = {"lane-ready", "backend:dot", "p1"}
        self.assertEqual(C.claim_labels(base), {"lane-claimed", "backend:dot", "p1"})
        self.assertEqual(C.pr_opened_labels(C.claim_labels(base)), {"lane-pr", "backend:dot", "p1"})
        self.assertEqual(C.failed_labels(C.claim_labels(base)), {"lane-failed", "backend:dot", "p1"})
        self.assertEqual(C.requeue_labels({"lane-claimed", "backend:dot"}), {"lane-ready", "backend:dot"})

    def test_thin_reason(self):
        self.assertEqual(C.thin_reason([]), "empty-diff")
        self.assertIn("effective<30", C.thin_reason([{"filename": "a.py", "additions": 10, "deletions": 2}]))
        self.assertIn("mostly-docs", C.thin_reason([{"filename": "a.py", "additions": 40},
                                                    {"filename": "docs/x.md", "additions": 50}]))
        self.assertEqual(C.thin_reason([{"filename": "tools/x.py", "additions": 25},
                                        {"filename": "pkg/tests/test_x.py", "additions": 10}]), "")

    def test_body_markers(self):
        body = "重基PR：#900 fleet/A2-issue874-x\n热文件：a.py, `b/c.py`\n依赖：#1 #2\n\n验收：`python3 -m pytest -q`"
        self.assertEqual(C.rework_target(body), (900, "fleet/A2-issue874-x"))
        self.assertEqual(C.hot_files(body), {"a.py", "b/c.py"})
        self.assertEqual(C.depends_on(body), [1, 2])
        self.assertEqual(C.verify_command(body), "python3 -m pytest -q")
        self.assertEqual(C.rework_target("Rework PR: #5 fleet/dot-issue1-1"), (5, "fleet/dot-issue1-1"))


if __name__ == "__main__":
    unittest.main()
