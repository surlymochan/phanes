#!/usr/bin/env python3
"""Offline contract for queued Codex work blocked by provider throttling (Refs #988)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# A queued item may wait for a provider retry window, but it must not remain
# silent forever. The control plane should reclaim it after this deadline.
QUEUED_PROVIDER_RECLAIM_MINUTES = 30


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def provider_throttled(status: dict[str, object]) -> bool:
    """Return whether a queued status carries a provider rate-limit reason."""
    reason = str(status.get("queue_reason", "")).lower()
    return any(term in reason for term in ("provider", "rate limit", "rate_limit", "429", "throttl"))


def should_reclaim(status: dict[str, object], now: datetime) -> bool:
    """Return whether stale queued provider work should be recycled."""
    if status.get("status") != "queued" or not provider_throttled(status):
        return False
    queued_at = status.get("queued_at")
    heartbeat_at = status.get("heartbeat_at")
    if not isinstance(queued_at, str) or not isinstance(heartbeat_at, str):
        return False
    age = now - parse_timestamp(queued_at)
    heartbeat_age = now - parse_timestamp(heartbeat_at)
    return age > timedelta(minutes=QUEUED_PROVIDER_RECLAIM_MINUTES) and heartbeat_age > timedelta(
        minutes=QUEUED_PROVIDER_RECLAIM_MINUTES
    )


def main() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    stale = {
        "status": "queued",
        "queue_reason": "provider rate limit (429)",
        "queued_at": "2026-01-01T11:00:00Z",
        "heartbeat_at": "2026-01-01T11:00:00Z",
    }
    fresh_heartbeat = {**stale, "heartbeat_at": "2026-01-01T11:45:00Z"}
    other_queue_reason = {**stale, "queue_reason": "waiting for lane"}

    assert should_reclaim(stale, now), "stale queued provider throttling must be reclaimed"
    assert not should_reclaim(fresh_heartbeat, now), "a fresh heartbeat must keep queued work"
    assert not should_reclaim(other_queue_reason, now), "non-provider queue reasons are not this contract"

    print(
        "OK: queued provider-throttled work is reclaimed after "
        f"{QUEUED_PROVIDER_RECLAIM_MINUTES} minutes without a heartbeat"
    )
    print("OK: fresh heartbeat and non-provider queue reasons are preserved")


if __name__ == "__main__":
    main()
