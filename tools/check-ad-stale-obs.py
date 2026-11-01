#!/usr/bin/env python3
"""Offline contract for the autodispatch stale-search observation field (Refs #1022)."""

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
    runtime.STALE_STRIPPED_N = 4
    fake_state = {"CX1": {"issue": 1022}}
    fake_lanes = {"CX1": {"host": "fake", "backend": "codex"}, "CX2": {"host": "fake", "backend": "codex"}}
    status = runtime.status_snapshot(
        fake_lanes,
        fake_state,
        {"CX1": {}, "CX2": {"lane": "idle"}},
        ready=0,
        blocked=0,
    )
    assert "_stale_stripped_n" in status
    assert isinstance(status["_stale_stripped_n"], int)
    assert status["_stale_stripped_n"] >= 0
    print("ad stale-skip observation contract: OK")


if __name__ == "__main__":
    main()
