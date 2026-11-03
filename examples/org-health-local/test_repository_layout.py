"""Check the scenario after its move to the repository examples directory."""
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import run_e2e
import run_tests


ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[1]


def iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_cases(item)
        else:
            yield item


class RepositoryLayoutTest(unittest.TestCase):
    def test_default_inputs_and_roles_use_the_moved_example(self):
        args = run_e2e.parse_args(['--workdir', 'output'])
        defaults = {
            'raw': 'da/sample-raw',
            'boss': 'boss/make_brief.py',
            'da': 'da/build_diagnosis.py',
            'builder': 'builder/build_site.py',
            'checker': 'builder/check_site.py',
            'reviewer': 'review/cross_check.py',
        }
        for option, relative in defaults.items():
            with self.subTest(option=option):
                self.assertEqual(getattr(args, option), ROOT / relative)
                self.assertTrue(getattr(args, option).exists())
        self.assertEqual(run_e2e.source_repo_root(), REPOSITORY)
        self.assertFalse((REPOSITORY / 'openbot/examples/org-health-local').exists())

    def test_default_pipeline_runs_from_repository_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'output'
            result = subprocess.run(
                [sys.executable, str(ROOT / 'run_e2e.py'), '--workdir', str(output)],
                cwd=REPOSITORY, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads((output / 'status.json').read_text(encoding='utf-8'))
            self.assertEqual(status['status'], 'ok')
            self.assertEqual([step['role'] for step in status['steps']],
                             ['boss', 'da', 'builder', 'checker'])
            for name in ('brief.json', 'diagnosis.json', 'dist/index.html',
                         'dist/styles.css', 'dist/app.bundle.js', 'dist/site-summary.json'):
                with self.subTest(artifact=name):
                    self.assertTrue((output / name).is_file())
            self.assertEqual((output / 'dist/app.bundle.js').read_bytes(),
                             (ROOT / 'demo/vendor/app.bundle.js').read_bytes())

    def test_source_commit_does_not_depend_on_caller_directory(self):
        expected = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=REPOSITORY, text=True).strip()
        with tempfile.TemporaryDirectory() as temporary:
            code = (f'import sys; sys.path.insert(0, {str(ROOT)!r}); '
                    'import run_e2e; print(run_e2e.source_commit())')
            result = subprocess.run([sys.executable, '-c', code], cwd=temporary,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_discovery_includes_every_test_module(self):
        suite = run_tests.discover_tests()
        modules = {case.__class__.__module__ for case in iter_cases(suite)}
        expected = {
            '.'.join(path.relative_to(ROOT).with_suffix('').parts)
            for path in ROOT.rglob('test_*.py')
        }
        self.assertFalse(expected - modules, f'Test modules not discovered: {expected - modules}')
        self.assertIn('demo.test_make_site_summary', modules)
        self.assertIn('tests.test_serve_contract', modules)

    def test_ci_validates_and_selects_the_moved_example(self):
        workflow = (REPOSITORY / '.github/workflows/phanes.yml').read_text(encoding='utf-8')
        self.assertIn('examples/org-health-local', workflow)
        self.assertIn('examples/org-health-local/validate.py', workflow)
        self.assertNotIn('openbot/examples/org-health-local', workflow)

    def test_frontend_translation_import_resolves_from_the_moved_source(self):
        source = (ROOT / 'app.js').read_text(encoding='utf-8')
        imports = re.findall(r'from ["\']([^"\']+)["\']', source)
        self.assertEqual(len(imports), 1)
        self.assertTrue(imports[0].startswith('.'))
        entry = (ROOT / imports[0]).resolve()
        self.assertEqual(entry, REPOSITORY / 'openbot/packages/i18n/src/index.ts')
        exports = entry.read_text(encoding='utf-8')
        self.assertRegex(exports, r'export\s*\{[^}]*\bresolveLocale\b')
        self.assertRegex(exports, r'export function translateFor\(')

    def test_schema_and_documentation_use_the_repository_layout(self):
        import test_data_definitions
        self.assertEqual(test_data_definitions.find_doc(),
                         REPOSITORY / test_data_definitions.DOC_RELATIVE)
        schema = json.loads((ROOT / 'schema/diagnosis.schema.json').read_text(encoding='utf-8'))
        self.assertIn('properties', schema)
        for resource in ('index.html', 'app.js', 'styles.css', 'data/diagnosis.json'):
            with self.subTest(resource=resource):
                self.assertTrue((ROOT / resource).is_file())


if __name__ == '__main__':
    unittest.main()
