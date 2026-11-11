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

# Evidence fields that must say something real, and those that must carry a number.
SUBSTANCE_FIELDS = ('Discordant results', 'Extra runs', 'Queue impact', 'Quota impact', 'Rationale')
NUMBER_FIELDS = ('Discordant results', 'Extra runs')
# Vague phrases carry no evidence; they are removed and what remains must still be substantive.
_VAGUE = re.compile(
    r'(?<![A-Za-z])(?:not\s+available|unavailable|not\s+applicable|not\s+measured|not\s+recorded|not\s+known|'
    r'n/?a|n\.a\.?|none|nil|null|unknown|tbd|todo|to\s+be\s+determined|no\s+impact|nothing|'
    r'same)(?![A-Za-z])|无|未知|不适用|暂无', re.I)
_INFRA_PASS = re.compile(
    r'\bINFRA\b[^.;\n]{0,40}?(?:->|=>|=|:)\s*PASS\b|\bINFRA\s+(?:is|are|was|were)\s+PASS\b|'
    r'\bPASS\s*\(\s*INFRA\s*\)|\b(?:count(?:s|ed)?|treat(?:s|ed)?|scored|recorded|reported|marked)\s+as\s+PASS\b', re.I)
_NEGATED = re.compile(r"(?:\bnot|\bnever|n't|\bnor)\s+$", re.I)


def _substantive(value, needs_number):
    """A field value that says something once vague phrases are removed."""
    text = ' '.join(_VAGUE.sub(' ', value).replace('-', ' ').split()).strip(' .,;:')
    if len(text) < 12 or len(text.split()) < 2:
        return False
    return bool(re.search(r'\d', text)) if needs_number else True


def _counts_infra_as_pass(document):
    """True if the text says INFRA counts as PASS in one of the explicit spellings.

    The structured field ``INFRA counted as PASS:`` is validated on its own and
    its line is skipped here. Other lines are matched on explicit phrases only
    (``counted as PASS``, ``INFRA -> PASS``, ``PASS (INFRA)``); a phrase directly
    preceded by not/never/n't is a denial and is allowed. No sentence guessing.
    """
    for line in document.splitlines():
        if re.match(r'\s*INFRA counted as PASS\s*:', line, re.I):
            continue
        for match in _INFRA_PASS.finditer(line):
            if not _NEGATED.search(line[:match.start()]):
                return True
    return False


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
    for field in SUBSTANCE_FIELDS:
        value = values.get(field)
        if value is not None and not _substantive(value, field in NUMBER_FIELDS):
            errors.append('Evidence is not substantive: ' + field)
    if _counts_infra_as_pass(document):
        errors.append('INFRA cannot be PASS')
    if re.search(r'\b(?:TODO|TBD|FIXME)\b|<[^>]+>|\[\s*\]', document, re.I):
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
