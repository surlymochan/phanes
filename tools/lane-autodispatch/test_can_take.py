#!/usr/bin/env python3
"""Fake-queue proof for backend starvation fallback (Refs #232), for the verify-card contract
(Refs #1811) and for the review lane load balancing (Refs #1701)."""
import copy
import json
import math
import os
import re
import tempfile
import unittest
import warnings
import importlib.util
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"  # gitignored scratch root

SOCKET_RE = re.compile(r"unix\s*socket|AF_UNIX|socketpair|needs-socket|Unix 套接字|套接字", re.I)

def can_take(backend, item):
    if item["backend"] in (None, backend):
        return True
    if item.get("strict"):
        return False
    if backend == "codex" and SOCKET_RE.search(item["title"] + "\n" + item.get("body", "")):
        return False
    return True

def rank(backend, item):
    return (0 if item["backend"] == backend else 1 if item["backend"] is None else 2, item["prio"], item["n"])

def pick(backend, queue):
    return sorted((i for i in queue if can_take(backend, i)), key=lambda i: rank(backend, i))

class BackendFallbackTest(unittest.TestCase):
    def test_prefer_own_pin_then_unpinned_then_cross(self):
        q = [
            {"n": 3, "title": "c", "body": "", "prio": 2, "backend": "codex", "strict": False},
            {"n": 1, "title": "u", "body": "", "prio": 2, "backend": None, "strict": False},
            {"n": 2, "title": "p", "body": "", "prio": 2, "backend": "pi", "strict": False},
            {"n": 4, "title": "own", "body": "", "prio": 2, "backend": "agy", "strict": False},
        ]
        got = [i["n"] for i in pick("agy", q)]
        self.assertEqual(got, [4, 1, 2, 3])  # own, unpinned, then cross by number

    def test_unpinned_feeds_starved_codex_when_only_pi_pinned_otherwise(self):
        q = [
            {"n": 10, "title": "pi work", "body": "", "prio": 1, "backend": "pi", "strict": False},
            {"n": 11, "title": "docs", "body": "", "prio": 2, "backend": None, "strict": False},
        ]
        # codex can take unpinned first; cross-pin pi is allowed but ranks after unpinned
        got = [i["n"] for i in pick("codex", q)]
        self.assertEqual(got[0], 11)
        self.assertIn(10, got)

    def test_strict_backend_blocks_cross_pin_only(self):
        q = [
            {"n": 5, "title": "strict pi", "body": "", "prio": 0, "backend": "pi", "strict": True},
            {"n": 6, "title": "free", "body": "", "prio": 2, "backend": None, "strict": False},
        ]
        self.assertFalse(can_take("codex", q[0]))
        self.assertTrue(can_take("pi", q[0]))
        self.assertEqual([i["n"] for i in pick("codex", q)], [6])

    def test_codex_skips_socket_cross_pin(self):
        q = [
            {"n": 7, "title": "needs AF_UNIX broker", "body": "Unix 套接字", "prio": 1, "backend": "pi", "strict": False},
            {"n": 8, "title": "plain", "body": "", "prio": 2, "backend": "pi", "strict": False},
        ]
        got = [i["n"] for i in pick("codex", q)]
        self.assertEqual(got, [8])
        self.assertEqual([i["n"] for i in pick("agy", q)], [7, 8])

    def test_exact_pin_always(self):
        item = {"n": 9, "title": "x", "body": "socketpair", "prio": 1, "backend": "codex", "strict": True}
        self.assertTrue(can_take("codex", item))

    def test_harvest_fetch_retry_is_bounded_and_matches_ssh_disconnects(self):
        self.assertTrue(AUTODISPATCH.should_retry_harvest_fetch("ssh: Connection closed", 1))
        self.assertTrue(AUTODISPATCH.should_retry_harvest_fetch("write: broken pipe", 2))
        self.assertFalse(AUTODISPATCH.should_retry_harvest_fetch("ssh: Connection closed", 3))
        self.assertFalse(AUTODISPATCH.should_retry_harvest_fetch("fatal: repository not found", 1))
        self.assertEqual(AUTODISPATCH.HARVEST_FETCH_MAX_ATTEMPTS, 3)

    def test_base_missing_is_only_the_missing_base_commit(self):
        # Refs #1810: only the missing base commit triggers the fetch-on-miss retry.
        self.assertTrue(AUTODISPATCH.base_missing("fatal: Not a valid object name abc123^{commit}"))
        self.assertFalse(AUTODISPATCH.base_missing("fatal: could not read Username for 'https://github.com'"))
        self.assertFalse(AUTODISPATCH.base_missing(""))
        self.assertEqual(AUTODISPATCH.DISPATCH_BASE_FETCH_ATTEMPTS, 3)


VERIFY_BODY = "## Verify Intent\n目标 PR: #1785\n证据要求:\n- [ ] 测试结果\n- [ ] 复现步骤"

def issue(number, body, labels=("lane-ready",), title="card"):
    """One `gh issue list` row as the queue sees it."""
    return {"number": number, "title": title, "body": body, "labels": list(labels)}


