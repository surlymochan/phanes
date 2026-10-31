#!/usr/bin/env python3
"""Check D1 migration prefixes for ordering and duplicate numbers."""

import argparse
import re
import sys
from pathlib import Path


# 0034 is already duplicated in the applied migration history. Never rename an
# applied migration; keep this exception until the history can be handled safely.
KNOWN_DUPLICATES = {"0034"}
PREFIX = re.compile(r"^(\d{4})_.+")


def check_migrations(directory):
    files = sorted(path for path in directory.iterdir() if path.is_file())
    errors = []
    seen = {}
    previous = None

    for path in files:
        match = PREFIX.match(path.name)
        if not match:
            errors.append(f"FAIL {path.name}: expected a four-digit prefix followed by '_'")
            continue

        number = match.group(1)
        value = int(number)
        if previous is not None and value < previous:
            errors.append(f"FAIL {path.name}: migration number {number} is out of order")
        previous = value

        seen.setdefault(number, []).append(path.name)
        print(f"PASS {path.name}: migration number {number}")

    for number, names in sorted(seen.items()):
        if len(names) > 1:
            if number in KNOWN_DUPLICATES:
                print(f"PASS duplicate {number}: known applied migrations preserved")
            else:
                errors.append(f"FAIL duplicate {number}: {', '.join(names)}")

    for error in errors:
        print(error, file=sys.stderr)
    return not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing migration files")
    args = parser.parse_args()
    try:
        success = check_migrations(args.directory)
    except OSError as error:
        print(f"FAIL {args.directory}: {error}", file=sys.stderr)
        return 1
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
