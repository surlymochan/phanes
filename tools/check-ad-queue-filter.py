#!/usr/bin/env python3
"""Offline contract for filtering stale/finished lane-ready issue cards."""

import importlib.util
from pathlib import Path


def load_autodispatch():
    path = Path(__file__).parent / "lane-autodispatch" / "autodispatch.py"
    spec = importlib.util.spec_from_file_location("autodispatch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    autodispatch = load_autodispatch()
    # Deliberately fake GH issue JSON: this check never contacts GH or AD.
    issues = [
        {"number": 1, "state": "OPEN", "labels": [{"name": "lane-ready"}]},
        {"number": 2, "state": "OPEN", "labels": ["lane-ready", "lane-failed"]},
        {"number": 3, "state": "OPEN", "labels": ["lane-ready", "lane-pr"]},
        {"number": 4, "state": "OPEN", "labels": ["lane-ready", "in-progress"]},
        {"number": 5, "state": "CLOSED", "labels": ["lane-ready"]},
    ]
    assert [issue["number"] for issue in issues if autodispatch.dispatchable_issue(issue)] == [1]
    print("autodispatch queue filter contract: OK")


if __name__ == "__main__":
    main()
