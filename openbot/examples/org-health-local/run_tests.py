#!/usr/bin/env python3
"""Discover and run all unit tests in this example."""

import pathlib
import sys
import unittest


def main():
    test_dir = pathlib.Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(test_dir),
        pattern="test_*.py",
        top_level_dir=str(test_dir),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failures = len(result.failures) + len(result.errors)
    print(
        f"\nRan {result.testsRun} tests: "
        f"{len(result.failures)} failures, {len(result.errors)} errors"
    )
    return failures


if __name__ == "__main__":
    sys.exit(main())
