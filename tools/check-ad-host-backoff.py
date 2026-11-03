#!/usr/bin/env python3
"""Offline contract check for the tiered per-host autodispatch backoff (Refs #967, #1689).

The check imports the runtime functions but replaces logging and the clock, so
it never reads or writes the real autodispatch state files.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"


def load_runtime():
    spec = importlib.util.spec_from_file_location("autodispatch_runtime", RUNTIME)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RUNTIME}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    runtime = load_runtime()
    runtime.BACKOFF.clear()
    runtime.log = lambda *args, **kwargs: None
    original_time = runtime.time.time
    now = 1_000_000.0
    runtime.time.time = lambda: now
    try:
        # the soft tier: short retries (30s, 60s) ...
        for wait in (30, 60):
            runtime.host_fail("ad-a.example", "synthetic transient failure")
            assert runtime.BACKOFF["ad-a.example"]["until"] == now + wait, wait
            assert runtime.BACKOFF["ad-a.example"]["tier"] == "soft"
            assert not runtime.host_ok("ad-a.example")

        # ... and from the limit on the hard tier: long waits (240s, 480s, 600s cap)
        for wait in (240, 480, 600, 600):
            runtime.host_fail("ad-a.example", "synthetic transient failure")
            assert runtime.BACKOFF["ad-a.example"]["until"] == now + wait, wait
            assert runtime.BACKOFF["ad-a.example"]["tier"] == "hard"

        # the hard tier keeps a low-frequency probe, but no dispatch
        assert not runtime.host_ok("ad-a.example")
        now = runtime.BACKOFF["ad-a.example"]["until"]
        assert runtime.host_ok("ad-a.example")
        assert not runtime.host_dispatchable("ad-a.example")

        # one success clears the streak: the host is never judged by its peak
        runtime.host_good("ad-a.example")
        assert "ad-a.example" not in runtime.BACKOFF
        assert runtime.host_ok("ad-a.example")
        assert runtime.host_dispatchable("ad-a.example")

        runtime.host_fail("ad-a.example", "synthetic failure after recovery")
        assert runtime.BACKOFF["ad-a.example"]["n"] == 1
        assert runtime.BACKOFF["ad-a.example"]["until"] == now + 30

        # the backoff state and the next probe time are visible in the lane status
        rows = runtime.host_backoff_rows()
        assert rows["ad-a.example"]["tier"] == "soft", rows
        assert rows["ad-a.example"]["streak"] == 1, rows
        assert rows["ad-a.example"]["next_probe_at"] == now + 30, rows

        # the host is judged by its ssh results, not by its lanes: one lane that
        # answered is enough to clear the streak (Refs #1689)
        assert runtime.host_reachable({"A1": {"lane": "idle"},
                                        "A2": {"error": "Connection closed"}})
        assert not runtime.host_reachable({"A1": {"error": "Connection closed"},
                                           "A2": {"error": "ssh status timeout"}})
        assert runtime.host_reachable({"A1": {"error": "host in backoff", "skip": True}})
    finally:
        runtime.time.time = original_time
        runtime.BACKOFF.clear()

    print("ad host backoff: soft 30s/60s -> hard 240s/480s/600s cap -> host_good reset "
          "-> host verdict by ssh results: OK")


if __name__ == "__main__":
    main()
