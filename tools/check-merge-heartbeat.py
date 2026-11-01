#!/usr/bin/env python3
"""lane-merge / merge-loop: merge-heartbeat and merge-green contract check (Refs #929).

Asserts:
1. Heartbeat path contract: default ~/phanes-ctl/autodispatch/merge-heartbeat.
2. merge-green evaluation: Linux-green check suite and mergeability assertion.
3. No-PR behavior: when no PR is pending or green, loop records heartbeat and skips cleanly.
4. Non-starvation: failing candidate PR does not starve the queue for subsequent PRs.
5. Mock/fake directory contract assertions without touching live environment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile


DEFAULT_HEARTBEAT = Path("~/phanes-ctl/autodispatch/merge-heartbeat").expanduser()
DEFAULT_THRESHOLD_MINUTES = 20
REQUIRED_DOC_TERMS = (
    "lane-merge",
    "merge-loop",
    "merge-heartbeat",
    "merge-green",
)


def parse_timestamp(value: str) -> datetime:
    """Parse merge-loop's timestamp (ISO-8601 or %Y-%m-%d %H:%M:%S)."""
    text = value.strip()
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        timestamp = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def is_merge_green(pr: dict) -> tuple[bool, str]:
    """Evaluate whether a PR meets the merge-green criteria.

    Contract:
    - State must be open.
    - Mergeable state must be true/MERGEABLE (no conflicts).
    - Linux checks must be green/passing. macOS and other non-blocking checks
      do not block the green decision.
    """
    if pr.get("state") != "open":
        return False, f"pr is not open (state={pr.get('state')!r})"

    mergeable = pr.get("mergeable")
    if mergeable not in (True, "MERGEABLE", "clean", "has_hooks", "unstable"):
        return False, f"pr is not mergeable (mergeable={mergeable!r})"

    # Check Linux checks status
    checks = pr.get("checks", {})
    if isinstance(checks, dict) and checks:
        linux_status = checks.get("linux")
        if linux_status is None:
            # check list of checks
            check_list = pr.get("status_checks", [])
            for c in check_list:
                name = c.get("name", "").lower()
                if "linux" in name and c.get("status") not in ("success", "pass", "green"):
                    return False, f"linux check {c.get('name')} is {c.get('status')}"
        elif linux_status not in ("success", "pass", "green"):
            return False, f"linux check status is {linux_status!r}"
    elif pr.get("linux_green") is False:
        return False, "linux checks are not green"

    return True, "green"


def merge_loop_tick(
    queue: list[dict],
    heartbeat_path: Path,
    merge_fn=None,
    now: datetime | None = None,
) -> dict:
    """Simulate one iteration of the lane-merge / merge-loop tick.

    Contract:
    - Touches/updates merge-heartbeat on every tick.
    - If queue is empty or no PR is green: skips cleanly without error.
    - If a candidate PR fails merge: records failure, skips to next PR;
      does not starve the queue for remaining green PRs.
    """
    ts = now or datetime.now(timezone.utc)
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(ts.isoformat() + "\n", encoding="utf-8")

    if not queue:
        return {
            "status": "skipped",
            "reason": "no_pr",
            "merged": [],
            "failed": [],
            "skipped": [],
            "heartbeat": str(heartbeat_path),
        }

    merged = []
    failed = []
    skipped = []

    for pr in queue:
        pr_id = pr.get("number") or pr.get("id") or pr.get("title")
        green, reason = is_merge_green(pr)
        if not green:
            skipped.append({"id": pr_id, "reason": reason})
            continue

        try:
            if merge_fn:
                merge_fn(pr)
            merged.append(pr_id)
        except Exception as exc:
            # PR merge failed; record error and DO NOT starve queue
            failed.append({"id": pr_id, "error": str(exc)})
            continue

    return {
        "status": "ok",
        "merged": merged,
        "failed": failed,
        "skipped": skipped,
        "heartbeat": str(heartbeat_path),
    }


def assert_static_docs(doc_path: Path) -> None:
    """Static assertion that docs/ctl-boot.md contains all required contract terms."""
    if not doc_path.is_file():
        raise AssertionError(f"Documentation file not found: {doc_path}")
    text = doc_path.read_text(encoding="utf-8")
    for term in REQUIRED_DOC_TERMS:
        if term not in text:
            raise AssertionError(f"Documentation {doc_path} missing required term: {term}")
    assert "merge-heartbeat" in text, "merge-heartbeat path missing in documentation"
    print(f"OK static doc check: {doc_path} contains all lane-merge / merge-green contract terms")


