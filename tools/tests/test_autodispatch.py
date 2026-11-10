#!/usr/bin/env python3
"""Refill contract of the lane autodispatcher (Refs #1662).

This file is the migrated home of the old `openswarm/tests/test_autodispatch.py`
(the `openswarm/` tree was replaced by `swrm/` in #1894, so the phanes-only
autodispatch tests live under `tools/` now, next to the dispatcher they cover).

The 2026-11-02 19:00 incident: the pool fell from 14 to 5 dispatchable issues,
``autodispatch.log`` got no mint record, and the 19:45 watchdog round found no
usable card among the 21 cards of the local backlog.  The contract of this file:

  * the pool is topped up to TARGET (not to LOW) when it drops below the threshold;
  * an unreadable queue mints within a bounded number of retries and then waits
    for an authoritative read (the minted cards raise the estimated level);
  * a card that does not satisfy the card rules is quarantined in
    ``backlog/rejected/`` without ever overwriting an earlier card of the same
    name, and one unusable card never blocks the rest of the batch;
  * an unreadable planner query keeps the single-planner gate closed and leaves
    the reason in the log.

Run it offline:

    python3 -m pytest -q tools/tests/test_autodispatch.py
    python3 -m unittest tools.tests.test_autodispatch -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
AUTODISPATCH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
READY_TARGET = ROOT / "tools" / "check-ad-ready-target.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)


def load_ready_target_contract():
    """Load the shared card-shape contract of tools/check-ad-ready-target.py."""
    spec = importlib.util.spec_from_file_location("ad_ready_target_contract", READY_TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReplenishTriggerTests(unittest.TestCase):
    """Refs #1662: a pool that runs dry is refilled, and every skipped card leaves a trace."""

    THICK = ("# Local card\nlabels: p1\nImplement the bounded refill path.\n"
             "验收：python3 tools/check-ready-refill-contract.py\n")
    THIN = "# Thin card\nlabels: p1\n"

    def patch(self, name, value):
        """Replace a module attribute for the duration of one test."""
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        self.patch("LAST_MINT_LOG", 0.0)
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        autodispatch.MINT_EVIDENCE.clear()
        self.filed = []
        self.real_file_issue = autodispatch.file_issue
        self.patch("file_issue", lambda text, extra=(): self.filed.append(text.splitlines()[0]) or "#501")
        self.patch("gh", self.fake_gh)
        self.patch("sh", lambda *args, **kwargs: (0, "", ""))

    def fake_gh(self, args, timeout=60):
        """A planner issue is already open, so no second planner card may be minted."""
        if args[:2] == ["issue", "list"] and "planner" in args:
            return 0, json.dumps([{"number": 42}]), ""
        return 0, json.dumps([]), ""

    def log_text(self):
        return (self.dir / "autodispatch.log").read_text(encoding="utf-8")

    def backlog(self, **cards):
        backlog = self.dir / "backlog"
        backlog.mkdir(exist_ok=True)
        for name, text in cards.items():
            (backlog / f"{name}.md").write_text(text, encoding="utf-8")
        return backlog

    def test_the_pool_is_topped_up_to_the_target_below_the_low_water_mark(self):
        self.assertEqual(autodispatch.refill_need(5), autodispatch.TARGET - 5)
        self.assertEqual(autodispatch.refill_need(autodispatch.LOW), 0)
        self.assertEqual(autodispatch.refill_need(autodispatch.LOW, starved=3), 3)
        self.assertEqual(autodispatch.refill_need(5, starved=25), 25)
        self.assertEqual(autodispatch.refill_need(autodispatch.TARGET + 5), 0)

    def test_a_target_below_low_does_not_disable_the_low_water_trigger(self):
        for ready, starved, expected in ((0, 0, 30), (19, 0, 11), (20, 0, 10),
                                         (25, 0, 5), (25, 8, 8), (30, 0, 0),
                                         (30, 3, 3), (31, 0, 0)):
            with self.subTest(ready=ready, starved=starved):
                self.assertEqual(autodispatch.refill_need(ready, low=30, target=20,
                                                        starved=starved), expected)

    def test_the_tick_refills_when_low_exceeds_target(self):
        needs = []
        self.patch("LOW", 30)
        self.patch("TARGET", 20)
        self.patch("queue", lambda *args, **kwargs: [{"n": n, "body": ""} for n in range(25)])
        self.patch("prefetch_labels", lambda numbers: None)
        self.patch("freshen_queue", lambda q, state: q)
        self.patch("replenish", lambda k, state: needs.append(k) or [])
        state = {}

        autodispatch.tick({}, state, 0)

        self.assertEqual(needs, [5])
        self.assertEqual(state["_ready"], 25)
        self.assertIn("need 5 card(s) (threshold 30, target 20)", self.log_text())

    def test_an_unusable_card_does_not_block_the_rest_of_the_batch(self):
        backlog = self.backlog(a_thin=self.THIN, b_good=self.THICK)

        filed = autodispatch.replenish(5, {})

        self.assertEqual(filed, ["#501"])
        self.assertEqual(self.filed, ["# Local card"])
        self.assertTrue((backlog / "rejected" / "a_thin.md").is_file())
        self.assertTrue((backlog / "filed" / "b_good.md").is_file())
        self.assertIn("backlog a_thin.md", self.log_text())
        self.assertIn("thin card", self.log_text())

    def test_a_failing_card_does_not_block_the_rest_of_the_batch(self):
        broken = self.THICK.replace("# Local card", "# Broken card")
        self.backlog(a_broken=broken, b_good=self.THICK)

        def file_issue(text, extra=()):
            if text.startswith("# Broken card"):
                raise RuntimeError("gh: could not add label: 'nope' not found")
            return "#502"

        self.patch("file_issue", file_issue)

        filed = autodispatch.replenish(5, {})

        self.assertEqual(filed, ["#502"])
        self.assertIn("backlog a_broken.md", self.log_text())

    def test_the_mint_pause_marker_skips_the_mint_and_says_why(self):
        self.backlog(a_good=self.THICK)
        (self.dir / "PAUSE-MINT").write_text("", encoding="utf-8")

        self.assertEqual(autodispatch.replenish(5, {}), [])
        self.assertIn("replenish skipped", self.log_text())
        self.assertIn("PAUSE-MINT", self.log_text())

    def test_an_open_planner_issue_is_logged_and_blocks_a_second_planner_card(self):
        self.assertEqual(autodispatch.replenish(5, {}), [])
        self.assertIn("planner issue already open (#42)", self.log_text())
        self.assertEqual(self.filed, [])

    def test_the_tick_refills_the_pool_when_it_drops_below_the_threshold(self):
        needs = []
        self.patch("queue", lambda *args, **kwargs: [])
        self.patch("replenish", lambda k, state: needs.append(k) or [])

        autodispatch.tick({}, {}, 0)

        self.assertEqual(needs, [autodispatch.TARGET])
        self.assertIn("WARNING queue low", self.log_text())
        self.assertIn(f"need {autodispatch.TARGET} card(s)", self.log_text())

    def test_the_card_rules_agree_with_the_shared_thin_card_contract(self):
        contract = load_ready_target_contract()
        samples = [
            "",
            "# Thin card\nlabels: p1\n",
            "No title\nlabels: p1\nImplement the path.\n验收：python3 check.py\n",
            "# Title\nNo labels line\nImplement the path.\n验收：python3 check.py\n",
            "# Title\nlabels: p1\nImplement the path.\nNo acceptance.\n",
            "# Title\nlabels: p1\n验收：python3 check.py\n",
            "# Title\nlabels: p1\nImplement the bounded refill path.\n验收：python3 check.py\n",
            "# Title\n\nlabels: p1\nBody\n验收：test\n",
        ]
        for text in samples:
            self.assertEqual(autodispatch.card_defect(text, strict=True) is None,
                             not contract.is_thin_card(text), text)

    def test_a_local_card_only_needs_a_title_a_labels_line_and_a_body(self):
        """The local backlog is the operator's drop box: the acceptance line is the
        convention of the planner cards, so a local card without it is still minted."""
        body = "请处理标题 ## Impl card delivery requirements 的兼容问题"
        self.assertIsNone(autodispatch.card_defect(f"# 标题\nlabels: p1\n{body}\n"))
        self.assertIsNotNone(autodispatch.card_defect("# 标题\nlabels: p1\n"))
        self.assertIsNotNone(autodispatch.card_defect("not a card\n"))

    def test_file_issue_accepts_blank_header_lines_and_preserves_the_body(self):
        created = []
        self.patch("gh", lambda args: created.append(args) or (0, "https://example.com/issues/501", ""))
        body = "Body\n\n```python\n    pass\n```\n\n验收：test\n"
        for header in ("# Title\n\nlabels: p1\n", "\n \n# Title\n\t\n  labels: p1  \n"):
            with self.subTest(header=header):
                text = header + body
                self.assertIsNone(autodispatch.card_defect(text, strict=True))
                self.assertEqual(self.real_file_issue(text, extra=("planner",)), "#501")
                self.assertEqual(created[-1], ["issue", "create", "-t", "Title", "-b", body,
                                               "-l", "lane-ready", "-l", "p1", "-l", "planner"])

    def test_replenish_mints_a_local_card_with_blank_header_lines(self):
        text = "# Title\n\nlabels: p1\nBody\n验收：test\n"
        backlog = self.backlog(good=text)
        self.patch("file_issue", self.real_file_issue)
        created = []
        self.patch("gh", lambda args: created.append(args) or (0, "https://example.com/issues/501", ""))

        self.assertEqual(autodispatch.replenish(1, {}), ["#501"])
        self.assertEqual(len(created), 1)
        self.assertEqual((backlog / "filed" / "good.md").read_text(encoding="utf-8"), text)

    def test_replenish_imports_a_repo_card_with_blank_header_lines_once(self):
        text = "# Title\n\nlabels: p1\nBody\n验收：test\n"
        path = "docs/backlog/next/good.md"
        self.patch("file_issue", self.real_file_issue)
        self.patch("sh", lambda args, **kwargs: (0, text if "show" in args else path, ""))
        created = []

        def gh(args):
            if args[:2] == ["issue", "create"]:
                created.append(args)
                return 0, "https://example.com/issues/501", ""
            return self.fake_gh(args)

        self.patch("gh", gh)
        state = {}

        self.assertEqual(autodispatch.replenish(1, state), ["#501"])
        self.assertEqual(state["_imported"], [path])
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(len(created), 1)

    def lost_answer_setup(self, name, listing):
        """A repo card, a server that records every create, and a search answered by `listing(server)`."""
        text = "# %s\n\nlabels: p1\nBody\n验收：test\n" % name
        path = "docs/backlog/next/%s.md" % name.replace(" ", "-")
        self.patch("file_issue", self.real_file_issue)
        self.patch("sh", lambda args, **kwargs: (0, text if "show" in args else path, ""))
        server, answers = [], []

        def gh(args):
            if args[:2] == ["issue", "create"]:
                server.append(args)  # the server created the issue either way
                return answers.pop(0)
            if args[:2] == ["issue", "list"] and "--json" in args and "number,body,createdAt" in args:
                return listing(server, args)
            return self.fake_gh(args)

        self.patch("gh", gh)
        return text, path, server, answers

    @staticmethod
    def settle(state):
        """Let the lost request be older than the settle time."""
        for marker in state["_import_pending"]:
            state["_import_pending"][marker] -= autodispatch.CREATE_SETTLE_S + 1

    @staticmethod
    def issue_of(args, number):
        return {"number": number, "title": args[3], "body": args[5], "createdAt": "2999-01-01T00:00:00Z"}

    def test_a_lost_create_answer_does_not_file_the_repo_card_twice_across_ticks(self):
        """The server creates the issue but the client sees a timeout: the next tick must find it, not file again."""
        text, path, server, answers = self.lost_answer_setup(
            "Lost answer", lambda server, args: (0, json.dumps([self.issue_of(server[0], 777)] if server else []), ""))
        answers += [(1, "", "context deadline exceeded")]
        state = {}

        self.assertEqual(autodispatch.replenish(1, state), [])     # tick 1: the answer is lost
        self.assertNotIn(path, state.get("_imported", []))
        self.assertEqual(len(state["_import_pending"]), 1)          # the unknown state is persisted
        self.assertEqual(autodispatch.replenish(1, state), ["#777(already filed)"])  # tick 2: found by its marker
        self.assertEqual(len(server), 1)                            # never created twice
        self.assertEqual(state["_imported"], [path])
        self.assertEqual(state["_import_pending"], {})
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(len(server), 1)

    def test_a_pending_card_is_filed_again_only_when_the_listing_proves_it_is_missing(self):
        """A short listing that holds no marker proves absence; only then is the card created again."""
        text, path, server, answers = self.lost_answer_setup("Never created", lambda server, args: (0, "[]", ""))
        answers += [(1, "", "connection refused"), (0, "https://example.com/issues/778", "")]
        state = {}
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(autodispatch.replenish(1, state), [])     # inside the settle time: may still be in flight
        self.settle(state)
        self.assertEqual(autodispatch.replenish(1, state), [])     # first empty proof after the settle time
        self.assertEqual(len(server), 1)
        self.assertEqual(autodispatch.replenish(1, state), ["#778"])  # second one: created again
        self.assertEqual(len(server), 2)
        self.assertEqual(state["_imported"], [path])

    def test_a_create_still_in_flight_is_not_filed_a_second_time(self):
        """The listing is empty while the lost request is still running; it succeeds afterwards."""
        text, path, server, answers = self.lost_answer_setup(
            "In flight", lambda server, args: (0, json.dumps([self.issue_of(server[0], 790)] if len(server) > 1 else []), ""))
        answers += [(1, "", "timeout")]
        state = {}
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(autodispatch.replenish(1, state), [])     # empty now: the request has not landed
        server.append(["issue", "create", "x", "In flight", "y", server[0][5]])  # the original request lands
        server[0], server[1] = server[1], server[0]
        self.assertEqual(autodispatch.replenish(1, state), ["#790(already filed)"])
        self.assertEqual(len(server), 2)                           # the lost request + its landing; no retry was sent

    def test_a_stop_after_the_create_leaves_a_record_that_prevents_a_second_issue(self):
        """The process stops between the create and the record of the import: the restart finds the issue."""
        text, path, server, answers = self.lost_answer_setup(
            "Stopped", lambda server, args: (0, json.dumps([self.issue_of(server[0], 779)] if server else []), ""))
        answers += [(0, "https://example.com/issues/779", "")]
        saved = []
        real_save = autodispatch.save_state

        def save(state):
            saved.append(json.loads(json.dumps(state)))
            if len(saved) == 2:
                raise KeyboardInterrupt  # the stop after the create
            real_save(state)

        self.patch("save_state", save)
        with self.assertRaises(KeyboardInterrupt):
            autodispatch.replenish(1, {})
        restart = saved[0]                                          # what the disk held at the stop
        self.assertEqual(len(restart["_import_pending"]), 1)
        self.patch("save_state", real_save)
        self.assertEqual(autodispatch.replenish(1, restart), ["#779(already filed)"])
        self.assertEqual(len(server), 1)

    def test_a_full_page_that_does_not_reach_back_past_the_attempt_never_retries(self):
        recent = [{"number": n, "title": "x", "body": "other", "createdAt": "2999-01-01T00:00:00Z"} for n in range(1, 201)]
        text, path, server, answers = self.lost_answer_setup("Crowded", lambda server, args: (0, json.dumps(recent), ""))
        answers += [(1, "", "timeout")]
        state = {}
        self.assertEqual(autodispatch.replenish(1, state), [])
        for _ in range(3):
            self.assertEqual(autodispatch.replenish(1, state), [])  # unknown: never created again, however long it takes
        self.assertEqual(len(server), 1)

    def test_a_failed_listing_never_retries(self):
        text, path, server, answers = self.lost_answer_setup("Unreadable", lambda server, args: (1, "", "rate limit"))
        answers += [(1, "", "timeout")]
        state = {}
        for _ in range(3):
            self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(len(server), 1)

    def test_a_full_page_reaching_back_past_the_attempt_proves_absence(self):
        old = [{"number": n, "title": "x", "body": "other", "createdAt": "2000-01-01T00:00:00Z"} for n in range(1, 201)]
        text, path, server, answers = self.lost_answer_setup("Old page", lambda server, args: (0, json.dumps(old), ""))
        answers += [(1, "", "timeout"), (0, "https://example.com/issues/781", "")]
        state = {}
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.settle(state)
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(autodispatch.replenish(1, state), ["#781"])
        self.assertEqual(len(server), 2)

    def test_an_issue_of_the_same_title_from_another_card_is_not_taken_for_this_card(self):
        other = {"number": 5, "title": "Same title", "body": "written by hand"}
        text, path, server, answers = self.lost_answer_setup("Same title", lambda server, args: (0, json.dumps([dict(other, createdAt="2999-01-01T00:00:00Z")]), ""))
        answers += [(1, "", "timeout"), (0, "https://example.com/issues/780", "")]
        state = {}
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.settle(state)
        self.assertEqual(autodispatch.replenish(1, state), [])
        self.assertEqual(autodispatch.replenish(1, state), ["#780"])
        self.assertEqual(state["_imported"], [path])


