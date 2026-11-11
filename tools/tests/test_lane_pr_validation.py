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
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "lane-autodispatch" / "autodispatch.py"
SPEC = importlib.util.spec_from_file_location("autodispatch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

ISSUE = 1704
RUN = "20261103-013813-216a11"
CFG = {"host": "host1", "backend": "codex", "model": "gpt", "repo": "~/work/phanes-fleet"}
MESSAGE_WITH_ALL = "Rewrote the parser.\n\nCloses #1704\n\n验收：python3 -m unittest tools/tests (passed)"


DELIVERY_MAP = "\n| 验收 | 改动文件 | 测试 |\n| --- | --- | --- |\n| check | code.py | unittest |\n"

def run_remote_status(home, payload, run=RUN, lane="codex"):
    """Run the AD's remote status snippet against a fake lane home.

    This is the real read path of the run info (the snippet the AD sends to the lane
    host), not a hand written stand-in: the fields the submit path relies on are read
    exactly as production reads them."""
    root = Path(home) / ".claude" / "swrm-executor-candidate"
    (root / "runs" / run).mkdir(parents=True, exist_ok=True)
    (root / "runs" / run / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    old_home, old_argv = os.environ.get("HOME"), sys.argv
    os.environ["HOME"] = str(home)
    sys.argv = ["status", lane, run]
    try:
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(root)}), \
                contextlib.redirect_stdout(buffer):
            exec(compile(MODULE.REMOTE_STATUS, "<remote_status>", "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = old_argv
        if old_home is None: os.environ.pop("HOME", None)
        else: os.environ["HOME"] = old_home
    return json.loads(buffer.getvalue().strip().splitlines()[-1])


def make_lane_repo(root):
    """A real local clone with a base commit and a lane branch.

    The harvest filters the fetched lane branch before it pushes it (Refs #1835), so
    the submit path needs a repository the filter can read."""
    repo = Path(root) / "repo"
    repo.mkdir()

    def git(*args):
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError("git %s failed: %s" % (args[0], proc.stderr.strip()))
        return proc.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "code.py").write_text("print('lane')\n", encoding="utf-8")
    git("add", "code.py")
    git("commit", "-qm", "lane")
    git("update-ref", "refs/fleet/codex", git("rev-parse", "HEAD"))
    return repo, base


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
        if args[:2] == ["api", "graphql"]:
            return 0, json.dumps({"data": {"repository": {"pullRequests": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}), ""
        if args[:2] == ["pr", "create"]:
            self.prs.append(dict(zip(args[2::2], args[3::2])))
            return 0, "https://github.com/surlymochan/phanes/pull/1", ""
        if args[:2] == ["issue", "view"]: return 0, json.dumps({"labels": [{"name": "lane-pr"}] if any("lane-pr" in edit for edit in self.label_edits) else [], "body": "# card"}), ""
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
        self.repo, self.base = make_lane_repo(self.tmp.name)
        self.addCleanup(setattr, MODULE, "LOCAL", MODULE.LOCAL)
        MODULE.LOCAL = str(self.repo)

    def finish(self, info=None, **kwargs):
        fake = FakeLaneHost(**kwargs)
        MODULE.sh, MODULE.gh, MODULE.log = fake.sh, fake.gh, fake.log
        st = {"issue": ISSUE, "run": RUN, "base": self.base, "host": CFG["host"]}
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

    def test_start_failure_with_commit_requeues_without_harvesting_or_counting(self):
        info = FakeLaneHost().info()
        info.update(status="failed", error_class="executor_start_failed")
        with mock.patch.object(MODULE, "pause_store_dir", return_value=self.tmp.name), \
             mock.patch.object(MODULE, "harvest_fetch", wraps=MODULE.harvest_fetch) as fetch:
            fake, result = self.finish(info=info, message=MESSAGE_WITH_ALL)
            records = MODULE.pause_records()
        fetch.assert_not_called()
        self.assertEqual(result["status"], "lane-ready")
        self.assertTrue(result["done"])
        self.assertEqual(fake.prs, [])
        self.assertEqual(fake.label_edits[-1][-1], "lane-ready")
        self.assertNotIn(ISSUE, self.state.get("_done", []))
        self.assertEqual(records[0]["scope"], "lane:codex")
        self.assertEqual(records[0]["expires_at"] - records[0]["created_at"], 600)

    def test_start_failure_with_commit_keeps_lane_pending_if_requeue_fails(self):
        info = FakeLaneHost().info()
        info.update(status="failed", error_class="executor_start_failed")
        with mock.patch.object(MODULE, "pause_store_dir", return_value=self.tmp.name), \
             mock.patch.object(MODULE, "gh", return_value=(1, "", "labels unavailable")) as gh, \
             mock.patch.object(MODULE, "open_pr_for_issue", return_value=None), \
             mock.patch.object(MODULE, "sh") as sh, \
             mock.patch.object(MODULE, "harvest_fetch") as fetch, \
             mock.patch.object(MODULE, "log"):
            result = MODULE.finish("codex", CFG, {"issue": ISSUE, "run": RUN}, info, self.state)
        self.assertFalse(result["done"])
        self.assertEqual(result["status"], "requeue-failed")
        self.assertEqual(result["error"], "PR guard unverified")
        self.assertNotIn(ISSUE, self.state.get("_done", []))
        gh.assert_called_once_with(["issue", "edit", str(ISSUE), "--remove-label", "in-progress",
                                    "--remove-label", "lane-failed", "--add-label", "lane-ready"])
        fetch.assert_not_called()
        sh.assert_not_called()

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
        self.repo, self.base = make_lane_repo(self.tmp.name)
        self.addCleanup(setattr, MODULE, "LOCAL", MODULE.LOCAL)
        MODULE.LOCAL = str(self.repo)

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
        st = {"issue": ISSUE, "run": RUN, "base": self.base, "host": CFG["host"]}
        result = MODULE.finish("codex", CFG, st, info, self.state)
        self.assertEqual(fake.prs, [])
        self.assertEqual(result["status"], "pr-validation-failed")
        self.assertEqual(result["missing"], ["PR 正文缺少验收段或 Verify 段"])


class ExecutorStartHarvestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(MODULE, "D", directory.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_start_failure_with_empty_commit_requeues_without_harvest(self):
        for rc in (0, 1):
            with self.subTest(relabel_rc=rc), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.object(MODULE, "pause_store_dir", return_value=directory), \
                 mock.patch.object(MODULE, "gh", return_value=(rc, "", "labels unavailable")) as gh, \
                 mock.patch.object(MODULE, "open_pr_for_issue", return_value=None), \
                 mock.patch.object(MODULE, "log"), \
                 mock.patch.object(MODULE, "harvest_fetch", return_value=(True, "", 0)) as fetch, \
                 mock.patch.object(MODULE, "filter_harvest", return_value=("c0ffee", [".lane-review/PR-1.md"], True)), \
                 mock.patch.object(MODULE, "label_lane_failed") as failed:
                state = {"A1": {"issue": 1595, "run": RUN}, "_done": [12]}
                before = {key: value.copy() for key, value in state.items()}
                info = {"status": "failed", "error_class": "executor_start_failed",
                        "branch": "work", "commit": "c0ffee", "repo": CFG["repo"]}
                result = MODULE.finish("A1", CFG, state["A1"], info, state)
                self.assertEqual(result["status"], "lane-ready" if rc == 0 else "requeue-failed")
                self.assertEqual(result["done"], rc == 0)
                self.assertEqual(state, before, "startup failures must not count as completed attempts")
                gh.assert_called_once_with(["issue", "edit", "1595", "--remove-label", "in-progress",
                                            "--remove-label", "lane-failed", "--add-label", "lane-ready"])
                records = MODULE.pause_records()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["scope"], "lane:A1")
                self.assertEqual(records[0]["expires_at"] - records[0]["created_at"], 600)
                fetch.assert_not_called()
                failed.assert_not_called()

    def test_start_failure_requeues_without_count_and_pauses_lane(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(MODULE, "pause_store_dir", return_value=directory), \
             mock.patch.object(MODULE, "gh", return_value=(0, "", "")) as gh, \
             mock.patch.object(MODULE, "open_pr_for_issue", return_value=None), \
             mock.patch.object(MODULE, "log"), \
             mock.patch.object(MODULE.time, "time", return_value=1000):
            state = {"_done": [12]}
            result = MODULE.finish("A1", CFG, {"issue": 1595, "run": RUN},
                                   {"status": "failed", "error_class": "executor_start_failed"}, state)
            self.assertEqual(result["status"], "lane-ready")
            self.assertTrue(result["done"])
            self.assertEqual(state["_done"], [12])
            edits = [c.args[0] for c in gh.call_args_list if c.args[0][:2] == ["issue", "edit"]]
            self.assertIn("--add-label", edits[0])
            self.assertEqual(edits[0][-1], "lane-ready")
            records = MODULE.pause_records(now=1000)
            self.assertEqual([(r["scope"], r["expires_at"]) for r in records], [("lane:A1", 1600)])
            self.assertTrue(MODULE.lane_paused("A1", scopes=MODULE.paused_scopes(now=1599)))
            self.assertFalse(MODULE.lane_paused("A1", scopes=MODULE.paused_scopes(now=1600)))

    def test_failed_requeue_keeps_harvest_pending(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(MODULE, "pause_store_dir", return_value=directory), \
             mock.patch.object(MODULE, "gh", return_value=(1, "", "labels unavailable")), \
             mock.patch.object(MODULE, "log"):
            state = {}
            result = MODULE.finish("A1", CFG, {"issue": 1595, "run": RUN},
                                   {"error_class": "executor_start_failed"}, state)
            self.assertFalse(result["done"])
            self.assertNotIn("_done", state)


    def test_tick_harvest_requeues_and_honors_new_pause_immediately(self):
        for outcome in ("no-commit", "with-commit", "requeue-failed"):
            with self.subTest(outcome=outcome):
                self.assert_tick_start_failure(outcome)

    def assert_tick_start_failure(self, outcome):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            for name in ("audit_switches", "host_good", "host_fail", "log", "save_state",
                         "update_busy_stale", "prefetch_labels", "replenish"):
                stack.enter_context(mock.patch.object(MODULE, name))
            stack.enter_context(mock.patch.object(MODULE, "D", directory))
            stack.enter_context(mock.patch.object(MODULE, "pause_store_dir", return_value=directory))
            stack.enter_context(mock.patch.object(MODULE, "host_ok", return_value=True))
            fake = FakeLaneHost(subject="fix #1595 startup crash", message="Closes #1595\n\nVerify: passed")
            def gh(args, timeout=60):
                if outcome == "requeue-failed" and args[:2] == ["issue", "edit"]:
                    return 1, "", "labels unavailable"
                return fake.gh(args, timeout=timeout)
            stack.enter_context(mock.patch.object(MODULE, "gh", side_effect=gh))
            info = {"error_class": "executor_start_failed"}
            if outcome != "no-commit":
                info.update(branch="work", commit="c0ffee", repo=CFG["repo"])
            stack.enter_context(mock.patch.object(MODULE, "remote", return_value={
                "lane": "idle", "run": info}))
            fetch = stack.enter_context(mock.patch.object(
                MODULE, "harvest_fetch", return_value=(True, "", 0)))
            stack.enter_context(mock.patch.object(
                MODULE, "filter_harvest", return_value=("c0ffee", [], False)))
            stack.enter_context(mock.patch.object(MODULE, "queue", return_value=[{"n": 1595}]))
            stack.enter_context(mock.patch.object(MODULE, "deps_met", return_value=True))
            stack.enter_context(mock.patch.object(MODULE, "freshen_queue", side_effect=lambda q, s: q))
            stack.enter_context(mock.patch.object(MODULE, "can_take", return_value=True))
            stack.enter_context(mock.patch.object(MODULE, "label_state", return_value=("fresh", "")))
            stack.enter_context(mock.patch.object(MODULE, "sh", side_effect=fake.sh))
            dispatch = stack.enter_context(mock.patch.object(MODULE, "dispatch", return_value=("new-run", None)))
            stack.enter_context(mock.patch.object(MODULE, "status_snapshot", return_value={}))
            state = {"A1": {"issue": 1595, "run": RUN}}
            MODULE.tick({"A1": CFG}, state, 0)
            if outcome == "requeue-failed":
                self.assertEqual(state["A1"]["run"], RUN)
                self.assertEqual(fake.label_edits, [])
            else:
                self.assertNotIn("A1", state)
                self.assertEqual(fake.label_edits[-1][-1], "lane-ready")
            self.assertEqual(state["_done"], [])
            self.assertEqual(fake.prs, [])
            self.assertTrue(MODULE.lane_paused("A1"))
            dispatch.assert_not_called()
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ReassignmentGuardTests(unittest.TestCase):
    def fixture(self):
        spec = importlib.util.spec_from_file_location('guard_fleet', SCRIPT.with_name('test_budget_requeue.py'))
        fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
        case = fixture.BudgetRequeueTest('runTest'); case.setUp()
        self.addCleanup(case.doCleanups)
        original = case.module.gh
        def gh(args, timeout=60):
            if args[:2] == ['issue', 'edit']:
                labels = case.fleet.issues[int(args[2])]['labels']
                for index, flag in enumerate(args):
                    if flag == '--remove-label' and args[index + 1] in labels: labels.remove(args[index + 1])
                    if flag == '--add-label' and args[index + 1] not in labels: labels.append(args[index + 1])
                return 0, '', ''
            return original(args, timeout)
        case.module.gh = gh
        return case, fixture

    def test_paginated_open_only_lookup(self):
        def page(prs, more=False):
            return 0, json.dumps({'data': {'repository': {'pullRequests': {
                'nodes': prs, 'pageInfo': {'hasNextPage': more, 'endCursor': 'next'}}}}}), ''
        pr = {'number': 99, 'state': 'OPEN', 'body': 'Closes #7', 'headRefName': '',
              'closingIssuesReferences': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}
        with mock.patch.object(MODULE, 'gh', side_effect=[page([dict(pr, number=i, body='') for i in range(100)], True), page([pr])]) as gh:
            self.assertEqual(MODULE.open_pr_for_issue(7), 99)
            self.assertIn('after:', gh.call_args.args[0][-1])
        with mock.patch.object(MODULE, 'gh', return_value=page([dict(pr, state='MERGED')])):
            self.assertIsNone(MODULE.open_pr_for_issue(7))
        with mock.patch.object(MODULE, 'gh', return_value=(1, '', 'offline')):
            with self.assertRaises(RuntimeError): MODULE.open_pr_for_issue(7)

    def test_requeue_paths_fail_closed_and_repair(self):
        case, fixture = self.fixture(); ad = case.module; n = fixture.ISSUE
        actions = [lambda: ad.requeue_lost_run('L', {'issue': n, 'run': 'r'}, {}, {}),
                   lambda: ad.block_pr_validation('L', {'host': 'test'}, {'issue': n, 'run': 'r'}, {}, {}, '', '', ['missing']),
                   lambda: ad.requeue_executor_start_failure('L', n, 'r')]
        for action in actions:
            for answer in (99, RuntimeError('offline')):
                case.fleet.issues[n]['labels'] = ['in-progress']
                with mock.patch.object(ad, 'open_pr_for_issue', side_effect=([answer] if isinstance(answer, Exception) else None), return_value=answer), mock.patch.object(ad, 'record_pr_validation'):
                    action()
                labels = case.fleet.issues[n]['labels']
                self.assertNotIn('lane-ready', labels)
                if answer == 99: self.assertEqual(set(labels) & ad.DELIVERY_LABELS, {'lane-pr'})

    def test_requeue_write_race_and_failed_readback(self):
        case, fixture = self.fixture(); ad = case.module; n = fixture.ISSUE
        for answer in (99, RuntimeError('offline')):
            case.fleet.issues[n]['labels'] = []
            with mock.patch.object(ad, 'open_pr_for_issue', side_effect=[None, answer]):
                result = ad.guarded_requeue(n)
            self.assertNotIn('lane-ready', case.fleet.issues[n]['labels'])
            self.assertEqual(result, answer == 99)

    def test_tick_dispatch_races(self):
        for answers, dispatched in (([99], False), ([None, 99], False),
                                    ([None, None, 99, 99], True),
                                    ([None, None, RuntimeError('offline'), None], True)):
            case, fixture = self.fixture(); ad = case.module; n = fixture.ISSUE
            case.fleet.issues[n]['labels'] = ['lane-ready']
            notes = []
            original_sh = ad.sh
            def sh(cmd, **kwargs):
                if ' stop ' in ' '.join(cmd):
                    case.fleet.set_lane(fixture.LANE, 'idle', None, case.clock.time())
                    return 0, '{"signalled":["abort"]}', ''
                return original_sh(cmd, **kwargs)
            def remote(lane, cfg, run=''):
                return {'lane': 'idle', 'runner_alive': False}
            with mock.patch.object(ad, 'open_pr_for_issue', side_effect=answers), \
                    mock.patch.object(ad, 'sh', side_effect=sh), mock.patch.object(ad, 'remote', side_effect=remote), \
                    mock.patch.object(ad, 'log', side_effect=lambda msg, brain=True: notes.append(msg)):
                case.tick()
            self.assertEqual(bool(case.fleet.dispatches), dispatched)
            self.assertNotIn(fixture.LANE, case.state)
            self.assertNotIn('in-progress', case.fleet.issues[n]['labels'], notes)
            self.assertTrue(any('open PR #99' in x or 'post-dispatch' in x for x in notes))

    def test_tick_does_not_dispatch_when_pr_lookup_fails(self):
        case, fixture = self.fixture(); ad = case.module
        case.fleet.issues[fixture.ISSUE]['labels'] = ['lane-ready']
        with mock.patch.object(ad, 'open_pr_for_issue', side_effect=RuntimeError('offline')):
            case.tick()
        self.assertEqual(case.fleet.dispatches, [])
        self.assertNotIn(fixture.LANE, case.state)

    def test_cancel_is_verified_and_retained_for_retry(self):
        case, fixture = self.fixture(); ad = case.module; n = fixture.ISSUE
        st = {'issue': n, 'run': 'r', 'cancel_pending': True}
        with mock.patch.object(ad, 'sh', return_value=(0, '{"signalled":["abort"]}', '')), \
                mock.patch.object(ad, 'remote', return_value={'lane': 'idle', 'runner_alive': False}), \
                mock.patch.object(ad, 'open_pr_for_issue', return_value=99):
            self.assertTrue(ad.cancel_guarded_dispatch(fixture.LANE, case.lanes[fixture.LANE], st))
        case.state[fixture.LANE] = st
        with mock.patch.object(ad, 'cancel_guarded_dispatch', return_value=False), mock.patch.object(ad, 'open_pr_for_issue', return_value=99):
            case.tick()
        self.assertTrue(case.state[fixture.LANE]['cancel_pending'])

    def lookup(self, response, issue=7):
        with mock.patch.object(MODULE, 'gh', return_value=(0, json.dumps(response), '')):
            return MODULE.open_pr_for_issue(issue)

    def connection(self, nodes, more=False):
        return {'data': {'repository': {'pullRequests': {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': 'c'}}}}}

    def test_truncated_closing_references_fail_closed(self):
        pr = {'number': 5, 'state': 'OPEN', 'body': '', 'headRefName': 'x',
              'closingIssuesReferences': {'nodes': [{'number': 1}], 'pageInfo': {'hasNextPage': True}}}
        with self.assertRaises(RuntimeError): self.lookup(self.connection([pr]))
        pr['closingIssuesReferences']['pageInfo'] = {'hasNextPage': False}
        pr['closingIssuesReferences']['nodes'] = [{'number': 7}]
        self.assertEqual(self.lookup(self.connection([pr])), 5)

    def test_malformed_pages_fail_closed(self):
        good = {'number': 5, 'state': 'OPEN', 'body': '', 'headRefName': '',
                'closingIssuesReferences': {'nodes': [], 'pageInfo': {'hasNextPage': False}}}
        bad = [
            {'data': {'repository': {'pullRequests': {'nodes': {}, 'pageInfo': {'hasNextPage': False}}}}},
            {'data': {'repository': {'pullRequests': {'nodes': [], 'pageInfo': {'hasNextPage': None}}}}},
            {'data': {'repository': {'pullRequests': {'nodes': [], 'pageInfo': {}}}}},
            {'data': {'repository': {'pullRequests': None}}},
            {'data': {'repository': None}}, {'data': None}, [], {},
            self.connection([dict(good, closingIssuesReferences={'nodes': {}, 'pageInfo': {'hasNextPage': False}})]),
            self.connection([dict(good, closingIssuesReferences={'nodes': []})]),
            self.connection([dict(good, number='5')]),
        ]
        for response in bad:
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError): self.lookup(response)
        self.assertIsNone(self.lookup(self.connection([good])))

    def test_cancel_accepts_the_real_remote_protocol_of_a_finished_or_idle_run(self):
        case, fixture = self.fixture(); ad = case.module; n = fixture.ISSUE
        finished = {'lane': 'idle', 'current_run': None, 'run': {'status': 'done', 'branch': 'b'}}
        idle_gone = {'lane': 'idle', 'current_run': None, 'run': None}
        still = {'lane': 'busy', 'current_run': 'r', 'run': 'running', 'runner_alive': True}
        unknown = {'error': 'ssh status timeout'}
        for probe, expected in ((finished, True), (idle_gone, True), (still, False), (unknown, False)):
            with self.subTest(probe=probe):
                self.assertEqual(ad.run_stopped(probe, 'r'), expected)
        st = {'issue': n, 'run': 'r', 'cancel_pending': True}
        with mock.patch.object(ad, 'sh', return_value=(0, '{"signalled":["abort"]}', '')), \
                mock.patch.object(ad, 'remote', return_value=finished), \
                mock.patch.object(ad, 'open_pr_for_issue', return_value=99):
            self.assertTrue(ad.cancel_guarded_dispatch(fixture.LANE, case.lanes[fixture.LANE], st))
