import unittest
from pathlib import Path

from tools.e1885_architecture_decision import validate


VALID = '''# e1885 decision

Frozen manifest digest: sha256:''' + 'a' * 64 + '''
Report digest: sha256:''' + 'b' * 64 + '''
Paired sample count: 12
Discordant results: 3 old only, 4 new only
Extra runs: 24 reruns of flaky pairs
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

    def test_empty_filler_is_not_evidence(self):
        for field in ('Discordant results', 'Extra runs', 'Queue impact', 'Quota impact', 'Rationale'):
            for filler in ('N/A', 'none', 'TBD', '-', 'unknown', 'to be determined', 'same'):
                line = [l for l in VALID.splitlines() if l.startswith(field + ':')][0]
                errors = validate(VALID.replace(line, field + ': ' + filler))
                self.assertTrue(errors, (field, filler))

    def test_numbers_required_for_counts(self):
        self.assertTrue(validate(VALID.replace('3 old only, 4 new only', 'several differences were seen')))
        self.assertTrue(validate(VALID.replace('Extra runs: 24 reruns of flaky pairs', 'Extra runs: a few more were needed')))

    def test_infra_pass_in_natural_language(self):
        for sentence in ('INFRA results were counted as PASS.', 'infra -> pass', 'PASS (INFRA)',
                         'INFRA   is   PASS', 'Infra outcomes count as pass;', 'INFRA=PASS'):
            self.assertTrue(validate(VALID + '\n' + sentence + '\n'), sentence)

    def test_negated_infra_statements_are_allowed(self):
        for sentence in ('INFRA results were not counted as PASS.', 'INFRA is never counted as PASS.',
                         'All non-INFRA runs PASS.'):
            self.assertEqual(validate(VALID + '\n' + sentence + '\n'), [], sentence)

    def test_filled_shipped_template_passes(self):
        path = Path(__file__).resolve().parents[2] / 'handoff' / 'ARCHITECTURE-PROPOSAL.md'
        text = path.read_text(encoding='utf-8')
        filled = {
            'Frozen manifest digest': 'sha256:' + 'a' * 64, 'Report digest': 'sha256:' + 'b' * 64,
            'Paired sample count': '12', 'Discordant results': '3 old only, 4 new only',
            'Extra runs': '24 reruns of flaky pairs', 'Queue impact': '5 minutes longer',
            'Quota impact': '24 additional runs', 'INFRA counted as PASS': 'no',
            'Decision': 'adopt', 'Rationale': 'The paired results support adoption.'}
        lines = []
        for line in text.splitlines():
            field = line.split(':', 1)[0]
            lines.append(field + ': ' + filled[field] if field in filled else line)
        self.assertEqual(validate('\n'.join(lines) + '\n'), [])

    def test_not_available_is_not_evidence(self):
        cases = {'Queue impact': 'not available', 'Discordant results': '0 not available',
                 'Quota impact': 'Not Available', 'Rationale': 'n.a. unknown', 'Extra runs': '无',
                 'Discordant results ': '未知'}
        for field, filler in cases.items():
            field = field.strip()
            line = [l for l in VALID.splitlines() if l.startswith(field + ':')][0]
            self.assertTrue(validate(VALID.replace(line, field + ': ' + filler)), (field, filler))

    def test_infra_counted_as_pass_after_unrelated_negation(self):
        text = VALID + '\nNo reruns were needed, and INFRA results were counted as PASS.\n'
        self.assertTrue(validate(text))

    def test_infra_and_pass_tracked_separately_is_allowed(self):
        text = VALID + '\nINFRA failures and PASS results were tracked separately.\n'
        self.assertEqual(validate(text), [])

    def test_structured_infra_field_must_be_no(self):
        self.assertTrue(validate(VALID.replace('INFRA counted as PASS: no', 'INFRA counted as PASS: yes')))
        self.assertTrue(validate(VALID.replace('INFRA counted as PASS: no\n', '')))


if __name__ == '__main__':
    unittest.main()
