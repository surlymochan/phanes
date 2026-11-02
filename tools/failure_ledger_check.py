#!/usr/bin/env python3
"""Check failed-issue coverage and evidence in a Markdown failure ledger.

The checker reads local files only. Exit 0 means complete, valid coverage; exit 1
means ledger gaps; exit 2 means that an input file cannot be read or decoded.
Coverage counts valid inventory rows, including rows with an undetermined cause.
Those rows remain quality gaps and produce exit 1.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FAILURE_TYPES = (
    "exec_error", "thin-gate", "no-commit", "timeout", "quota",
    "watchdog-abort", "premise-dead", "label-lag", "other",
)
COLUMNS = ("issue", "lane", "type", "root cause and evidence", "lesson", "follow-up")
ISSUE_LINK = re.compile(r"\[#([1-9][0-9]*)\]\(https://github\.com/[^/\s]+/[^/\s]+/issues/([1-9][0-9]*)\)")
GITHUB_EVIDENCE = re.compile(r"https://github\.com/[^\s)<>]+/[^\s)<>]+/[^\s)<>]+")
BRAIN_EVIDENCE = re.compile(r"(?:^|[\s`(])(?:[^\s`()]*/)?brain/[^\s`()#:]+(?::[1-9][0-9]*|#L[1-9][0-9]*)")
UNKNOWN_ROOT = re.compile(r"未定性|\bundetermined\b", re.IGNORECASE)


def table_cells(line):
    """Split Markdown table cells without splitting escaped pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [part.replace("\\|", "|").strip() for part in re.split(r"(?<!\\)\|", text)]


def expected_issues(issues):
    if not isinstance(issues, list):
        raise ValueError("Issues input must be a JSON array")
    expected = set()
    seen = set()
    for item in issues:
        if not isinstance(item, dict):
            raise ValueError("Each issue must be a JSON object")
        number = item.get("number")
        if type(number) is not int or number < 1 or number in seen:
            raise ValueError("Issue numbers must be unique positive integers")
        seen.add(number)
        labels = item.get("labels", [])
        if not isinstance(labels, list):
            raise ValueError(f"Issue #{number}: labels must be an array")
        names = []
        for label in labels:
            name = label.get("name") if isinstance(label, dict) else label
            if not isinstance(name, str):
                raise ValueError(f"Issue #{number}: each label must have a name")
            names.append(name)
        if "lane-failed" in names:
            expected.add(number)
    return expected


def check_ledger(issues, markdown):
    expected = expected_issues(issues)
    covered = set()
    undetermined = set()
    seen = set()
    duplicates = set()
    invalid = []
    counts = dict.fromkeys(FAILURE_TYPES, 0)
    active = False
    found_table = False

    def reject(line, issue, reason):
        invalid.append({"line": line, "issue": issue, "reason": reason})

    for line_number, line in enumerate(markdown.splitlines(), 1):
        if not line.strip():
            active = False
            continue
        if not line.lstrip().startswith("|"):
            active = False
            continue
        cells = table_cells(line)
        header = tuple(cell.strip("`* ").casefold() for cell in cells)
        if header == COLUMNS:
            active = True
            found_table = True
            continue
        if not active:
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        match = ISSUE_LINK.fullmatch(cells[0]) if cells else None
        number = int(match[1]) if match else None
        errors = []
        if match is None or match[1] != match[2]:
            errors.append("Issue must be a matching GitHub issue link")
        if number is not None:
            if number in seen:
                duplicates.add(number)
            seen.add(number)
        if len(cells) != len(COLUMNS):
            errors.append(f"Expected {len(COLUMNS)} cells, found {len(cells)}")
        else:
            missing = [name for name, cell in zip(COLUMNS, cells) if not cell or cell in ("-", "—")]
            if missing:
                errors.append("Missing fields: " + ", ".join(missing))
            if cells[2] not in FAILURE_TYPES:
                errors.append("Unknown failure type: " + cells[2])
            evidence = cells[3]
            if not (GITHUB_EVIDENCE.search(evidence) or BRAIN_EVIDENCE.search(evidence)):
                errors.append("Root cause needs a GitHub evidence link or brain path with a line number")
            if number is not None and UNKNOWN_ROOT.search(evidence):
                undetermined.add(number)
        if errors:
            reject(line_number, number, "; ".join(errors))
            continue
        if number is not None:
            covered.add(number)
            if number not in duplicates:
                counts[cells[2]] += 1
    if not found_table:
        reject(0, None, "Missing ledger table with columns: " + ", ".join(COLUMNS))
    missing = sorted(expected - covered)
    covered_count = len(expected & covered)
    return {
        "expected_count": len(expected),
        "covered_count": covered_count,
        "coverage_percent": round(100 * covered_count / len(expected), 2) if expected else 100.0,
        "missing_issues": missing,
        "undetermined_issues": sorted(undetermined),
        "invalid_rows": invalid,
        "duplicate_issues": sorted(duplicates),
        "total_gap_issues": sorted(set(missing) | undetermined | duplicates | {
            row["issue"] for row in invalid if row["issue"] is not None
        }),
        "type_counts": counts,
        "recurrent_types": {name: count for name, count in counts.items() if count >= 2},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issues", type=Path, required=True, help="Local gh issue JSON array")
    parser.add_argument("--ledger", type=Path, required=True, help="Markdown failure ledger")
    parser.add_argument("--json", action="store_true", help="Emit JSON (the default)")
    args = parser.parse_args(argv)
    try:
        issues = json.loads(args.issues.read_text(encoding="utf-8-sig"))
        report = check_ledger(issues, args.ledger.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return int(any(report[key] for key in (
        "missing_issues", "undetermined_issues", "invalid_rows", "duplicate_issues",
    )))


if __name__ == "__main__":
    sys.exit(main())