class VerifyCardTest(unittest.TestCase):
    """The verify-card contract: both markers, the required fields, and the review lanes only."""

    def test_verify_requirements_reject_a_missing_target_pr(self):
        valid, missing = AUTODISPATCH.verify_requirements(VERIFY_BODY)
        self.assertTrue(valid)
        self.assertEqual(missing, [])
        # a missing 目标 PR must not borrow the value of the next line
        valid, missing = AUTODISPATCH.verify_requirements("## Verify Intent\n目标 PR:\n证据要求:\n- [ ] run checks")
        self.assertFalse(valid)
        self.assertEqual(missing, ["目标 PR"])
        # an empty value is a missing value
        valid, missing = AUTODISPATCH.verify_requirements("## Verify Intent\n目标 PR:  \n证据要求:\n- [ ] run checks")
        self.assertFalse(valid)
        self.assertEqual(missing, ["目标 PR"])
        # the evidence checklist is required as well
        valid, missing = AUTODISPATCH.verify_requirements("## Verify Intent\n目标 PR: #1785\n证据要求:\n- 普通条目")
        self.assertFalse(valid)
        self.assertEqual(missing, ["证据要求清单"])

    def test_both_markers_are_required(self):
        self.assertTrue(AUTODISPATCH.is_verify_issue({"verify-intent"}, VERIFY_BODY))
        self.assertFalse(AUTODISPATCH.is_verify_issue(set(), VERIFY_BODY))
        self.assertFalse(AUTODISPATCH.is_verify_issue({"verify-intent"}, "目标 PR: #1785"))

    def test_verify_cards_only_fit_review_lanes(self):
        card = {"backend": None, "prio": 1, "n": 10, "verify": True, "title": "v", "body": ""}
        for lane in ("CX2", "CX4", "CX5", "cx2"):
            self.assertTrue(AUTODISPATCH.can_take("codex", card, lane))
        for lane in ("CX1", "CX3", "CX6", "CX10", "PI1", None):
            self.assertFalse(AUTODISPATCH.can_take("codex", card, lane))

    def test_rank_keeps_the_pin_first_contract_and_orders_work_before_verify(self):
        own_verify = {"backend": "codex", "prio": 1, "n": 1, "verify": True}
        own_work = {"backend": "codex", "prio": 1, "n": 2}
        unpinned_work = {"backend": None, "prio": 0, "n": 3}
        # rule 1 of tools/autodispatch-backend-fallback.md: the own pin comes first
        self.assertLess(AUTODISPATCH.rank("codex", own_verify), AUTODISPATCH.rank("codex", unpinned_work))
        # inside one pin class and one priority a work card precedes a verify card
        self.assertLess(AUTODISPATCH.rank("codex", own_work), AUTODISPATCH.rank("codex", own_verify))

    def test_ordinary_cards_keep_the_documented_order(self):
        q = [
            {"n": 3, "backend": "codex", "prio": 2},
            {"n": 1, "backend": None, "prio": 2},
            {"n": 2, "backend": "pi", "prio": 2},
            {"n": 4, "backend": "agy", "prio": 2},
        ]
        got = [i["n"] for i in sorted(q, key=lambda i: AUTODISPATCH.rank("agy", i))]
        self.assertEqual(got, [4, 1, 2, 3])  # own, unpinned, then cross by number

    def test_queue_keeps_valid_verify_cards_and_drops_invalid_ones(self):
        valid = issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))
        invalid = issue(2, "## Verify Intent\n目标 PR:\n证据要求:\n- [ ] run checks", ("lane-ready", "verify-intent"))
        heading_only = issue(3, VERIFY_BODY, ("lane-ready",))
        logs = []
        old_gh, old_log = AUTODISPATCH.gh, AUTODISPATCH.log
        try:
            AUTODISPATCH.VERIFY_REJECT_LOGGED.clear()
            AUTODISPATCH.gh = lambda *a, **k: (0, json.dumps([valid, invalid, heading_only]), "")
            AUTODISPATCH.log = lambda msg, brain=True: logs.append(msg)
            q = AUTODISPATCH.queue()
        finally:
            AUTODISPATCH.gh, AUTODISPATCH.log = old_gh, old_log
        self.assertEqual([i["n"] for i in q], [1, 3])
        self.assertTrue(q[0]["verify"])
        self.assertFalse(q[1]["verify"])  # the heading alone keeps it an ordinary work card
        self.assertTrue(any("verify #2 rejected" in m and "目标 PR" in m for m in logs))


