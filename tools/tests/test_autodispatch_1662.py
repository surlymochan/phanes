"""Regression tests for #1662's refill safety findings.

These tests intentionally describe the required behavior before the dispatcher
implements it: malformed planner responses must not create a second planner,
and invalid backlog cards must be quarantined without replacing prior evidence.

Run with:

    python3 -m unittest tools.tests.test_autodispatch_1662 -v
"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch_1662", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Replenish1662Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, MODULE, "D", MODULE.D)
        self.addCleanup(setattr, MODULE, "gh", MODULE.gh)
        self.addCleanup(setattr, MODULE, "sh", MODULE.sh)
        self.addCleanup(setattr, MODULE, "log", MODULE.log)
        MODULE.D = self.tmp.name
        self.logs = []
        MODULE.log = lambda message, brain=True: self.logs.append(message)
        MODULE.sh = lambda *args, **kwargs: (0, "", "")

    def test_invalid_backlog_card_is_quarantined_without_overwriting_prior_copy(self):
        """Two rejected cards sharing a filename both remain recoverable."""
        backlog = Path(self.tmp.name) / "backlog"
        rejected = backlog / "rejected"
        rejected.mkdir(parents=True)
        (rejected / "same.md").write_text("earlier rejected card\n", encoding="utf-8")
        (backlog / "same.md").write_text("not a card\n", encoding="utf-8")
        MODULE.gh = lambda args, timeout=60: (1, "", "unexpected GitHub call")

        MODULE.replenish(1, {})

        copies = list(rejected.glob("same*.md"))
        self.assertEqual(len(copies), 2, "quarantine must retain the existing card and incoming card")
        self.assertEqual({path.read_text(encoding="utf-8") for path in copies},
                         {"earlier rejected card\n", "not a card\n"})

    def test_malformed_planner_query_skips_creation_and_records_diagnostic(self):
        """A successful but malformed planner-list response is unknown, not empty."""
        created = []

        def gh(args, timeout=60):
            if args[:3] == ["issue", "list", "--state"]:
                if "planner" in args:
                    return 0, "{not-json", ""
                return 0, "[]", ""
            if args[:2] == ["issue", "create"]:
                created.append(args)
                return 0, "https://github.com/surlymochan/phanes/issues/16620", ""
            return 0, "", ""

        MODULE.gh = gh
        MODULE.replenish(1, {})

        self.assertEqual(created, [], "planner creation is unsafe when the existing-planner query is unknown")
        self.assertTrue(any("planner" in message.lower() and "json" in message.lower()
                            for message in self.logs), "the skipped creation must explain the malformed response")


if __name__ == "__main__":
    unittest.main()
