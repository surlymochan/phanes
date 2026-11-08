#!/usr/bin/env python3
"""Regression coverage for dispatch-priority starvation (Refs #1705)."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "autodispatch", Path(__file__).with_name("autodispatch.py")
)
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


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


if __name__ == "__main__":
    unittest.main()
