#!/usr/bin/env python3
"""Discover and run all tests in the example and its child packages."""

import pathlib
import sys
import unittest


def discover_tests(test_dir=None):
    test_dir = pathlib.Path(test_dir) if test_dir is not None else pathlib.Path(__file__).resolve().parent
    return unittest.TestLoader().discover(
        start_dir=str(test_dir),
        pattern="test_*.py",
        top_level_dir=str(test_dir),
    )


def main():
    result = unittest.TextTestRunner(verbosity=2).run(discover_tests())
    failures = len(result.failures) + len(result.errors)
    print(
        f"\nRan {result.testsRun} tests: "
        f"{len(result.failures)} failures, {len(result.errors)} errors"
    )
    return failures


if __name__ == "__main__":
    sys.exit(main())