def assert_fake_directory_contract() -> None:
    """Assert lane-merge and merge-heartbeat contracts in a fake/mock directory."""
    scratch_dir = os.environ.get("TMPDIR")
    if scratch_dir:
        os.makedirs(scratch_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_dir) as raw_dir:
        tmp = Path(raw_dir)
        ctl_dir = tmp / "phanes-ctl"
        ad_dir = ctl_dir / "autodispatch"
        heartbeat_file = ad_dir / "merge-heartbeat"

        # 1. Test empty queue (无 PR 时跳过)
        tick_res = merge_loop_tick([], heartbeat_file)
        assert tick_res["status"] == "skipped"
        assert tick_res["reason"] == "no_pr"
        assert heartbeat_file.is_file(), "merge-heartbeat must be created on empty tick"
        first_content = heartbeat_file.read_text().strip()
        assert first_content, "merge-heartbeat must not be empty"
        print("OK: empty queue skipped cleanly and merge-heartbeat updated")

        # 2. Test failure non-starvation (失败不饿死队列)
        queue = [
            {
                "number": 101,
                "state": "open",
                "mergeable": True,
                "checks": {"linux": "success", "macos": "failure"},  # green because macOS is non-blocking
            },
            {
                "number": 102,
                "state": "open",
                "mergeable": True,
                "checks": {"linux": "success"},
            },
            {
                "number": 103,
                "state": "open",
                "mergeable": False,  # conflict, not green
                "checks": {"linux": "success"},
            },
        ]

        def fail_first_merge(pr):
            if pr["number"] == 101:
                raise RuntimeError("simulated squash merge conflict")
            # 102 succeeds

        tick_res2 = merge_loop_tick(queue, heartbeat_file, merge_fn=fail_first_merge)
        assert tick_res2["status"] == "ok"
        assert len(tick_res2["failed"]) == 1 and tick_res2["failed"][0]["id"] == 101
        assert len(tick_res2["merged"]) == 1 and tick_res2["merged"][0] == 102
        assert len(tick_res2["skipped"]) == 1 and tick_res2["skipped"][0]["id"] == 103
        print("OK: failure on PR #101 did not starve PR #102 in the queue")

        # 3. Test merge-green criteria: non-blocking checks ignored, red Linux blocked
        green_macos_fail, _ = is_merge_green({
            "state": "open",
            "mergeable": True,
            "checks": {"linux": "success", "macos": "error"},
        })
        assert green_macos_fail, "macOS failure must not block merge-green"

        red_linux, reason = is_merge_green({
            "state": "open",
            "mergeable": True,
            "checks": {"linux": "failure", "macos": "success"},
        })
        assert not red_linux, "Linux check failure must block merge-green"
        print("OK: merge-green criteria verified (Linux-green enforced, non-blocking ignored)")

        # 4. Test heartbeat freshness check
        now = datetime.now(timezone.utc)
        fresh_ts = now.isoformat() + "\n"
        heartbeat_file.write_text(fresh_ts, encoding="utf-8")
        parsed = parse_timestamp(heartbeat_file.read_text())
        age_minutes = (now - parsed).total_seconds() / 60.0
        assert age_minutes < DEFAULT_THRESHOLD_MINUTES, "fresh heartbeat must be within threshold"
        print("OK: merge-heartbeat timestamp parsing and freshness verified")

    print("OK: lane-merge contract passed (all fake directory assertions verified)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify lane-merge, merge-green, and merge-heartbeat contracts offline."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--heartbeat-file", type=Path, default=DEFAULT_HEARTBEAT)
    source.add_argument("--fixture-old", action="store_true", help="use a heartbeat older than threshold")
    source.add_argument("--fixture-fresh", action="store_true", help="use a current heartbeat")
    source.add_argument("--self-test", action="store_true", help="run offline contract self-test")
    parser.add_argument(
        "--threshold-minutes",
        type=float,
        default=DEFAULT_THRESHOLD_MINUTES,
        help=f"maximum heartbeat age in minutes (default: {DEFAULT_THRESHOLD_MINUTES})",
    )
    parser.add_argument(
        "--doc-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs" / "ctl-boot.md",
        help="path to docs/ctl-boot.md",
    )
    args = parser.parse_args()

    if args.threshold_minutes <= 0:
        parser.error("--threshold-minutes must be greater than zero")

    # If --self-test or no explicit fixture/heartbeat flags given, run self-test and doc check
    if args.self_test or (not args.fixture_old and not args.fixture_fresh and args.heartbeat_file == DEFAULT_HEARTBEAT and not args.heartbeat_file.exists()):
        assert_static_docs(args.doc_file)
        assert_fake_directory_contract()
        return 0

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
            print(f"WARNING: merge-heartbeat missing: {args.heartbeat_file}", file=sys.stderr)
            return 2
        except (OSError, ValueError) as exc:
            print(f"WARNING: merge-heartbeat unreadable ({args.heartbeat_file}): {exc}", file=sys.stderr)
            return 2

    age = max(0.0, (now - heartbeat).total_seconds() / 60)
    if age > args.threshold_minutes:
        print(
            f"WARNING: lane-merge heartbeat stalled/silent for {age:.1f} minutes "
            f"(threshold {args.threshold_minutes:g} minutes; source {source_name})",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: merge-heartbeat fresh ({age:.1f} minutes old; "
        f"threshold {args.threshold_minutes:g} minutes; source {source_name})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
