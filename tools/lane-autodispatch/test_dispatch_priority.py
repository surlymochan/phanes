#!/usr/bin/env python3
"""Regression coverage for dispatch-priority starvation (Refs #1705)."""

import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "autodispatch", Path(__file__).with_name("autodispatch.py")
)
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class DispatchPriorityTest(unittest.TestCase):
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
