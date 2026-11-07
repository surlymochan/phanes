#!/usr/bin/env python3
"""Parse a raw evidence pack of a lane review into structured fields (Refs #1788).

The review lane delivers the *raw evidence* of a PR in a fixed format (Epic #1785,
P1-2).  This script is the reader of that format: it parses a pack into structured
fields (CI conclusions, test logs, diff statistics, acceptance checks, preliminary
opinion) and reports whether the pack is complete.

    python3 tools/check-review-evidence-pack.py docs/evidence/review-pack/in-flight-pr-1788.md
    python3 tools/check-review-evidence-pack.py --json <pack.md>

Exit code 0 when the pack is complete, 1 when a section is missing or the format is
broken (the dispatcher records the same verdict in `reviews.json`).
"""

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATHS = (
    os.path.join(os.path.dirname(HERE), "swrm", "lib", "review", "evidence.py"),
    os.path.join(HERE, "..", "swrm", "lib", "review", "evidence.py"),
)


def load_evidence_module():
    """Load lib/review/evidence.py by path (the review directory is a namespace package)."""
    for path in MODULE_PATHS:
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("swrm_review_evidence", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("cannot find swrm/lib/review/evidence.py")


def report(parsed):
    """Return the human readable report of one parsed pack."""
    lines = []
    lines.append("sections : %s" % (", ".join(parsed["sections"]) or "-"))
    lines.append("missing  : %s" % (", ".join(parsed["missing"]) or "-"))
    lines.append("errors   : %s" % (", ".join(parsed["errors"]) or "-"))
    for item in parsed["ci"]:
        lines.append("ci       : %s | %s | %s" % (item["name"], item["status"], item["detail"]))
    for item in parsed["test_logs"]:
        lines.append("test     : %s | %s | %s" % (item["command"], item["summary"], item["link"]))
    stats = parsed["diffstat"]
    lines.append("diffstat : files=%s insertions=%s deletions=%s effective_lines=%s"
                 % (stats.get("files"), stats.get("insertions"), stats.get("deletions"),
                    stats.get("effective_lines")))
    for item in parsed["checks"]:
        lines.append("check    : %s | %s | %s" % (item["item"], item["result"], item["evidence"]))
    if parsed["opinion"]["present"]:
        lines.append("opinion  : %s" % parsed["opinion"]["text"].replace("\n", " "))
    lines.append("complete : %s" % ("yes" if parsed["complete"] else "no"))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pack", help="the evidence pack (markdown) to parse")
    parser.add_argument("--json", action="store_true", help="print the structured fields as JSON")
    args = parser.parse_args(argv)

    evidence = load_evidence_module()
    with open(args.pack, encoding="utf-8") as stream:
        text = stream.read()
    parsed = evidence.parse_evidence(text)
    if args.json:
        print(json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(report(parsed))
    return 0 if parsed["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
