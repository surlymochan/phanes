"""Offline contract for #1982's recorded role-chain acceptance gate.

The fixture is a synthetic, de-identified public recording, not evidence of a
real model run. Receipt digests are SHA-256 of canonical JSON excluding their
own digest; artifacts carry base64 bytes and their byte hash. The verifier's
input is the decoded fixture and its result is a list of diagnostic strings.
Run: python3 -m unittest tools.tests.test_evalid_recorded_chain
"""

import base64
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tools/tests/fixtures/evalid-recorded-chain.json'
MODULE = 'tools.evalid_recorded_chain'


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=False).encode('utf-8')
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def reseal(chain):
    """Make tampered receipts self-consistent so semantic checks must catch them."""
    parent = None
    for receipt in chain['receipts']:
        receipt['parent_digest'] = parent
        receipt.pop('digest', None)
        receipt['digest'] = digest(receipt)
        parent = receipt['digest']


class RecordedChainTests(unittest.TestCase):
    def setUp(self):
        self.chain = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def verify(self, chain):
        self.assertIsNotNone(importlib.util.find_spec(MODULE),
                             '#1982 offline recorded-chain verifier is missing')
        module = importlib.import_module(MODULE)
        self.assertTrue(callable(getattr(module, 'verify_chain', None)))
        snapshot = copy.deepcopy(chain)
        with mock.patch('socket.socket', side_effect=AssertionError('network access')):
            errors = module.verify_chain(chain)
        self.assertEqual(chain, snapshot, 'verification must not mutate the recording')
        self.assertIsInstance(errors, list)
        self.assertTrue(all(isinstance(item, str) and item.strip() for item in errors))
        return errors

    def assert_rejected(self, chain):
        self.assertTrue(self.verify(chain), 'invalid recording must report an error')

    def test_valid_recording(self):
        self.assertEqual(self.verify(self.chain), [])

    def test_recording_must_be_a_mapping(self):
        self.assertEqual(self.verify([]), ['recording_not_a_mapping'])

    def test_unsupported_recording_version(self):
        self.chain['version'] = 2
        self.assert_rejected(self.chain)

    def test_missing_spec_check(self):
        self.chain['receipts'] = [r for r in self.chain['receipts']
                                  if r['stage'] != 'spec-check']
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_actual_model_change(self):
        for stage in ('spec', 'spec-check', 'red', 'green', 'review'):
            with self.subTest(stage=stage):
                chain = copy.deepcopy(self.chain)
                next(r for r in chain['receipts'] if r['stage'] == stage)['model'] = 'unexpected-model'
                reseal(chain)
                self.assert_rejected(chain)

    def test_parent_digest_change(self):
        self.chain['receipts'][2]['parent_digest'] = 'sha256:' + '0' * 64
        self.assert_rejected(self.chain)

    def test_fixed_base_and_head(self):
        for stage, field in (('issue', 'base_commit'), ('green', 'head_commit'),
                             ('holdout', 'subject_head'), ('review', 'reviewed_base'),
                             ('review', 'reviewed_head'), ('delivery', 'head_commit')):
            with self.subTest(stage=stage, field=field):
                chain = copy.deepcopy(self.chain)
                next(r for r in chain['receipts'] if r['stage'] == stage)[field] = 'd' * 40
                reseal(chain)
                self.assert_rejected(chain)

    def test_reviewer_omits_commit(self):
        review = next(r for r in self.chain['receipts'] if r['stage'] == 'review')
        review['commits'] = review['commits'][1:]
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_artifact_bytes_changed(self):
        self.chain['artifacts'][0]['bytes_base64'] = base64.b64encode(b'changed').decode()
        self.assert_rejected(self.chain)

    def test_final_verified_is_required(self):
        self.chain['receipts'][-1]['controller_status'] = 'REVIEW_PENDING'
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_chain_level_base_and_head_drift(self):
        for field in ('base_commit', 'head_commit'):
            with self.subTest(field=field):
                chain = copy.deepcopy(self.chain)
                chain[field] = 'e' * 40
                self.assert_rejected(chain)

    def test_tampered_receipt_digest(self):
        self.chain['receipts'][1]['status'] = 'FAIL'
        self.assert_rejected(self.chain)

    def test_private_content_is_not_a_receipt_field(self):
        holdout = next(r for r in self.chain['receipts'] if r['stage'] == 'holdout')
        holdout['holdout_tests'] = ['assert reserved_value == 739281']
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_holdout_isolation_must_be_verified(self):
        holdout = next(r for r in self.chain['receipts'] if r['stage'] == 'holdout')
        holdout['isolation'] = 'unisolated'
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_review_blocker_finding(self):
        review = next(r for r in self.chain['receipts'] if r['stage'] == 'review')
        review['findings'] = [{'severity': 'BLOCKER', 'reason': 'public requirement unmet'}]
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_spec_artifact_must_exist(self):
        spec = next(r for r in self.chain['receipts'] if r['stage'] == 'spec')
        spec['artifact'] = 'absent.md'
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_stage_order_mismatch(self):
        receipts = self.chain['receipts']
        receipts[2], receipts[3] = receipts[3], receipts[2]
        reseal(self.chain)
        self.assert_rejected(self.chain)

    def test_cli_reports_the_violated_bindings(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            invalid = Path(directory) / 'invalid.json'
            self.chain['receipts'][-1]['controller_status'] = 'REVIEW_PENDING'
            reseal(self.chain)
            invalid.write_text(json.dumps(self.chain), encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', MODULE, str(invalid)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('delivery_not_verified', result.stdout + result.stderr)

    def test_cli_exits_nonzero_for_invalid_recording(self):
        self.assertIsNotNone(importlib.util.find_spec(MODULE),
                             '#1982 offline recorded-chain CLI is missing')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            invalid = Path(directory) / 'invalid.json'
            self.chain['receipts'][-1]['controller_status'] = 'REVIEW_PENDING'
            reseal(self.chain)
            invalid.write_text(json.dumps(self.chain), encoding='utf-8')
            for path, ok in ((FIXTURE, True), (invalid, False)):
                with self.subTest(path=path.name):
                    result = subprocess.run([sys.executable, '-m', MODULE, str(path)],
                                            cwd=ROOT, capture_output=True, text=True,
                                            timeout=15)
                    self.assertEqual(result.returncode == 0, ok, result.stderr)
                    if not ok:
                        self.assertTrue((result.stdout + result.stderr).strip())


if __name__ == '__main__':
    unittest.main()
