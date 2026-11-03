"""The fix/rebase task book carries one shared method block (Refs #1838).

A fix task book is the issue body a fix lane receives. Three places write into it: the review
CHANGES round (fix task), the red-CI fix (`cifix`) and the conflict rebase. They share one method
block (Refs #1838): enumerate the same-defect occurrences first, write the failing regression
test first, verify with real commands, tick every previous BLOCKER off in the delivery notes and
continue on the PR head. The block is written once per task book, not once per section, and the
items that need the previous round's BLOCKERs (① and ④) are left out when the task book has none.

The tests drive the real dispatcher (`tick`): the engine writes the task book into the issue and
the next tick hands that issue to the executing lane, so the assertions are about the task book
that really reached the lane (Refs #1838).
"""
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_fix_method_test",
                                               os.path.join(HERE, "..", "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

BASE_SHA = "b" * 40
COMMIT_SHA = "c" * 40
# Fixture SHA; the real PR #1744 head has not been verified.
PR1744_HEAD = "cfe6411079ccf70676b330edad9699213f722e66"
METHOD_TITLE = "修复方法（Refs #1838）"
# The label pair of the review CHANGES round: `gh pr edit <pr> --add-label A --remove-label B`.
LABEL_ADD = "needs-changes"
LABEL_REMOVE = "lane-reviewed"
VERDICT = ("VERDICT: CHANGES\n"
           "SUMMARY: the label is written the old way\n"
           "\n"
           "## Findings\n"
           "- [BLOCKER] src/a.py:12 the label is written the old way\n")


def issue(n, body="", labels=(), title=None):
    return {"number": n, "title": title or f"card {n}", "body": body, "labels": set(labels),
            "state": "OPEN"}


def pr(n, issue_n, mergeable="MERGEABLE", checks=("SUCCESS",), head=PR1744_HEAD, branch=None, labels=()):
    roll = [{"name": f"linux-{i}", "conclusion": c} for i, c in enumerate(checks)]
    return {"number": n, "title": f"pr {n}", "headRefName": branch or f"fleet/M5-issue{issue_n}-r1",
            "headRefOid": head, "isDraft": False, "mergeable": mergeable, "statusCheckRollup": roll,
            "labels": set(labels), "body": f"Closes #{issue_n}", "state": "OPEN"}


class FakeGitHub:
    """A fake `gh`/`git`/ssh that keeps the issue bodies, labels and PR labels it writes.

    Round 2 sees the body of round 1, and the task book that reaches a lane is remembered, so
    the tests assert on what the lane really got (Refs #1838).
    """

    def __init__(self, issues=None, prs=None, verdict=VERDICT, lane_status=None):
        self.issues = {n: dict(spec) for n, spec in (issues or {}).items()}
        self.prs = {p["number"]: dict(p) for p in (prs or [])}
        self.verdict = verdict
        self.lane_status = dict(lane_status or {})
        self.calls = []
        self.task_uploads = []      # (lane, task text): every task book that reached a lane
        self.pr_edits = []           # every `gh pr edit` command
        self.issue_edits = []        # every `gh issue edit` command

    # ------------------------------------------------------------ helpers
    def body(self, n):
        return self.issues[n]["body"]

    def labels(self, n):
        return set(self.issues[n]["labels"])

    def called(self, needle):
        return [c for c in self.calls if needle in c]

    def set_pr(self, **fields):
        self.prs[fields.pop("number")].update(fields)

    # ------------------------------------------------------------ the fake runner
    def runner(self, cmd, timeout=120, inp=None, env=None):
        args = [str(c) for c in cmd] if not isinstance(cmd, str) else cmd.split()
        text = " ".join(args)
        self.calls.append(text)
        if args[0] == "gh":
            return self.gh(args)
        if args[0] == "git":
            return self.git(args)
        if args[0] == "ssh":
            return self.ssh(args, inp)
        return 0, "", ""

    def gh(self, args):
        if args[1] == "issue":
            if args[2] == "list":
                label = args[args.index("--label") + 1] if "--label" in args else None
                out = [{"number": num, "title": spec["title"], "body": spec["body"],
                        "labels": [{"name": l} for l in sorted(spec["labels"])]}
                       for num, spec in sorted(self.issues.items())
                       if spec.get("state", "OPEN") == "OPEN" and (label is None or label in spec["labels"])]
                return 0, json.dumps(out), ""
            if args[2] == "view":
                num = int(args[3])
                spec = self.issues.get(num) or issue(num)
                return 0, json.dumps({"state": spec.get("state", "OPEN"), "title": spec["title"],
                                      "body": spec["body"],
                                      "labels": [{"name": l} for l in sorted(spec["labels"])]}), ""
            if args[2] == "edit":
                num = int(args[3])
                spec = self.issues.setdefault(num, issue(num))
                if "--body" in args:
                    spec["body"] = args[args.index("--body") + 1]
                self.set_labels(spec["labels"], args)
                self.issue_edits.append(" ".join(args))
                return 0, "", ""
        if args[1] == "pr":
            if args[2] == "list":
                out = [self.pr_json(p) for p in sorted(self.prs.values(), key=lambda p: p["number"])]
                return 0, json.dumps(out), ""
            if args[2] == "view":
                num = int(args[3])
                return 0, json.dumps(self.pr_json(self.prs.get(num) or {"number": num})), ""
            if args[2] == "diff":
                return 0, "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n", ""
            if args[2] == "edit":
                num = int(args[3])
                spec = self.prs.get(num)
                if spec is not None:
                    self.set_labels(spec["labels"], args)
                self.pr_edits.append(" ".join(args))
                return 0, "", ""
            if args[2] == "comment":
                return 0, "", ""
        if args[1] == "api":
            return 0, "", ""
        return 0, "", ""

    @staticmethod
    def set_labels(labels, args):
        for flag in ("--add-label", "--remove-label"):
            for lab in [args[i + 1] for i, a in enumerate(args) if a == flag]:
                if flag == "--add-label":
                    labels.add(lab)
                else:
                    labels.discard(lab)

    def pr_json(self, p):
        return {"number": p.get("number"), "title": p.get("title", ""), "state": p.get("state", "OPEN"),
                "headRefOid": p.get("headRefOid", BASE_SHA), "headRefName": p.get("headRefName", ""),
                "body": p.get("body", ""), "mergeable": p.get("mergeable", "MERGEABLE"),
                "isDraft": p.get("isDraft", False), "statusCheckRollup": p.get("statusCheckRollup", []),
                "labels": [{"name": l} for l in sorted(p.get("labels") or [])]}

    def git(self, args):
        if "ls-remote" in args:
            return 0, f"{BASE_SHA}\trefs/heads/main", ""
        if "show" in args:
            path = args[-1]
            if re.search(r"PR-\d+\.md$", path):
                return 0, self.verdict, ""
            return 0, "", ""
        return 0, "", ""

    def ssh(self, args, inp):
        text = " ".join(args)
        if "cat > /tmp/task-" in text:
            lane = text.split("cat > /tmp/task-")[1].split(".md")[0]
            self.task_uploads.append((lane, inp or ""))
            return 0, "", ""
        if "lane assign" in text:
            return 0, '{"id": "run-fake-1"}', ""
        if "python3" in args:
            return 0, json.dumps(self.lane_status), ""
        return 0, "", ""


class Base(unittest.TestCase):
    LANES = {"CX1": {"host": "box", "backend": "codex"}, "M2": {"host": "box", "backend": "pi"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-fix-method-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": [],
               "burn": {"file": "BURN-X", "backend": "pi"},
               # the CI fix task is queued in burn mode only; the test drives it without the marker
               "review": {"cifix_needs_burn": False}}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(path)

    def make(self, gh):
        d = E.Dispatcher(self.cfg, runner=gh.runner, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        return d

    def tick(self, d, gh, state):
        """One dispatcher tick: the engine writes the task book, the next tick hands it to a lane."""
        with mock.patch.dict(os.environ, {"GH_TOKEN": "test-token"}):
            d.tick(self.LANES, state)
        return state

    @staticmethod
    def idle_lanes():
        return {"CX1": {"lane": "idle", "run": None}, "M2": {"lane": "idle", "run": None}}

    @staticmethod
    def review_state(pr_number, head, lane="CX1"):
        """The state of a lane that is running a review of `pr_number` (the harvest finishes it)."""
        return {lane: {"issue": f"PR{pr_number}", "run": "r1", "host": "box", "base": BASE_SHA,
                       "at": "2026-11-03 00:00:00", "verify": None,
                       "review_pr": pr_number, "review_sha": head}}

    @staticmethod
    def review_lane_status(pr_number, lane="CX1"):
        return {"lane": "busy", "current_run": "r1",
                "run": {"status": "ok", "branch": f"fleet/{lane}-issue{pr_number}-r1",
                        "commit": COMMIT_SHA, "backend": "codex", "model": "m",
                        "repo": "/tmp/lane-repo"}}

    @staticmethod
    def finish_fix_run(gh, state, lane, issue_number=1744):
        """The harvest of a finished fix run: the lane is free again, the issue waits for the review."""
        state.pop(lane, None)
        labels = gh.issues[issue_number]["labels"]
        labels.discard("in-progress")
        labels.discard("lane-ready")
        labels.add("lane-pr")


class MethodBlock(Base):
    """The shared block itself: one copy, and only the items that apply."""

    def test_block_names_the_failure_of_every_kind(self):
        self.assertIn("该 BLOCKER", E.fix_method_block(True, "fix"))
        self.assertIn("CI 失败", E.fix_method_block(False, "cifix"))
        self.assertIn("冲突", E.fix_method_block(False, "rebase"))

    def test_items_without_previous_blockers_are_left_out(self):
        block = E.fix_method_block(False, "cifix")
        self.assertNotIn("先 grep 同类写法", block)      # ① needs the previous round's BLOCKERs
        self.assertNotIn("逐条自检勾选", block)           # ④ too
        self.assertIn("先写会失败的回归测试", block)        # ②
        self.assertIn("用真实命令与真实样本验证", block)   # ③
        self.assertIn("在 PR 当前 head 上续改", block)     # ⑤


class TickDelivery(Base):
    """The task book reaches the executing lane through the tick path (Refs #1838)."""

    def test_cifix_after_review_preserves_blocker_items(self):
        gh = FakeGitHub(issues={1744: issue(1744, body="Closes #1729")},
                        prs=[pr(1744, 1744)],
                        lane_status={"CX1": self.review_lane_status(1744), "M2": {"lane": "idle", "run": None}})
        d = self.make(gh)
        state = self.review_state(1744, PR1744_HEAD)
        self.tick(d, gh, state)
        self.tick(d, gh, state)
        self.assertEqual(len(gh.task_uploads), 1)
        self.finish_fix_run(gh, state, gh.task_uploads[-1][0])
        gh.lane_status = self.idle_lanes()
        gh.set_pr(number=1744, headRefOid=COMMIT_SHA,
                  statusCheckRollup=[{"name": "linux-0", "conclusion": "FAILURE"}])
        self.tick(d, gh, state)
        self.tick(d, gh, state)
        self.assertEqual(len(gh.task_uploads), 2)
        task = gh.task_uploads[-1][1]
        self.assertIn("Auto fix CI (red)", task)
        self.assertIn("[BLOCKER] src/a.py:12", task)
        self.assertEqual(task.count(METHOD_TITLE), 1)
        self.assertEqual(task.count("先 grep 同类写法"), 1)
        self.assertEqual(task.count("**逐条自检勾选**"), 1)

    def test_review_changes_task_book_reaches_the_lane(self):
        gh = FakeGitHub(issues={1744: issue(1744, body="Closes #1729")},
                        prs=[pr(1744, 1744)],
                        lane_status={"CX1": self.review_lane_status(1744), "M2": {"lane": "idle", "run": None}})
        d = self.make(gh)
        state = self.review_state(1744, PR1744_HEAD)
        self.tick(d, gh, state)                       # the review ends → the fix task is written
        self.assertIn(METHOD_TITLE, gh.body(1744))
        self.assertIn("先 grep 同类写法", gh.body(1744))
        self.tick(d, gh, state)                       # the next tick does not hand it out twice
        self.assertEqual(len(gh.task_uploads), 1)
        lane, task = gh.task_uploads[0]
        self.assertIn(lane, self.LANES)
        self.assertEqual(task.count(METHOD_TITLE), 1)
        self.assertIn("Lane review requested changes", task)
        self.assertIn("先 grep 同类写法", task)
        self.assertIn("该 BLOCKER", task)             # ② reproduces the BLOCKER of the round

    def test_cifix_task_book_reaches_the_lane(self):
        gh = FakeGitHub(issues={105: issue(105, body="orig body")},
                        prs=[pr(15, 105, checks=("FAILURE",))],
                        lane_status=self.idle_lanes())
        d = self.make(gh)
        state = {}
        self.tick(d, gh, state)                       # the CI fix task is written
        self.assertIn(METHOD_TITLE, gh.body(105))
        self.assertNotIn("先 grep 同类写法", gh.body(105))     # ① needs the previous round's BLOCKERs
        self.assertNotIn("逐条自检勾选", gh.body(105))          # ④ too
        self.assertIn("CI 失败", gh.body(105))                 # ② names the CI failure to reproduce
        self.tick(d, gh, state)                       # the next tick hands the task book to a lane
        self.assertEqual(len(gh.task_uploads), 1)
        lane, task = gh.task_uploads[0]
        self.assertIn(lane, self.LANES)
        self.assertEqual(task.count(METHOD_TITLE), 1)
        self.assertIn("Auto fix CI (red)", task)
        self.assertIn("CI 失败", task)
        self.assertNotIn("先 grep 同类写法", task)

    def test_rebase_task_book_reaches_the_lane(self):
        gh = FakeGitHub(issues={100: issue(100, body="orig body")},
                        prs=[pr(10, 100, mergeable="CONFLICTING")],
                        lane_status=self.idle_lanes())
        d = self.make(gh)
        state = {}
        self.tick(d, gh, state)                       # the rebase task is written
        self.assertIn(METHOD_TITLE, gh.body(100))
        self.assertNotIn("先 grep 同类写法", gh.body(100))     # ① needs the previous round's BLOCKERs
        self.assertNotIn("逐条自检勾选", gh.body(100))          # ④ too
        self.assertIn("冲突", gh.body(100))                    # ② names the conflict to reproduce
        self.tick(d, gh, state)                       # the next tick hands the task book to a lane
        self.assertEqual(len(gh.task_uploads), 1)
        lane, task = gh.task_uploads[0]
        self.assertIn(lane, self.LANES)
        self.assertEqual(task.count(METHOD_TITLE), 1)
        self.assertIn("Auto rebase (conflict)", task)
        self.assertIn("冲突", task)
        self.assertNotIn("逐条自检勾选", task)

    def test_stacked_fixes_write_the_method_block_only_once(self):
        """rebase → cifix → review CHANGES: three injection points, one shared block."""
        gh = FakeGitHub(issues={1744: issue(1744, body="Closes #1729")},
                        prs=[pr(1744, 1744, mergeable="CONFLICTING")],
                        lane_status=self.idle_lanes())
        d = self.make(gh)
        state = {}
        self.tick(d, gh, state)                                    # rebase task
        self.assertIn(METHOD_TITLE, gh.body(1744))
        self.tick(d, gh, state)                                    # the rebase task reaches a lane
        self.assertEqual(len(gh.task_uploads), 1)
        self.finish_fix_run(gh, state, gh.task_uploads[0][0])
        gh.set_pr(number=1744, mergeable="MERGEABLE",
                  statusCheckRollup=[{"name": "linux-0", "conclusion": "FAILURE"}])
        self.tick(d, gh, state)                                    # CI fix task
        self.assertEqual(gh.body(1744).count(METHOD_TITLE), 1)
        self.tick(d, gh, state)                                    # the CI fix task reaches a lane
        self.assertEqual(len(gh.task_uploads), 2)
        self.finish_fix_run(gh, state, gh.task_uploads[-1][0])
        gh.lane_status = {"CX1": self.review_lane_status(1744), "M2": {"lane": "idle", "run": None}}
        state.update(self.review_state(1744, PR1744_HEAD))
        self.tick(d, gh, state)                                    # review CHANGES → fix task
        self.assertEqual(gh.body(1744).count(METHOD_TITLE), 1)
        self.assertEqual(gh.body(1744).count("先 grep 同类写法"), 1)
        self.tick(d, gh, state)                                    # the fix task reaches a lane
        self.assertEqual(len(gh.task_uploads), 3)
        lane, task = gh.task_uploads[-1]
        self.assertEqual(task.count(METHOD_TITLE), 1)
        self.assertEqual(task.count("先 grep 同类写法"), 1)


class Replay1744(Base):
    """Simulated #1744: two consecutive CHANGES rounds (`gh pr edit --add-label A --remove-label B`)."""

    def test_replay_1744_two_changes_rounds(self):
        gh = FakeGitHub(issues={1744: issue(1744, body="Closes #1729")},
                        prs=[pr(1744, 1744, branch="fleet/M10-issue1729-r1")],
                        lane_status={"CX1": self.review_lane_status(1744), "M2": {"lane": "idle", "run": None}})
        d = self.make(gh)
        state = self.review_state(1744, PR1744_HEAD)
        self.tick(d, gh, state)                        # round 1: the review says CHANGES
        state.update(self.review_state(1744, PR1744_HEAD))
        self.tick(d, gh, state)                        # round 2: the review says CHANGES again
        edits = [c for c in gh.pr_edits if f"pr edit 1744 --add-label {LABEL_ADD}" in c]
        self.assertEqual(len(edits), 2)
        for command in edits:
            self.assertIn(f"--remove-label {LABEL_REMOVE}", command)
        body = gh.body(1744)
        self.assertEqual(body.count(METHOD_TITLE), 1)
        self.assertEqual(body.count("先 grep 同类写法"), 1)
        self.assertIn("Lane review requested changes", body)
        # the task book of the replay, printed for the PR description (Refs #1838)
        print("\n===== replay #1744: the generated fix task book =====\n" + body + "\n===== end =====")

    def test_replay_1744_task_book_reaches_the_lane(self):
        gh = FakeGitHub(issues={1744: issue(1744, body="Closes #1729")},
                        prs=[pr(1744, 1744, branch="fleet/M10-issue1729-r1")],
                        lane_status={"CX1": self.review_lane_status(1744), "M2": {"lane": "idle", "run": None}})
        d = self.make(gh)
        state = self.review_state(1744, PR1744_HEAD)
        self.tick(d, gh, state)
        self.tick(d, gh, state)
        self.assertEqual(len(gh.task_uploads), 1)
        lane, task = gh.task_uploads[0]
        self.assertIn(lane, self.LANES)
        self.assertEqual(task.count(METHOD_TITLE), 1)
        self.assertIn("先 grep 同类写法", task)


if __name__ == "__main__":
    unittest.main()
