"""Review backlog balance (Refs #1701).

The dispatcher used to pick the reviewer with the LRU history that all dispatches
share: a lane that only takes reviews keeps the oldest counter and wins every time,
so one review lane reviewed round after round while the other two stood idle.  The
dispatcher now counts the reviews every lane still owes a verdict for (its review
backlog), gives the next PR to the lane with the smallest backlog, serves lanes with
the same backlog round-robin and skips a lane that already holds `review.lane_max`
reviews.

Pure module: `openswarm/lib/dispatch/review_balance.py` (loaded by file path, the
`lib` packages are namespace packages).
"""
import importlib.util
import json
import os
import pathlib
import unittest

from tests.test_dispatch_engine import Base, SHA, pr

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "dispatch" / "review_balance.py"
_spec = importlib.util.spec_from_file_location("review_balance", MODULE_PATH)
balance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(balance)


class ReviewBalanceModule(unittest.TestCase):
    """The pure backlog arithmetic."""

    def test_backlog_counts_the_claims_of_every_lane(self):
        reviews = {"11": {"pending_lane": "CX2"}, "12": {"pending_lane": "CX2"}, "13": {}}
        self.assertEqual(balance.backlog(reviews), {"CX2": 2})

    def test_backlog_counts_a_running_review_without_a_claim(self):
        state = {"CX4": {"review_pr": 14, "review_sha": "a"}}
        self.assertEqual(balance.backlog({}, state), {"CX4": 1})

    def test_backlog_does_not_count_a_claim_twice(self):
        reviews = {"14": {"pending_lane": "CX4"}}
        state = {"CX4": {"review_pr": 14}}
        self.assertEqual(balance.backlog(reviews, state), {"CX4": 1})

    def test_least_loaded_orders_by_backlog_and_name(self):
        counts = {"CX2": 2, "CX4": 0, "CX5": 0}
        self.assertEqual(balance.least_loaded(["CX2", "CX4", "CX5"], counts), ["CX4", "CX5", "CX2"])

    def test_over_cap_uses_the_lane_count(self):
        counts = {"CX2": 1, "CX4": 0}
        self.assertTrue(balance.over_cap("CX2", counts, 1))
        self.assertFalse(balance.over_cap("CX4", counts, 1))
        self.assertFalse(balance.over_cap("CX2", counts, 0))       # a cap of 0 disables the check
        self.assertFalse(balance.over_cap("CX4", counts, 2))

    def test_releasable_drops_the_claims_of_prs_that_left_the_pipeline(self):
        reviews = {"11": {"pending_lane": "CX2"}, "12": {"pending_lane": "CX4"}, "13": {"pending_lane": "CX5"}}
        self.assertEqual(balance.releasable(reviews, [11], [12]), ["13"])
        self.assertEqual(balance.releasable(reviews, [], []), ["11", "12", "13"])

    def test_render_lists_every_review_lane(self):
        self.assertEqual(balance.render({"CX2": 0, "CX4": 1}, ["CX2", "CX4", "CX5"]), "CX2=0 CX4=1 CX5=0")


