"""The compatibility adapter keeps bundle generation on its executor host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ADAPTER = Path(__file__).resolve().parents[1] / 'bin' / 'openswarm-fleet-bundle-adapter'
RESULT_BUNDLE = Path(__file__).resolve().parents[1] / 'bin' / 'fleet-result-bundle'


class FleetBundleAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / 'executor-only-repo'
        self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()
        git('init', '-q')
        (self.repo / 'file').write_text('before')
        git('add', 'file')
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('checkout', '-qb', 'openswarm/run-1')
        (self.repo / 'file').write_text('after')
        git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qam', 'result')
        commit = git('rev-parse', 'HEAD')
        self.base = root / 'old-openswarm'
        self.base.write_text('#!' + sys.executable + '\n'
                             'import json,sys\n'
                             'if sys.argv[1:3] == ["fleet", "_run-info"]:\n'
                             ' print(' + repr(json.dumps({'id': 'run-1', 'state': 'finished', 'result': {
                                 'branch': 'openswarm/run-1', 'commit': commit,
                                 'workspace': {'repo': str(self.repo), 'base': base}}})) + ')\n'
                             'else: print(json.dumps({"delegated":sys.argv[1:]}))\n')
        self.base.chmod(0o755)
        self.bundle = RESULT_BUNDLE

    def invoke(self, *args):
        return subprocess.run([str(ADAPTER), *args], text=True, capture_output=True, timeout=15,
                              env={**os.environ, 'OPENSWARM_FLEET_BASE_BIN': str(self.base),
                                   'OPENSWARM_FLEET_BUNDLE_BIN': str(self.bundle)})

    def test_bundle_uses_metadata_and_helper_on_executor(self):
        result = self.invoke('fleet', '_bundle', 'run-1')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['branch'], 'openswarm/run-1')
        self.assertTrue(payload['bundle'])

    def test_nonzero_base_error_keeps_structured_stdout(self):
        self.base.write_text('#!' + sys.executable + '\n'
                             'import json,sys\n'
                             'print(json.dumps({"ok":False,"error":"base runtime failed"}))\n'
                             'raise SystemExit(7)\n')
        self.base.chmod(0o755)
        result = self.invoke('fleet', '_bundle', 'run-1')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': False, 'error': 'base runtime failed'})

    def test_other_commands_delegate_to_existing_runtime(self):
        result = self.invoke('lane', 'status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'delegated': ['lane', 'status']})

    def test_wrapped_legacy_run_error_is_not_mistaken_for_a_bundle(self):
        self.base.write_text('#!' + sys.executable + '\n'
                             'import json\n'
                             'print(json.dumps({"ok":False,"error":"no such run"}))\n')
        result = self.invoke('fleet', '_bundle', 'missing-run')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'ok': False, 'error': 'no such run'})


if __name__ == '__main__':
    unittest.main()