class TickHarness:
    """The faked side effects of one dispatch tick (GitHub, ssh, dispatch)."""

    LANES = {"CX2": {"host": "h2", "backend": "codex"}, "CX10": {"host": "h10", "backend": "codex"}}

    def run_tick(self, issues, lanes=None, state=None):
        """One tick with the GitHub/ssh/dispatch side effects faked; returns (dispatched, logs).

        The state the tick worked on is kept in `self.last_state` (the tick updates it in
        place, e.g. the review queue of the review lanes)."""
        dispatched, logs, edits = [], [], []
        state = dict(state or {})

        def fake_gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(issues), ""
            if args[:2] == ["issue", "view"]:
                n = int(args[2])
                for row in issues:
                    if row["number"] == n:
                        return 0, json.dumps({"number": n, "state": "OPEN", "labels": row["labels"]}), ""
                return 0, json.dumps({"number": n, "state": "CLOSED", "labels": []}), ""
            if args[:2] == ["issue", "edit"]:
                edits.append(args)
                return 0, "", ""
            return 0, "", ""

        def fake_dispatch(lane, cfg, item, base):
            dispatched.append((lane, item["n"]))
            return "run-1", None

        patched = ("D", "gh", "sh", "log", "host_ok", "host_fail", "host_good", "remote", "deps_met",
                   "dispatch", "replenish", "save_state", "status_snapshot")
        saved = {name: getattr(AUTODISPATCH, name) for name in patched}
        try:
            SCRATCH.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=SCRATCH) as scratch:
                AUTODISPATCH.D = scratch
                AUTODISPATCH.gh = fake_gh
                AUTODISPATCH.sh = lambda *a, **k: (0, "0" * 40 + "\n", "")
                AUTODISPATCH.log = lambda msg, brain=True: logs.append(msg)
                AUTODISPATCH.host_ok = lambda h: True
                AUTODISPATCH.host_fail = lambda h, why: None
                AUTODISPATCH.host_good = lambda h: None
                AUTODISPATCH.remote = lambda lane, cfg, run="": {"lane": "idle"}
                AUTODISPATCH.deps_met = lambda item: True
                AUTODISPATCH.dispatch = fake_dispatch
                AUTODISPATCH.replenish = lambda k, state: []
                AUTODISPATCH.save_state = lambda state: None
                AUTODISPATCH.status_snapshot = lambda *a, **k: {}
                AUTODISPATCH.VERIFY_REJECT_LOGGED.clear()
                # tick() writes lanes-status.json without closing it: the warning is pre-existing
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    AUTODISPATCH.tick(dict(lanes or self.LANES), state, 0)
        finally:
            for name, value in saved.items():
                setattr(AUTODISPATCH, name, value)
        self.last_state = state
        return dispatched, logs


class TickVerifyDispatchTest(TickHarness, unittest.TestCase):
    """tick() level proof: a valid verify card reaches a review lane only, an invalid card reaches
    no lane at all, and a work card of the same pin class is dispatched first."""

    def test_share_keeps_both_classes_moving_during_floods(self):
        old = AUTODISPATCH.AD_VERIFY_SHARE_ENABLED
        AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = True
        try:
            verify = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 6)]
            work = [issue(n, "普通工作卡", ("lane-ready",)) for n in range(10, 15)]
            for rows in (work + verify[:1], verify + work[:1]):
                dispatched, logs = self.run_tick(rows)
                self.assertEqual(dispatched, [("CX2", 1), ("CX10", 10)])
                self.assertTrue(any("other class yields" in line for line in logs))
        finally:
            AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = old

    def test_share_keeps_the_work_floor_during_a_verify_flood(self):
        # Three lanes: two review lanes (CX2/CX4) and one work lane (CX10). The verify reservation
        # is one lane, so a verify flood may not swallow every review lane: the work class keeps
        # its floor (total - reserve = 2 lanes) and only the reserved lane takes a verify card.
        lanes = {"CX2": {"host": "h2", "backend": "codex"}, "CX4": {"host": "h4", "backend": "codex"},
                 "CX10": {"host": "h10", "backend": "codex"}}
        verify = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 6)]
        work = [issue(n, "普通工作卡", ("lane-ready",)) for n in range(10, 13)]
        old = AUTODISPATCH.AD_VERIFY_SHARE_ENABLED
        AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = True
        try:
            dispatched, _ = self.run_tick(verify + work, lanes)
        finally:
            AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = old
        self.assertEqual([lane for lane, _ in dispatched], ["CX2", "CX4", "CX10"])
        self.assertEqual([n for lane, n in dispatched if lane == "CX2"], [1])  # the reserved lane
        self.assertEqual(sorted(n for lane, n in dispatched if lane != "CX2"), [10, 11])  # work floor

    def test_share_configuration_falls_back_to_the_default_on_a_bad_value(self):
        # A typo in the configuration must not kill the AD loop: the value falls back to the default.
        self.assertEqual(AUTODISPATCH.env_number("AD_VERIFY_SHARE", 0.2), 0.2)
        os.environ["AD_TEST_BAD_NUMBER"] = "abc"
        try:
            self.assertEqual(AUTODISPATCH.env_number("AD_TEST_BAD_NUMBER", 0.2), 0.2)
            self.assertEqual(AUTODISPATCH.env_number("AD_TEST_BAD_NUMBER", 1, int), 1)
        finally:
            del os.environ["AD_TEST_BAD_NUMBER"]

    def test_share_uses_available_capacity_and_allows_single_lane(self):
        lanes = {"CX2": {}, "CX4": {}, "CX10": {}}
        self.assertEqual(AUTODISPATCH.queue_class_limits(lanes, {}, ["CX2"]), (0, 1))
        self.assertEqual(AUTODISPATCH.queue_class_limits(lanes, {"CX10": {"class": "work"}}, ["CX2"]), (1, 0))

    def test_verify_card_is_dispatched_to_a_review_lane_only(self):
        dispatched, _ = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))])
        self.assertEqual(dispatched, [("CX2", 1)])  # CX10 is not a review lane

    def test_invalid_verify_card_is_not_dispatched(self):
        dispatched, logs = self.run_tick([issue(2, "## Verify Intent\n目标 PR:\n证据要求:\n- [ ] run checks",
                                           ("lane-ready", "verify-intent"))])
        self.assertEqual(dispatched, [])
        self.assertTrue(any("verify #2 rejected" in m for m in logs))

    def test_work_card_is_dispatched_before_the_verify_card(self):
        dispatched, _ = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent")),
                                       issue(2, "普通工作卡", ("lane-ready",))])
        self.assertEqual(dispatched, [("CX2", 2)])

    def test_own_pin_verify_card_wins_over_a_cross_pin_work_card(self):
        dispatched, _ = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent", "backend:codex")),
                                       issue(2, "普通工作卡", ("lane-ready", "p0", "backend:pi"))])
        # the pin class still comes first (autodispatch-backend-fallback.md rule 1), the verify card
        # goes to the review lane and the cross-pin p0 work card to the remaining codex lane
        self.assertEqual(dispatched, [("CX2", 1), ("CX10", 2)])


