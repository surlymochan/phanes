import sys
from pathlib import Path as _FixturePath
sys.path.insert(0, str(_FixturePath(__file__).resolve().parent))
from pr_guard_fixtures import no_open_prs
#!/usr/bin/env python3
"""Regression coverage for dispatch-priority starvation (Refs #1705)."""

import importlib.util
import json
from card_fixtures import fixture_json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "autodispatch", Path(__file__).with_name("autodispatch.py")
)
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)



def fake_sh(cmd, *args, **kwargs):
    """Answer the codex memory probe with plenty of KiB; every other call gets a SHA."""
    if "/proc/meminfo" in " ".join(map(str, cmd if isinstance(cmd, (list, tuple)) else [cmd])):
        return 0, "8388608\n", ""
    return 0, "a" * 40, ""

class DispatchPriorityTest(unittest.TestCase):
    NOW = AUTODISPATCH.timestamp("2026-11-08T23:40:00Z")

    def card(self, number, *, backend="codex", budget=30, entered="2026-11-08T23:40:00Z"):
        return {"n": number, "backend": backend, "prio": 2, "verify": False,
                "budget_minutes": budget, "pool_entered_at": entered}

    def test_large_budget_adds_size_score(self):
        small = self.card(1, budget=30)
        large = self.card(2, budget=90)
        small_score, _ = AUTODISPATCH.priority_score("codex", small, self.NOW)
        large_score, basis = AUTODISPATCH.priority_score("codex", large, self.NOW)
        self.assertGreater(large_score, small_score)
        self.assertEqual(basis["size"], AUTODISPATCH.PRIORITY_LARGE_BONUS)

    def test_real_pool_wait_increases_score(self):
        fresh = self.card(1)
        waiting = self.card(2, entered="2026-11-08T23:10:00Z")
        fresh_score, _ = AUTODISPATCH.priority_score("codex", fresh, self.NOW)
        waiting_score, basis = AUTODISPATCH.priority_score("codex", waiting, self.NOW)
        self.assertGreater(waiting_score, fresh_score)
        self.assertEqual(basis["wait_s"], 30 * 60)

    def test_backend_match_contributes_to_score_without_changing_eligibility(self):
        exact = self.card(1, backend="codex")
        unpinned = self.card(2, backend=None)
        cross = self.card(3, backend="pi")
        scores = [AUTODISPATCH.priority_score("codex", item, self.NOW)[0]
                  for item in (exact, unpinned, cross)]
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])
        self.assertTrue(AUTODISPATCH.can_take("codex", exact))

    def test_small_card_gets_configured_starvation_lift_at_threshold(self):
        threshold = self.NOW - AUTODISPATCH.PRIORITY_STARVATION_S
        starved = self.card(1, entered=threshold)
        _, basis = AUTODISPATCH.priority_score("codex", starved, self.NOW)
        self.assertEqual(basis["starvation"], AUTODISPATCH.PRIORITY_STARVATION_BONUS)

    def test_newly_admitted_old_small_card_does_not_overtake_waiting_large_card(self):
        """An old creation date must not masquerade as time spent in the ready pool.

        This is the reported starvation sequence: the 90-minute card has already
        waited in the pool, while the 30-minute card was created long ago but only
        just became dispatchable.  A stream of cards like the latter must not keep
        pushing the large card back on every tick.
        """
        waiting_large = {
            "n": 1705,
            "backend": "codex",
            "prio": 2,
            "verify": False,
            "budget_minutes": 90,
            "created_at": "2026-11-08T22:00:00Z",
            "pool_entered_at": "2026-11-08T23:20:00Z",
        }
        newly_admitted_old_small = {
            "n": 1,
            "backend": "codex",
            "prio": 2,
            "verify": False,
            "budget_minutes": 30,
            "created_at": "2026-11-01T00:00:00Z",
            "pool_entered_at": "2026-11-08T23:39:59Z",
        }

        # rank() reads the clock internally; keep the new arrival one second old
        # regardless of when this regression test is run.
        with patch.object(AUTODISPATCH.time, "time", return_value=self.NOW):
            ordered = sorted(
                [newly_admitted_old_small, waiting_large],
                key=lambda card: AUTODISPATCH.rank("codex", card),
            )

        self.assertIs(
            ordered[0],
            waiting_large,
            "a just-admitted small card must not use its creation age to jump a "
            "large card that has actually waited in the pool",
        )

    def test_small_card_survives_once_restarts_and_overtakes_new_large_cards(self):
        """Reload the dispatcher and its real state file for each --once run."""
        small = {"number": 1, "title": "waiting small", "body": "预算：30",
                 "labels": ["lane-ready"], "createdAt": "2026-11-01T00:00:00Z"}
        issues = [small]
        dispatched, logs, states = [], [], []

        @no_open_prs

        def gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                return 0, fixture_json(issues), ""
            item = next(i for i in issues if i["number"] == int(args[2]))
            if args[:2] == ["issue", "view"]:
                return 0, fixture_json({**item, "state": "OPEN"}), ""
            if args[:2] == ["issue", "edit"]:
                item["labels"].append("in-progress")
                return 0, "", ""
            self.fail(f"unexpected GitHub command: {args}")

        def dispatch(lane, cfg, item, base):
            dispatched.append(item["n"])
            return f"run-{item['n']}", None

        scratch = Path(__file__).resolve().parents[2] / ".swrm-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            (root / "lanes.json").write_text(json.dumps({"CX10": {"host": "h", "backend": "codex"}}))
            threshold = AUTODISPATCH.PRIORITY_STARVATION_S
            for index, elapsed in enumerate((0, threshold / 2, threshold)):
                issues.append({"number": 100 + index, "title": "new large", "body": "预算：90",
                               "labels": ["lane-ready"], "createdAt": "2026-11-01T00:00:00Z"})
                module = importlib.util.module_from_spec(SPEC)
                SPEC.loader.exec_module(module)
                with ExitStack() as stack:
                    replacements = {
                        "D": directory, "gh": gh, "dispatch": dispatch,
                        "sh": fake_sh,
                        "remote": lambda *a: {"lane": "idle", "run": {"ended_at": self.NOW}},
                        "finish": lambda *a: {"done": True},
                        "audit_switches": lambda: None,
                        "paused_scopes": lambda **k: {"mint"},
                        "log": lambda message, brain=True: logs.append(message),
                    }
                    for name, value in replacements.items():
                        stack.enter_context(patch.object(module, name, value))
                    stack.enter_context(patch.object(module.time, "time", return_value=self.NOW + elapsed))
                    stack.enter_context(patch.object(module.sys, "argv", ["autodispatch.py", "--once"]))
                    module.main()
                states.append(json.loads((root / "state.json").read_text()))
            self.assertEqual(dispatched, [100, 101, 1])
            for state in states[:-1]:
                self.assertEqual(state["_pool_entered_at"]["1"], self.NOW)
            self.assertNotIn("1", states[-1]["_pool_entered_at"])
            self.assertTrue(any("dispatched #1 →" in line and '"starvation": 16' in line for line in logs))

    def test_pool_clock_resets_after_leaving_and_survives_queue_read_failure(self):
        issue = {"number": 1, "title": "small", "body": "预算：30", "labels": ["lane-ready"]}
        state = {}
        with patch.object(AUTODISPATCH, "gh", return_value=(0, fixture_json([issue]), "")), \
                patch.object(AUTODISPATCH.time, "time", return_value=self.NOW):
            AUTODISPATCH.queue(state)
        original = dict(state["_pool_entered_at"])
        with patch.object(AUTODISPATCH, "gh", return_value=(1, "", "unavailable")):
            with self.assertRaises(RuntimeError):
                AUTODISPATCH.queue(state)
        self.assertEqual(state["_pool_entered_at"], original)
        with patch.object(AUTODISPATCH, "gh", return_value=(0, "[]", "")):
            AUTODISPATCH.queue(state)
        self.assertEqual(state["_pool_entered_at"], {})
        with patch.object(AUTODISPATCH, "gh", return_value=(0, fixture_json([issue]), "")), \
                patch.object(AUTODISPATCH.time, "time", return_value=self.NOW + 7200):
            returned = AUTODISPATCH.queue(state)
        self.assertEqual(returned[0]["pool_entered_at"], self.NOW + 7200)

    def test_dependency_block_resets_pool_clock(self):
        issue = {"number": 1, "title": "small", "body": "Depends on: #2\n预算：30", "labels": ["lane-ready"]}
        state = {"_pool_entered_at": {"1": self.NOW - 7200}}
        with patch.object(AUTODISPATCH, "gh", return_value=(0, fixture_json([issue]), "")), \
                patch.object(AUTODISPATCH, "deps_met", return_value=False), \
                patch.object(AUTODISPATCH.time, "time", return_value=self.NOW):
            blocked = AUTODISPATCH.queue(state)
        self.assertFalse(blocked[0]["deps_ok"])
        self.assertEqual(state["_pool_entered_at"], {})
        with patch.object(AUTODISPATCH, "gh", return_value=(0, fixture_json([issue]), "")), \
                patch.object(AUTODISPATCH, "deps_met", return_value=True), \
                patch.object(AUTODISPATCH.time, "time", return_value=self.NOW + 7200):
            ready = AUTODISPATCH.queue(state)
        self.assertEqual(ready[0]["pool_entered_at"], self.NOW + 7200)

    def test_full_pool_read_keeps_waiting_cards_beyond_first_page(self):
        large = {"number": 1705, "title": "waiting large", "body": "预算：90",
                 "labels": ["lane-ready"]}
        for count in (100, 101, 200, 251):
            with self.subTest(count=count):
                state = {"_pool_entered_at": {"1705": self.NOW - 3600}}
                # Include excluded rows: completeness depends on raw rows, not on
                # how many cards remain after eligibility checks.
                rows = [{"number": n, "title": "other", "body": "预算：30",
                         "labels": ["lane-ready", "in-progress"]}
                        for n in range(1, count)] + [large]

                @no_open_prs

                def gh(args, timeout=60):
                    limit = int(args[args.index("--limit") + 1])
                    return 0, fixture_json(rows[:limit]), ""

                with patch.object(AUTODISPATCH, "gh", side_effect=gh), \
                        patch.object(AUTODISPATCH.time, "time", return_value=self.NOW):
                    queued = AUTODISPATCH.queue(state)
                self.assertEqual([item["n"] for item in queued], [1705])
                self.assertEqual(queued[0]["pool_entered_at"], self.NOW - 3600)
                self.assertEqual(state["_pool_entered_at"]["1705"], self.NOW - 3600)

    def test_incomplete_pool_read_does_not_replace_clocks(self):
        state = {"_pool_entered_at": {"1705": self.NOW - 3600}}
        rows = [{"number": n, "title": "small", "body": "预算：30",
                 "labels": ["lane-ready"]} for n in range(1, 101)]
        responses = [(0, fixture_json(rows), ""), (1, "", "next page unavailable")]
        with patch.object(AUTODISPATCH, "gh", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "next page unavailable"):
                AUTODISPATCH.queue(state)
        self.assertEqual(state, {"_pool_entered_at": {"1705": self.NOW - 3600}})

    def test_large_pool_preserves_wait_across_restarts_and_dispatches_top_score(self):
        large = {"number": 1705, "title": "waiting large", "body": "预算：90",
                 "labels": ["lane-ready"]}
        rows, dispatched, reads = [large], [], []

        @no_open_prs

        def gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                limit = int(args[args.index("--limit") + 1])
                return 0, fixture_json(rows[:limit]), ""
            item = next(i for i in rows if i["number"] == int(args[2]))
            if args[:2] == ["issue", "view"]:
                reads.append(item["number"])
                return 0, fixture_json({**item, "state": "OPEN"}), ""
            if args[:2] == ["issue", "edit"]:
                item["labels"].append("in-progress")
                return 0, "", ""
            self.fail(f"unexpected GitHub command: {args}")

        def dispatch(lane, cfg, item, base):
            dispatched.append(item["n"])
            return f"run-{item['n']}", None

        scratch = Path(__file__).resolve().parents[2] / ".swrm-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            (root / "lanes.json").write_text(json.dumps({"CX10": {"host": "h", "backend": "codex"}}))
            for index, elapsed in enumerate((0, 3600, 3660)):
                if index == 1:
                    rows[:0] = [{"number": n, "title": "new small", "body": "预算：30",
                                 "labels": ["lane-ready"], "createdAt": "2026-11-01T00:00:00Z"}
                                for n in range(1, 251)]
                module = importlib.util.module_from_spec(SPEC)
                SPEC.loader.exec_module(module)
                reads.clear()
                with ExitStack() as stack:
                    replacements = {
                        "D": directory, "gh": gh, "dispatch": dispatch,
                        "sh": fake_sh,
                        "remote": lambda *a: {"lane": "idle" if index == 2 else "busy"},
                        "audit_switches": lambda: None,
                        "paused_scopes": lambda **k: {"mint"},
                        "log": lambda *a, **k: None,
                    }
                    for name, value in replacements.items():
                        stack.enter_context(patch.object(module, name, value))
                    stack.enter_context(patch.object(module.time, "time", return_value=self.NOW + elapsed))
                    stack.enter_context(patch.object(module.sys, "argv", ["autodispatch.py", "--once"]))
                    module.main()
                state = json.loads((root / "state.json").read_text())
                self.assertLessEqual(len(reads), module.FRESH_READ_BUDGET)
                if index < 2:
                    self.assertEqual(state["_pool_entered_at"].get("1705"), self.NOW)
                else:
                    self.assertNotIn("1705", state["_pool_entered_at"])
            self.assertEqual(dispatched, [1705])


if __name__ == "__main__":
    unittest.main()
