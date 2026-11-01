#!/usr/bin/env python3
"""Offline contract for the dispatch lane order (Refs #1360).

`dispatch.lane_pick` decides which idle lane gets the next card: `lru` hands it to the
lane that was dispatched longest ago (a lane that never ran counts as never dispatched),
`first` keeps the order of the lanes configuration. A paused lane is never dispatched.

The fixture is entirely local: the autodispatcher is driven with stubbed gh/ssh/dispatch
calls, the state directories are temporary, and nothing outside them is written.
"""

import importlib.util
import json
import os
import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"


def load_autodispatch():
    """Import the real autodispatcher so the contract is checked against shipped code."""
    spec = importlib.util.spec_from_file_location("autodispatch_lane_lru_contract", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub(module, dispatched):
    """Replace every side effect of a tick: no gh, no ssh, no dispatch, no logs."""
    module.log = lambda *args, **kwargs: None
    module.gh = lambda args, timeout=60: (0, "", "")
    module.sh = lambda cmd, **kwargs: (0, "base-sha\n", "")
    module.remote = lambda lane, cfg, run="": {"lane": "idle"}
    module.queue = lambda: [{"n": 7, "title": "card", "body": "", "prio": 3,
                              "backend": None, "strict": False, "deps_ok": True}]
    module.deps_met = lambda item: True
    module.prefetch_labels = lambda numbers, cache=None: None
    module.label_state = lambda n, cache=None: ("fresh", {"state": "OPEN",
                                                           "labels": [{"name": "lane-ready"}]})
    module.replenish = lambda count, state: []

    def fake_dispatch(lane, cfg, item, base):
        dispatched.append(lane)
        return f"run-{lane}", "ok"

    module.dispatch = fake_dispatch


def tick_once(module, lanes):
    """Run one tick and return the lanes that received a card."""
    dispatched = []
    stub(module, dispatched)
    module.tick(lanes, {}, 0)
    return dispatched


def write_lru(home, recent):
    pathlib.Path(home, "lane-lru.json").write_text(json.dumps({"recent": recent}))


def main():
    module = load_autodispatch()
    # the configuration order is lane-a, lane-b: only the lru order prefers lane-b
    lanes = {"lane-a": {"host": "host-a", "backend": None}, "lane-b": {"host": "host-b", "backend": None}}
    with tempfile.TemporaryDirectory() as workdir:
        module.D = os.path.join(workdir, "ad")  # the AD state dir never leaves the temp dir
        os.makedirs(module.D)
        home = os.path.join(workdir, "home")
        os.makedirs(home)
        os.environ["OPENSWARM_HOME"] = home

        # lane-a was dispatched last, so lane-b (idle the longest) gets the next card
        write_lru(home, ["lane-b", "lane-a"])
        assert tick_once(module, lanes) == ["lane-b"], "lru must pick the least recently dispatched lane"
        state = json.loads(pathlib.Path(home, "lane-lru.json").read_text())
        assert state["recent"] == ["lane-a", "lane-b"], "the dispatched lane must be recorded"

        # a paused lane is skipped, even when the lru order prefers it
        write_lru(home, ["lane-b", "lane-a"])
        pathlib.Path(module.D, "PAUSE-lane-b").write_text("")
        assert tick_once(module, lanes) == ["lane-a"], "a paused lane must never be dispatched"
        os.unlink(pathlib.Path(module.D, "PAUSE-lane-b"))

        # `first` keeps the lanes configuration order, whatever the lru state says
        write_lru(home, ["lane-b", "lane-a"])
        pathlib.Path(home, "policy.local.json").write_text(json.dumps({"dispatch": {"lane_pick": "first"}}))
        assert tick_once(module, lanes) == ["lane-a"], "first must keep the configuration order"
        os.unlink(pathlib.Path(home, "policy.local.json"))

        # a lane that never ran counts as never dispatched, so it comes first
        write_lru(home, ["lane-a"])
        assert tick_once(module, lanes) == ["lane-b"], "a lane that never ran must come first"

    print("ad dispatch lane order contract: OK")


if __name__ == "__main__":
    main()