class TickWorkClassCapTest(TickHarness, unittest.TestCase):
    """Refs #1864: the work class cap is a limit, not a preference.

    The work class may hold at most `total - reserve` lanes. With `work_slots == 0` the work
    cards wait: they are not dispatched on any lane, while the verify class still takes the
    reserved lane."""

    def busy_work_state(self):
        """CX10 holds a work run: the work class is at its cap (2 lanes - 1 reserved = 1 lane)."""
        return {"CX10": {"issue": 9, "run": "run-9", "host": "h10", "base": "b" * 40,
                         "at": "2026-01-01 00:00:00", "verify": None, "class": "work"}}

    def with_share(self, fn):
        """Run one tick with the queue share policy enabled (the switch is off by default)."""
        old = AUTODISPATCH.AD_VERIFY_SHARE_ENABLED
        AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = True
        try:
            return fn()
        finally:
            AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = old

    def test_work_card_is_not_dispatched_when_the_work_class_is_at_its_cap(self):
        dispatched, logs = self.with_share(
            lambda: self.run_tick([issue(10, "普通工作卡", ("lane-ready",))], self.LANES, self.busy_work_state()))
        self.assertEqual(dispatched, [])  # the work class holds its cap: the work card waits
        self.assertTrue(any("work class at its cap" in line for line in logs))

    def test_work_card_is_dispatched_while_the_work_class_has_a_slot(self):
        dispatched, _ = self.with_share(
            lambda: self.run_tick([issue(10, "普通工作卡", ("lane-ready",))], self.LANES, {}))
        self.assertEqual(dispatched, [("CX2", 10)])  # work_slots > 0: the dispatch is unchanged

    def test_the_cap_stops_the_second_work_card_of_the_same_tick(self):
        # two work cards, two lanes: the work class may hold one lane (total - reserve), the
        # second card waits for a free lane
        dispatched, _ = self.with_share(
            lambda: self.run_tick([issue(10, "普通工作卡", ("lane-ready",)),
                                   issue(11, "普通工作卡", ("lane-ready",))], self.LANES, {}))
        self.assertEqual(dispatched, [("CX2", 10)])

    def test_the_verify_card_still_takes_the_reserved_lane_under_the_cap(self):
        dispatched, _ = self.with_share(
            lambda: self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))],
                                   self.LANES, self.busy_work_state()))
        self.assertEqual(dispatched, [("CX2", 1)])  # the cap limits the work class only


