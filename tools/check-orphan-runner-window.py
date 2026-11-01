#!/usr/bin/env python3
"""Offline contract check for the post-assign orphan runner window.

The fixture models the remote lane status consumed by the control plane.  It
does not connect to a host or mutate the live dispatch state.
"""
from copy import deepcopy


ORPHAN_WINDOW_SECONDS = 120


def reclaim_orphan(state, now, window_seconds=ORPHAN_WINDOW_SECONDS):
    """Recycle an assigned task when its runner never became alive."""
    assigned_at = state.get("assigned_at")
    if assigned_at is None:
        return False
    if state.get("runner_alive") is not False:
        return False
    if state.get("run_state") != "running":
        return False
    if now - assigned_at <= window_seconds:
        return False

    state["run_state"] = "orphaned"
    state["assignment"] = None
    state["labels"] = ["lane-ready"]
    return True


def main():
    assigned = {
        "assigned_at": 1000,
        "runner_alive": False,
        "run_state": "running",
        "assignment": {"lane": "cx5", "task": "#961"},
        "labels": [],
    }

    before_window = deepcopy(assigned)
    assert not reclaim_orphan(before_window, 1000 + ORPHAN_WINDOW_SECONDS)
    assert before_window == assigned

    alive = deepcopy(assigned)
    alive["runner_alive"] = True
    assert not reclaim_orphan(alive, 1000 + ORPHAN_WINDOW_SECONDS + 1)
    assert alive == {**assigned, "runner_alive": True}

    reclaimed = deepcopy(assigned)
    assert reclaim_orphan(reclaimed, 1000 + ORPHAN_WINDOW_SECONDS + 1)
    assert reclaimed["run_state"] == "orphaned"
    assert reclaimed["assignment"] is None
    assert reclaimed["labels"] == ["lane-ready"]

    finished = deepcopy(assigned)
    finished["run_state"] = "completed"
    assert not reclaim_orphan(finished, 1000 + ORPHAN_WINDOW_SECONDS + 1)

    print("orphan runner window contract: OK")


if __name__ == "__main__":
    main()
