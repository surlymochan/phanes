"""Check reviewer paths in temporary pipeline workspaces."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_run_e2e as contracts


class ReviewWorkspaceRootTest(unittest.TestCase):
    def setUp(self):
        self.fixture = contracts.RunE2EReviewContractTest(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def prepare(self, brief=None, summary=None):
        fixture = self.fixture
        fixture.write_json_stub('boss', contracts.BRIEF if brief is None else brief)
        fixture.write_json_stub('da', contracts.DIAGNOSIS)
        fixture.write_json_stub('builder', contracts.DIAGNOSIS, summary_payload=summary)
        fixture.write_checker_stub()

    def use_checkout_workspace(self):
        temporary = tempfile.TemporaryDirectory(prefix='.review-path-test-', dir=contracts.REPO_ROOT)
        self.addCleanup(temporary.cleanup)
        self.fixture.workdir = Path(temporary.name) / 'pipeline' / 'out'
        return self.fixture.workdir.relative_to(contracts.REPO_ROOT).as_posix()

    def run_metadata_failure(self):
        fixture = self.fixture
        result = subprocess.run(fixture.command(), cwd=fixture.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('FAIL 角色元数据', fixture.record(result))
        error_file = fixture.workdir / contracts.STEP_ERROR_DIR / 'review.json'
        error = json.loads(error_file.read_text(encoding='utf-8'))
        command = error['command']
        results_path = Path(command[command.index('--json-out') + 1])
        return results_path, error

    def check_metadata_failure(self, expected_path):
        results_path, _ = self.run_metadata_failure()
        payload = json.loads(results_path.read_text(encoding='utf-8'))
        self.assertFalse(payload['ok'])
        self.assertIn(expected_path, [finding['path'] for finding in payload['findings']])
        for finding in payload['findings']:
            self.assertFalse(Path(finding['path']).is_absolute())
            self.assertNotIn('\\', finding['path'])
        self.assertTrue(any(check['name'] == '角色元数据' and not check['ok']
                            for check in payload['checks']))

    def test_bundled_reviewer_uses_checkout_root_for_brief_finding(self):
        prefix = self.use_checkout_workspace()
        brief = copy.deepcopy(contracts.BRIEF)
        del brief['producer']
        self.prepare(brief=brief)
        self.check_metadata_failure(f'{prefix}/brief.json')

    def test_bundled_reviewer_uses_checkout_root_for_summary_finding(self):
        prefix = self.use_checkout_workspace()
        summary = copy.deepcopy(contracts.SITE_SUMMARY)
        del summary['periods']
        self.prepare(summary=summary)
        self.check_metadata_failure(f'{prefix}/dist/site-summary.json')

    def check_outside_workspace(self, expected_file):
        results_path, error = self.run_metadata_failure()
        self.assertFalse(results_path.exists())
        self.assertFalse((self.fixture.workdir / contracts.REVIEW_RESULTS).exists())
        self.assertEqual(error['exit_code'], 2)
        reports = list(self.fixture.workdir.glob('.review.md.review.*.tmp'))
        self.assertEqual(len(reports), 1)
        content = reports[0].read_text(encoding='utf-8')
        self.assertIn('outside the repository', content)
        self.assertIn(expected_file, content)
        self.assertIn('FAIL 角色元数据', content)

    def test_outside_workspace_rejects_brief_finding_without_json(self):
        brief = copy.deepcopy(contracts.BRIEF)
        del brief['producer']
        self.prepare(brief=brief)
        self.check_outside_workspace('brief.json')

    def test_outside_workspace_rejects_summary_finding_without_json(self):
        summary = copy.deepcopy(contracts.SITE_SUMMARY)
        del summary['periods']
        self.prepare(summary=summary)
        self.check_outside_workspace('site-summary.json')

    def test_custom_reviewer_keeps_strict_argument_contract(self):
        self.prepare()
        fixture = self.fixture
        reviewer = fixture.root / 'strict_reviewer.py'
        arguments = fixture.root / 'reviewer-arguments.json'
        reviewer.write_text(
            'import argparse, json, pathlib, sys\n'
            'parser = argparse.ArgumentParser()\n'
            'for name in ("--brief", "--data", "--dist", "--json-out"):\n'
            '    parser.add_argument(name, required=True)\n'
            'args = parser.parse_args()\n'
            f'pathlib.Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n'
            'pathlib.Path(args.json_out).write_text(json.dumps({"ok": True, '
            '"findings": [], "checks": [], "summary": {"total": 0, '
            '"passed": 0, "failed": 0}}))\n'
            'print("Custom review complete")\n', encoding='utf-8')
        command = fixture.command()
        command[command.index('--reviewer') + 1] = str(reviewer)
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(arguments.read_text())[::2],
                         ['--brief', '--data', '--dist', '--json-out'])
        self.assertIn('Custom review complete', fixture.record(result))
        status = json.loads((fixture.workdir / contracts.STATUS_FILE).read_text())
        self.assertTrue(status['review']['summarized'])
        self.assertFalse(status['review']['blocking'])


if __name__ == '__main__':
    unittest.main()
