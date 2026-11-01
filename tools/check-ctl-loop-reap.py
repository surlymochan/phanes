#!/usr/bin/env python3
"""Offline contract for ctl-loop child reaping and restart (Refs #951).

The production ``~/phanes-ctl/bin/ctl-loop`` uses the same inputs: one pid file
and one heartbeat file per child.  This check uses fake files and a supplied
set of live pids, so it never signals or starts a real process.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import time


HEARTBEAT_MAX_AGE = 120


@dataclass(frozen=True)
class Child:
    name: str
    heartbeat: Path
    pid_file: Path


@dataclass(frozen=True)
class Restart:
    name: str
    reason: str
    terminate_existing: bool


def read_pid(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def heartbeat_stale(path: Path, now: float, max_age: float = HEARTBEAT_MAX_AGE) -> bool:
    try:
        return now - path.stat().st_mtime > max_age
    except OSError:
        return True


def reap_decisions(children: list[Child], now: float, live_pids: set[int]) -> list[Restart]:
    """Return one restart decision per unhealthy child, never a duplicate."""
    decisions: list[Restart] = []
    for child in children:
        pid = read_pid(child.pid_file)
        stale = heartbeat_stale(child.heartbeat, now)
        alive = pid is not None and pid in live_pids
        if not stale and alive:
            continue
        if stale and alive:
            reason = "heartbeat expired"
        elif pid is None:
            reason = "pid missing or invalid"
        else:
            reason = "pid exited"
        # A live old pid must be terminated/reaped before its replacement is
        # spawned; this is the duplicate-instance guard.
        decisions.append(Restart(child.name, reason, terminate_existing=alive))
    return decisions


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ctl-loop-reap-", dir=Path(__file__).parent) as fixture:
        root = Path(fixture)
        now = time.time()
        children = [
            Child(name, root / f"{name}.heartbeat", root / f"{name}.pid")
            for name in ("ad", "health", "ib-refresh")
        ]
        for index, child in enumerate(children, start=1):
            child.heartbeat.touch()
            child.pid_file.write_text(str(1000 + index), encoding="utf-8")

        # Healthy children are left alone.
        assert reap_decisions(children, now, {1001, 1002, 1003}) == []

        # An expired heartbeat requests exactly one restart, even if its pid
        # still exists; ctl-loop must reap that instance first.
        old = now - HEARTBEAT_MAX_AGE - 1
        os.utime(children[0].heartbeat, (old, old))
        decisions = reap_decisions(children, now, {1001, 1002, 1003})
        assert decisions == [Restart("ad", "heartbeat expired", True)]

        # A disappeared pid also requests one restart, without starting a
        # second instance for any of the healthy children.
        children[0].heartbeat.touch()
        children[1].pid_file.unlink()
        decisions = reap_decisions(children, now, {1001, 1003})
        assert decisions == [Restart("health", "pid missing or invalid", False)]

        # A stale heartbeat plus a dead pid is still one decision, not two.
        children[1].pid_file.write_text("1002", encoding="utf-8")
        children[2].heartbeat.touch()
        os.utime(children[2].heartbeat, (old, old))
        children[2].pid_file.write_text("1003", encoding="utf-8")
        decisions = reap_decisions(children, now, {1001, 1002})
        assert decisions == [Restart("ib-refresh", "pid exited", False)]

        print("OK: fresh heartbeat + live pid does not restart")
        print("OK: expired heartbeat restarts once and reaps a live old pid")
        print("OK: missing/exited pid restarts once without duplicate instances")


if __name__ == "__main__":
    main()
