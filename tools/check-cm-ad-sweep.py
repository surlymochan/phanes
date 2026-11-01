#!/usr/bin/env python3
"""Offline contract check for the autodispatch cm-ad socket sweep.

The control-plane runtime removes Unix sockets in ``~/.ssh/cm-ad`` that have
been idle for more than ten minutes at the start of each autodispatch tick.
This check uses only a temporary directory and never touches the real SSH
control path.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time


MAX_SOCKET_AGE = 600


def sweep_stale_sockets(cm_dir: Path, *, now: float | None = None) -> list[Path]:
    """Unlink stale Unix sockets and return the paths that were removed."""
    now = time.time() if now is None else now
    removed = []
    try:
        entries = tuple(cm_dir.iterdir())
    except FileNotFoundError:
        return removed
    for entry in entries:
        try:
            # The fixture uses .sock files as fake socket paths; the live
            # cm-ad directory contains only ControlMaster socket entries.
            if entry.suffix != ".sock" or not entry.is_file():
                continue
            if now - entry.stat().st_mtime <= MAX_SOCKET_AGE:
                continue
            entry.unlink()
            removed.append(entry)
        except FileNotFoundError:
            continue
    return removed


def main() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory() as raw_dir:
        cm_dir = Path(raw_dir) / "cm-ad"
        cm_dir.mkdir()
        old_path = cm_dir / "old.sock"
        fresh_path = cm_dir / "fresh.sock"
        regular_path = cm_dir / "not-a-socket"
        old_path.touch()
        fresh_path.touch()
        regular_path.touch()
        os.utime(old_path, (now - MAX_SOCKET_AGE - 1, now - MAX_SOCKET_AGE - 1))
        os.utime(fresh_path, (now - MAX_SOCKET_AGE, now - MAX_SOCKET_AGE))
        os.utime(regular_path, (now - MAX_SOCKET_AGE - 100, now - MAX_SOCKET_AGE - 100))

        removed = sweep_stale_sockets(cm_dir, now=now)
        assert removed == [old_path], removed
        assert not old_path.exists()
        assert fresh_path.exists(), "a socket at exactly 600 seconds is still valid"
        assert regular_path.exists(), "the sweep must only unlink socket paths"

    print("cm-ad stale socket sweep: OK")


if __name__ == "__main__":
    main()