class ReplenishUnreadableQueueTests(unittest.TestCase):
    """Refs #1662: an unreadable queue must not mint a new batch on every tick."""

    def patch(self, name, value):
        """Replace a module attribute for the duration of one test."""
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.needs = []
        self.patch("replenish", lambda k, state: self.needs.append(k) or ["#501"])

        def broken_queue(*args, **kwargs):
            raise RuntimeError("gh: API rate limit exceeded")

        self.patch("queue", broken_queue)

    def log_text(self):
        return (self.dir / "autodispatch.log").read_text(encoding="utf-8")

    def test_a_persistent_queue_failure_mints_within_a_bound_and_then_waits(self):
        state = {"_ready": 5}

        for _ in range(autodispatch.QUEUE_UNREADABLE_REFILL_MAX + 2):
            autodispatch.tick({}, state, 0)

        # the retries are bounded, and every minted card raises the estimated pool level
        self.assertEqual(len(self.needs), autodispatch.QUEUE_UNREADABLE_REFILL_MAX)
        self.assertEqual(self.needs[0], autodispatch.TARGET - 5)
        self.assertEqual(self.needs, sorted(self.needs, reverse=True))
        self.assertIn("bounded retries exhausted", self.log_text())
        self.assertEqual(state["_queue_failures"], autodispatch.QUEUE_UNREADABLE_REFILL_MAX + 2)

    def test_the_minted_cards_are_counted_into_the_pool_level(self):
        state = {"_ready": 5}

        autodispatch.tick({}, state, 0)

        self.assertEqual(state["_minted"], 1)
        self.assertEqual(autodispatch.pool_level(state), 6)

    def test_an_unreadable_queue_refills_to_low_when_target_is_lower(self):
        self.patch("LOW", 30)
        self.patch("TARGET", 20)
        self.patch("replenish", lambda k, state: self.needs.append(k) or ["#501"] * k)
        state = {"_ready": 25}

        for _ in range(autodispatch.QUEUE_UNREADABLE_REFILL_MAX + 2):
            autodispatch.tick({}, state, 0)

        self.assertEqual(self.needs, [5])
        self.assertEqual(state["_minted"], 5)
        self.assertEqual(autodispatch.pool_level(state), 30)

    def test_an_unknown_pool_level_mints_nothing(self):
        state = {}

        autodispatch.tick({}, state, 0)

        self.assertEqual(self.needs, [])
        self.assertIn("pool level unknown", self.log_text())

    def test_a_readable_queue_resets_the_failure_counter_and_the_level(self):
        state = {"_ready": 5}
        autodispatch.tick({}, state, 0)

        self.patch("queue", lambda *args, **kwargs: [])
        autodispatch.tick({}, state, 0)

        self.assertEqual(state["_queue_failures"], 0)
        self.assertEqual(state["_ready"], 0)
        self.assertIn("queue error", self.log_text())


