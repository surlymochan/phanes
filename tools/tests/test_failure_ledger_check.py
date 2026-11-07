"""Run the failure ledger CLI against offline issue and Markdown fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'failure_ledger_check.py'
TYPES = {'exec_error', 'thin-gate', 'no-commit', 'timeout', 'quota',
         'watchdog-abort', 'premise-dead', 'label-lag', 'other'}
HEADER = ('| Issue | Lane | Type | Root cause and evidence | Lesson | Follow-up |\n'
          '| --- | --- | --- | --- | --- | --- |\n')


def issue(number, labels=None, state='OPEN'):
    return {'number': number, 'state': state,
            'labels': ['lane-failed'] if labels is None else labels}


def row(number, kind='exec_error', cause=None, lesson='Check the command before retry.', follow_up=None):
    link = f'[#{number}](https://github.com/surlymochan/phanes/issues/{number})'
    if cause is None:
        cause = ('The command exited before writing its result. '
                 f'[evidence](https://github.com/surlymochan/phanes/issues/{number}#issuecomment-123)')
    if follow_up is None:
        follow_up = f'待裁决 {link}'
    return f'| {link} | A1 | {kind} | {cause} | {lesson} | {follow_up} |\n'


class FailureLedgerCheckTests(unittest.TestCase):
    def run_check(self, issues, rows, as_json=False, raw_issues=None):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            issues_path, ledger_path = directory / 'issues.json', directory / 'ledger.md'
            issues_path.write_text(json.dumps(issues) if raw_issues is None else raw_issues, encoding='utf-8')
            ledger_path.write_text('# Failure ledger\n\n' + HEADER + ''.join(rows), encoding='utf-8')
            original = (issues_path.read_bytes(), ledger_path.read_bytes())
            command = [sys.executable, str(SCRIPT), '--issues', str(issues_path), '--ledger', str(ledger_path)]
            if as_json:
                command.append('--json')
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(original, (issues_path.read_bytes(), ledger_path.read_bytes()))
        self.assertNotIn('Traceback', result.stderr)
        return result, json.loads(result.stdout)

    def test_complete_coverage_includes_closed_issues_and_stable_json(self):
        issues = [issue(3, state='closed'), issue(1, labels=[{'name': 'lane-failed'}]), issue(2)]
        rows = [row(3, 'quota'), row(1), row(2, 'timeout')]
        result, report = self.run_check(issues, rows)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report['expected_count'], 3)
        self.assertEqual(report['covered_count'], 3)
        self.assertEqual(report['coverage_percent'], 100)
        for field in ('missing_issues', 'undetermined_issues', 'invalid_rows', 'duplicate_issues'):
            self.assertEqual(report[field], [])
        self.assertEqual(set(report['type_counts']), TYPES)
        self.assertEqual(report['type_counts']['exec_error'], 1)
        repeated, reordered = self.run_check(list(reversed(issues)), rows, as_json=True)
        self.assertEqual(repeated.returncode, 0)
        self.assertEqual(report, reordered)
        self.assertEqual(result.stdout, repeated.stdout)

    def test_missing_expected_issue_fails_with_partial_coverage(self):
        result, report = self.run_check([issue(2), issue(1)], [row(1)])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['missing_issues'], [2])
        self.assertEqual(report['covered_count'], 1)
        self.assertEqual(report['coverage_percent'], 50)

    def test_unknown_root_cause_fails_even_with_all_rows_present(self):
        for diagnosis in ('未定性', 'undetermined'):
            with self.subTest(diagnosis=diagnosis):
                cause = diagnosis + ' [evidence](https://github.com/surlymochan/phanes/issues/1#issuecomment-123)'
                result, report = self.run_check([issue(1)], [row(1, cause=cause)])
                self.assertEqual(result.returncode, 1)
                self.assertEqual(report['undetermined_issues'], [1])

    def test_unknown_roots_still_count_toward_type_recurrence(self):
        rows = [row(number, cause=(
            'undetermined '
            f'[evidence](https://github.com/surlymochan/phanes/issues/{number}#issuecomment-123)'))
                for number in (2, 1)]
        result, report = self.run_check([issue(2), issue(1)], rows)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['undetermined_issues'], [1, 2])
        self.assertEqual(report['type_counts']['exec_error'], 2)
        self.assertEqual(report['recurrent_types'], {'exec_error': 2})
        self.assertEqual(report['covered_count'], 2)
        self.assertEqual(report['coverage_percent'], 100)
        self.assertEqual(report['total_gap_issues'], [1, 2])

    def test_unknown_type_and_duplicate_issue_rows_fail(self):
        for rows, expected_field in (([row(1, 'unrecognized')], 'invalid_rows'),
                                     ([row(1), row(1)], 'duplicate_issues')):
            with self.subTest(expected_field=expected_field):
                result, report = self.run_check([issue(1)], rows)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(report[expected_field])
                if expected_field == 'duplicate_issues':
                    self.assertEqual(report[expected_field], [1])

    def test_unrelated_and_partial_label_names_do_not_create_requirements(self):
        issues = [issue(1), issue(2, ['lane-ready']), issue(3, [{'name': 'not-lane-failed'}]),
                  issue(4, ['lane-failed-extra']), issue(5, [])]
        result, report = self.run_check(issues, [row(1)])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report['expected_count'], 1)
        self.assertEqual(report['missing_issues'], [])

    def test_historical_rows_count_toward_recurrent_types(self):
        result, report = self.run_check([issue(1)], [row(1), row(8), row(9, 'quota')])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report['expected_count'], 1)
        self.assertEqual(report['covered_count'], 1)
        self.assertEqual(report['coverage_percent'], 100)
        self.assertEqual(report['type_counts']['exec_error'], 2)
        self.assertEqual(report['type_counts']['quota'], 1)
        self.assertEqual(report['recurrent_types'], {'exec_error': 2})

    def test_incomplete_quality_fields_are_invalid_rows(self):
        for invalid in (row(1, cause='The command failed without a linked source.'),
                        row(1, lesson=''), row(1, follow_up='')):
            with self.subTest(row=invalid):
                result, report = self.run_check([issue(1)], [invalid])
                self.assertEqual(result.returncode, 1)
                self.assertTrue(report['invalid_rows'])
                self.assertEqual(report['invalid_rows'][0]['issue'], 1)
                self.assertTrue(report['invalid_rows'][0]['reason'])

    def test_malformed_issue_json_returns_clean_json_error(self):
        result, report = self.run_check([], [], raw_issues='{ broken JSON')
        self.assertEqual(result.returncode, 2)
        self.assertIsInstance(report['error'], str)
        self.assertTrue(report['error'])


if __name__ == '__main__':
    unittest.main()
