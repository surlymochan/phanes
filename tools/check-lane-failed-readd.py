#!/usr/bin/env python3
"""Offline regression check: do not re-add lane-ready when an issue has a PR."""

import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tools/check-lane-failed-requeue.py"
spec = importlib.util.spec_from_file_location("lane_failed_requeue", CONTRACT_PATH)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def main() -> None:
    labels = {"lane-failed", "lane-pr"}
    issue_body = "验收：`python3 tools/check-lane-failed-readd.py`"

    assert not contract.can_requeue(labels, {}, issue_body), (
        "lane-pr issue must not be recommended for lane-ready re-add"
    )
    print("lane-failed re-add with lane-pr contract: OK")


if __name__ == "__main__":
    main()