class QuarantineKeepsBothCardsTests(unittest.TestCase):
    """Refs #1662: a quarantined card never overwrites an earlier card of the same name."""

    def patch(self, name, value):
        """Replace a module attribute for the duration of one test."""
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch("file_issue", lambda text, extra=(): "#501")
        self.patch("gh", lambda args, timeout=60: (0, json.dumps([{"number": 42}]), ""))
        self.patch("sh", lambda *args, **kwargs: (0, "", ""))

    def test_move_without_overwrite_preserves_both_card_contents(self):
        filed = self.dir / "filed"
        filed.mkdir()
        original = filed / "card.md"
        original.write_text("earlier card")
        source = self.dir / "incoming.md"
        source.write_text("new card")

        moved = pathlib.Path(autodispatch.move_without_overwrite(str(source), str(original)))

        self.assertEqual(original.read_text(), "earlier card")
        self.assertEqual(moved.read_text(), "new card")
        self.assertNotEqual(moved, original)

    def test_the_quarantine_keeps_an_earlier_card_of_the_same_name(self):
        backlog = self.dir / "backlog"
        backlog.mkdir()
        (backlog / "a_thin.md").write_text("# Thin card\nlabels: p1\n")
        rejected = backlog / "rejected"
        rejected.mkdir()
        (rejected / "a_thin.md").write_text("earlier card")

        filed = autodispatch.replenish(5, {})

        self.assertEqual(filed, [])
        self.assertEqual((rejected / "a_thin.md").read_text(), "earlier card")
        moved = rejected / "a_thin.1.md"
        self.assertTrue(moved.is_file(), sorted(path.name for path in rejected.iterdir()))
        self.assertIn("Thin card", moved.read_text())


class PlannerGateTests(unittest.TestCase):
    """Refs #1662: an unreadable planner query keeps the single-planner gate closed."""

    def patch(self, name, value):
        """Replace a module attribute for the duration of one test."""
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        self.patch("LAST_MINT_LOG", 0.0)
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch("sh", lambda *args, **kwargs: (0, "", ""))

    def log_text(self):
        return (self.dir / "autodispatch.log").read_text(encoding="utf-8")

    def test_an_unreadable_planner_query_skips_the_creation(self):
        commands = []

        def fake_gh(args, timeout=60):
            commands.append(args)
            if args[:2] == ["issue", "list"] and "planner" in args:
                return 0, "not-json", ""
            return 0, json.dumps([]), ""

        self.patch("gh", fake_gh)

        self.assertEqual(autodispatch.replenish(1, {}), [])
        self.assertEqual(len(commands), 1)
        self.assertIn("planner query unreadable", self.log_text())
        self.assertIn("skip planner creation", self.log_text())


if __name__ == "__main__":
    unittest.main()
