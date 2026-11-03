"""The AD delivery-note mapping pre-check (Refs #1849, rubric A7/F3).

A delivery note without the 「验收条目→改动文件→测试」mapping table goes back to the
author BEFORE the push: no PR, no review, no review round burned.
"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


AD = Path(__file__).resolve().parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch_delivery_map", AD)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ISSUE, RUN = 1849, "20261103-230000-abcdef"
CFG = {"host": "host1", "backend": "codex", "model": "gpt", "repo": "~/work/phanes-fleet"}
CARD = "# 卡\n\n验收（可执行）：\n- 单测：含完整映射表样本→通过\n- CI 中 OpenSwarm tests 全部通过\n"
TABLE = """| 验收 | 改动文件 | 测试 |
| --- | --- | --- |
| 单测：含完整映射表样本→通过 | `tools/lane-autodispatch/autodispatch.py` | `python3 -m unittest openswarm.tests.test_lane_delivery_map` |
| CI 中 OpenSwarm tests 全部通过 | `openswarm/tests/test_lane_delivery_map.py` | `python3 -m unittest discover -s tests` |
"""
NOTE = "Refs #1849\nCloses #1849\n\n交付说明：\n\n" + TABLE
NO_TABLE = "Refs #1849\nCloses #1849\n\n改完了。"


class FakeLaneHost:
    """Records every gh/git call the harvest path makes."""

    def __init__(self, message=NOTE, card=CARD, note_file=""):
        self.message, self.card, self.note_file = message, card, note_file
        self.prs, self.pushes, self.label_edits, self.comments, self.logs = [], [], [], [], []

    def info(self):
        return {"status": "done", "branch": "work", "commit": "c0ffee", "repo": CFG["repo"],
                "backend": "codex", "model": "gpt", "summary": "done", "tests": "unittest", "verify_rc": 0}

    def sh(self, cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        if "--format=%s" in argv: return 0, "fix #1849 delivery map", ""
        if "--format=%B" in argv: return 0, self.message, ""
        if "show" in argv: return 0, self.note_file, ""
        if "push" in argv: self.pushes.append(list(argv))
        return 0, "", ""

    def gh(self, args, timeout=60):
        if args[:2] == ["pr", "create"]: self.prs.append(dict(zip(args[2::2], args[3::2]))); return 0, "https://x/pull/1", ""
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"body": self.card} if "body" in args else {"labels": []}), ""
        if args[:2] == ["issue", "edit"]: self.label_edits.append(list(args))
        if args[:2] == ["issue", "comment"]: self.comments.append(list(args))
        return 0, "", ""

    def log(self, msg, brain=True): self.logs.append(msg)


class DeliveryMapPrecheckTests(unittest.TestCase):
    """The harvest path: what the AD really does with a delivery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("D", self.tmp.name), ("sh", MODULE.sh), ("gh", MODULE.gh), ("log", MODULE.log)):
            self.addCleanup(setattr, MODULE, name, getattr(MODULE, name))
            setattr(MODULE, name, value)
        self.state = {}
        os.environ.pop(MODULE.PR_VALIDATION_ENV, None)

    def finish(self, **kwargs):
        fake = FakeLaneHost(**kwargs)
        MODULE.sh, MODULE.gh, MODULE.log = fake.sh, fake.gh, fake.log
        st = {"issue": ISSUE, "run": RUN, "base": "0123456789abcdef0123", "host": CFG["host"]}
        return fake, MODULE.finish("codex", CFG, st, fake.info(), self.state)

    def test_a_complete_mapping_table_passes(self):
        fake, result = self.finish()
        self.assertEqual(result["status"], "pr-created")
        self.assertEqual((len(fake.pushes), len(fake.prs)), (1, 1))

    def test_a_missing_row_goes_back_to_the_author(self):
        fake, result = self.finish(message=NOTE.split("\n| CI 中 OpenSwarm tests")[0])
        self.assertEqual(result["status"], "delivery-map-failed")
        self.assertEqual(result["missing"], ["验收条目缺映射行：CI 中 OpenSwarm tests 全部通过"])
        self.assertEqual((fake.prs, fake.pushes), ([], []), "the check runs before the push and before the PR")

    def test_a_delivery_note_without_a_table_goes_back_to_the_author(self):
        fake, result = self.finish(message=NO_TABLE)
        self.assertEqual(result["status"], "delivery-map-failed")
        self.assertEqual(result["missing"], [MODULE.DELIVERY_MAP_MISSING])
        self.assertEqual((fake.prs, fake.pushes), ([], []))

    def test_the_reason_is_written_and_the_card_goes_back_to_the_author(self):
        fake, result = self.finish(message=NO_TABLE)
        edits = " ".join(" ".join(args) for args in fake.label_edits)
        self.assertIn("--add-label lane-ready", edits)
        self.assertNotIn("--add-label lane-failed", edits)
        comment = " ".join(fake.comments[-1])
        self.assertIn(MODULE.DELIVERY_MAP_MISSING, comment)
        self.assertIn("不计评审轮次", comment)
        record = self.state["_pr_validation"][-1]
        self.assertEqual((record["class"], record["error_class"]), ("quick_fix", MODULE.DELIVERY_MAP_CLASS))

    def test_the_delivery_note_file_is_a_second_carrier(self):
        fake, result = self.finish(message=NO_TABLE, note_file=NOTE)
        self.assertEqual(result["status"], "pr-created")
        self.assertEqual(len(fake.prs), 1)


if __name__ == "__main__":
    unittest.main()
