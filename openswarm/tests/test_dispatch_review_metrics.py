"""The lane review metrics write path (lib/dispatch/engine.py): the records the metrics read (Refs #1733)."""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_review_metrics_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)

SHA = "a" * 40
SHA2 = "b" * 40
REBUILT = "c" * 40          # the head the artifact filter rebuilt (Refs #1835)


class FakeRunner:
    """Answers commands by first matching substring rule; records every call."""

    def __init__(self, rules=None):
        self.base_rules = list(rules or [])      # the rules that hold for every call
        self.rules = list(self.base_rules)
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for needle, result in self.rules:
            if needle in text:
                return result(text) if callable(result) else result
        return 0, "", ""

    def called(self, needle):
        return [c for c in self.calls if needle in c]


class ReviewRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-metrics-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "remote_home": self.tmp, "ssh": {"control_dir": os.path.join(self.tmp, "cm")},
               "local_hosts": ["box"], "burn": {"file": "BURN-X", "backend": "pi"}}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as stream:
            json.dump(raw, stream)
        self.cfg = E.load_config(path)

    def make(self, rules=None, dry_run=False):
        self.runner = FakeRunner(rules)
        dispatcher = E.Dispatcher(self.cfg, runner=self.runner, dry_run=dry_run, out=open(os.devnull, "w"))
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    def review(self, dispatcher, verdict="VERDICT: APPROVE\nBLOCKERS: 0\nSUMMARY: ok\n",
               pr=20, sha=SHA, branch="fleet/CX10-issue1733-2026", body="Closes #1733", rules=None):
        dispatcher.runner.rules = list(dispatcher.runner.base_rules) + list(rules or []) + [
            ("git -C", (0, verdict, "")),
            (f"pr view {pr}", (0, json.dumps({"headRefOid": sha, "state": "OPEN",
                                              "headRefName": branch, "body": body}), ""))]
        state = {"issue": f"PR{pr}", "run": "r1", "review_pr": pr, "review_sha": sha}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        dispatcher.finish_review("M5", {"host": "box", "backend": "pi"}, state, info, {})
        return dispatcher.load_reviews()[str(pr)]

    def test_a_review_record_carries_the_fields_the_metrics_read(self):
        dispatcher = self.make()
        record = self.review(dispatcher)
        self.assertEqual(record["backend"], "pi")
        self.assertEqual(record["model"], "m")
        self.assertEqual(record["pr"], 20)
        self.assertEqual(record["issue"], 1733)
        self.assertEqual(record["author_lane"], "CX10")
        self.assertEqual(record["blockers"], 0)
        self.assertIsNone(record["follow_ups"])
        self.assertEqual(record["review_rounds"], 1)
        self.assertEqual(record["first_verdict"], "APPROVE")
        self.assertTrue(record["at"])

    def test_the_finding_counts_of_the_conclusion_are_recorded(self):
        dispatcher = self.make()
        record = self.review(dispatcher, verdict="VERDICT: CHANGES\nBLOCKERS: 4\nFOLLOW-UPS: 2\nSUMMARY: x\n")
        self.assertEqual(record["blockers"], 4)
        self.assertEqual(record["follow_ups"], 2)

    def test_the_finding_entries_of_the_review_format_are_recorded(self):
        dispatcher = self.make()
        record = self.review(dispatcher, verdict=("VERDICT: CHANGES\nSUMMARY: x\n\n## Findings\n"
                                                  "- [BLOCKER] `tools/a.py:1` wrong\n"
                                                  "- [FOLLOW-UP] add the missing test\n"))
        self.assertEqual(record["blockers"], 1)
        self.assertEqual(record["follow_ups"], 1)

    def test_a_changes_round_queues_a_rework_task_and_no_follow_up_card(self):
        dispatcher = self.make([("issue view 1733", (0, json.dumps({"body": "do x"}), ""))])
        record = self.review(dispatcher, verdict="VERDICT: CHANGES\nBLOCKERS: 1\nSUMMARY: x\n")
        self.assertEqual(record["verdict"], "CHANGES")
        self.assertEqual(record["review_rounds"], 1)
        self.assertEqual(record["first_verdict"], "CHANGES")
        # the rework task lives on the original issue: it is not a follow-up card
        self.assertEqual(record["rework_tasks"], 1)
        self.assertEqual(record["followup_issues"], [])
        self.assertFalse(dispatcher.runner.called("issue create"))

    def test_a_follow_up_finding_opens_a_real_follow_up_card(self):
        dispatcher = self.make([("issue create", (0, "https://github.com/acme/project/issues/99\n", ""))])
        record = self.review(dispatcher, verdict=("VERDICT: APPROVE\nSUMMARY: x\n\n## Findings\n"
                                                  "- [FOLLOW-UP] add the missing test\n"))
        self.assertEqual(record["followup_issues"], [99])
        self.assertEqual(record.get("rework_tasks", 0), 0)
        created = dispatcher.runner.called("issue create")
        self.assertEqual(len(created), 1)
        self.assertIn("add the missing test", created[0])
        # the label is added after the card exists: a repo without that label still
        # gets the card (the label is missing, not the card)
        labelled = [c for c in dispatcher.runner.called("--add-label") if "follow-up" in c]
        self.assertEqual(len(labelled), 1)
        self.assertIn("follow-up", labelled[0])

    def test_the_card_is_created_even_when_the_label_does_not_exist(self):
        dispatcher = self.make([("issue create", (0, "https://github.com/acme/project/issues/99\n", "")),
                                ("issue edit", (1, "", "could not add label: 'follow-up' not found"))])
        record = self.review(dispatcher, verdict=("VERDICT: APPROVE\nSUMMARY: x\n\n## Findings\n"
                                                  "- [FOLLOW-UP] add the missing test\n"))
        self.assertEqual(record["followup_issues"], [99])
        self.assertEqual(len(dispatcher.runner.called("issue create")), 1)

    def test_a_follow_up_card_that_could_not_be_created_is_not_counted(self):
        dispatcher = self.make([("issue create", (1, "", "boom"))])
        record = self.review(dispatcher, verdict=("VERDICT: APPROVE\nSUMMARY: x\n\n## Findings\n"
                                                  "- [FOLLOW-UP] add the missing test\n"))
        self.assertEqual(record["followup_issues"], [])

    def test_the_same_head_does_not_open_the_follow_up_cards_twice(self):
        dispatcher = self.make([("issue create", (0, "https://github.com/acme/project/issues/99\n", ""))])
        verdict = ("VERDICT: APPROVE\nSUMMARY: x\n\n## Findings\n- [FOLLOW-UP] add the missing test\n")
        self.review(dispatcher, verdict=verdict)
        record = self.review(dispatcher, verdict=verdict)
        self.assertEqual(record["followup_issues"], [99])
        self.assertEqual(len(dispatcher.runner.called("issue create")), 1)

    def test_the_approve_after_a_changes_is_the_second_round(self):
        dispatcher = self.make([("issue view 1733", (0, json.dumps({"body": "do x"}), ""))])
        record = self.review(dispatcher, verdict="VERDICT: CHANGES\nSUMMARY: x\n")
        self.assertEqual(record["review_rounds"], 1)
        self.assertEqual(record["first_verdict"], "CHANGES")
        # the lane fixed the findings: the head moved and the second review approves
        record = self.review(dispatcher, verdict="VERDICT: APPROVE\nSUMMARY: ok\n", sha=SHA2)
        self.assertEqual(record["verdict"], "APPROVE")
        self.assertEqual(record["review_rounds"], 2)
        # the first verdict did not change: the pull request failed its first round
        self.assertEqual(record["first_verdict"], "CHANGES")

    def test_a_rebase_re_review_is_not_a_formal_round(self):
        dispatcher = self.make()
        dispatcher.put_review(20, {"sha": SHA, "verdict": "CHANGES", "lane": "M5", "rounds": 1,
                                   "review_rounds": 1, "first_verdict": "CHANGES", "rebased_sha": SHA})
        record = self.review(dispatcher, verdict="VERDICT: APPROVE\nSUMMARY: ok\n")
        self.assertEqual(record["review_rounds"], 1)     # the rebase re-review does not add a round
        self.assertEqual(record["rebase_reviews"], 1)
        # the verdict of the re-review does not rewrite the verdict of the first round
        self.assertEqual(record["verdict"], "APPROVE")
        self.assertEqual(record["first_verdict"], "CHANGES")

    def test_a_rebase_re_review_of_a_filtered_head_is_not_a_formal_round(self):
        # the artifact filter rebuilt the branch (Refs #1835): the head the re-review
        # sees is the rebuilt one, and it is still a rebase re-review
        dispatcher = self.make()
        dispatcher.put_review(20, {"sha": SHA, "verdict": "CHANGES", "lane": "M5", "rounds": 1,
                                   "review_rounds": 1, "first_verdict": "CHANGES",
                                   "rebased_sha": REBUILT})
        record = self.review(dispatcher, verdict="VERDICT: APPROVE\nSUMMARY: ok\n", sha=REBUILT)
        self.assertEqual(record["review_rounds"], 1)
        self.assertEqual(record["rebase_reviews"], 1)
        self.assertEqual(record["first_verdict"], "CHANGES")

    def test_the_rebase_harvest_remembers_the_head_it_moved(self):
        dispatcher = self.make([("diff --stat", (0, "file.py | 2 +-\n", "")),
                                ("rev-parse", (0, SHA2 + "\n", ""))])
        # nothing was excluded: the pushed head is the head of the lane branch
        dispatcher.filter_lane_branch = lambda lane, st: ("refs/fleet/M5", [], False)
        state = {"issue": "PR20", "run": "r1", "base": "c" * 40, "rebase_pr": 20,
                 "rebase_branch": "fleet/CX10-issue1733-2026", "rebase_head": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m", "status": "done"}
        dispatcher.finish("M5", {"host": "box", "backend": "pi"}, state, info, {})
        self.assertEqual(dispatcher.load_reviews()["20"]["rebased_sha"], SHA2)
        # the rebase of that head is done: main's key (#1842) is kept next to it, so the
        # dispatcher does not queue the same rebase again
        self.assertEqual(dispatcher.load_reviews()["20"]["rebase_sha"], SHA2)
        self.assertEqual(dispatcher.load_reviews()["20"]["rebase_base_sha"], state["base"])

    def test_the_rebase_harvest_remembers_the_head_the_filter_rebuilt(self):
        # the artifact filter rebuilds the branch before the push: the head the
        # re-review sees is the rebuilt one, not the lane branch
        dispatcher = self.make([("diff --stat", (0, "file.py | 2 +-\n", ""))])
        dispatcher.filter_lane_branch = lambda lane, st: (REBUILT, [".lane-review/PR-20.md"], False)
        state = {"issue": "PR20", "run": "r1", "base": "c" * 40, "rebase_pr": 20,
                 "rebase_branch": "fleet/CX10-issue1733-2026", "rebase_head": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m", "status": "done"}
        dispatcher.finish("M5", {"host": "box", "backend": "pi"}, state, info, {})
        self.assertEqual(dispatcher.load_reviews()["20"]["rebased_sha"], REBUILT)
        # the rebuilt head is the head that went to the PR branch: main's key (#1842)
        # names it too, so the rebase is not queued again
        self.assertEqual(dispatcher.load_reviews()["20"]["rebase_sha"], REBUILT)


class MetricsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-metrics-file-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "a/b", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"]}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as stream:
            json.dump(raw, stream)
        self.cfg = E.load_config(path)
        self.dispatcher = E.Dispatcher(self.cfg, out=open(os.devnull, "w"))
        self.addCleanup(self.dispatcher.out.close)

    def test_the_metrics_are_written_next_to_the_lane_status(self):
        lanes = {"M5": {"backend": "pi", "host": "box", "model": "m"}}
        self.dispatcher.probe = lambda _lanes, _state: {"M5": {"lane": "idle", "run": {"model": "m"}}}
        self.dispatcher.harvest = lambda *_args: None
        self.dispatcher.sweep_leaked_inprogress = lambda *_args: None
        self.dispatcher.queue = lambda: []
        self.dispatcher.dispatch_reviews = lambda *_args: None
        self.dispatcher.assign_issues = lambda *_args: None
        self.dispatcher.replenish = lambda *_args: None
        self.dispatcher.put_review(20, {"sha": "a" * 40, "verdict": "APPROVE", "lane": "M5", "backend": "pi",
                                        "at": "2026-11-03 10:00:00", "review_rounds": 1,
                                        "first_verdict": "APPROVE"})
        self.dispatcher.tick(lanes, {})
        with open(self.dispatcher.path("lane-review-metrics.json"), encoding="utf-8") as stream:
            report = json.load(stream)
        self.assertEqual(report["lanes"]["M5"]["reviews"], 1)
        self.assertEqual(report["lanes"]["M5"]["first_round_pass_rate"], 1.0)
        self.assertEqual(report["targets"]["first_round_pass_rate"], 0.6)
        self.assertIn("first_round_pass", report["definitions"])
        self.assertTrue(os.path.exists(self.dispatcher.path("lanes-status.json")))


if __name__ == "__main__":
    unittest.main()
