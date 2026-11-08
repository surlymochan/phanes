#!/usr/bin/env python3
"""Fake-queue proof for backend starvation fallback (Refs #232) and for the verify-card contract (Refs #1811)."""
import json
import os
import re
import tempfile
import unittest
import warnings
import importlib.util
from pathlib import Path

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
        """One tick with the GitHub/ssh/dispatch side effects faked; returns (dispatched, logs)."""
        dispatched, logs, edits = [], [], []

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
                    AUTODISPATCH.tick(dict(lanes or self.LANES), dict(state or {}), 0)
        finally:
            for name, value in saved.items():
                setattr(AUTODISPATCH, name, value)
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


if __name__ == "__main__":
    unittest.main()
