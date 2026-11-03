"""Contract of the lane PR pre-submit validation and of the submit path (Refs #1704).

The submit path is driven with a fake lane host: the tests assert what the AD would
really send to GitHub (PR created or not, labels, comment, run record) instead of only
the return value of the pure validator.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ISSUE = 1704
RUN = "20261103-013813-216a11"
CFG = {"host": "host1", "backend": "codex", "model": "gpt", "repo": "~/work/phanes-fleet"}
MESSAGE_WITH_ALL = "Rewrote the parser.\n\nCloses #1704\n\n验收：python3 -m unittest tools/tests (passed)"
# The delivery note of the fake lane carries the mapping table of the delivery-note
# pre-check (Refs #1849); the mapping table itself is asserted in
# openswarm/tests/test_lane_delivery_map.py.
DELIVERY_MAP = "\n| 验收 | 改动文件 | 测试 |\n| --- | --- | --- |\n| `python3 -m unittest tools/tests` | `tools/lane-autodispatch/autodispatch.py` | `python3 -m unittest tools/tests` |\n"


def run_remote_status(home, payload, run=RUN, lane="codex"):
    """Run the AD's remote status snippet against a fake lane home.

    This is the real read path of the run info (the snippet the AD sends to the lane
    host), not a hand written stand-in: the fields the submit path relies on are read
    exactly as production reads them."""
    root = Path(home) / ".claude" / "openswarm-executor-candidate"
    (root / "runs" / run).mkdir(parents=True, exist_ok=True)
    (root / "runs" / run / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    old_home, old_argv = os.environ.get("HOME"), sys.argv
    os.environ["HOME"] = str(home)
    sys.argv = ["status", lane, run]
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exec(compile(MODULE.REMOTE_STATUS, "<remote_status>", "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = old_argv
        if old_home is None: os.environ.pop("HOME", None)
        else: os.environ["HOME"] = old_home
    return json.loads(buffer.getvalue().strip().splitlines()[-1])


class FakeLaneHost:
    """Records every gh/git call the submit path makes."""

    def __init__(self, subject="fix #1704 parser", message="", summary="", tests="", verify_rc=None):
        self.subject = subject
        self.message = message
        self.summary = summary
        self.tests = tests
        self.verify_rc = verify_rc
        self.prs = []          # gh pr create payloads
        self.label_edits = []  # gh issue edit payloads
        self.comments = []     # gh issue comment payloads
        self.run_records = []  # run records written next to the run on the lane host
        self.logs = []

    def info(self):
        return {"status": "done", "branch": "work", "commit": "c0ffee", "repo": CFG["repo"],
                "backend": CFG["backend"], "model": CFG["model"], "summary": self.summary,
                "tests": self.tests, "verify_rc": self.verify_rc}

    def sh(self, cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        if isinstance(cmd, list) and "--format=%s" in cmd: return 0, self.subject, ""
        if isinstance(cmd, list) and "--format=%B" in cmd: return 0, self.message + DELIVERY_MAP, ""
        if inp and "pr-validation.json" in inp:  # the run record write on the lane host
            self.run_records.append(json.loads(base64.b64decode(argv[-1]).decode("utf-8")))
            return 0, "ok", ""
        return 0, "", ""

    def gh(self, args, timeout=60):
        if args[:2] == ["pr", "create"]:
            self.prs.append(dict(zip(args[2::2], args[3::2])))
            return 0, "https://github.com/surlymochan/phanes/pull/1", ""
        if args[:2] == ["issue", "view"]: return 0, json.dumps({"labels": []}), ""
        if args[:2] == ["issue", "edit"]:
            self.label_edits.append(list(args)); return 0, "", ""
        if args[:2] == ["issue", "comment"]:
            self.comments.append(list(args)); return 0, "", ""
        return 0, "", ""

    def log(self, msg, brain=True):
        self.logs.append(msg)


class LanePrValidationTests(unittest.TestCase):
    """The pure rules: closes reference, acceptance section, issue number in the title."""

    def test_missing_closes(self):
        self.assertEqual(MODULE.validate_lane_pr(ISSUE, "Fix #1704", "验收：passed"),
                         ["PR 正文缺少 Closes/Fixes/Resolves #1704"])

    def test_missing_acceptance(self):
        self.assertIn("PR 正文缺少验收段或 Verify 段",
                      MODULE.validate_lane_pr(ISSUE, "Fix #1704", "Closes #1704"))

    def test_missing_title_issue(self):
        self.assertIn("PR 标题缺少 issue 号 #1704",
                      MODULE.validate_lane_pr(ISSUE, "Fix issue", "Closes #1704\n\nVerify: passed"))

    def test_complete(self):
        self.assertEqual(MODULE.validate_lane_pr(ISSUE, "Fix #1704", "Resolves #1704\n\nVerify: passed"), [])

    def test_acceptance_needs_content(self):
        self.assertIn("PR 正文缺少验收段或 Verify 段",
                      MODULE.validate_lane_pr(ISSUE, "Fix #1704", "Closes #1704\n\nVerify:"))

    def test_closing_reference_of_another_issue_does_not_count(self):
        self.assertIn("PR 正文缺少 Closes/Fixes/Resolves #1704",
                      MODULE.validate_lane_pr(ISSUE, "Fix #1704", "Closes #999\n\n验收：passed"))


class LanePrSubmitTests(unittest.TestCase):
    """The submit path: what the AD really sends to GitHub for each sample."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("D", self.tmp.name), ("sh", MODULE.sh), ("gh", MODULE.gh), ("log", MODULE.log)):
            self.addCleanup(setattr, MODULE, name, getattr(MODULE, name))
            setattr(MODULE, name, value)
        self.state = {}

    def finish(self, info=None, **kwargs):
        fake = FakeLaneHost(**kwargs)
        MODULE.sh, MODULE.gh, MODULE.log = fake.sh, fake.gh, fake.log
        st = {"issue": ISSUE, "run": RUN, "base": "0123456789abcdef0123", "host": CFG["host"]}
        result = MODULE.finish("codex", CFG, st, info or fake.info(), self.state)
        return fake, result

    def test_missing_closes_is_blocked_and_the_pr_is_not_created(self):
        # the title carries the issue number, but the lane never wrote a closing keyword:
        # a number in the title is not a closing instruction and the AD must not invent one
        fake, result = self.finish(subject="fix #1704 parser",
                                   message="Rewrote the parser.\n\n验收：python3 -m unittest (passed)")
        self.assertEqual(fake.prs, [], "a PR with a missing Closes reference must not be created")
        self.assertEqual(result["status"], "pr-validation-failed")
        self.assertEqual(result["missing"], ["PR 正文缺少 Closes/Fixes/Resolves #1704"])
        self.assertIn("PR 正文缺少 Closes/Fixes/Resolves #1704", result["error"])

    def test_missing_closes_and_title_are_listed_one_by_one(self):
        fake, result = self.finish(subject="fix the parser",
                                   message="Rewrote the parser.\n\n验收：python3 -m unittest (passed)")
        self.assertEqual(fake.prs, [])
        self.assertEqual(result["missing"], ["PR 正文缺少 Closes/Fixes/Resolves #1704",
                                             "PR 标题缺少 issue 号 #1704"])

    def test_closing_keyword_of_another_issue_does_not_close_the_task(self):
        # the lane closed a different issue: the task issue stays open, so the PR is blocked
        fake, result = self.finish(subject="fix #1704 parser",
                                   message="Closes #999\n\n验收：python3 -m unittest (passed)")
        self.assertEqual(fake.prs, [])
        self.assertEqual(result["missing"], ["PR 正文缺少 Closes/Fixes/Resolves #1704"])

    def test_missing_acceptance_is_blocked_and_the_pr_is_not_created(self):
        fake, result = self.finish(subject="fix #1704 parser", message="Rewrote the parser.\n\nCloses #1704")
        self.assertEqual(fake.prs, [])
        self.assertEqual(result["missing"], ["PR 正文缺少验收段或 Verify 段"])

    def test_title_without_issue_number_is_blocked(self):
        fake, result = self.finish(subject="fix the parser", message=MESSAGE_WITH_ALL)
        self.assertEqual(fake.prs, [])
        self.assertEqual(result["missing"], ["PR 标题缺少 issue 号 #1704"])

    def test_complete_pr_is_created_with_the_validated_title_and_body(self):
        fake, result = self.finish(subject="fix #1704 parser", message=MESSAGE_WITH_ALL)
        self.assertEqual(result["status"], "pr-created")
        self.assertIsNone(result["error"])
        self.assertEqual(len(fake.prs), 1)
        self.assertEqual(fake.prs[0]["--title"], "fix #1704 parser")
        self.assertIn("Closes #1704", fake.prs[0]["--body"])
        self.assertIn("验收：python3 -m unittest tools/tests (passed)", fake.prs[0]["--body"])

    def test_host_verify_rc_is_the_acceptance_evidence(self):
        fake, _ = self.finish(subject="fix #1704 parser", message="Rewrote the parser.\n\nCloses #1704",
                              verify_rc=0)
        self.assertIn("验收：host verify rc `0`", fake.prs[0]["--body"])

    def test_missing_items_are_listed_one_by_one(self):
        fake, result = self.finish(subject="fix the parser", message="Rewrote the parser.")
        self.assertEqual(result["missing"], ["PR 正文缺少 Closes/Fixes/Resolves #1704",
                                             "PR 正文缺少验收段或 Verify 段",
                                             "PR 标题缺少 issue 号 #1704"])
        self.assertIn("PR 正文缺少 Closes/Fixes/Resolves #1704", result["error"])
        self.assertIn("PR 正文缺少验收段或 Verify 段", result["error"])
        self.assertIn("PR 标题缺少 issue 号 #1704", result["error"])
        comment = " ".join(fake.comments[-1])
        for item in result["missing"]:
            self.assertIn(item, comment)

    def test_strict_mode_is_the_default(self):
        self.assertEqual(MODULE.pr_validation_mode(), "strict")
        os.environ.pop(MODULE.PR_VALIDATION_ENV, None)
        self.assertEqual(MODULE.pr_validation_mode(), "strict")

    def test_warn_mode_opens_the_pr_and_only_warns(self):
        os.environ[MODULE.PR_VALIDATION_ENV] = "warn"
        self.addCleanup(os.environ.pop, MODULE.PR_VALIDATION_ENV, None)
        fake, result = self.finish(subject="fix the parser", message="Rewrote the parser.")
        self.assertEqual(len(fake.prs), 1)
        self.assertEqual(result["status"], "pr-created")
        self.assertTrue(any("PR validation (warn)" in line for line in fake.logs))

    def test_unknown_mode_falls_back_to_strict(self):
        os.environ[MODULE.PR_VALIDATION_ENV] = "whatever"
        self.addCleanup(os.environ.pop, MODULE.PR_VALIDATION_ENV, None)
        self.assertEqual(MODULE.pr_validation_mode(), "strict")

    def test_blocked_pr_is_a_quick_fix_and_not_a_lane_failure(self):
        fake, result = self.finish(subject="fix the parser",
                                   message="Rewrote the parser.\n\n验收：python3 -m unittest (passed)")
        edits = " ".join(" ".join(args) for args in fake.label_edits)
        self.assertIn("lane-ready", edits)
        self.assertNotIn("--add-label lane-failed", edits)
        self.assertEqual(self.state["_pr_validation"][-1]["class"], "quick_fix")
        self.assertEqual(self.state["_pr_validation"][-1]["error_class"], "pr_validation")
        self.assertEqual(self.state["_pr_validation"][-1]["missing"], result["missing"])

    def test_validation_result_is_written_into_the_run_record_and_the_run_log(self):
        fake, result = self.finish(subject="fix the parser",
                                   message="Rewrote the parser.\n\n验收：python3 -m unittest (passed)")
        self.assertEqual(len(fake.run_records), 1)
        record = fake.run_records[0]
        self.assertEqual(record["run"], RUN)
        self.assertEqual(record["class"], "quick_fix")
        self.assertEqual(record["error_class"], "pr_validation")
        self.assertEqual(record["missing"], result["missing"])
        run_log = Path(self.tmp.name) / "runs" / f"{RUN}.log"
        self.assertTrue(run_log.exists())
        logged = run_log.read_text(encoding="utf-8")
        self.assertIn("pr_validation", logged)
        self.assertIn("PR 正文缺少 Closes/Fixes/Resolves #1704", logged)


