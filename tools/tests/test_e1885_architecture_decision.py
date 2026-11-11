import unittest
from pathlib import Path

from tools.e1885_architecture_decision import validate


VALID = '''# e1885 decision

Frozen manifest digest: sha256:''' + 'a' * 64 + '''
Report digest: sha256:''' + 'b' * 64 + '''
Paired sample count: 12
Discordant results: 3 old only, 4 new only
Extra runs: 24
Queue impact: 5 minutes longer
Quota impact: 24 additional runs
INFRA counted as PASS: no
Decision: adopt
Rationale: The paired results support adoption.
'''


class DecisionTests(unittest.TestCase):
    def test_complete_report(self):
        self.assertEqual(validate(VALID), [])
        self.assertEqual(validate(VALID.replace('Decision: adopt', 'Decision: abandon')), [])

    def test_placeholders(self):
        self.assertTrue(validate(VALID.replace('5 minutes longer', 'TODO')))

    def test_sample_boundaries(self):
        for count in ('9', '21', 'abc'):
            self.assertTrue(validate(VALID.replace('count: 12', 'count: ' + count)))
        for count in ('10', '20'):
            self.assertEqual(validate(VALID.replace('count: 12', 'count: ' + count)), [])

    def test_digests(self):
        for old in ('Frozen manifest digest', 'Report digest'):
            self.assertTrue(validate(VALID.replace(old + ': sha256:', old + ': bad:')))
            self.assertTrue(validate(VALID.replace(old + ':', 'Missing:')))

    def test_infra_not_pass(self):
        self.assertTrue(validate(VALID.replace('INFRA counted as PASS: no', 'INFRA counted as PASS: yes')))
        self.assertTrue(validate(VALID.replace('INFRA counted as PASS: no', 'INFRA counted as PASS: no\nINFRA: PASS')))

    def test_decision(self):
        self.assertTrue(validate(VALID.replace('Decision: adopt', 'Decision: pending')))
        self.assertTrue(validate(VALID.replace('Decision: adopt', '')))

    def test_holdout_content(self):
        self.assertTrue(validate(VALID + '\nHoldout: private test details\n'))

    def test_local_paths(self):
        self.assertTrue(validate(VALID + '\nEvidence: ' + '/' + 'tmp/example\n'))
        self.assertTrue(validate(VALID + '\nEvidence: ' + 'C:' + '\\example\\file\n'))

    def test_required_evidence(self):
        for field in ('Discordant results', 'Extra runs', 'Queue impact', 'Quota impact', 'Rationale', 'INFRA counted as PASS'):
            self.assertTrue(validate(VALID.replace(field + ':', 'Other:')), field)

    def test_shipped_template_fails(self):
        path = Path(__file__).resolve().parents[2] / 'handoff' / 'ARCHITECTURE-PROPOSAL.md'
        self.assertTrue(validate(path.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