class ReviewBacklogAssignmentTest(unittest.TestCase):
    """Review lanes must be assigned by in-flight review load (Refs #1701)."""

    LANES = ("CX2", "CX4", "CX5")

    def setUp(self):
        # the rotation cursor is global: reset it so the rotation test does not depend
        # on the order the test classes run in
        self.rotation = copy.deepcopy(AUTODISPATCH.REVIEW_ROTATION)
        AUTODISPATCH.REVIEW_ROTATION.clear()

    def tearDown(self):
        AUTODISPATCH.REVIEW_ROTATION.clear()
        AUTODISPATCH.REVIEW_ROTATION.update(self.rotation)

    def test_selects_the_lane_with_the_smallest_queue_plus_active_reviews(self):
        lane, loads = AUTODISPATCH.pick_reviewer(
            self.LANES,
            {"CX2": 2, "CX4": 0, "CX5": 1},
        )
        self.assertEqual(lane, "CX4")
        self.assertEqual(loads, {"CX2": 2, "CX4": 0, "CX5": 1})

    def test_equal_load_rotates_across_review_lanes(self):
        first, _ = AUTODISPATCH.pick_reviewer(self.LANES, {lane: 1 for lane in self.LANES})
        second, _ = AUTODISPATCH.pick_reviewer(self.LANES, {lane: 1 for lane in self.LANES})
        third, _ = AUTODISPATCH.pick_reviewer(self.LANES, {lane: 1 for lane in self.LANES})
        self.assertEqual((first, second, third), self.LANES)

    def test_at_capacity_lane_is_skipped_and_decision_logs_snapshot(self):
        lines = []
        lane, loads = AUTODISPATCH.pick_reviewer(
            self.LANES,
            {"CX2": 3, "CX4": 1, "CX5": 2},
            max_in_flight=2,
            log=lines.append,
            pr=1701,
        )
        self.assertEqual(lane, "CX4")
        self.assertIn("PR #1701", lines[0])
        for lane_name, count in loads.items():
            self.assertIn(f"{lane_name}={count}", lines[0])

    def test_the_rotation_goes_on_with_the_next_lane_when_one_is_busy(self):
        # CX2 has its turn, then it is busy for a moment: the rotation goes on with the
        # next review lane, the turn of CX2 is not given to it a second time
        first, _ = AUTODISPATCH.pick_reviewer(self.LANES, {lane: 0 for lane in self.LANES})
        second, _ = AUTODISPATCH.pick_reviewer(self.LANES, {lane: 0 for lane in self.LANES},
                                               eligible=("CX4", "CX5"))
        third, _ = AUTODISPATCH.pick_reviewer(self.LANES, {lane: 0 for lane in self.LANES})
        self.assertEqual((first, second, third), ("CX2", "CX4", "CX5"))

    def test_the_plan_spreads_the_review_cards_over_the_review_lanes(self):
        # three review cards, three free review lanes: every lane takes one card, the
        # load of a lane is the load it already carries plus the cards the plan gave it
        plan, snapshots = AUTODISPATCH.plan_reviews(
            self.LANES, [{"n": 1}, {"n": 2}, {"n": 3}],
            {lane: 0 for lane in self.LANES}, max_in_flight=1,
        )
        self.assertEqual([plan[lane][0]["n"] for lane in self.LANES], [1, 2, 3])
        self.assertEqual(sorted(snapshots), [1, 2, 3])

    def test_target_pr_accepts_a_url_and_never_falls_back_to_issue_number(self):
        self.assertEqual(AUTODISPATCH.review_target_pr({"n": 1, "body": VERIFY_BODY}), "1785")
        self.assertEqual(AUTODISPATCH.review_target_pr(
            {"n": 1, "body": "目标 PR: https://github.com/org/repo/pull/1785"}), "1785")
        self.assertEqual(AUTODISPATCH.review_target_pr({"n": 1}), "unknown")

    def test_waiting_counts_do_not_drop_overflow_at_the_cap_or_advance_rotation(self):
        rotation = copy.deepcopy(AUTODISPATCH.REVIEW_ROTATION)
        counts = AUTODISPATCH.review_waiting_counts(
            self.LANES, [{"n": n} for n in range(5)], dict.fromkeys(self.LANES, 1), 1)
        self.assertEqual(counts, {"CX2": 2, "CX4": 2, "CX5": 1})
        self.assertEqual(AUTODISPATCH.REVIEW_ROTATION, rotation)

    def test_waiting_forecast_reserves_the_next_turn_without_consuming_it(self):
        AUTODISPATCH.pick_reviewer(self.LANES, dict.fromkeys(self.LANES, 0))
        rotation = copy.deepcopy(AUTODISPATCH.REVIEW_ROTATION)
        counts = AUTODISPATCH.review_waiting_counts(
            self.LANES, [{"n": 2}, {"n": 3}], dict.fromkeys(self.LANES, 0),
            head_eligible=self.LANES)
        self.assertEqual(counts, {"CX5": 1, "CX2": 1})
        self.assertEqual(AUTODISPATCH.REVIEW_ROTATION, rotation)

    def test_the_plan_skips_the_lane_that_is_at_its_cap(self):
        # CX2 already runs a review (its load is the cap): the card goes to another lane
        plan, _ = AUTODISPATCH.plan_reviews(
            self.LANES, [{"n": 1}], {"CX2": 1, "CX4": 0, "CX5": 0}, max_in_flight=1,
        )
        self.assertEqual(list(plan), ["CX4"])


