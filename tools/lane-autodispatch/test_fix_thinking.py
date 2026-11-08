#!/usr/bin/env python3
"""The thinking level of a pi fix round (Refs #1847).

A fix round - the card that applies the notes of a review - runs at the thinking level of its
round: the first round at "high", the later rounds step down, because a later round only has to
apply the notes. Only pi lanes get a level (the other executors have no such flag), and a review
card keeps the level of its lane, so the review rules stay untouched.
"""
import importlib.util
import json
import tempfile
import unittest
import warnings
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"  # gitignored scratch root


def card(body="", verify=False):
    """One queue row as the dispatcher sees it."""
    return {"n": 7, "title": "card", "body": body, "prio": 1, "backend": None, "strict": False,
            "verify": verify}


def round_body(round_no):
    """The body of a fix-round card: the marker the review flow writes into the issue body."""
    return (f"Review round {round_no} (lane CX2) on PR #12 head `abcdef0123` returned CHANGES. "
            "Start from branch `rebase-pr-12`, rebase onto main and apply the notes below.")


class FixThinkingLevelTest(unittest.TestCase):
    """The Nth fix round takes the Nth level; only pi lanes and only fix rounds get one."""

    def test_the_first_fix_round_runs_at_high(self):
        self.assertEqual(AUTODISPATCH.fix_thinking({"backend": "pi"}, card(round_body(1))), "high")

    def test_the_level_steps_down_with_the_round(self):
        self.assertEqual(AUTODISPATCH.fix_thinking({"backend": "pi"}, card(round_body(2))), "medium")
        self.assertEqual(AUTODISPATCH.fix_thinking({"backend": "pi"}, card(round_body(3))), "low")

    def test_the_last_level_covers_every_further_round(self):
        for round_no in (4, 9, 40):
            with self.subTest(round=round_no):
                self.assertEqual(AUTODISPATCH.fix_thinking({"backend": "pi"}, card(round_body(round_no))), "low")

    def test_the_highest_round_of_a_card_wins(self):
        body = round_body(1) + "\n" + round_body(2)
        self.assertEqual(AUTODISPATCH.fix_thinking({"backend": "pi"}, card(body)), "medium")

    def test_the_other_backends_keep_their_own_level(self):
        for backend in ("codex", "agy", "cursor"):
            with self.subTest(backend=backend):
                self.assertIsNone(AUTODISPATCH.fix_thinking({"backend": backend}, card(round_body(1))))

    def test_a_review_card_keeps_the_level_of_the_lane(self):
        self.assertIsNone(AUTODISPATCH.fix_thinking({"backend": "pi"}, card(round_body(1), verify=True)))

    def test_an_ordinary_card_gets_no_level(self):
        self.assertIsNone(AUTODISPATCH.fix_thinking({"backend": "pi"}, card("普通工作卡")))


class DispatchThinkingFlagTest(unittest.TestCase):
    """The assign of a pi fix round carries the level; the other assigns do not."""

    def setUp(self):
        AUTODISPATCH.NO_THINKING_HOSTS.clear()
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        AUTODISPATCH.NO_THINKING_HOSTS.clear()

    def dispatch(self, cfg, item, answers=None):
        """Run one dispatch with the ssh side effects faked; returns (commands, logs)."""
        commands, logs = [], []
        answers = list(answers or [])
        old_sh, old_d, old_log = AUTODISPATCH.sh, AUTODISPATCH.D, AUTODISPATCH.log
        try:
            with tempfile.TemporaryDirectory(dir=SCRATCH) as scratch:
                AUTODISPATCH.D = scratch
                AUTODISPATCH.log = lambda msg, brain=True: logs.append(msg)

                def fake_sh(cmd, timeout=120, inp=None):
                    commands.append(cmd if isinstance(cmd, str) else " ".join(cmd))
                    if answers:
                        return answers.pop(0)
                    return (0, json.dumps({"id": "run-1"}), "")

                AUTODISPATCH.sh = fake_sh
                # the task file of the dispatch is not closed by the dispatcher: the warning is
                # pre-existing
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    AUTODISPATCH.dispatch("PI1", cfg, item, "b" * 40)
        finally:
            AUTODISPATCH.sh, AUTODISPATCH.D, AUTODISPATCH.log = old_sh, old_d, old_log
        return commands, logs

    def assigns(self, commands):
        return [command for command in commands if "lane assign" in command]

    def test_the_assign_of_a_pi_fix_round_carries_the_level(self):
        commands, _ = self.dispatch({"host": "h1", "backend": "pi"}, card(round_body(1)))
        self.assertIn("--thinking high", " ".join(self.assigns(commands)))

    def test_an_ordinary_card_is_assigned_without_a_level(self):
        commands, _ = self.dispatch({"host": "h1", "backend": "pi"}, card("普通工作卡"))
        self.assertNotIn("--thinking", " ".join(self.assigns(commands)))

    def test_a_codex_lane_is_assigned_without_a_level(self):
        commands, _ = self.dispatch({"host": "h1", "backend": "codex"}, card(round_body(1)))
        self.assertNotIn("--thinking", " ".join(self.assigns(commands)))

    def test_a_host_without_the_flag_runs_the_round_at_the_level_of_the_lane(self):
        # A lane host on an older release rejects the flag: the round still runs, without it.
        commands, logs = self.dispatch({"host": "h1", "backend": "pi"}, card(round_body(1)), answers=[
            (0, "", ""),                                                  # the task upload
            (1, "", "swrm: error: unrecognized arguments: --thinking high"),
            (0, json.dumps({"id": "run-1"}), ""),                         # the retry without the flag
        ])
        assigns = self.assigns(commands)
        self.assertEqual(len(assigns), 2)
        self.assertIn("--thinking high", assigns[0])
        self.assertNotIn("--thinking", assigns[1])
        self.assertTrue(any("does not know --thinking" in line for line in logs))
        # the host is not asked again for a while
        self.assertFalse(AUTODISPATCH.host_knows_thinking("h1"))
        self.assertTrue(AUTODISPATCH.host_knows_thinking("h2"))


if __name__ == "__main__":
    unittest.main()
