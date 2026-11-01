#!/usr/bin/env python3
"""Offline contract for stale autodispatch observations (Refs #1102)."""

import importlib.util
from pathlib import Path


def main() -> None:
    path = Path(__file__).parent / "lane-autodispatch" / "autodispatch.py"
    spec = importlib.util.spec_from_file_location("autodispatch", path)
    assert spec and spec.loader
    autodispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(autodispatch)

    # Deliberately fake state: this check must not inspect or write live GH/AD state.
    autodispatch.note_stale_skip()
    autodispatch.note_stale_skip()
    status = autodispatch.status_snapshot(
        {"CX1": {"host": "fake"}}, {}, {"CX1": {"lane": "idle"}}, 0, 0
    )
    value = status.get("_stale_stripped_n")
    assert value == 2 and isinstance(value, int) and not isinstance(value, bool) and value >= 0, (
        "lanes-status must expose a non-negative integer _stale_stripped_n"
    )
    print("stale-skip lanes-status observation: OK")


if __name__ == "__main__":
    main()