class ReviewBacklogTickTest(TickHarness, unittest.TestCase):
    """tick() level proof of the review lane balancing (Refs #1701).

    The review cards go to the review lane with the smallest review load (the reviews it
    runs plus the cards the plan gave it), a lane that already carries
    `AD_REVIEW_MAX_IN_FLIGHT` reviews is skipped (the review waits for a free lane), and
    the decision is logged with the backlog snapshot."""

    LANES = {"CX2": {"host": "h2", "backend": "codex"}, "CX4": {"host": "h4", "backend": "codex"},
             "CX5": {"host": "h5", "backend": "codex"}}

    def setUp(self):
        self.rotation = copy.deepcopy(AUTODISPATCH.REVIEW_ROTATION)
        AUTODISPATCH.REVIEW_ROTATION.clear()

    def tearDown(self):
        AUTODISPATCH.REVIEW_ROTATION.clear()
        AUTODISPATCH.REVIEW_ROTATION.update(self.rotation)

    def busy(self, lane, cls="work"):
        """The state of a lane that runs a card."""
        return {lane: {"issue": 9, "run": "run-9", "host": "h2", "base": "b" * 40,
                       "at": "2026-01-01 00:00:00", "verify": None, "class": cls}}

    def test_the_review_goes_to_the_lane_with_the_smallest_load(self):
        # CX2 runs a review (load 1) and CX4 runs a work card: only CX5 is free, so the
        # review card goes there - never to the lane that already reviews
        state = self.busy("CX2", "verify")
        state.update(self.busy("CX4", "work"))
        dispatched, logs = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))],
                                         self.LANES, state)
        self.assertEqual(dispatched, [("CX5", 1)])
        line = [row for row in logs if "review assignment" in row][0]
        self.assertIn("PR #1785 →", line)
        self.assertIn("lane CX5", line)
        self.assertIn("CX2=1", line)   # the load of every review lane is in the log
        self.assertIn("CX4=0", line)
        self.assertIn("CX5=0", line)
        # the decision itself is logged with the same backlog snapshot
        plan = [row for row in logs if "review plan" in row][0]
        self.assertIn("PR #1785 →", plan)
        self.assertIn("lane CX5", plan)
        self.assertIn("CX2=1", plan)

    def test_the_review_waits_when_the_free_review_lanes_are_at_their_cap(self):
        # CX5 is free and takes the first review card; the second card waits for a free
        # lane instead of piling up on CX5
        state = self.busy("CX2", "verify")
        state.update(self.busy("CX4", "verify"))
        dispatched, logs = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent")),
                                          issue(2, VERIFY_BODY, ("lane-ready", "verify-intent"))],
                                         self.LANES, state)
        self.assertEqual(dispatched, [("CX5", 1)])
        self.assertTrue(any("review" in row and "wait" in row for row in logs))

    def test_a_free_lane_is_not_blocked_by_the_queue_of_the_last_tick(self):
        # the queue of the last tick named CX4, but CX4 is free now: it takes the review
        # (a lane is never blocked by its own queue entry)
        state = self.busy("CX2", "verify")
        state.update(self.busy("CX5", "verify"))
        state["_review_waiting"] = {"CX4": 2}
        dispatched, _ = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))],
                                      self.LANES, state)
        self.assertEqual(dispatched, [("CX4", 1)])

    def test_work_dispatch_does_not_strand_a_review_on_its_old_plan(self):
        rows = [issue(1, VERIFY_BODY, ("lane-ready", "verify-intent")),
                issue(10, "work", ("lane-ready", "backend:codex"))]
        dispatched, logs = self.run_tick(rows, self.LANES)
        self.assertEqual(dispatched, [("CX2", 10), ("CX4", 1)])
        assignment = next(row for row in logs if "review assignment" in row)
        self.assertIn("PR #1785 → lane CX4", assignment)
        self.assertIn("CX2=0", assignment)  # CX2 runs work, not a planned review

    def test_two_reviews_are_reassigned_after_priority_work(self):
        rows = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in (1, 2)]
        rows.append(issue(10, "work", ("lane-ready", "backend:codex")))
        dispatched, logs = self.run_tick(rows, self.LANES)
        self.assertEqual(dispatched, [("CX2", 10), ("CX4", 1), ("CX5", 2)])
        assignments = [row for row in logs if "review assignment" in row]
        # At the first decision one review really waits; at the second none do.
        self.assertIn("CX2=1, CX4=0, CX5=0", assignments[0])
        self.assertIn("CX2=0, CX4=1, CX5=0", assignments[1])

    def test_waiting_overflow_is_counted_even_when_all_lanes_are_at_capacity(self):
        state = {}
        for lane in self.LANES:
            state.update(self.busy(lane, "verify"))
        rows = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 6)]
        dispatched, logs = self.run_tick(rows, self.LANES, state)
        self.assertEqual(dispatched, [])
        self.assertEqual(self.last_state["_review_waiting"], {"CX2": 2, "CX4": 2, "CX5": 1})
        self.assertTrue(any("CX2=3, CX4=3, CX5=2" in row for row in logs))

    def test_a_queue_below_the_configured_backlog_cap_can_be_serviced(self):
        rows = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 9)]
        with patch.object(AUTODISPATCH, "REVIEW_MAX_IN_FLIGHT", 4):
            dispatched, logs = self.run_tick(rows, self.LANES)
        self.assertEqual(len(dispatched), 3)
        self.assertEqual(sum(self.last_state["_review_waiting"].values()), 5)
        assignment = next(row for row in logs if "review assignment" in row)
        self.assertIn("CX2=2, CX4=3, CX5=2", assignment)
        self.assertIn("lane CX2", assignment)

    def test_waiting_queue_larger_than_capacity_drains_at_the_default_cap(self):
        pending = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 9)]
        state, assignments = {}, []
        with patch.object(AUTODISPATCH, "REVIEW_MAX_IN_FLIGHT", 1):
            for expected in (3, 3, 2):
                dispatched, _ = self.run_tick(pending, self.LANES, state)
                self.assertEqual(len(dispatched), expected)
                self.assertEqual(len({lane for lane, _ in dispatched}), expected)
                assignments.extend(lane for lane, _ in dispatched)
                completed = {n for _, n in dispatched}
                pending = [row for row in pending if row["number"] not in completed]
                self.assertEqual(sum(self.last_state["_review_waiting"].values()), len(pending))
                # Finish this round, retaining the previous tick's queue forecast.
                state = {key: value for key, value in self.last_state.items() if key not in self.LANES}
        self.assertEqual(pending, [])
        self.assertEqual(assignments, ["CX2", "CX4", "CX5", "CX2", "CX4", "CX5", "CX2", "CX4"])

    def test_four_waiting_reviews_fill_idle_lanes_at_the_default_cap(self):
        rows = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 5)]
        with patch.object(AUTODISPATCH, "REVIEW_MAX_IN_FLIGHT", 1):
            dispatched, logs = self.run_tick(rows, self.LANES)
            self.assertEqual(dispatched, [("CX2", 1), ("CX4", 2), ("CX5", 3)])
            self.assertEqual(sum(self.last_state["_review_waiting"].values()), 1)
            plan = next(row for row in logs if "review plan" in row)
            self.assertIn("lane CX2 (backlog: CX2=1, CX4=1, CX5=1)", plan)
            assignment = next(row for row in logs if "review assignment" in row)
            self.assertIn("PR #1785 → lane CX2 (backlog: CX2=1, CX4=1, CX5=1)", assignment)
            # No lane can take another review while its admitted review is running.
            blocked, _ = self.run_tick(rows[3:], self.LANES, self.last_state)
            self.assertEqual(blocked, [])
            self.last_state.pop("CX2")
            resumed, _ = self.run_tick(rows[3:], self.LANES, self.last_state)
            self.assertEqual(resumed, [("CX2", 4)])
            self.assertEqual(sum(self.last_state["_review_waiting"].values()), 0)

    def test_tick_rotation_records_the_unique_minimum_before_the_next_round(self):
        rows = [issue(n, VERIFY_BODY, ("lane-ready", "verify-intent")) for n in range(1, 5)]
        with patch.object(AUTODISPATCH, "REVIEW_MAX_IN_FLIGHT", 1):
            first, _ = self.run_tick(rows, self.LANES, {})
            second, _ = self.run_tick(rows, self.LANES, {})
        self.assertEqual([lane for lane, _ in first], list(self.LANES))
        self.assertEqual([lane for lane, _ in second], list(self.LANES))

    def test_the_reviews_rotate_over_the_review_lanes(self):
        # the reviews rotate over the review lanes: no lane reviews a second round while
        # another review lane is free
        card = [issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))]
        first, _ = self.run_tick(card, self.LANES, {})
        self.assertEqual(first, [("CX2", 1)])
        # CX2 reviews now: the next review goes to the lane that waits for its turn
        second, _ = self.run_tick(card, self.LANES, self.busy("CX2", "verify"))
        self.assertEqual(second, [("CX4", 1)])
        # every lane is free again: the lane that had its turn the longest ago takes it
        third, _ = self.run_tick(card, self.LANES, {})
        self.assertEqual(third, [("CX5", 1)])

    def test_the_review_goes_out_when_the_reserved_verify_lane_is_busy(self):
        # the verify share reserves a lane for the verify class, it does not limit the
        # verify class to that lane: a free review lane takes the review even when the
        # reserved lane already runs a review
        old = AUTODISPATCH.AD_VERIFY_SHARE_ENABLED
        AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = True
        try:
            state = self.busy("CX2", "verify")
            dispatched, _ = self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))],
                                          self.LANES, state)
        finally:
            AUTODISPATCH.AD_VERIFY_SHARE_ENABLED = old
        self.assertEqual(dispatched, [("CX4", 1)])

    def test_the_waiting_reviews_are_the_review_queue_of_their_lane(self):
        # the review card that could not be dispatched is counted as the review queue of
        # the lane it waits for, so the backlog report shows where the queue stands
        state = self.busy("CX2", "work")
        state.update(self.busy("CX4", "verify"))
        state.update(self.busy("CX5", "verify"))
        self.run_tick([issue(1, VERIFY_BODY, ("lane-ready", "verify-intent"))], self.LANES, state)
        waiting = {lane: count for lane, count in self.last_state.get("_review_waiting", {}).items() if count}
        self.assertEqual(sum(waiting.values()), 1)


