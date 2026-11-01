#!/usr/bin/env python3
"""Offline check for a stale lane-autodispatch heartbeat.

The normal check only reads the heartbeat file; it never starts or stops
autodispatch.  ``--fixture-*`` keeps the check self-contained for smoke tests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


DEFAULT_HEARTBEAT = Path("~/phanes-ctl/autodispatch/heartbeat").expanduser()
DEFAULT_THRESHOLD_MINUTES = 20


def parse_timestamp(value: str) -> datetime:
    """Parse autodispatch's ``%F %T`` or an ISO-8601 heartbeat timestamp."""
    text = value.strip()
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        timestamp = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return timestamp.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report a silent/stalled lane-autodispatch heartbeat without touching AD."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--heartbeat-file", type=Path, default=DEFAULT_HEARTBEAT)
    source.add_argument("--fixture-old", action="store_true", help="use a heartbeat older than the threshold")
    source.add_argument("--fixture-fresh", action="store_true", help="use a current heartbeat")
    parser.add_argument(
        "--threshold-minutes",
        type=float,
        default=DEFAULT_THRESHOLD_MINUTES,
        help=f"maximum heartbeat age in minutes (default: {DEFAULT_THRESHOLD_MINUTES})",
    )
    args = parser.parse_args()
    if args.threshold_minutes <= 0:
        parser.error("--threshold-minutes must be greater than zero")

    now = datetime.now(timezone.utc)
    if args.fixture_old:
        heartbeat = now - timedelta(minutes=args.threshold_minutes + 1)
        source_name = "fixture-old"
    elif args.fixture_fresh:
        heartbeat = now
        source_name = "fixture-fresh"
    else:
        source_name = str(args.heartbeat_file)
        try:
            heartbeat = parse_timestamp(args.heartbeat_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"WARNING: autodispatch heartbeat missing: {args.heartbeat_file}", file=sys.stderr)
            return 2
        except (OSError, ValueError) as exc:
            print(f"WARNING: autodispatch heartbeat unreadable ({args.heartbeat_file}): {exc}", file=sys.stderr)
            return 2

    age = max(0.0, (now - heartbeat).total_seconds() / 60)
    if age > args.threshold_minutes:
        print(
            f"WARNING: autodispatch heartbeat stalled/silent for {age:.1f} minutes "
            f"(threshold {args.threshold_minutes:g} minutes; source {source_name})",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: autodispatch heartbeat fresh ({age:.1f} minutes old; "
        f"threshold {args.threshold_minutes:g} minutes; source {source_name})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
