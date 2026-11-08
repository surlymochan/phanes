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

    def run_tick(self, issues, lanes=None, state=None, remote_info=None):
        """One tick with the GitHub/ssh/dispatch side effects faked; returns (dispatched, logs).

        The recorded side effects stay readable on the harness: `self.edits` (the label
        edits), `self.snapshots` (the lane status counts) and `self.replenish_calls`."""
        dispatched, logs, edits = [], [], []
        snapshots, replenish_calls = [], []

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

        def fake_status_snapshot(lanes_, state_, res_, ready, blocked, blocked_memory=None):
            snapshots.append({"ready": ready, "blocked": blocked, "blocked_memory": blocked_memory})
            return {}

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
                AUTODISPATCH.remote = lambda lane, cfg, run="": {"lane": "idle", **(remote_info or {})}
                AUTODISPATCH.deps_met = lambda item: True
                AUTODISPATCH.dispatch = fake_dispatch
                AUTODISPATCH.replenish = lambda k, state: replenish_calls.append(k) or []
                AUTODISPATCH.save_state = lambda state: None
                AUTODISPATCH.status_snapshot = fake_status_snapshot
                AUTODISPATCH.VERIFY_REJECT_LOGGED.clear()
                # tick() writes lanes-status.json without closing it: the warning is pre-existing
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    AUTODISPATCH.tick(dict(lanes or self.LANES), dict(state or {}), 0)
        finally:
            for name, value in saved.items():
                setattr(AUTODISPATCH, name, value)
        self.edits = edits
        self.snapshots = snapshots
        self.replenish_calls = replenish_calls
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


