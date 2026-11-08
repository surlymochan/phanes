"""Acceptance fixtures for the L2 paired-outcome report CLI.

The frozen manifest lists each card's two first-attempt heads and suite digest.
The CLI reads --manifest and --receipts JSON files and writes one JSON report.
Attempts are zero-based, matching the public role-controller receipts.
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'tools' / 'l2_paired_report.py'
ARMS = ('control', 'treatment')
SUITE = 'sha256:' + 'a' * 64


def fixture():
    outcomes = {
        'card-1': ('PASS', 'PASS'),
        'card-2': ('FAIL', 'FAIL'),
        'card-3': ('PASS', 'FAIL'),
        'card-4': ('FAIL', 'PASS'),
        'card-5': ('PASS', 'INFRA'),
    }
    cards, receipts = [], []
    for index, (card, statuses) in enumerate(outcomes.items(), 1):
        heads = {arm: str(index) * 39 + str(i) for i, arm in enumerate(ARMS)}
        cards.append({'change_id': card, 'heads': heads, 'suite_digest': SUITE})
        for arm, status in zip(ARMS, statuses):
            receipts.append({'change_id': card, 'arm': arm, 'attempt': 0,
                             'subject_head': heads[arm], 'suite_digest': SUITE,
                             'status': status})
    return {'arms': list(ARMS), 'cards': cards}, receipts


class PairedReportTests(unittest.TestCase):
    def invoke(self, manifest, receipts):
        self.assertTrue(SCRIPT.is_file(), 'paired report CLI is missing')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            manifest_path = Path(directory) / 'manifest.json'
            receipts_path = Path(directory) / 'receipts.json'
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            receipts_path.write_text(json.dumps(receipts), encoding='utf-8')
            return subprocess.run([sys.executable, str(SCRIPT), '--manifest', str(manifest_path),
                                   '--receipts', str(receipts_path)],
                                  capture_output=True, text=True, check=False)

    def report(self, manifest, receipts):
        result = self.invoke(manifest, receipts)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_pairs_not_pooled_and_repair_is_not_first_round(self):
        manifest, receipts = fixture()
        repair = dict(receipts[6], attempt=1, status='PASS')  # card-4 control
        receipts.append(repair)
        report = self.report(manifest, receipts)
        # Pooling all available results yields control 3/5 vs treatment 2/4;
        # among complete pairs each arm has two passes and one discordant win.
        self.assertEqual(report['concordant'], {'pass_pass': 1, 'fail_fail': 1})
        self.assertEqual(report['discordant'], {'control_only': 1, 'treatment_only': 1})
        self.assertEqual(report['first_round_pass_difference'], 0.0)
        self.assertEqual(report['sign_test'], {'control_wins': 1, 'treatment_wins': 1})
        self.assertEqual(report['infra_missing'], [
            {'change_id': 'card-5', 'arm': 'treatment', 'status': 'INFRA'}])

    def test_missing_arm_duplicate_and_digest_drift_are_rejected(self):
        manifest, receipts = fixture()
        cases = {}
        cases['missing arm'] = receipts[:-1]
        cases['duplicate result'] = receipts + [copy.deepcopy(receipts[0])]
        for field in ('subject_head', 'suite_digest'):
            changed = copy.deepcopy(receipts)
            changed[0][field] = 'drift'
            cases[field] = changed
        for name, invalid in cases.items():
            with self.subTest(name=name):
                result = self.invoke(manifest, invalid)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertTrue(result.stderr.strip(), name)

    def test_output_is_sorted_and_rounding_is_deterministic(self):
        manifest, receipts = fixture()
        # Three complete pairs, with treatment winning one more than control.
        manifest['cards'] = manifest['cards'][:3]
        receipts = receipts[:6]
        receipts[4]['status'] = 'FAIL'
        receipts[5]['status'] = 'PASS'
        first_result = self.invoke(manifest, receipts)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        reversed_manifest = copy.deepcopy(manifest)
        reversed_manifest['cards'].reverse()
        second_result = self.invoke(reversed_manifest, list(reversed(receipts)))
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        self.assertEqual(first_result.stdout, second_result.stdout)
        first = json.loads(first_result.stdout)
        self.assertEqual(first['first_round_pass_difference'], 0.333333)
        self.assertEqual([row['change_id'] for row in first['pairs']],
                         ['card-1', 'card-2', 'card-3'])


if __name__ == '__main__':
    unittest.main()
