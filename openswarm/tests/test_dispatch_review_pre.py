"""The pre-review layer (Refs #1731): a pi PR passes the hygiene pre-review before the formal review.

The pre-review is no review round: it writes no reviews.json round, posts no commit status and
labels nothing.  Only the formal review (terra) counts.

The failure modes the layer has to survive:

* every pre-review lane is busy -> the PR waits for its timeout and then degrades into the formal
  review; the degradation stays on the PR (a comment and the dispatch log).
* the pre-review and the formal review disagree -> the formal review decides.
* codex output skips the pre-review; only the pi output goes through it.
* the pre-review run ends without a verdict (a VM pause, a quota) -> the PR is pre-reviewed again
  instead of slipping into the formal review unreviewed.
* the issue cannot be read or written -> the original requirements are kept and the fix task is
  queued again.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_pre_test",
                                               os.path.join(HERE, "..", "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

SHA = "a" * 40
SHA2 = "b" * 40
DIFF = ("diff --git a/engine.py b/engine.py\n--- a/engine.py\n+++ b/engine.py\n"
        "@@ -1 +1 @@\n-old\n+new\n")

PI_LANE = {"M5": {"host": "box", "backend": "pi"}}
PRE_LANE = {"C9": {"host": "box", "backend": "codex", "model": "gpt-6-luna", "review_pre": True}}
FORMAL_LANE = {"T1": {"host": "box", "backend": "codex", "model": "gpt-5.6-terra"}}


def pr(n, issue_n, author="M5", head=SHA, labels=()):
    return {"number": n, "title": f"pr {n}", "headRefName": f"fleet/{author}-issue{issue_n}-r1",
            "headRefOid": head, "isDraft": False, "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "linux-0", "conclusion": "SUCCESS"},
                                  {"name": "OpenSwarm tests (macos-latest)", "conclusion": "FAILURE"}],
            "labels": [{"name": l} for l in labels], "body": f"Closes #{issue_n}"}


def idle(*lanes):
    return {name: {"lane": "idle"} for name in lanes}


def pr_view(n, issue_n, head=SHA):
    return (0, json.dumps({"headRefOid": head, "state": "OPEN",
                            "headRefName": f"fleet/M5-issue{issue_n}-r1",
                            "body": f"Closes #{issue_n}"}), "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-pre-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = 1000.0
        self.pre_cfg = {}
        self.cfg_path = os.path.join(self.tmp, "cfg.json")

    def write_cfg(self):
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"],
               "review": {"pre": dict(self.pre_cfg)}}
        with open(self.cfg_path, "w") as f:
            json.dump(raw, f)
        return E.load_config(self.cfg_path)

    def make(self, rules=None, dry_run=False):
        self.calls = []
        self.dispatched = []

        def runner(cmd, **kwargs):
            text = " ".join(str(c) for c in cmd)
            self.calls.append(text)
            for needle, result in list(rules or []) + [("pr diff", (0, DIFF, ""))]:
                if needle in text:
                    return result(text) if callable(result) else result
            return 0, "", ""

        d = E.Dispatcher(self.write_cfg(), runner=runner, dry_run=dry_run, clock=lambda: self.now,
                         out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        d.main_sha = lambda: SHA

        def dispatch(lane, lc, item, base):
            self.dispatched.append((lane, item))
            return f"run-{lane}", "verify"

        d.dispatch = dispatch
        return d

    def called(self, needle):
        return [c for c in self.calls if needle in c]

    def log_text(self, d):
        path = d.path("dispatch.log")
        if not os.path.exists(path):
            return ""
        with open(path) as stream:
            return stream.read()

    def lanes(self, **extra):
        out = dict(PI_LANE)
        out.update(PRE_LANE)
        out.update(FORMAL_LANE)
        out.update(extra)
        return out

    def tick(self, d, lanes, state, res=None):
        """One dispatcher tick with the fleet probe and the issue queue stubbed out."""
        d.probe = lambda lanes, state: dict(res or {L: {"lane": "idle"} for L in lanes})
        d.queue = lambda: []
        d.replenish = lambda *args, **kwargs: None
        d.tick(lanes, state)

    def harvest(self, d, lanes, state, lane, model="gpt-6-luna"):
        """The harvest of a finished pre-review run (the lane wrote its verdict file)."""
        info = {"run": {"status": "done", "branch": f"fleet/{lane}", "commit": "c",
                        "backend": "codex", "model": model}}
        d.harvest(lanes, {lane: info}, state)


class PreReviewDispatch(Base):
    def test_pi_pr_goes_through_the_pre_review_first(self):
        d = self.make([("pr list", (0, json.dumps([pr(42, 420)]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["C9"]["pre_review_pr"], 42)
        self.assertEqual(state["C9"]["pre_review_sha"], SHA)
        self.assertNotIn("review_pr", state["C9"])         # the formal review did not start
        self.assertNotIn("T1", state)
        self.assertEqual(d.load_pre_reviews()["42"]["status"], "dispatched")
        self.assertEqual(d.load_pre_reviews()["42"]["lane"], "C9")
        self.assertEqual(d.load_reviews(), {})              # no review round yet
        self.assertEqual([lane for lane, _ in self.dispatched], ["C9"])
        self.assertTrue(self.dispatched[0][1]["pre_review"])

    def test_codex_output_skips_the_pre_review(self):
        d = self.make([("pr list", (0, json.dumps([pr(43, 430, author="C9")]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(d.load_pre_reviews(), {})          # no pre-review at all
        self.assertEqual(state["T1"]["review_pr"], 43)      # straight into the formal review (terra)
        self.assertNotIn("C9", state)

    def test_only_a_marked_lane_runs_the_pre_review(self):
        d = self.make([("pr list", (0, json.dumps([pr(47, 470)]), ""))])
        lanes = self.lanes(C8={"host": "box", "backend": "codex", "model": "gpt-6-luna", "review_pre": True})
        lanes["C9"] = {"host": "box", "backend": "codex", "model": "gpt-6-luna"}   # no marker
        state = {}
        d.dispatch_reviews(lanes, idle("M5", "C8", "C9", "T1"), state, [])
        self.assertEqual(state["C8"]["pre_review_pr"], 47)
        self.assertNotIn("C9", state)

    def test_a_lane_of_another_backend_is_not_a_pre_review_lane(self):
        d = self.make([("pr list", (0, json.dumps([pr(48, 480)]), ""))])
        lanes = self.lanes()
        lanes["P7"] = {"host": "box", "backend": "pi", "model": "m", "review_pre": True}
        state = {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "P7", "T1"), state, [])
        self.assertEqual(state["C9"]["pre_review_pr"], 48)
        self.assertNotIn("P7", state)

    def test_pre_review_concurrency_is_capped(self):
        self.pre_cfg = {"max_concurrency": 1}
        d = self.make([("pr list", (0, json.dumps([pr(49, 490), pr(50, 500)]), ""))])
        lanes = self.lanes(C8={"host": "box", "backend": "codex", "model": "gpt-6-luna", "review_pre": True})
        state = {}
        d.dispatch_reviews(lanes, idle("M5", "C8", "C9", "T1"), state, [])
        self.assertEqual(len([s for s in state.values() if s.get("pre_review_pr")]), 1)
        self.assertEqual(d.load_pre_reviews()["50"]["status"], "waiting")

    def test_an_orphaned_pre_review_run_is_queued_again(self):
        d = self.make([("pr list", (0, json.dumps([pr(62, 620)]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        st = state["C9"]
        self.now = time.time()                       # the run has to look old enough to be orphaned
        st["at"] = time.strftime("%F %T", time.localtime(self.now - 3600))
        self.assertTrue(d.reconcile("C9", lanes["C9"], st,
                                    {"run": "running", "lane": "idle", "current_run": "other",
                                     "runner_alive": False}, state))
        rec = d.load_pre_reviews()["62"]
        self.assertEqual(rec["status"], "waiting")   # queued again, not stuck until the timeout
        self.assertEqual(rec["reset_reason"], "the pre-review run was orphaned")
        self.assertEqual(d.load_reviews(), {})       # and no review round was counted


class FormalReviewLane(Base):
    """The formal review runs on a review lane (terra); a pre-review lane never takes it."""

    def test_a_pre_review_lane_is_never_a_formal_reviewer(self):
        d = self.make()
        lanes = self.lanes()
        self.assertNotIn("C9", d.formal_lanes(lanes, ["M5", "C9", "T1"]))
        self.assertIn("T1", d.formal_lanes(lanes, ["M5", "C9", "T1"]))

    def test_busy_terra_does_not_fall_back_to_other_idle_lanes(self):
        d = self.make([("pr list", (0, json.dumps([pr(80, 800)]), ""))])
        lanes, state = self.lanes(), {}
        d.put_pre_review(80, {"status": "approved", "sha": SHA, "lane": "C9"})
        res = idle("M5", "C9", "T1")
        res["T1"] = {"lane": "busy"}
        d.dispatch_reviews(lanes, res, state, [])
        self.assertEqual(state, {})

    def test_a_busy_terra_lane_makes_the_formal_review_wait(self):
        """An idle lane that is not a review lane never takes the formal review: the review waits
        for the terra lane instead (the review round is not lost, the PR stays a candidate)."""
        d = self.make([("pr list", (0, json.dumps([pr(83, 830)]), ""))])
        lanes, state = self.lanes(), {}
        lanes["M5"] = {"host": "box", "backend": "pi", "model": "pi-1"}    # idle, but not a review lane
        d.put_pre_review(83, {"status": "approved", "sha": SHA, "lane": "C9"})
        res = idle("M5", "C9", "T1")
        res["T1"] = {"lane": "busy"}                                        # the only terra lane is busy
        d.dispatch_reviews(lanes, res, state, [])
        self.assertEqual(state, {})                 # the formal review waits for the terra lane
        self.assertEqual(self.dispatched, [])       # and no lane is dispatched at all
        self.assertIn("waiting for a formal review lane", self.log_text(d))

    def test_the_formal_review_waits_for_the_terra_lane_through_tick(self):
        """The same wait on the tick level: the pre-review is done, terra is busy, another lane
        is idle -> the formal review is not dispatched and the PR is picked up again later."""
        d = self.make([("pr list", (0, json.dumps([pr(84, 840)]), ""))])
        lanes, state = self.lanes(), {}
        lanes["M5"] = {"host": "box", "backend": "pi", "model": "pi-1"}
        d.put_pre_review(84, {"status": "approved", "sha": SHA, "lane": "C9"})
        res = {"M5": {"lane": "idle"}, "C9": {"lane": "idle"}, "T1": {"lane": "busy"}}
        self.tick(d, lanes, state, res=res)
        self.assertFalse([L for L, s in state.items() if isinstance(s, dict) and s.get("review_pr")])
        self.assertEqual(self.dispatched, [])
        # the terra lane falls free: the formal review runs now
        res["T1"] = {"lane": "idle"}
        self.tick(d, lanes, state, res=res)
        self.assertEqual(state["T1"]["review_pr"], 84)
        self.assertNotIn("M5", state)

    def test_the_formal_review_goes_to_the_terra_lane(self):
        d = self.make([("pr list", (0, json.dumps([pr(80, 800)]), ""))])
        lanes, state = self.lanes(), {}
        d.put_pre_review(80, {"status": "approved", "sha": SHA, "lane": "C9"})
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["T1"]["review_pr"], 80)     # the terra lane
        self.assertNotIn("C9", state)                       # never the pre-review lane
        self.assertEqual([lane for lane, _ in self.dispatched], ["T1"])

    def test_the_formal_review_prefers_a_lane_that_states_the_terra_model(self):
        d = self.make([("pr list", (0, json.dumps([pr(81, 810)]), ""))])
        lanes, state = self.lanes(), {}
        lanes["M5"] = {"host": "box", "backend": "pi", "model": "pi-1"}      # a model, but no terra
        d.put_pre_review(81, {"status": "approved", "sha": SHA, "lane": "C9"})
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["T1"]["review_pr"], 81)

    def test_without_a_terra_lane_the_review_falls_back_instead_of_stalling(self):
        d = self.make([("pr list", (0, json.dumps([pr(82, 820)]), ""))])
        lanes, state = self.lanes(), {}
        d.put_pre_review(82, {"status": "approved", "sha": SHA, "lane": "C9"})
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertTrue(state.get("M5", {}).get("review_pr") or state.get("T1", {}).get("review_pr"))

    def test_a_fleet_without_a_review_lane_keeps_the_old_behaviour(self):
        """An old lanes.json without a review lane: the formal review still runs (it does not
        stall for ever), but the gap is left in the dispatch log."""
        d = self.make([("pr list", (0, json.dumps([pr(85, 850)]), ""))])
        lanes = {"M5": {"host": "box", "backend": "pi"},
                 "M6": {"host": "box", "backend": "pi"},
                 "C9": {"host": "box", "backend": "codex", "model": "gpt-6-luna", "review_pre": True}}
        state = {}
        d.put_pre_review(85, {"status": "approved", "sha": SHA, "lane": "C9"})
        d.dispatch_reviews(lanes, idle("M5", "M6", "C9"), state, [])
        self.assertEqual(state["M6"]["review_pr"], 85)      # a lane that is not a pre-review lane
        self.assertIn("no lane of the fleet states the formal review model", self.log_text(d))

    def test_a_fleet_without_a_review_lane_waits_when_the_fallback_is_forbidden(self):
        """`review.formal.required` turns the fallback off: then the formal review waits."""
        d = self.make([("pr list", (0, json.dumps([pr(86, 860)]), ""))])
        d.F["required"] = True
        lanes = {"M5": {"host": "box", "backend": "pi"},
                 "M6": {"host": "box", "backend": "pi"},
                 "C9": {"host": "box", "backend": "codex", "model": "gpt-6-luna", "review_pre": True}}
        state = {}
        d.put_pre_review(86, {"status": "approved", "sha": SHA, "lane": "C9"})
        d.dispatch_reviews(lanes, idle("M5", "M6", "C9"), state, [])
        self.assertEqual(state, {})


class PreReviewRetry(Base):
    """A pre-review run that ends without a verdict is pre-reviewed again, not dropped."""

    def test_a_paused_pre_review_run_is_queued_again(self):
        d = self.make([("pr list", (0, json.dumps([pr(63, 630)]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 63, "pre_review_sha": SHA},
                            {"status": "failed", "error_class": "vm_paused"}, state)
        rec = d.load_pre_reviews()["63"]
        self.assertEqual(rec["status"], "waiting")        # retryable, not stuck in "dispatched"
        self.assertEqual(d.load_reviews(), {})            # and no review round
        state.pop("C9", None)
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["C9"]["pre_review_pr"], 63)   # the pre-review runs again

    def test_a_quota_pre_review_run_is_queued_again(self):
        d = self.make([("pr list", (0, json.dumps([pr(64, 640)]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 64, "pre_review_sha": SHA},
                            {"status": "failed", "error_class": "quota"}, state)
        self.assertEqual(d.load_pre_reviews()["64"]["status"], "waiting")
        self.assertEqual(d.load_reviews(), {})

    def test_the_retry_still_obeys_the_bounded_timeout(self):
        d = self.make([("pr list", (0, json.dumps([pr(65, 650)]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 65, "pre_review_sha": SHA},
                            {"status": "failed", "error_class": "model_unavailable"}, state)
        self.now += 1000                                    # the timeout passes
        state.pop("C9", None)
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        rec = d.load_pre_reviews()["65"]
        self.assertEqual(rec["status"], "degraded")
        self.assertEqual(state["T1"]["review_pr"], 65)      # the formal review took over


class PreReviewFixTask(Base):
    """The fix task of a CHANGES verdict reaches the issue, or it is queued again."""

    def test_a_failed_issue_read_keeps_the_original_body(self):
        d = self.make([("pr list", (0, json.dumps([pr(66, 660)]), "")),
                       ("git -C", (0, "VERDICT: CHANGES\nSUMMARY: x\n", "")),
                       ("pr view 66", pr_view(66, 660)),
                       ("issue view 660", (1, "", "Could not resolve to an Issue"))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 66, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        rec = d.load_pre_reviews()["66"]
        self.assertEqual(rec["status"], "changes_pending")     # the fix task waits
        self.assertFalse(self.called("issue edit"))             # the issue was never overwritten
        self.assertEqual(d.load_reviews(), {})                 # and no review round was counted

    def test_a_failed_issue_write_leaves_the_fix_task_pending(self):
        d = self.make([("pr list", (0, json.dumps([pr(67, 670)]), "")),
                       ("git -C", (0, "VERDICT: CHANGES\nSUMMARY: x\n", "")),
                       ("pr view 67", pr_view(67, 670)),
                       ("issue view 670", (0, json.dumps({"body": "do x"}), "")),
                       ("issue edit", (1, "", "HTTP 502"))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 67, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertEqual(d.load_pre_reviews()["67"]["status"], "changes_pending")
        self.assertEqual(d.load_reviews(), {})

    def test_the_pending_fix_task_is_queued_again_on_the_next_tick(self):
        edits = {"n": 0}

        def issue_edit(_text):
            edits["n"] += 1
            return (1, "", "HTTP 502") if edits["n"] == 1 else (0, "", "")

        d = self.make([("pr list", (0, json.dumps([pr(68, 680)]), "")),
                       ("git -C", (0, "VERDICT: CHANGES\nSUMMARY: x\n", "")),
                       ("pr view 68", pr_view(68, 680)),
                       ("issue view 680", (0, json.dumps({"body": "do x"}), "")),
                       ("issue edit", issue_edit)])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 68, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertEqual(d.load_pre_reviews()["68"]["status"], "changes_pending")
        # the write works now: the next tick queues the fix task
        state.pop("C9")
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        rec = d.load_pre_reviews()["68"]
        self.assertEqual(rec["status"], "changes")
        self.assertEqual(d.load_reviews(), {})


class PreReviewVerdict(Base):
    def test_approval_lets_the_formal_review_start(self):
        d = self.make([("pr list", (0, json.dumps([pr(51, 510)]), "")),
                       ("git -C", (0, "VERDICT: APPROVE\nSUMMARY: clean\n", "")),
                       ("pr view 51", pr_view(51, 510))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["C9"]["pre_review_pr"], 51)
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 51, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertEqual(d.load_pre_reviews()["51"]["status"], "approved")
        self.assertEqual(d.load_reviews(), {})           # the pre-review is no review round
        self.assertFalse(self.called("statuses/"))       # and it posts no commit status
        state.pop("C9")
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["T1"]["review_pr"], 51)   # the formal review (terra) runs now
        self.assertNotIn("C9", state)

    def test_changes_queues_the_fix_task_and_counts_no_review_round(self):
        d = self.make([("pr list", (0, json.dumps([pr(52, 520)]), "")),
                       ("git -C", (0, "VERDICT: CHANGES\nSUMMARY: binary blob\n", "")),
                       ("pr view 52", pr_view(52, 520)),
                       ("issue view 520", (0, json.dumps({"body": "do x"}), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 52, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        rec = d.load_pre_reviews()["52"]
        self.assertEqual(rec["status"], "changes")
        self.assertEqual(rec["round"], 1)
        self.assertTrue(any("--add-label lane-ready" in c for c in self.calls))
        self.assertTrue(any("Lane pre-review requested changes" in c for c in self.calls))
        self.assertEqual(d.load_reviews(), {})           # the control: no review round was counted
        self.assertFalse(self.called("statuses/"))       # and no commit status

    def test_a_missing_verdict_is_counted_on_the_pre_review_record_only(self):
        d = self.make([("pr list", (0, json.dumps([pr(53, 530)]), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 53, "pre_review_sha": SHA},
                            {"status": "failed", "error_class": "exec_error"}, state)
        rec = d.load_pre_reviews()["53"]
        self.assertEqual((rec["status"], rec["fails"]), ("waiting", 1))
        self.assertEqual(d.load_reviews(), {})

    def test_a_moved_head_discards_the_pre_review_verdict(self):
        d = self.make([("pr list", (0, json.dumps([pr(54, 540)]), "")),
                       ("git -C", (0, "VERDICT: APPROVE\n", "")),
                       ("pr view 54", pr_view(54, 540, head=SHA2))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 54, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertNotEqual(d.load_pre_reviews()["54"].get("status"), "approved")

    def test_the_harvest_of_a_pre_review_run_keeps_it_out_of_the_review_records(self):
        d = self.make([("git -C", (0, "VERDICT: APPROVE\nSUMMARY: clean\n", "")),
                       ("pr view 59", pr_view(59, 590))])
        lanes = self.lanes()
        st = {"issue": "PR59", "run": "run-C9", "pre_review_pr": 59, "pre_review_sha": SHA}
        info = {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}
        self.assertTrue(d.finish("C9", lanes["C9"], st, info, {}))
        self.assertEqual(d.load_pre_reviews()["59"]["status"], "approved")
        self.assertEqual(d.load_reviews(), {})           # the harvest wrote no review record
        self.assertFalse(self.called("statuses/"))


class PreReviewTimeout(Base):
    def test_all_pre_review_lanes_busy_degrades_into_the_formal_review(self):
        d = self.make([("pr list", (0, json.dumps([pr(55, 550)]), ""))])
        lanes = self.lanes()
        state = {"C9": {"issue": "PR9", "run": "busy"}}     # the only pre-review lane is busy
        res = {"M5": {"lane": "idle"}, "C9": {"lane": "busy"}, "T1": {"lane": "idle"}}
        d.dispatch_reviews(lanes, res, state, [])
        self.assertEqual(d.load_pre_reviews()["55"]["status"], "waiting")
        self.assertNotIn("T1", state)                       # the formal review waits for the pre-review
        self.now += 1000                                     # the timeout passes
        d.dispatch_reviews(lanes, res, state, [])
        rec = d.load_pre_reviews()["55"]
        self.assertEqual(rec["status"], "degraded")
        self.assertIn("no pre-review verdict in time", rec["degraded_reason"])
        self.assertEqual(state["T1"]["review_pr"], 55)      # the formal review took over
        self.assertTrue(any("lane-pre-review-degraded" in c for c in self.calls))
        self.assertIn("degraded to the formal review", self.log_text(d))

    def test_the_fix_round_of_a_changes_verdict_also_degrades(self):
        d = self.make([("pr list", (0, json.dumps([pr(56, 560)]), "")),
                       ("git -C", (0, "VERDICT: CHANGES\nSUMMARY: x\n", "")),
                       ("pr view 56", pr_view(56, 560)),
                       ("issue view 560", (0, json.dumps({"body": "do x"}), ""))])
        lanes, state = self.lanes(), {}
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 56, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertEqual(d.load_pre_reviews()["56"]["status"], "changes")
        self.now += 4000                                     # the fix round does not come back
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        rec = d.load_pre_reviews()["56"]
        self.assertEqual(rec["status"], "degraded")
        self.assertIn("the fix round did not come back in time", rec["degraded_reason"])


class PreReviewPrecedence(Base):
    def test_the_formal_review_supersedes_the_pre_review(self):
        d = self.make([("git -C", (0, "VERDICT: APPROVE\nSUMMARY: ok\n", "")),
                       ("pr view 57", pr_view(57, 570))])
        d.put_pre_review(57, {"status": "approved", "sha": SHA, "lane": "C9"})
        d.finish_review("M5", {"host": "box", "backend": "pi"}, {"run": "r1", "review_pr": 57, "review_sha": SHA},
                        {"branch": "fleet/M5", "commit": "c", "backend": "pi", "model": "m"}, {})
        self.assertEqual(d.load_pre_reviews()["57"]["status"], "superseded")
        self.assertEqual(d.load_pre_reviews()["57"]["superseded_by"], "M5")
        self.assertEqual(d.load_reviews()["57"]["verdict"], "APPROVE")

    def test_a_late_pre_review_verdict_does_not_override_the_formal_review(self):
        d = self.make([("git -C", (0, "VERDICT: CHANGES\nSUMMARY: x\n", "")),
                       ("pr view 58", pr_view(58, 580)),
                       ("issue view 580", (0, json.dumps({"body": "do x"}), ""))])
        d.put_pre_review(58, {"status": "degraded", "sha": SHA, "lane": "C9"})
        d.finish_pre_review("C9", {"host": "box", "backend": "codex"},
                            {"pre_review_pr": 58, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, {})
        self.assertEqual(d.load_pre_reviews()["58"]["status"], "degraded")
        self.assertFalse(any("--add-label lane-ready" in c for c in self.calls))


class PreReviewChain(Base):
    """The acceptance sample: a pi PR goes through the pre-review, the findings are fixed and
    only then does the PR enter the formal review.  Every step leaves a log line."""

    def test_the_whole_chain_is_logged_and_the_review_round_counter_stays_at_zero(self):
        head, verdict = {"sha": SHA}, {"text": "VERDICT: CHANGES\nSUMMARY: a binary blob\n"}

        def pr_list(_text):
            p = pr(60, 600)
            p["headRefOid"] = head["sha"]
            return 0, json.dumps([p]), ""

        d = self.make([("pr list", pr_list),
                       ("git -C", lambda _text: (0, verdict["text"], "")),
                       ("pr view 60", lambda _text: pr_view(60, 600, head=head["sha"])),
                       ("issue view 600", (0, json.dumps({"body": "do x"}), ""))])
        lanes, state = self.lanes(), {}
        # 1. the pi PR goes to the pre-review lane first: the formal review does not start
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["C9"]["pre_review_pr"], 60)
        self.assertNotIn("review_pr", state["C9"])
        # 2. the pre-review finds problems: the production lane gets the fix task
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 60, "pre_review_sha": SHA},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertEqual(d.load_pre_reviews()["60"]["status"], "changes")
        self.assertTrue(any("Lane pre-review requested changes" in c for c in self.calls))
        self.assertEqual(d.load_reviews(), {})          # no review round yet
        # 3. the fix comes back with a new head: the pre-review runs again on that head
        state.pop("C9")
        head["sha"] = SHA2
        verdict["text"] = "VERDICT: APPROVE\nSUMMARY: clean now\n"
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["C9"]["pre_review_sha"], SHA2)
        # 4. the pre-review approves: only now may the formal review start
        d.finish_pre_review("C9", lanes["C9"], {"pre_review_pr": 60, "pre_review_sha": SHA2},
                            {"branch": "fleet/C9", "commit": "c", "backend": "codex", "model": "gpt-6-luna"}, state)
        self.assertEqual(d.load_pre_reviews()["60"]["status"], "approved")
        self.assertEqual(d.load_reviews(), {})          # the pre-review still counts no review round
        state.pop("C9")
        d.dispatch_reviews(lanes, idle("M5", "C9", "T1"), state, [])
        self.assertEqual(state["T1"]["review_pr"], 60)  # the formal review (terra) runs now
        self.assertNotIn("C9", state)                   # the pre-review lane never takes it
        # 5. the whole chain is in the dispatch log
        log = self.log_text(d)
        self.assertIn("pre-review dispatched PR #60", log)
        self.assertIn("CHANGES → fix task queued on #600", log)
        self.assertIn("APPROVE → the formal review may start", log)


class PreReviewIntegration(Base):
    """The same acceptance sample on the tick level: the dispatcher itself drives the chain."""

    def test_the_whole_chain_goes_through_tick(self):
        head, verdict = {"sha": SHA}, {"text": "VERDICT: CHANGES\nSUMMARY: a binary blob\n"}

        def pr_list(_text):
            p = pr(70, 700)
            p["headRefOid"] = head["sha"]
            return 0, json.dumps([p]), ""

        d = self.make([("pr list", pr_list),
                       ("PRE-PR-", lambda _text: (0, verdict["text"], "")),
                       ("pr view 70", lambda _text: pr_view(70, 700, head=head["sha"])),
                       ("issue view 700", (0, json.dumps({"body": "do x"}), "")),
                       ("issue list", (0, "[]", ""))])
        lanes, state = self.lanes(), {}
        # 1. the tick hands the pi PR to the pre-review lane, the formal review waits
        self.tick(d, lanes, state)
        self.assertEqual(state["C9"]["pre_review_pr"], 70)
        self.assertNotIn("review_pr", state["C9"])
        self.assertEqual(d.load_reviews(), {})          # no review round yet
        # 2. the harvest of the pre-review run queues the fix task on the issue
        self.harvest(d, lanes, state, "C9")
        self.assertEqual(d.load_pre_reviews()["70"]["status"], "changes")
        self.assertTrue(any("Lane pre-review requested changes" in c for c in self.calls))
        self.assertEqual(d.load_reviews(), {})          # the pre-review counts no review round
        # 3. the production lane fixed the findings: a new head comes back and is pre-reviewed again
        head["sha"] = SHA2
        verdict["text"] = "VERDICT: APPROVE\nSUMMARY: clean now\n"
        self.tick(d, lanes, state)
        self.assertEqual(state["C9"]["pre_review_sha"], SHA2)
        # 4. the pre-review approves: only now the formal review (terra) may start
        self.harvest(d, lanes, state, "C9")
        self.assertEqual(d.load_pre_reviews()["70"]["status"], "approved")
        self.assertEqual(d.load_reviews(), {})          # still no review round
        self.tick(d, lanes, state)
        self.assertEqual(state["T1"]["review_pr"], 70)  # the formal review runs on the terra lane
        self.assertNotIn("C9", state)
        self.assertEqual(d.load_reviews(), {})          # the formal review has not finished yet
        # 5. the whole chain is in the dispatch log
        log = self.log_text(d)
        self.assertIn("pre-review dispatched PR #70", log)
        self.assertIn("CHANGES → fix task queued on #700", log)
        self.assertIn("APPROVE → the formal review may start", log)
        self.assertIn("review dispatched PR #70", log)

    def test_the_all_busy_timeout_degrades_into_the_formal_review_through_tick(self):
        d = self.make([("pr list", (0, json.dumps([pr(71, 710)]), "")),
                       ("issue list", (0, "[]", ""))])
        lanes = self.lanes()
        state = {"C9": {"issue": "PR9", "run": "busy", "at": time.strftime("%F %T")}}   # the pre-review lane is busy
        res = {"M5": {"lane": "idle"}, "C9": {"lane": "busy"}, "T1": {"lane": "idle"}}
        self.tick(d, lanes, state, res=res)
        self.assertEqual(d.load_pre_reviews()["71"]["status"], "waiting")
        self.assertNotIn("T1", state)                    # the formal review waits for the pre-review
        self.now += 1000                                  # the timeout passes
        self.tick(d, lanes, state, res=res)
        rec = d.load_pre_reviews()["71"]
        self.assertEqual(rec["status"], "degraded")
        self.assertIn("no pre-review verdict in time", rec["degraded_reason"])
        self.assertEqual(state["T1"]["review_pr"], 71)   # the formal review took over
        self.assertTrue(any("lane-pre-review-degraded" in c for c in self.calls))
        self.assertIn("degraded to the formal review", self.log_text(d))
        self.assertEqual(d.load_reviews(), {})           # the degradation counts no review round