class ReviewSequenceReplayTest(TickHarness, unittest.TestCase):
    """Replay of the review sequence of the night of 2026-11-02 (Refs #1701).

    The sequence is the review rounds the three review lanes published that night: one
    round per `Lane review` comment of the capture in
    `docs/evidence/architecture-review/snapshot.json.gz` (the duplicate comments of one
    run are counted once). The replay feeds the same demand to the two assignment rules:
    the old one (the lane order decides: the first free lane) and the new one (the lane
    with the smallest backlog, the ties rotate)."""

    LANES = ReviewBacklogTickTest.LANES
    SERVICE = 12  # minutes: the median interval between two review rounds of that night
    # "<minutes since the first round>:<PR number>", the recorded sequence of that night.
    # It is the `Lane review` comments of `docs/evidence/architecture-review/snapshot.json.gz`
    # between 2026-11-02T10:00Z and 2026-11-02T19:06Z (the night of the issue), one round
    # per run (the duplicate comments of one run are counted once).
    SEQUENCE = ("0:1656 0:1657 21:1653 21:1655 32:1631 41:1489 42:1659 52:1583 56:1489 62:1665 65:1664 69:1657 "
                "73:1489 73:1631 81:1676 90:1676 93:1677 95:1665 97:1655 99:1659 109:1678 118:1679 121:1680 "
                "121:1665 125:1682 127:1681 128:1683 133:1682 139:1683 143:1677 143:1684 177:1657 177:1676 "
                "177:1653 190:1665 190:1655 190:1678 193:1681 193:1680 193:1682 195:1691 195:1692 197:1683 "
                "209:1683 209:1680 218:1678 223:1657 229:1699 237:1706 237:1707 239:1708 243:1676 249:1691 "
                "252:1654 254:1706 262:1715 301:1724 301:1707 301:1725 303:1708 312:1654 332:1735 335:1715 "
                "341:1522 341:1620 345:1699 357:1682 357:1653 360:1744 360:1743 370:1746 376:1749 376:1657 "
                "380:1748 382:1655 384:1715 386:1751 394:1755 395:1758 398:1756 404:1743 406:1759 409:1654 "
                "411:1760 413:1764 415:1749 417:1759 421:1766 421:1744 430:1744 431:1759 434:1767 439:1751 "
                "442:1768 448:1770 452:1755 455:1774 457:1758 461:1755 468:1723 468:1743 469:1751 473:1778").split()

    def setUp(self):
        self.rotation = copy.deepcopy(AUTODISPATCH.REVIEW_ROTATION)
        # the replay starts with a fresh rotation: the result does not depend on the
        # state the other tests left behind
        AUTODISPATCH.REVIEW_ROTATION.clear()

    def tearDown(self):
        AUTODISPATCH.REVIEW_ROTATION.clear()
        AUTODISPATCH.REVIEW_ROTATION.update(self.rotation)

    def replay(self, policy):
        """Assign the recorded rounds to the three review lanes.

        A review occupies its lane for `SERVICE` minutes; the rounds that arrive while
        every lane is busy wait in the review queue. Returns (counts, assignments,
        the largest backlog difference seen)."""
        free = {lane: 0 for lane in self.LANES}
        queue, assignments = [], []
        index, minute, spread = 0, 0, 0
        while (index < len(self.SEQUENCE) or queue) and minute < 4000:
            while index < len(self.SEQUENCE) and int(self.SEQUENCE[index].split(":")[0]) <= minute:
                queue.append(self.SEQUENCE[index]); index += 1
            loads = {lane: (1 if free[lane] > minute else 0) for lane in self.LANES}
            spread = max(spread, max(loads.values()) - min(loads.values()))
            if queue:
                lane = policy(loads)
                if lane is not None:
                    queue.pop(0)
                    free[lane] = minute + self.SERVICE
                    assignments.append(lane)
            minute += 1
        counts = {lane: assignments.count(lane) for lane in self.LANES}
        return counts, assignments, spread

    def old_policy(self, loads):
        """The rule before #1701: the lane order decides, the first free lane takes it."""
        return next((lane for lane in self.LANES if not loads[lane]), None)

    def longest_run(self, assignments):
        """The longest run of the same lane in the assignment sequence."""
        run = best = 0
        previous = None
        for lane in assignments:
            run = run + 1 if lane == previous else 1
            previous = lane
            best = max(best, run)
        return best

    def replay_tick(self):
        """Feed recorded arrivals through tick, including its waiting queue accounting."""
        free = dict.fromkeys(self.LANES, 0)
        pending, assignments, state = [], [], {}
        index, minute, spread = 0, 0, 0
        # Replay bursts at the default cap; unassigned demand must never stop admission.
        with patch.object(AUTODISPATCH, "REVIEW_MAX_IN_FLIGHT", 1):
            while (index < len(self.SEQUENCE) or pending) and minute < 4000:
                while index < len(self.SEQUENCE) and int(self.SEQUENCE[index].split(":")[0]) <= minute:
                    pr = self.SEQUENCE[index].split(":")[1]
                    pending.append(issue(index + 1, VERIFY_BODY.replace("#1785", f"#{pr}"),
                                         ("lane-ready", "verify-intent")))
                    index += 1
                for lane in self.LANES:
                    if free[lane] <= minute: state.pop(lane, None)
                if pending:
                    dispatched, _ = self.run_tick(pending, self.LANES, state)
                    state = self.last_state
                    for lane, n in dispatched:
                        pending = [row for row in pending if row["number"] != n]
                        free[lane] = minute + self.SERVICE
                        assignments.append(lane)
                    loads = AUTODISPATCH.review_backlog(self.LANES, state)
                    spread = max(spread, max(loads.values()) - min(loads.values()))
                minute += 1
        self.assertEqual(index, len(self.SEQUENCE))
        self.assertEqual(pending, [], "the replay must service every recorded review")
        self.assertEqual(len(assignments), len(self.SEQUENCE))
        return {lane: assignments.count(lane) for lane in self.LANES}, assignments, spread

    def test_the_backlog_difference_of_the_three_lanes_converges(self):
        old, _, _ = self.replay(self.old_policy)
        new, _, _ = self.replay_tick()
        fair = math.ceil(len(self.SEQUENCE) / len(self.LANES))
        # the old rule piles the reviews on the first lane of the order (the symptom)
        self.assertGreater(max(old.values()) - min(old.values()), 1)
        self.assertGreater(max(old.values()), fair)
        # the new rule spreads them: the backlog difference of the three lanes converges
        self.assertLessEqual(max(new.values()) - min(new.values()), 1)
        self.assertLessEqual(max(new.values()), fair)

    def test_no_lane_stacks_the_reviews_of_the_night(self):
        old, old_sequence, _ = self.replay(self.old_policy)
        new, new_sequence, new_spread = self.replay_tick()
        # the busiest lane of the night takes no more than its share of the rounds
        self.assertLessEqual(max(new.values()), math.ceil(len(self.SEQUENCE) / len(self.LANES)))
        # the in-flight backlog of the three lanes never differs by more than one review
        self.assertLessEqual(new_spread, 1)
        # the old rule gives the first lane of the order clearly more rounds
        self.assertGreater(old["CX2"], new["CX2"])
        self.assertEqual(len(old_sequence), len(new_sequence))
        # the new rule does not stack the rounds on one lane: no lane reviews two rounds
        # in a row while another review lane is free
        self.assertGreaterEqual(self.longest_run(old_sequence), 2)
        self.assertEqual(self.longest_run(new_sequence), 1)


if __name__ == "__main__":
    unittest.main()
