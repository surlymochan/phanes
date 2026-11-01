#!/usr/bin/env python3
"""Offline regression check for issue-bus claim lease expiry (Refs #1028).

使用固定时钟和伪造评论，确认过期 claim 不会永久锁死任务，且其它 worker
只能在租约过期后接管。
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


def load_openswarm():
    source = Path(__file__).resolve().parents[1] / "openswarm" / "bin" / "openswarm"
    loader = importlib.machinery.SourceFileLoader("openswarm_claim_lease_check", str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def fake_claim(worker, created_at, comment_id):
    return {
        "id": comment_id,
        "body": f"/claim {worker}",
        "user": {"login": worker},
        "created_at": created_at,
    }


def main():
    bus = load_openswarm()
    allowed = {"worker-a", "worker-b"}
    lease_seconds = 60
    comments = [
        fake_claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
        fake_claim("worker-b", "2026-01-01T00:00:30+00:00", 2),
    ]

    # A different worker cannot take over while the first lease is active.
    active = bus.bus_winning_claim(
        comments, allowed, lease_seconds, bus.to_epoch("2026-01-01T00:00:45+00:00")
    )
    assert active["id"] == 1, "an unexpired claim must remain exclusive"

    # Once the first lease expires, the later fake comment can take over.
    takeover = bus.bus_winning_claim(
        comments, allowed, lease_seconds, bus.to_epoch("2026-01-01T00:01:01+00:00")
    )
    assert takeover["id"] == 2, "an expired claim must allow another worker to take over"

    # An expired claim by itself leaves the task available for recovery.
    expired_only = bus.bus_winning_claim(
        comments[:1], allowed, lease_seconds, bus.to_epoch("2026-01-01T00:01:00+00:00")
    )
    assert expired_only is None, "an expired lone claim must not remain an active lock"

    print("ok: expired issue-bus claims can be taken over by another worker")


if __name__ == "__main__":
    main()
