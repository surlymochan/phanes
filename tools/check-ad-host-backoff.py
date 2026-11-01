#!/usr/bin/env python3
"""Offline contract check for per-host autodispatch backoff (Refs #967).

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
        expected = [30, 60, 120, 240, 480, 600, 600]
        for wait in expected:
            runtime.host_fail("ad-a.example", "synthetic transient failure")
            assert runtime.BACKOFF["ad-a.example"]["until"] == now + wait
            assert not runtime.host_ok("ad-a.example")

        runtime.host_good("ad-a.example")
        assert "ad-a.example" not in runtime.BACKOFF
        assert runtime.host_ok("ad-a.example")

        runtime.host_fail("ad-a.example", "synthetic failure after recovery")
        assert runtime.BACKOFF["ad-a.example"]["n"] == 1
        assert runtime.BACKOFF["ad-a.example"]["until"] == now + 30
    finally:
        runtime.time.time = original_time
        runtime.BACKOFF.clear()

    print("ad host backoff: 30s -> 600s cap -> host_good reset: OK")


if __name__ == "__main__":
    main()