class ReviewBalancePick(Base):
    """The dispatcher picks the reviewer by backlog."""

    LANES = {"CX2": {"host": "govpn2", "backend": "codex"},
             "CX4": {"host": "govpn2", "backend": "codex"},
             "CX5": {"host": "govpn", "backend": "codex"}}

    def claims(self, d, mapping):
        os.makedirs(d.cfg["state_dir"], exist_ok=True)
        with open(d.path("reviews.json"), "w") as stream:
            json.dump({str(pr): {"pending_lane": lane} for pr, lane in mapping.items()}, stream)

    def log_text(self, d):
        path = d.path(d.cfg["log_file"])
        if not os.path.exists(path):
            return ""
        with open(path) as stream:
            return stream.read()

    def dispatch_one(self, d, candidate):
        """Dispatch one review and return the lane that took it."""
        d.review_candidates = lambda *args: [candidate]
        d.main_sha = lambda: SHA
        d.review_item = lambda item: {"n": item["number"], "title": "review", "body": "", "review": True}
        d.dispatch = lambda lane, *args: (f"run-{lane}", "verify")
        state = {}
        d.dispatch_reviews(self.LANES, {L: {"lane": "idle"} for L in self.LANES}, state, [])
        return next(iter(state), None)

    def test_the_lane_with_the_smallest_backlog_gets_the_review(self):
        d = self.make()
        self.claims(d, {11: "CX4", 12: "CX4"})
        counts = d.review_backlog(self.LANES, {}, d.load_reviews())
        self.assertEqual(counts, {"CX2": 0, "CX4": 2, "CX5": 0})
        self.assertEqual(d.pick_reviewer(list(self.LANES), self.LANES, None, counts), "CX2")

    def test_equal_backlog_is_served_round_robin(self):
        d = self.make()
        chosen = []
        for number in (11, 12, 13, 14):
            lane = self.dispatch_one(d, pr(number, 100 + number))
            chosen.append(lane)
            d.release_claim(number)          # the review concluded
        self.assertEqual(chosen, ["CX2", "CX4", "CX5", "CX2"])

    def test_a_lane_at_the_cap_is_skipped(self):
        d = self.make()
        self.assertEqual(d.review_lane_cap(), 1)
        self.claims(d, {11: "CX2", 12: "CX2"})
        counts = d.review_backlog(self.LANES, {}, d.load_reviews())
        self.assertEqual(counts["CX2"], 2)
        self.assertNotEqual(d.pick_reviewer(list(self.LANES), self.LANES, None, counts), "CX2")
        self.assertIn("at the in-flight cap", self.log_text(d))

    def test_the_cap_can_be_switched_off(self):
        d = self.make()
        d.R["lane_max"] = 0
        self.assertEqual(d.review_lane_cap(), 0)
        self.claims(d, {11: "CX2", 12: "CX2"})
        counts = d.review_backlog(self.LANES, {}, d.load_reviews())
        self.assertEqual(d.pick_reviewer(list(self.LANES), self.LANES, None, counts), "CX4")
        self.assertNotIn("at the in-flight cap", self.log_text(d))

    def test_a_review_is_still_dispatched_when_every_lane_is_at_the_cap(self):
        d = self.make()
        self.claims(d, {11: "CX2", 12: "CX4", 13: "CX5"})
        counts = d.review_backlog(self.LANES, {}, d.load_reviews())
        self.assertIn(d.pick_reviewer(list(self.LANES), self.LANES, None, counts), self.LANES)

    def test_the_dispatch_log_carries_the_backlog_snapshot(self):
        d = self.make()
        self.claims(d, {12: "CX4"})
        self.dispatch_one(d, pr(11, 111))
        log = self.log_text(d)
        self.assertIn("review dispatched PR #11", log)
        self.assertIn("CX2=", log)
        self.assertIn("CX4=1", log)
        self.assertIn("CX5=0", log)

    def test_the_claim_is_taken_on_dispatch_and_released_with_the_verdict(self):
        d = self.make([("git -C", (0, "VERDICT: APPROVE\nSUMMARY: ok\n", "")),
                       ("pr view 11", (0, json.dumps({"headRefOid": SHA, "state": "OPEN",
                                                      "headRefName": "fleet/M5-issue111-r",
                                                      "body": "Closes #111"}), ""))])
        self.assertEqual(self.dispatch_one(d, pr(11, 111)), "CX2")
        self.assertEqual(d.load_reviews()["11"]["pending_lane"], "CX2")
        st = {"issue": "PR11", "run": "run-CX2", "review_pr": 11, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "codex", "model": "m"}
        self.assertTrue(d.finish_review("CX2", {"host": "govpn2", "backend": "codex"}, st, info, {}))
        self.assertIsNone(d.load_reviews()["11"]["pending_lane"])

    def test_a_failed_review_keeps_the_claim(self):
        d = self.make()
        self.dispatch_one(d, pr(11, 111))
        st = {"issue": "PR11", "run": "run-CX2", "review_pr": 11, "review_sha": SHA}
        self.assertTrue(d.finish_review("CX2", {"host": "govpn2", "backend": "codex"}, st,
                                        {"status": "failed", "error_class": "exec_error"}, {}))
        self.assertEqual(d.load_reviews()["11"]["pending_lane"], "CX2")

    def test_the_claim_of_a_parked_pr_is_released(self):
        d = self.make([("pr list", (0, json.dumps([pr(30, 130, labels=("needs-human",))]), "")),
                       ("issue view", (0, json.dumps({"state": "OPEN", "body": "orig"}), ""))])
        d.put_review(30, {"pending_lane": "CX2"})
        d.review_candidates({}, {}, [])
        self.assertIsNone(d.load_reviews()["30"]["pending_lane"])

    def test_only_the_configured_lanes_take_reviews(self):
        d = self.make()
        d.R["lanes"] = ["CX2", "CX4"]
        lanes = dict(self.LANES)
        lanes["M1"] = {"host": "box", "backend": "pi"}
        state = {}
        d.review_candidates = lambda *args: [pr(11, 111)]
        d.main_sha = lambda: SHA
        d.review_item = lambda item: {"n": item["number"], "title": "review", "body": "", "review": True}
        d.dispatch = lambda lane, *args: (f"run-{lane}", "verify")
        d.dispatch_reviews(lanes, {L: {"lane": "idle"} for L in lanes}, state, [])
        self.assertIn("CX2", state)
        self.assertNotIn("M1", state)


