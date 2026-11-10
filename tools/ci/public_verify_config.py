#!/usr/bin/env python3
"""Report whether the public-verify workflow has a nonempty denylist."""

import os
from pathlib import Path


SKIPPED_MESSAGE = "SKIPPED: denylist not configured"


def main():
    content = os.environ.get("PUBLIC_VERIFY_DENYLIST", "")
    # Blank lines and comments contain no rules. Malformed nonempty rules
    # remain configured so the scanner can reject them rather than skip them.
    configured = any(line.strip() and not line.strip().startswith("#")
                     for line in content.splitlines())
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        stream.write("configured=" + str(configured).lower() + "\n")
    if not configured:
        print(SKIPPED_MESSAGE)
        print("::warning::" + SKIPPED_MESSAGE)
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(SKIPPED_MESSAGE + "\n")


if __name__ == "__main__":
    main()
