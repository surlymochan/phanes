#!/usr/bin/env python3
"""Validate the e1885 decision document; exit nonzero for incomplete reports."""
import argparse
import re
import sys
from pathlib import Path


FIELDS = (
    'Frozen manifest digest', 'Report digest', 'Paired sample count',
    'Discordant results', 'Extra runs', 'Queue impact', 'Quota impact',
    'INFRA counted as PASS', 'Decision', 'Rationale',
)


def validate(document):
    errors = []
    values = {}
    for field in FIELDS:
        matches = re.findall(r'^' + re.escape(field) + r':[ \t]*([^\n]*)$', document, re.M | re.I)
        if len(matches) != 1 or not matches[0].strip():
            errors.append('Missing or duplicate field: ' + field)
        else:
            values[field] = matches[0].strip()
    for field in ('Frozen manifest digest', 'Report digest'):
        if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', values.get(field, '')):
            errors.append('Invalid digest: ' + field)
    count = values.get('Paired sample count', '')
    if not re.fullmatch(r'\d+', count) or not 10 <= int(count) <= 20:
        errors.append('Paired sample count must be 10–20')
    if values.get('Decision', '').lower() not in ('adopt', 'abandon'):
        errors.append('Decision must be adopt or abandon')
    if values.get('INFRA counted as PASS', '').lower() != 'no':
        errors.append('INFRA must not be counted as PASS')
    if re.search(r'\bINFRA\s*[:=]\s*PASS\b', document, re.I):
        errors.append('INFRA cannot be PASS')
    if re.search(r'\b(?:TODO|TBD|placeholder|pending|FIXME)\b|<[^>]+>|\[\s*\]', document, re.I):
        errors.append('Placeholder found')
    if re.search(r'\bhold[ -]?out\b', document, re.I):
        errors.append('Holdout content is forbidden')
    if re.search(r'(?<![\w:/])/(?:[\w.~-]+/)*[\w.~-]+|\b[A-Za-z]:\\', document):
        errors.append('Local absolute path found')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('document', type=Path)
    args = parser.parse_args()
    try:
        errors = validate(args.document.read_text(encoding='utf-8'))
    except (OSError, UnicodeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
