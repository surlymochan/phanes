#!/usr/bin/env python3
"""Lane review metrics: first-round pass rate, average review rounds, findings spread (Refs #1733).

The dispatcher writes the review records into ``reviews.json``; this script turns
them into the metrics file the dispatcher keeps next to ``lanes-status.json``
(``lane-review-metrics.json``) and prints the one-screen summary.

The measurement rules are written into the head of the metrics file
(``definitions``); the short version:

* a formal round is one review verdict on a pull request head;
* a first-round pass is a pull request whose first verdict was ``APPROVE``;
* a rebase re-review (the head the dispatcher itself moved) is not a formal round;
* a record without a verdict or without a usable round count is skipped and
  counted in ``skipped`` -- it never enters a rate.

Usage::

    python3 tools/lane-review-metrics.py --reviews ~/.claude/openswarm/reviews.json
    python3 tools/lane-review-metrics.py --reviews reviews.json --prs prs.json --json

``--prs`` reads the real pull request data (``gh pr list --json number,headRefName,...``)
and fills in what an old record does not carry (the author lane of the pull request).
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "openswarm" / "lib" / "dispatch" / "review_metrics.py"


def load_module():
    """Load the metrics module by path (openswarm/lib is a namespace package)."""
    spec = importlib.util.spec_from_file_location("openswarm_dispatch_review_metrics", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_reviews():
    """The review records of the openswarm home."""
    home = os.environ.get("OPENSWARM_HOME") or os.path.join(os.path.expanduser("~"), ".claude", "openswarm")
    return os.path.join(home, "reviews.json")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def main(argv=None):
    metrics = load_module()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reviews", default=default_reviews(),
                        help="the review records (reviews.json)")
    parser.add_argument("--prs", help="optional pull request data (gh pr list --json ...)")
    parser.add_argument("--out", help="the metrics file to write (default: next to the review records)")
    parser.add_argument("--json", action="store_true", help="print the metrics as JSON")
    parser.add_argument("--no-write", action="store_true", help="do not write the metrics file")
    args = parser.parse_args(argv)

    reviews = read_json(args.reviews, {})
    if not isinstance(reviews, dict):
        print(f"error: {args.reviews} does not hold an object", file=sys.stderr)
        return 2
    prs = read_json(args.prs, None) if args.prs else None
    report = metrics.compute(reviews, prs=prs, generated_at=time.strftime("%F %T"))
    report["source"] = os.path.abspath(args.reviews)

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.reviews)), metrics.METRICS_FILE)
    if not args.no_write:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{out}.tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=1, ensure_ascii=False)
        os.replace(tmp, out)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(metrics.render(report))
        print(f"metrics file: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
