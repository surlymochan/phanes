#!/usr/bin/env python3
"""Offline contract for the autodispatch stale-skip observation field."""

from __future__ import annotations

import json
from typing import Any


def check_stale_observation(status: dict[str, Any]) -> None:
    """Require a process-local, non-negative integer stale-skip counter."""
    value = status.get("_stale_stripped_n")
    assert type(value) is int and value >= 0, (
        f"_stale_stripped_n must be a non-negative integer, got {value!r}"
    )


def main() -> None:
    # Deliberately fake state: this check must not read or modify real GH/AD state.
    status = json.loads(
        '{"CX1":"idle","_busy":0,"_ready":2,"_blocked":0,"_stale_stripped_n":3}'
    )
    check_stale_observation(status)
    print("ad stale observation contract: OK")


if __name__ == "__main__":
    main()
