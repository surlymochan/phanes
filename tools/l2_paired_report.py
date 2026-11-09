#!/usr/bin/env python3
"""Compare first-attempt outcomes on frozen control/treatment card pairs.

The manifest contains arms and cards (change_id, heads by arm, suite_digest).
Receipts are a JSON array with change_id, arm, attempt, subject_head,
suite_digest and PASS/FAIL/INFRA status. Later attempts are never scored.
The difference is treatment minus control over pairs without INFRA; it is
null when there are no such pairs.
"""

import argparse
import json
from pathlib import Path
import sys


ARMS = ('control', 'treatment')
STATUSES = ('PASS', 'FAIL', 'INFRA')


def nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def paired_report(manifest, receipts):
    if not isinstance(manifest, dict) or manifest.get('arms') != list(ARMS):
        raise ValueError('manifest arms must be [control, treatment]')
    cards = manifest.get('cards')
    if not isinstance(cards, list) or not cards:
        raise ValueError('manifest cards must be a nonempty array')
    frozen = {}
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError('invalid card')
        change_id = card.get('change_id')
        heads = card.get('heads')
        if (not nonempty_string(change_id) or change_id in frozen or
                not isinstance(heads, dict) or set(heads) != set(ARMS) or
                not all(nonempty_string(heads[arm]) for arm in ARMS) or
                not nonempty_string(card.get('suite_digest'))):
            raise ValueError('invalid or duplicate manifest card')
        frozen[change_id] = card

    if not isinstance(receipts, list):
        raise ValueError('receipts must be an array')
    seen = set()
    first = {}
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ValueError('invalid receipt')
        change_id, arm, attempt = (receipt.get(key) for key in ('change_id', 'arm', 'attempt'))
        if (not isinstance(change_id, str) or change_id not in frozen or
                arm not in ARMS or type(attempt) is not int or attempt < 0 or
                receipt.get('status') not in STATUSES or
                not nonempty_string(receipt.get('subject_head'))):
            raise ValueError('invalid receipt fields')
        key = (change_id, arm, attempt)
        if key in seen:
            raise ValueError('duplicate result: ' + str(key))
        seen.add(key)
        card = frozen[change_id]
        if receipt.get('suite_digest') != card['suite_digest']:
            raise ValueError('suite digest drift: ' + str(key))
        if attempt == 0:
            if receipt['subject_head'] != card['heads'][arm]:
                raise ValueError('subject head drift: ' + str(key))
            first[(change_id, arm)] = receipt['status']

    concordant = {'pass_pass': 0, 'fail_fail': 0}
    discordant = {'control_only': 0, 'treatment_only': 0}
    pairs, infra_missing = [], []
    for change_id in sorted(frozen):
        if any((change_id, arm) not in first for arm in ARMS):
            raise ValueError('missing first-attempt arm: ' + change_id)
        control, treatment = (first[(change_id, arm)] for arm in ARMS)
        pairs.append({'change_id': change_id, 'control': control, 'treatment': treatment})
        for arm, status in zip(ARMS, (control, treatment)):
            if status == 'INFRA':
                infra_missing.append({'change_id': change_id, 'arm': arm, 'status': status})
        if 'INFRA' in (control, treatment):
            continue
        if control == treatment:
            concordant['pass_pass' if control == 'PASS' else 'fail_fail'] += 1
        else:
            discordant['control_only' if control == 'PASS' else 'treatment_only'] += 1

    complete = sum(concordant.values()) + sum(discordant.values())
    difference = ((discordant['treatment_only'] - discordant['control_only']) / complete
                  if complete else None)
    return {'pairs': pairs, 'concordant': concordant, 'discordant': discordant,
            'first_round_pass_difference': round(difference, 6) if difference is not None else None,
            'sign_test': {'control_wins': discordant['control_only'],
                          'treatment_wins': discordant['treatment_only']},
            'infra_missing': infra_missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        receipts = json.loads(args.receipts.read_text(encoding='utf-8'))
        report = paired_report(manifest, receipts)
    except (OSError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