class TickMemoryPrecheckTest(TickHarness, unittest.TestCase):
    """Refs #1712: memory admission follows the selected executor, not just the card's pin."""

    def test_codex_lane_skips_pi_pinned_fallback_at_135mb(self):
        # The pi pin is non-strict, so codex can take it when no pi lane is available. The host's
        # 135 MB available-memory sample must still block starting a codex session.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
        dispatched, logs = self.run_tick(
            [issue(1712, "codex session task", ("lane-ready", "backend:pi"))],
            lanes=lanes,
            remote_info={"memory_available_mb": 135},
        )
        self.assertEqual(dispatched, [])
        self.assertTrue(any("gores2" in line and "135" in line and "400" in line for line in logs))

    def test_a_codex_task_is_dispatched_when_the_host_has_enough_memory(self):
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
        dispatched, logs = self.run_tick(
            [issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
            lanes=lanes,
            remote_info={"memory_available_mb": 900},
        )
        self.assertEqual(dispatched, [("CX10", 1712)])
        self.assertFalse(any("blocked-by-host-memory" in line for line in logs))

    def test_the_135mb_sample_blocks_the_codex_task_but_not_the_pi_task(self):
        # The replay of the gores2 sample: the codex task waits for the host, the pi task runs.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}, "PI1": {"host": "gores2", "backend": "pi"}}
        codex_card = issue(1712, "codex session task", ("lane-ready", "backend:codex", "strict-backend"))
        pi_card = issue(1713, "pi session task", ("lane-ready", "backend:pi"))
        dispatched, logs = self.run_tick([codex_card, pi_card], lanes=lanes,
                                         remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [("PI1", 1713)])  # the pi lane is not gated by the codex budget
        self.assertTrue(any("blocked-by-host-memory" in line and "gores2" in line for line in logs))
        snapshot = self.snapshots[-1]
        self.assertEqual(snapshot["blocked"], 1)  # the codex card waits for the host
        self.assertEqual(snapshot["ready"], 0)

    def test_a_pi_lane_is_not_gated_by_the_codex_budget(self):
        lanes = {"PI1": {"host": "gores2", "backend": "pi"}}
        dispatched, _ = self.run_tick([issue(1713, "pi session task", ("lane-ready", "backend:pi"))],
                                      lanes=lanes, remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [("PI1", 1713)])

    def test_the_host_threshold_configuration_wins(self):
        # The lane configuration carries the threshold of its host: 135 MB is enough there.
        lanes = {"CX10": {"host": "gores2", "backend": "codex", "memory_min_mb": 128}}
        dispatched, _ = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                      lanes=lanes, remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [("CX10", 1712)])

    def test_a_higher_host_threshold_blocks_the_lane(self):
        lanes = {"CX10": {"host": "gores2", "backend": "codex", "memory_min_mb": 512}}
        dispatched, logs = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                         lanes=lanes, remote_info={"memory_available_mb": 400})
        self.assertEqual(dispatched, [])
        self.assertTrue(any("512" in line for line in logs))

    def test_the_per_host_environment_threshold_wins(self):
        os.environ["AD_MEMORY_MIN_MB_GORES2"] = "128"
        try:
            lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
            dispatched, _ = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                          lanes=lanes, remote_info={"memory_available_mb": 135})
            self.assertEqual(dispatched, [("CX10", 1712)])
        finally:
            del os.environ["AD_MEMORY_MIN_MB_GORES2"]

    def test_the_codex_budget_reads_the_environment(self):
        os.environ["AD_CODEX_MEMORY_MIN_MB"] = "256"
        try:
            self.assertEqual(AUTODISPATCH.backend_memory_min_mb("codex"), 256)
            lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
            dispatched, _ = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                          lanes=lanes, remote_info={"memory_available_mb": 300})
            self.assertEqual(dispatched, [("CX10", 1712)])  # 300 MB >= the configured 256 MB
        finally:
            del os.environ["AD_CODEX_MEMORY_MIN_MB"]

    def test_the_skip_is_not_a_lane_failure(self):
        # The card keeps its lane-ready label: no lane-failed, no comment, no done marker.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
        dispatched, logs = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                         lanes=lanes, remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [])
        self.assertFalse([edit for edit in self.edits if "lane-failed" in edit])
        self.assertFalse([edit for edit in self.edits if "lane-pr" in edit])
        self.assertFalse([line for line in logs if "lane-failed" in line])

    def test_another_backend_takes_the_task_the_codex_lane_may_not_run(self):
        # The codex lane is skipped for the host memory, the pi lane runs the card: the card is
        # not blocked and the queue is not refilled for it.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}, "PI1": {"host": "gores1", "backend": "pi"}}
        dispatched, _ = self.run_tick([issue(1712, "plain card", ("lane-ready",))], lanes=lanes,
                                      remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [("PI1", 1712)])
        snapshot = self.snapshots[-1]
        self.assertEqual(snapshot["blocked"], 0)
        self.assertEqual(snapshot["blocked_memory"], {})

    def test_a_task_without_another_backend_is_blocked_once(self):
        # Two codex lanes of the same low-memory host reject the same card: it counts once.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}, "CX11": {"host": "gores2", "backend": "codex"}}
        dispatched, _ = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                      lanes=lanes, remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [])
        snapshot = self.snapshots[-1]
        self.assertEqual(snapshot["blocked"], 1)  # counted once, never twice
        self.assertEqual(snapshot["ready"], 0)

    def test_a_backend_consumed_this_tick_cannot_keep_a_remaining_task_ready(self):
        # B1: pi takes its strict card first; its occupied lane cannot rescue the plain
        # card from the 135 MB codex host, regardless of lane iteration order.
        cards = [issue(1713, "pi session task", ("lane-ready", "backend:pi", "strict-backend")),
                 issue(1712, "plain card", ("lane-ready",))]
        for order in (("CX10", "PI1"), ("PI1", "CX10")):
            with self.subTest(order=order):
                configs = {"CX10": {"host": "gores2", "backend": "codex"},
                           "PI1": {"host": "gores1", "backend": "pi"}}
                dispatched, logs = self.run_tick(cards, lanes={L: configs[L] for L in order},
                                                 remote_info={"memory_available_mb": 135})
                self.assertEqual(dispatched, [("PI1", 1713)])
                snapshot = self.snapshots[-1]
                self.assertEqual(snapshot["ready"], 0)
                self.assertEqual(snapshot["blocked"], 1)
                self.assertEqual(set(snapshot["blocked_memory"]), {1712})
                reason = snapshot["blocked_memory"][1712]
                self.assertEqual(reason["reason"], "blocked-by-host-memory")
                self.assertEqual(reason["hosts"]["gores2"],
                                 {"available_mb": 135, "threshold_mb": 400, "lanes": ["CX10"]})
                self.assertTrue(any("blocked #1712: blocked-by-host-memory" in line for line in logs))

    def test_the_blocked_diagnostics_carry_the_reason_code(self):
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
        self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                      lanes=lanes, remote_info={"memory_available_mb": 135})
        rows = self.snapshots[-1]["blocked_memory"][1712]
        self.assertEqual(rows["reason"], "blocked-by-host-memory")
        host = rows["hosts"]["gores2"]
        self.assertEqual(host["available_mb"], 135)
        self.assertEqual(host["threshold_mb"], 400)
        self.assertEqual(host["lanes"], ["CX10"])

    def test_a_blocked_queue_does_not_trigger_a_refill(self):
        # The queue is long enough, the host is not: no new issue is minted for it.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
        cards = [issue(1700 + n, "codex session task", ("lane-ready", "backend:codex", "strict-backend"))
                 for n in range(12)]
        dispatched, _ = self.run_tick(cards, lanes=lanes, remote_info={"memory_available_mb": 135})
        self.assertEqual(dispatched, [])
        self.assertEqual(self.replenish_calls, [])

    def test_an_unreadable_memory_never_blocks_a_lane(self):
        # The probe failed (no memory in the status): the dispatch stays as it was.
        lanes = {"CX10": {"host": "gores2", "backend": "codex"}}
        dispatched, logs = self.run_tick([issue(1712, "codex session task", ("lane-ready", "backend:codex"))],
                                         lanes=lanes)
        self.assertEqual(dispatched, [("CX10", 1712)])
        self.assertFalse(any("blocked-by-host-memory" in line for line in logs))


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


if __name__ == "__main__":
    unittest.main()
