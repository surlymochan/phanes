"""Dispatch risk feedback (#1597): a blocked operation travels into the next task.

A lane run that was stopped by the risk guard writes the blocked operation into
``result.json`` (``risk_blocks``: ``{"risk", "command"}`` pairs, command already
redacted).  The dispatcher keeps those pairs per card, renders them into the next
task (implementation card) or the next review task, and retries a risk-blocked
card once on another backend before it is labelled ``lane-failed``.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_under_test",
                                               os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

_risk_spec = importlib.util.spec_from_file_location("dispatch_risk_feedback_under_test",
                                                     os.path.join(os.path.dirname(HERE), "lib", "dispatch", "risk_feedback.py"))
RF = importlib.util.module_from_spec(_risk_spec)
_risk_spec.loader.exec_module(RF)

from tests.test_dispatch_engine import Base, SHA, pr

RESULT = {                      # a result.json as the lane writes it
    "status": "failed",
    "error_class": "risk_blocked",
    "backend": "pi",
    "model": "minimax",
    "summary": "blocked by the risk guard",
    "risk_blocks": [{"risk": "secret_read", "command": "secret print"}],
}

RUN = "20261102-072724-9d9f8c"


def run_payload(result):
    """The run payload the engine gets from the lane: result.json through REMOTE_STATUS."""
    home = tempfile.mkdtemp(prefix="openswarm-home-")
    try:
        os.makedirs(os.path.join(home, "lanes", "M5"))
        os.makedirs(os.path.join(home, "runs", RUN))
        with open(os.path.join(home, "lanes", "M5", "state.json"), "w") as f:
            json.dump({"state": "idle"}, f)
        with open(os.path.join(home, "runs", RUN, "result.json"), "w") as f:
            json.dump(result, f)
        # the very script the engine runs on the lane host
        proc = subprocess.run([sys.executable, "-", "M5", RUN, home],
                              input=E.REMOTE_STATUS, capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError(proc.stderr)
        return json.loads(proc.stdout)["run"]
    finally:
        shutil.rmtree(home, ignore_errors=True)


class RunPayload(Base):
    """The blocked operations must survive the trip from result.json to the dispatcher."""

    def test_result_json_risk_blocks_reach_the_run_payload(self):
        payload = run_payload(RESULT)
        self.assertEqual(payload["error_class"], "risk_blocked")
        self.assertEqual(payload["risk_blocks"], RESULT["risk_blocks"])


class Feedback(unittest.TestCase):
    """The pure module: extract, deduplicate, cap and render."""

    def test_render_lists_the_blocked_operation(self):
        notes = RF.accumulate(None, RESULT)
        text = RF.render(notes)
        self.assertIn(RF.HEADING, text)
        self.assertIn("secret_read", text)
        self.assertIn("secret print", text)

    def test_render_is_empty_without_notes(self):
        self.assertEqual(RF.render(None), "")
        self.assertEqual(RF.render([]), "")

    def test_notes_are_deduplicated(self):
        notes = RF.accumulate(None, RESULT)
        notes = RF.accumulate(notes, RESULT)          # the same block seen again
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["command"], "secret print")

    def test_notes_are_capped_at_five(self):
        many = {"risk_blocks": [{"risk": "r%d" % i, "command": "cmd %d" % i} for i in range(8)]}
        notes = RF.accumulate(None, many)
        self.assertEqual(len(notes), RF.MAX_NOTES)
        self.assertEqual([n["command"] for n in notes], ["cmd 3", "cmd 4", "cmd 5", "cmd 6", "cmd 7"])

    def test_command_is_truncated(self):
        long_command = "secret print " + "x" * 400
        notes = RF.accumulate(None, {"risk_blocks": [{"risk": "secret_read", "command": long_command}]})
        self.assertEqual(len(notes[0]["command"]), RF.COMMAND_LIMIT)


class TaskText(Base):
    """The implementation card carries the forbidden operations."""

    def test_task_text_appends_the_risk_section(self):
        d = self.make()
        notes = RF.accumulate(None, RESULT)
        text = d.task_text({"n": 1509, "title": "t", "body": "do the thing", "risk_notes": notes})
        self.assertIn(RF.HEADING, text)
        self.assertIn("secret print", text)
        self.assertGreater(text.index(RF.HEADING), text.index("do the thing"))

    def test_task_text_without_notes_is_unchanged(self):
        d = self.make()
        item = {"n": 1509, "title": "t", "body": "do the thing"}
        self.assertEqual(d.task_text(item),
                         f"# #1509 t\n\ndo the thing\n\n{d.cfg['templates']['rules'].format(n=1509)}\n")


class ReviewTask(Base):
    """The review task carries the forbidden operations too."""

    def test_review_task_contains_the_risk_section(self):
        d = self.make()
        d.put_review(1509, {"risk_feedback": RF.accumulate(None, RESULT)})
        d.gh = lambda args, **kwargs: (0, "diff --git a/a b/a\n+change\n", "")
        item = d.review_item(pr(1509, 1509))
        self.assertIn(RF.HEADING, item["body"])
        self.assertIn("secret print", item["body"])

    def test_review_task_without_feedback_is_unchanged(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, "diff --git a/a b/a\n+change\n", "")
        item = d.review_item(pr(1509, 1509))
        self.assertNotIn(RF.HEADING, item["body"])


class RiskBlocked(Base):
    """A risk-blocked card is rerouted once and only then labelled lane-failed."""

    ST = {"issue": 1509, "run": RUN}

    def blocked_info(self, backend="pi"):
        """The harvest payload of a risk-blocked run, as the lane host reports it."""
        result = dict(RESULT, backend=backend)
        return run_payload(result)

    def harvest(self, d, state, backend="pi"):
        """One harvest of a risk-blocked run; returns the calls it made."""
        before = len(d.runner.calls)
        st = dict(self.ST)
        self.assertTrue(d.finish("M5", {"host": "box", "backend": backend}, st, self.blocked_info(backend), state))
        return d.runner.calls[before:]

    def test_risk_blocked_reroutes_once_then_fails(self):
        d = self.make()
        gh_calls = []
        d.gh = lambda args, **kwargs: (gh_calls.append(args) or (0, "", ""))
        state = {}

        first = self.harvest(d, state)
        labels = " ".join(c for c in first if "issue edit" in c)
        self.assertIn("--add-label lane-ready", labels)
        self.assertNotIn("--add-label lane-failed", labels)
        self.assertTrue(any("blocked by risk controls" in " ".join(a) for a in gh_calls))
        self.assertFalse(any("blocked by risk controls again" in " ".join(a) for a in gh_calls))
        # the blocked operation is kept for the next task and the backend is remembered
        self.assertEqual(state["_risk_feedback"]["1509"][0]["command"], "secret print")
        self.assertEqual(state["_risk_backend"]["1509"], "pi")

        gh_calls.clear()
        second = self.harvest(d, state)
        labels = " ".join(c for c in second if "issue edit" in c)
        self.assertIn("--add-label lane-failed", labels)
        self.assertNotIn("--add-label lane-ready", labels)
        self.assertTrue(any("blocked by risk controls again" in " ".join(a) for a in gh_calls))
        # a terminal block ends the retry sequence: no stale backend or budget is kept
        self.assertNotIn("1509", state.get("_risk_backend") or {})
        self.assertNotIn("1509", state.get("_retry_attempts") or {})
        self.assertNotIn("1509", state.get("_risk_feedback") or {})

    def test_blocked_backend_is_skipped_and_the_notes_reach_the_next_task(self):
        d = self.make()
        notes = RF.accumulate(None, RESULT)
        state = {"_risk_feedback": {"1509": notes}, "_risk_backend": {"1509": "pi"}}
        lanes = {"M1": {"host": "box", "backend": "pi"}, "M2": {"host": "box", "backend": "codex"}}
        d.label_state = lambda n: "fresh"
        d.main_sha = lambda: SHA
        picked = []
        d.dispatch = lambda lane, lc, item, base: (picked.append((lane, item)) or ("run-1", None))
        q = [{"n": 1509, "title": "t", "body": "do the thing", "deps_ok": True,
              "backend": None, "strict": False, "prio": 1}]
        d.assign_issues(lanes, {lane: {"lane": "idle"} for lane in lanes}, state, q)
        self.assertEqual(len(picked), 1)
        lane, item = picked[0]
        self.assertEqual(lane, "M2")                      # the blocked backend is skipped
        self.assertEqual(item["risk_notes"], notes)
        text = d.task_text(item)
        self.assertIn(RF.HEADING, text)
        self.assertIn("secret print", text)


class RerouteLimits(Base):
    """The reroute only applies when another backend can really take the card (#1597).

    A card pinned to one backend (`strict-backend`) or a fleet with a single
    backend has nowhere else to go: excluding the blocked backend there would
    strand the card in `lane-ready` forever, without a lane ever taking it.
    """

    ST = {"issue": 1509, "run": RUN}

    def blocked(self, d, state, backend="pi"):
        """One risk-blocked harvest; returns the calls it made."""
        before = len(d.runner.calls)
        info = run_payload(dict(RESULT, backend=backend))
        d.finish("M5", {"host": "box", "backend": backend}, dict(self.ST), info, state)
        return d.runner.calls[before:]

    def dispatch_once(self, d, state, lanes, q):
        """One assignment round; returns the (lane, item) that took the card."""
        d.label_state = lambda n: "fresh"
        d.main_sha = lambda: SHA
        picked = []
        d.dispatch = lambda lane, lc, item, base: (picked.append((lane, item)) or ("run-1", None))
        d.assign_issues(lanes, {lane: {"lane": "idle"} for lane in lanes}, state, list(q))
        return picked[0] if picked else None

    def card(self, backend=None, strict=False):
        return {"n": 1509, "title": "t", "body": "do the thing", "deps_ok": True,
                "backend": backend, "strict": strict, "prio": 1}

    def test_strict_card_goes_back_to_its_own_backend(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, "", "")
        state = {}
        self.blocked(d, state)
        self.assertEqual(state["_risk_backend"]["1509"], "pi")
        lanes = {"M1": {"host": "box", "backend": "pi"}, "M2": {"host": "box", "backend": "codex"}}
        picked = self.dispatch_once(d, state, lanes, [self.card(backend="pi", strict=True)])
        self.assertIsNotNone(picked, "a strict card must be dispatched again, not stranded in lane-ready")
        self.assertEqual(picked[0], "M1")                # only the pinned backend may take it
        self.assertIn(RF.HEADING, d.task_text(picked[1]))

    def test_single_backend_fleet_dispatches_the_card_again(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, "", "")
        state = {}
        self.blocked(d, state)
        lanes = {"M1": {"host": "box", "backend": "pi"}}
        picked = self.dispatch_once(d, state, lanes, [self.card()])
        self.assertIsNotNone(picked, "with a single backend the card must be retried, not stranded")
        self.assertEqual(picked[0], "M1")

    def test_drained_alternative_waits_until_available(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, "", "")
        d.host_ok = lambda host: True
        d.paused_lane = lambda lane, lc: False
        state = {}
        self.blocked(d, state)
        lanes = {"M1": {"host": "box", "backend": "pi"},
                 "M2": {"host": "box", "backend": "codex", "drain": True}}
        picked = self.dispatch_once(d, state, lanes, [self.card()])
        self.assertIsNone(picked)
        lanes["M2"]["drain"] = False
        self.assertEqual(self.dispatch_once(d, state, lanes, [self.card()])[0], "M2")

    def test_paused_alternative_waits_until_available(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, "", "")
        d.host_ok = lambda host: True
        d.paused_lane = lambda lane, lc: lane == "M2"
        state = {}
        self.blocked(d, state)
        lanes = {"M1": {"host": "box", "backend": "pi"},
                 "M2": {"host": "box", "backend": "codex"}}
        picked = self.dispatch_once(d, state, lanes, [self.card()])
        self.assertIsNone(picked)
        d.paused_lane = lambda lane, lc: False
        self.assertEqual(self.dispatch_once(d, state, lanes, [self.card()])[0], "M2")

    def test_strict_card_is_lane_failed_after_the_second_block(self):
        d = self.make()
        gh_calls = []
        d.gh = lambda args, **kwargs: (gh_calls.append(args) or (0, "", ""))
        state = {}
        first = self.blocked(d, state)
        self.assertIn("--add-label lane-ready", " ".join(first))
        second = self.blocked(d, state)
        self.assertIn("--add-label lane-failed", " ".join(second))
        self.assertNotIn("--add-label lane-ready", " ".join(second))
        self.assertTrue(any("blocked by risk controls again" in " ".join(a) for a in gh_calls))


class RiskStateCleanup(Base):
    """The risk bookkeeping must not leak into a later, manual attempt (#1597)."""

    def dirty_state(self):
        return {"_risk_backend": {"1509": "pi"}, "_retry_attempts": {"1509": 1},
                "_risk_feedback": {"1509": RF.accumulate(None, RESULT)}}

    def assert_cleared(self, state):
        for name in ("_risk_backend", "_retry_attempts", "_risk_feedback"):
            self.assertNotIn("1509", state.get(name) or {})

    def test_generic_failure_clears_the_risk_state(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, "", "")
        state = self.dirty_state()
        info = run_payload(dict(RESULT, error_class="exec_error"))
        d.finish("M5", {"host": "box", "backend": "pi"}, {"issue": 1509, "run": RUN}, info, state)
        self.assert_cleared(state)

    def test_closed_card_loses_its_risk_state(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, json.dumps({"state": "CLOSED", "labels": []}), "")
        state = self.dirty_state()
        d.sweep_closed_risk_state(state, [])
        self.assert_cleared(state)

    def test_open_card_keeps_its_risk_state(self):
        d = self.make()
        d.gh = lambda args, **kwargs: (0, json.dumps({"state": "OPEN", "labels": []}), "")
        state = self.dirty_state()
        d.sweep_closed_risk_state(state, [])
        self.assertIn("1509", state["_risk_backend"])
        self.assertEqual(state["_risk_feedback"]["1509"][0]["command"], "secret print")


class MergedReviewCleanup(Base):
    def test_merged_pr_clears_feedback_without_a_verdict(self):
        d = self.make()
        review = {"risk_feedback": RF.accumulate(None, RESULT), "fails": 1}
        d.put_review(1509, review)
        d.gh = lambda args, **kwargs: (0, json.dumps({"state": "MERGED"}), "")
        info = {"status": "failed", "error_class": "risk_blocked"}
        st = {"review_pr": 1509, "review_sha": SHA}
        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {}))
        saved = d.load_reviews()["1509"]
        self.assertNotIn("risk_feedback", saved)
        self.assertEqual(saved["fails"], 1)


if __name__ == "__main__":
    unittest.main()