class LaneRunInfoReadPathTests(unittest.TestCase):
    """The real run info read path: missing fields must stay missing, not become evidence."""

    RESULT = {"status": "done", "branch": "work", "commit": "c0ffee",
              "workspace": {"repo": "~/work/phanes-fleet"}, "backend": "codex", "model": "gpt"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("D", self.tmp.name), ("sh", MODULE.sh), ("gh", MODULE.gh), ("log", MODULE.log)):
            self.addCleanup(setattr, MODULE, name, getattr(MODULE, name))
            setattr(MODULE, name, value)
        self.state = {}

    def read_run_info(self, result):
        payload = dict(self.RESULT, result=result)
        with tempfile.TemporaryDirectory() as home:
            status = run_remote_status(home, payload)
        self.assertIsInstance(status["run"], dict)
        return status["run"]

    def test_missing_tests_and_summary_stay_empty(self):
        info = self.read_run_info({"summary": "Rewrote the parser."})
        self.assertEqual(info["tests"], "", "a missing test result must not become the string `None`")
        self.assertEqual(info["summary"], "Rewrote the parser.")
        self.assertIsNone(info["verify_rc"])

    def test_empty_result_keeps_summary_and_tests_empty(self):
        info = self.read_run_info({})
        self.assertEqual(info["summary"], "")
        self.assertEqual(info["tests"], "")
        self.assertIsNone(info["verify_rc"])

    def test_missing_acceptance_from_the_real_run_info_is_blocked(self):
        # the run reported no acceptance and the host verify rc is missing: the PR body must
        # not get a made up acceptance line, and the PR must not be created
        info = self.read_run_info({"summary": "Rewrote the parser."})
        fake = FakeLaneHost(subject="fix #1704 parser", message="Rewrote the parser.\n\nCloses #1704")
        MODULE.sh, MODULE.gh, MODULE.log = fake.sh, fake.gh, fake.log
        st = {"issue": ISSUE, "run": RUN, "base": "0123456789abcdef0123", "host": CFG["host"]}
        result = MODULE.finish("codex", CFG, st, info, self.state)
        self.assertEqual(fake.prs, [])
        self.assertEqual(result["status"], "pr-validation-failed")
        self.assertEqual(result["missing"], ["PR 正文缺少验收段或 Verify 段"])


if __name__ == "__main__":
    unittest.main()
