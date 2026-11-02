#!/usr/bin/env python3
"""Offline replay of the 2026-11-02 review sequence (Refs #1701).

Context (2026-11-02 evening, measured on the control plane):
    the reviews only ran on the three terra lanes CX2/CX4/CX5, but the dispatch
    picked the same lane again and again: one lane reviewed round after round
    while the other two stood idle, so the PRs queued for a verdict and the
    CHANGES rounds dragged on.

Why the old tie-break piled up:
    the reviewer was the lane with the oldest counter of the *shared* dispatch
    history (`lane-lru.json`).  The fix tasks of the CHANGES rounds were written
    into the same history, so the lanes that took fix tasks were never the
    oldest one and the lane that only reviewed won every time.

This check replays that night against the real dispatcher code
(`openswarm/lib/dispatch/engine.py` + `lib/dispatch/review_balance.py`) and
asserts that the backlog balance spreads the reviews over the three lanes:

    * the per-lane backlog spread stays at 1 (no lane runs away with the queue),
    * no lane takes two reviews in a row while another lane was free,
    * the legacy tie-break is still the piling one (the regression is visible).

The replay is offline: no GitHub, no SSH, no credentials.  The dispatcher state
lives in a temporary directory that is removed afterwards.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "openswarm" / "lib" / "dispatch" / "engine.py"

LANES = {"CX2": {"host": "govpn2", "backend": "codex"},
         "CX4": {"host": "govpn2", "backend": "codex"},
         "CX5": {"host": "govpn", "backend": "codex"}}
# The night: one review per tick, the fix tasks of the day share the dispatch history.
NIGHT = 6


def load_engine():
    spec = importlib.util.spec_from_file_location("dispatch_engine_replay", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_dispatcher(engine, state_dir):
    os.makedirs(state_dir, exist_ok=True)
    config = {"repo": "acme/project", "local_repo": state_dir, "state_dir": state_dir,
              "ssh": {"control_dir": os.path.join(state_dir, "cm")}, "local_hosts": ["box"]}
    path = os.path.join(state_dir, "dispatch.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(config, stream)
    return engine.Dispatcher(engine.load_config(path), runner=lambda *a, **k: (0, "", ""),
                              out=open(os.devnull, "w"))


def legacy_pick(dispatcher, idle):
    """The tie-break the dispatcher used before the backlog balance."""
    picker = dispatcher.lru_picker()
    oldest = picker.pick(idle)
    history = picker.state()["lanes"]
    tied = [lane for lane in idle if history.get(lane) == history.get(oldest)]
    return min(tied, key=lambda lane: (dispatcher.R["pref"].get(LANES[lane]["backend"], 9), lane))


def replay(dispatcher, legacy=False):
    """Replay the night and return the lane of every review."""
    chosen = []
    claimed = []
    for tick in range(NIGHT):
        for lane in ("CX4", "CX5"):        # the fix tasks of the day share the dispatch history
            dispatcher.lru_picker().touch(lane)
        for number in claimed:             # the review of the previous tick concluded
            dispatcher.release_claim(number)
        claimed = []
        idle = list(LANES)
        counts = dispatcher.review_backlog(LANES, {}, dispatcher.load_reviews())
        lane = legacy_pick(dispatcher, idle) if legacy else dispatcher.pick_reviewer(idle, LANES, None, counts)
        if lane is None:
            continue
        chosen.append(lane)
        dispatcher.lru_picker().touch(lane)
        if not legacy:
            dispatcher.review_picker().touch(lane)
            dispatcher.merge_review(1600 + tick, {"pending_lane": lane})
            claimed = [1600 + tick]
    return chosen


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    engine = load_engine()
    state_dir = tempfile.mkdtemp(prefix="review-balance-replay-")
    try:
        dispatcher = make_dispatcher(engine, state_dir)
        chosen = replay(dispatcher)
        legacy = replay(make_dispatcher(engine, os.path.join(state_dir, "legacy")), legacy=True)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)

    spread = {lane: chosen.count(lane) for lane in LANES}
    print(f"reviews per lane: {spread}")
    print(f"review order:      {' '.join(chosen)}")
    print(f"legacy order:      {' '.join(legacy)}")

    check(len(chosen) == NIGHT, f"the replay lost reviews: {chosen}")
    check(max(spread.values()) - min(spread.values()) <= 1,
          f"the backlog did not converge, one lane piled up: {spread}")
    for previous, lane in zip(chosen, chosen[1:]):
        check(previous != lane, f"one lane took the reviews one after the other: {chosen}")
    check(len(set(legacy)) == 1 and legacy[0] == "CX2",
          f"the legacy tie-break does not pile up any more (regression gone?): {legacy}")
    print("review balance replay: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: {error}")
        raise SystemExit(1)