class ReviewNightReplay(Base):
    """Replay of the review sequence of the 2026-11-02 night (Refs #1701).

    That night the fix tasks of the CHANGES rounds kept the shared dispatch history
    of CX4 and CX5 busy, so the legacy tie-break ("the lane with the oldest dispatch
    counter") picked CX2 for every single review: one lane reviewed round after
    round while the other two stood idle.  The backlog balance spreads the reviews
    over the three review lanes.
    """

    LANES = {"CX2": {"host": "govpn2", "backend": "codex"},
             "CX4": {"host": "govpn2", "backend": "codex"},
             "CX5": {"host": "govpn", "backend": "codex"}}
    NIGHT = 6          # six reviews, one per tick

    def legacy_pick(self, d, idle):
        """The tie-break the dispatcher used before the backlog balance."""
        picker = d.lru_picker()
        oldest = picker.pick(idle)
        history = picker.state()["lanes"]
        tied = [lane for lane in idle if history.get(lane) == history.get(oldest)]
        return min(tied, key=lambda lane: (d.R["pref"].get(self.LANES[lane]["backend"], 9), lane))

    def replay(self, d, legacy=False):
        """Replay the night and return the lane of every review."""
        chosen = []
        claimed = []
        for tick in range(self.NIGHT):
            for lane in ("CX4", "CX5"):        # the fix tasks of the day share the dispatch history
                d.lru_picker().touch(lane)
            for pr_number in claimed:          # the review of the previous tick concluded
                d.release_claim(pr_number)
            claimed = []
            idle = list(self.LANES)
            counts = d.review_backlog(self.LANES, {}, d.load_reviews())
            lane = self.legacy_pick(d, idle) if legacy else d.pick_reviewer(idle, self.LANES, None, counts)
            if lane is None:
                continue
            chosen.append(lane)
            d.lru_picker().touch(lane)
            if not legacy:
                d.review_picker().touch(lane)
                d.merge_review(1600 + tick, {"pending_lane": lane})
                claimed = [1600 + tick]
        return chosen

    def test_the_night_is_spread_over_the_review_lanes(self):
        d = self.make()
        chosen = self.replay(d)
        self.assertEqual(len(chosen), self.NIGHT)
        spread = {lane: chosen.count(lane) for lane in self.LANES}
        self.assertLessEqual(max(spread.values()) - min(spread.values()), 1, spread)
        for previous, lane in zip(chosen, chosen[1:]):
            self.assertNotEqual(previous, lane, f"one lane took the reviews one after the other: {chosen}")

    def test_the_legacy_tie_break_piled_the_reviews_on_one_lane(self):
        d = self.make()
        chosen = self.replay(d, legacy=True)
        self.assertEqual(len(set(chosen)), 1, chosen)
        self.assertEqual(chosen[0], "CX2")


if __name__ == "__main__":
    unittest.main()
