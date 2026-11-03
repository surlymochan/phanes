"""Fix-round cards: one card per issue and no stale cards in the pool (Refs #1713).

Covers the rules of ``lib/dispatch/fix_cards.py``, the wiring in the dispatch engine
(a review that returned CHANGES queues at most one fix card per issue, and a fix card
whose pull request was merged leaves the pool) and a replay of the multi-round fix
sample of 2026-11-02.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("dispatch_engine_under_test",
                                               os.path.join(ROOT, "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

fix_cards = E.fix_cards

SHA = "a" * 40
VERDICT = "VERDICT: CHANGES\nSUMMARY: please fix\n"
FIX_BODY = ("Rebase-PR: #21 fleet/M5-issue201-r1\n\n"
            "## Lane review requested changes\n\n"
            "Review round 1 (M5 (pi/m)) on PR #21 head `aaaaaaaaaa` returned CHANGES.\n")


class FakeGitHub:
    """A tiny GitHub simulator: issues with labels and a body, pull requests with a state."""

    def __init__(self, issues=None, prs=None, verdict=VERDICT):
        self.issues = issues or {}
        self.prs = prs or {}
        self.verdict = verdict
        self.calls = []

    def issue(self, n):
        return self.issues.setdefault(n, {"labels": [], "body": "", "state": "OPEN"})

    def labels(self, n):
        return list(self.issue(n).get("labels") or [])

    def run(self, cmd, timeout=120, inp=None, env=None):
        args = list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd).split()
        self.calls.append(" ".join(args))
        if args[:1] == ["git"]:
            return 0, self.verdict, ""
        if "issue" in args and "view" in args:
            n = int(args[args.index("view") + 1])
            issue = self.issues.get(n)
            if issue is None:
                return 1, "", "no such issue"
            return 0, json.dumps({"number": n, "body": issue.get("body") or "",
                                   "labels": [{"name": l} for l in issue.get("labels") or []],
                                   "state": issue.get("state") or "OPEN"}), ""
        if "issue" in args and "edit" in args:
            n = int(args[args.index("edit") + 1])
            issue = self.issue(n)
            if "--body" in args:
                issue["body"] = args[args.index("--body") + 1]
            for flag, add in (("--add-label", True), ("--remove-label", False)):
                for i, arg in enumerate(args):
                    if arg != flag or i + 1 >= len(args):
                        continue
                    name = args[i + 1]
                    labels = issue.setdefault("labels", [])
                    if add and name not in labels:
                        labels.append(name)
                    elif not add and name in labels:
                        labels.remove(name)
            return 0, "", ""
        if "issue" in args and "list" in args:
            label = args[args.index("--label") + 1] if "--label" in args else None
            state = args[args.index("--state") + 1] if "--state" in args else "open"
            rows = []
            for n, issue in sorted(self.issues.items()):
                labels = issue.get("labels") or []
                if label and label not in labels:
                    continue
                if state and (issue.get("state") or "OPEN").lower() != state.lower():
                    continue
                rows.append({"number": n, "state": issue.get("state") or "OPEN",
                             "labels": [{"name": l} for l in labels], "body": issue.get("body") or ""})
            return 0, json.dumps(rows), ""
        if "pr" in args and "view" in args:
            n = int(args[args.index("view") + 1])
            pr = self.prs.get(n) or {}
            return 0, json.dumps({"number": n, "state": pr.get("state") or "OPEN",
                                   "headRefOid": pr.get("head") or SHA,
                                   "headRefName": pr.get("branch") or f"fleet/M5-issue{pr.get('issue')}-r1",
                                   "body": pr.get("body") or f"Closes #{pr.get('issue')}",
                                   "mergeable": pr.get("mergeable") or "MERGEABLE", "isDraft": False,
                                   "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}]}), ""
        if "pr" in args and "list" in args:
            rows = []
            for n, pr in sorted(self.prs.items()):
                rows.append({"number": n, "title": f"pr {n}", "headRefName": pr.get("branch") or "",
                             "headRefOid": pr.get("head") or SHA, "isDraft": False,
                             "mergeable": "MERGEABLE", "statusCheckRollup": [], "labels": [],
                             "body": pr.get("body") or f"Closes #{pr.get('issue')}"})
            return 0, json.dumps(rows), ""
        return 0, "", ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fix-cards-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.brain = os.path.join(self.tmp, "brain.md")
        raw = {"repo": "acme/project", "local_repo": self.tmp,
               "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")},
               "local_hosts": ["box"], "brain_log": self.brain, "brain_log_prefix": "dispatch"}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as handle:
            json.dump(raw, handle)
        self.cfg = E.load_config(path)

    def make(self, gh, state_dir=None, dry_run=False):
        cfg = dict(self.cfg)
        if state_dir:
            cfg["state_dir"] = state_dir
        dispatcher = E.Dispatcher(cfg, runner=gh.run, dry_run=dry_run, out=open(os.devnull, "w"))
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    def brain_lines(self):
        if not os.path.exists(self.brain):
            return []
        with open(self.brain, encoding="utf-8") as handle:
            return handle.read().splitlines()

    def log_text(self, dispatcher):
        path = dispatcher.path(dispatcher.cfg["log_file"])
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def review_changes(self, dispatcher, pr, issue, state=None):
        """Run one CHANGES review of a pull request; returns the state of the dispatcher."""
        state = {} if state is None else state
        st = {"issue": f"PR{pr}", "run": "r1", "review_pr": pr, "review_sha": SHA}
        dispatcher.finish_review("M5", {"host": "box", "backend": "pi"}, st,
                                 {"branch": "lane/x", "commit": "c"}, state)
        return state


class FixCardRules(unittest.TestCase):
    """The pure rules of lib/dispatch/fix_cards.py."""

    def test_card_is_queued_when_no_round_is_in_flight(self):
        plan = fix_cards.plan_fix_card(["lane-pr"], FIX_BODY)
        self.assertTrue(plan["queue"])
        self.assertEqual(plan["reason"], "queued")
        self.assertIsNone(plan["blocking"])
        self.assertEqual(plan["pr"], 21)
        self.assertTrue(plan["fix_card"])

    def test_card_is_blocked_while_a_lane_works_on_the_previous_round(self):
        plan = fix_cards.plan_fix_card(["lane-ready", "in-progress"], FIX_BODY)
        self.assertFalse(plan["queue"])
        self.assertEqual(plan["reason"], "in-progress")

    def test_card_is_blocked_while_the_previous_round_is_still_queued(self):
        plan = fix_cards.plan_fix_card(["lane-ready"], FIX_BODY)
        self.assertFalse(plan["queue"])
        self.assertEqual(plan["reason"], "lane-ready")

    def test_next_round_is_queued_after_the_old_card_finished_or_failed(self):
        for labels in (["lane-pr"], ["lane-failed"], []):
            with self.subTest(labels=labels):
                self.assertTrue(fix_cards.plan_fix_card(labels, FIX_BODY)["queue"])

    def test_lane_holding_the_issue_blocks_the_card_without_the_label(self):
        plan = fix_cards.plan_fix_card([], FIX_BODY, held=True)
        self.assertFalse(plan["queue"])
        self.assertEqual(plan["reason"], "lane-held")

    def test_source_pr_reads_the_rebase_line_and_the_section(self):
        self.assertEqual(fix_cards.source_pr(FIX_BODY), 21)
        section_only = "## Lane review requested changes\n\non PR #77 head `x` returned CHANGES."
        self.assertEqual(fix_cards.source_pr(section_only), 77)
        self.assertIsNone(fix_cards.source_pr("just a normal card"))

    def test_a_plain_card_is_no_fix_card(self):
        self.assertFalse(fix_cards.is_fix_card("do the thing\n\n验收：`make test`"))
        self.assertFalse(fix_cards.stale_cards(
            [{"number": 7, "state": "OPEN", "labels": ["lane-ready"], "body": "do the thing"}],
            {21: "MERGED"}))

    def test_stale_cards_flag_a_merged_pr_and_a_closed_issue(self):
        cards = [{"number": 201, "state": "OPEN", "labels": ["lane-ready"], "body": FIX_BODY},
                 {"number": 202, "state": "CLOSED", "labels": ["lane-ready"], "body": FIX_BODY}]
        stale = fix_cards.stale_cards(cards, {21: "MERGED"})
        self.assertEqual([(c["number"], c["reason"]) for c in stale], [(201, "pr-merged"), (202, "issue-closed")])
        # the pull request is still open: only the card of the closed issue is stale
        self.assertEqual([(c["number"], c["reason"]) for c in fix_cards.stale_cards(cards, {21: "OPEN"})],
                         [(202, "issue-closed")])

    def test_audit_line_is_greppable(self):
        self.assertEqual(fix_cards.audit_line("dedup", 201), "fix-card dedup #201")
        self.assertIn("fix-card cleanup #201", fix_cards.audit_line("cleanup", 201, "PR #21 merged"))


class FixCardEngine(Base):
    """The dispatch engine: one fix card per issue, no stale cards in the pool."""

    def test_changes_does_not_stack_a_second_fix_card(self):
        gh = FakeGitHub(issues={201: {"labels": ["lane-ready", "in-progress"], "body": "orig"}},
                        prs={21: {"issue": 201}})
        d = self.make(gh)
        self.review_changes(d, 21, 201)
        edits = [c for c in gh.calls if "issue edit 201" in c]
        self.assertFalse([c for c in edits if "--add-label lane-ready" in c], edits)
        self.assertEqual(gh.issue(201)["body"], "orig")   # the card of the running round stays untouched
        self.assertTrue(any("fix-card dedup #201" in line for line in self.brain_lines()))

    def test_changes_queues_the_next_round_after_the_old_card_is_done(self):
        gh = FakeGitHub(issues={201: {"labels": ["lane-pr"], "body": "orig"}}, prs={21: {"issue": 201}})
        d = self.make(gh)
        state = self.review_changes(d, 21, 201)
        self.assertIn("lane-ready", gh.labels(201))
        self.assertNotIn("lane-pr", gh.labels(201))
        self.assertIn("Lane review requested changes", gh.issue(201)["body"])
        self.assertEqual(state["_fix_cards"]["201"]["pr"], 21)

    def test_a_card_held_by_a_lane_blocks_the_round_even_without_the_label(self):
        gh = FakeGitHub(issues={201: {"labels": ["lane-pr"], "body": "orig"}}, prs={21: {"issue": 201}})
        d = self.make(gh)
        state = {"M5": {"issue": 201, "run": "r1"}}
        self.review_changes(d, 21, 201, state)
        self.assertNotIn("lane-ready", gh.labels(201))
        self.assertTrue(any("fix-card dedup #201" in line for line in self.brain_lines()))

    def test_stale_fix_card_leaves_the_pool_when_the_pr_was_merged(self):
        gh = FakeGitHub(issues={201: {"labels": ["lane-ready"], "body": FIX_BODY}},
                        prs={21: {"issue": 201, "state": "MERGED"}})
        d = self.make(gh)
        d.sweep_stale_fix_cards({})
        self.assertNotIn("lane-ready", gh.labels(201))
        self.assertTrue(any("fix-card cleanup #201" in line for line in self.brain_lines()))

    def test_sweep_keeps_a_fix_card_whose_pr_is_still_open(self):
        gh = FakeGitHub(issues={201: {"labels": ["lane-ready"], "body": FIX_BODY}},
                        prs={21: {"issue": 201, "state": "OPEN"}})
        d = self.make(gh)
        d.sweep_stale_fix_cards({})
        self.assertIn("lane-ready", gh.labels(201))
        self.assertEqual([line for line in self.brain_lines() if "fix-card" in line], [])

    def test_sweep_removes_the_card_of_a_closed_issue(self):
        gh = FakeGitHub(issues={201: {"labels": ["lane-ready"], "body": FIX_BODY, "state": "CLOSED"}},
                        prs={21: {"issue": 201, "state": "OPEN"}})
        d = self.make(gh)
        d.sweep_stale_fix_cards({})
        self.assertNotIn("lane-ready", gh.labels(201))
        self.assertTrue(any("fix-card cleanup #201" in line for line in self.brain_lines()))


class NightReplay(Base):
    """Replay the multi-round fix sample of 2026-11-02 (Refs #1713)."""

    def sample(self):
        with open(os.path.join(HERE, "fixtures", "fix_rounds_20261102.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_pool_holds_at_most_one_inflight_card_per_issue(self):
        for case in self.sample()["cases"]:
            with self.subTest(issue=case["issue"], title=case["title"]):
                self.replay(case)

    def replay(self, case):
        issue, pr = case["issue"], case["pr"]
        state_dir = os.path.join(self.tmp, "state-%s" % issue)
        gh = FakeGitHub(issues={issue: {"labels": list(case["steps"][0].get("labels") or []), "body": "orig"}},
                        prs={pr: {"issue": issue}})
        d = self.make(gh, state_dir=state_dir)
        state = {}
        queued = settled = 0
        for step in case["steps"]:
            event = step["event"]
            if event == "changes":
                self.review_changes(d, pr, issue, state)
                queued = self.log_text(d).count("fix task queued on #%s" % issue)
            elif event == "take":
                gh.issue(issue)["labels"] = list(step["labels"])
            elif event in ("done", "failed"):
                gh.issue(issue)["labels"] = list(step["labels"])
                settled += 1
            elif event == "merged":
                gh.prs[pr]["state"] = "MERGED"
                d.sweep_stale_fix_cards(state)
                self.assertNotIn("lane-ready", gh.labels(issue))
                settled += 1
            else:
                self.fail("unknown event %r" % event)
            self.assertLessEqual(queued - settled, 1,
                                 "issue #%s: %s fix cards in flight" % (issue, queued - settled))


if __name__ == "__main__":
    unittest.main()
