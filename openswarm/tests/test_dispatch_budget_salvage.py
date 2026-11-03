"""budget_exceeded runs with a usable diff are pushed to a WIP branch and requeued, not dropped (Refs #1599)."""
import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_salvage_under_test",
                                               os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

SHA = "a" * 40
STAT = " tools/quota-reclaim-drill.py | 297 +\n 1 file changed, 297 insertions(+)\n"


class FakeRunner:
    """Answers commands by first matching substring rule; records every call."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for needle, result in self.rules:
            if needle in text:
                return result
        if "issue view" in text:
            return 0, '{"state":"OPEN","labels":[]}', ""
        return 0, "", ""

    def called(self, needle):
        return [c for c in self.calls if needle in c]


class BudgetSalvage(unittest.TestCase):
    INFO = {"status": "budget_exceeded", "error_class": "budget_exceeded", "branch": "lane/x", "commit": "c",
            "repo": "/w", "summary": "ran out of budget"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="budget-salvage-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"]}
        p = os.path.join(self.tmp, "cfg.json")
        with open(p, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(p)

    def harvest(self, rules, info=None, st=None, wip=None):
        self.runner = FakeRunner(rules)
        d = E.Dispatcher(self.cfg, runner=self.runner, dry_run=False, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        st = dict(st or {"issue": 1, "run": "r1", "base": SHA, "at": time.strftime("%F %T")})
        state = {"M5": st}
        if wip is not None:
            state["_wip"] = wip
        done = d.finish("M5", {"host": "box", "backend": "pi"}, st, dict(info or self.INFO), state)
        return done, state

    def edits(self, n=1):
        return " ".join(self.runner.called(f"issue edit {n}"))

    def comments(self, n=1):
        return " ".join(self.runner.called(f"issue comment {n}"))

    def test_diff_is_pushed_to_wip_branch_commented_and_requeued(self):
        done, state = self.harvest([("diff --stat", (0, STAT, ""))])
        self.assertTrue(done)
        pushes = self.runner.called(" push -q origin")
        self.assertEqual(len(pushes), 1)
        self.assertIn("refs/heads/fleet/M5-issue1-wip-r1", pushes[0])
        self.assertEqual(self.runner.called("pr create"), [])
        self.assertIn("--add-label lane-ready", self.edits())
        self.assertIn("--remove-label lane-failed", self.edits())
        self.assertNotIn("lane-failed", self.edits().replace("--remove-label lane-failed", ""))
        comment = self.comments()
        self.assertIn("fleet/M5-issue1-wip-r1", comment)
        self.assertIn("tools/quota-reclaim-drill.py | 297 +", comment)
        self.assertIn("min with a diff", comment)
        self.assertEqual(state["_wip"]["1"], {"wip_ref": "fleet/M5-issue1-wip-r1", "count": 1})
        self.assertEqual(state["_done"], [1])

    def test_no_commit_is_lane_failed_and_nothing_is_pushed(self):
        info = {"status": "budget_exceeded", "error_class": "budget_exceeded", "summary": "ran out of budget"}
        done, state = self.harvest([], info=info)
        self.assertTrue(done)
        self.assertEqual(self.runner.called(" push "), [])
        self.assertIn("--add-label lane-failed", self.edits())
        self.assertNotIn("--add-label lane-ready", self.edits())
        self.assertNotIn("_wip", state)

    def test_third_budget_exceed_is_lane_failed(self):
        state_wip = {"1": {"wip_ref": "fleet/M5-issue1-wip-r2", "count": 2}}
        done, state = self.harvest([("diff --stat", (0, STAT, ""))], st={"issue": 1, "run": "r3", "base": SHA,
                                   "at": time.strftime("%F %T")}, wip=state_wip)
        self.assertTrue(done)
        self.assertIn("--add-label lane-failed", self.edits())
        self.assertNotIn("--add-label lane-ready", self.edits())
        self.assertIn("ran out of budget again", self.comments())
        self.assertNotIn("1", state["_wip"])
        self.assertTrue(self.runner.called("refs/heads/fleet/M5-issue1-wip-r3"))

    def test_second_budget_exceed_still_requeues(self):
        done, state = self.harvest([("diff --stat", (0, STAT, ""))], wip={"1": {"wip_ref": "fleet/M5-issue1-wip-r1",
                                                                             "count": 1}})
        self.assertTrue(done)
        self.assertIn("--add-label lane-ready", self.edits())
        self.assertEqual(state["_wip"]["1"]["count"], 2)

    def test_empty_wip_diff_keeps_regular_budget_path(self):
        self.runner = FakeRunner([("diff --stat", (0, "", ""))])
        d = E.Dispatcher(self.cfg, runner=self.runner, dry_run=False, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        st = {"issue": 1, "run": "r1", "base": SHA, "at": time.strftime("%F %T")}
        state = {"M5": st}
        self.assertIsNone(d.salvage_budget("M5", {"host": "box"}, st, dict(self.INFO), state))
        self.assertEqual(self.runner.called(" push "), [])
        self.assertNotIn("_wip", state)

    def test_task_text_explains_the_wip_resume(self):
        d = E.Dispatcher(self.cfg, runner=FakeRunner(), dry_run=True, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        item = {"n": 1, "title": "card", "body": "body"}
        plain = d.task_text(item)
        self.assertNotIn("续做", plain)
        self.assertNotIn("git merge --no-edit", plain)
        resumed = d.task_text(dict(item, wip_ref="fleet/M5-issue1-wip-r1"))
        self.assertIn("git merge --no-edit fleet/M5-issue1-wip-r1", resumed)
        self.assertIn("已完成部分不要重写", resumed)
        self.assertIn("# #1 card", resumed)


if __name__ == "__main__":
    unittest.main()
