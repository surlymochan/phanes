#!/usr/bin/env python3
"""Run the frozen cases through eventd.decide with a conservative spending cap."""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

import eventd

HERE = Path(__file__).resolve().parent
PRICES = {'gpt-6-luna': (0.10, 0.50), 'gpt-6.1-terra': (1.00, 5.00)}
MAX_OUTPUT = 2048
BUDGET = 2.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--backend', choices=['openai', 'decisions'], default='openai')
    args = parser.parse_args()
    os.environ.pop('OPENAI_API_KEY', None)
    eventd.load_env_file(args.env_file)
    if not os.environ.get('OPENAI_API_KEY'):
        raise SystemExit('OPENAI_API_KEY missing')
    cases = [json.loads(line) for line in (HERE / 'eval-cases.jsonl').read_text().splitlines()]
    original = urllib.request.urlopen
    path = HERE / 'eval-raw.jsonl'
    prior = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    completed = {(r.get('requested_backend', 'openai'), r['requested_model'], r['case_id']) for r in prior if 'case_id' in r and not r['decision_info']['fallback'] and not any(a.get('error') for a in r['attempts']) and not r['reason'].startswith('decision unavailable:')}
    spent = 0.0
    for record in prior:
        for attempt in record.get('attempts', []):
            usage = attempt.get('usage')
            pin, pout = PRICES[attempt['model']]
            spent += ((usage['input_tokens'] * pin + usage['output_tokens'] * pout) / 1_000_000 if usage else 0.02)
    calls = []

    def bounded(req, timeout):
        nonlocal spent
        payload = json.loads(req.data)
        is_decisions = req.full_url.endswith('/decisions')
        if not is_decisions:
            payload['max_output_tokens'] = MAX_OUTPUT
        data = json.dumps(payload).encode()
        model = payload['model']
        pin, pout = PRICES[model]
        # Each UTF-8 byte is a conservative upper bound for input tokens.
        reserve = (len(data) * pin + MAX_OUTPUT * pout) / 1_000_000
        if spent + reserve > BUDGET:
            raise RuntimeError('budget reservation exceeded')
        req.data = data
        attempt = {'model': model, 'effort': payload.get('reasoning', {}).get('effort'), 'usage': None, 'response': None}
        calls.append(attempt)
        try:
            with original(req, timeout=timeout) as response:
                obj = json.load(response)
            attempt['usage'] = obj.get('usage')
            attempt['response'] = obj
            usage = attempt['usage']
            if not usage or 'input_tokens' not in usage or 'output_tokens' not in usage:
                spent += reserve
                raise ValueError('missing usage')
            spent += (usage['input_tokens'] * pin + usage['output_tokens'] * pout) / 1_000_000
            from io import BytesIO
            return BytesIO(json.dumps(obj).encode())
        except Exception as exc:
            attempt['error'] = type(exc).__name__
            # Failed requests may still be billable: reserve the maximum.
            if attempt['usage'] is None:
                spent += reserve
            raise

    urllib.request.urlopen = bounded
    with path.open('a') as out:
        models = [('gpt-6-luna', None)] if args.backend == 'decisions' else [('gpt-6-luna', 'high'), ('gpt-6.1-terra', 'medium')]
        for model, effort in models:
            for case in cases:
                if (args.backend, model, case['id']) in completed:
                    continue
                calls.clear()
                info = {}
                action, reason, elapsed, tokens = eventd.decide(args.backend, case['event'], case['context'], 60, model, effort, info)
                record = {'requested_backend': args.backend, 'case_id': case['id'], 'expected': case['expected'], 'requested_model': model, 'requested_effort': effort, 'action': action, 'reason': reason, 'elapsed_ms': elapsed, 'tokens': tokens, 'decision_info': info, 'attempts': list(calls)}
                out.write(json.dumps(record, ensure_ascii=False) + '\n')
                out.flush()
                if reason.startswith('decision unavailable:') or any(c.get('error') for c in calls):
                    raise SystemExit('Decision unavailable; stopped to avoid repeated failed requests.')
    print(f'completed {len(cases) * len(models)} decisions; conservative accounted cost ${spent:.6f}')


if __name__ == '__main__':
    main()
