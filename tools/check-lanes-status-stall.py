#!/usr/bin/env python3
"""Compatibility entrypoint for the lanes-status probe bundled with swrm."""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "swrm" / "bin"))

# Preserve the probe's importable API as well as its command-line entrypoint.
from swrm_lanes_health import *  # noqa: F401,F403,E402


if __name__ == "__main__":
    raise SystemExit(main())
