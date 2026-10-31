"""The compatibility adapter keeps bundle generation on its executor host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ADAPTER = Path(__file__).resolve().parents[1] / 'bin' / 'openswarm-fleet-bundle-adapter'


class FleetBundleAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base = root / 'old-openswarm'
        self.base.write_text('#!' + sys.executable + '\n'
                             'import json,sys\n'
                             'if sys.argv[1:3] == ["fleet", "_run-info"]:\n'
                             ' print(json.dumps({"id":sys.argv[3],"workspace":{"repo":"/executor-only/worktree"}}))\n'
                             'else: print(json.dumps({"delegated":sys.argv[1:]}))\n')
        self.base.chmod(0o755)
        self.bundle = root / 'fleet-result-bundle'
        self.bundle.write_text('import json,sys\nmeta=json.load(sys.stdin)\n'
                               'print(json.dumps({"bundle":"eA==","source_repo":meta["workspace"]["repo"]}))\n')

    def invoke(self, *args):
        return subprocess.run([str(ADAPTER), *args], text=True, capture_output=True, timeout=15,
                              env={**os.environ, 'OPENSWARM_FLEET_BASE_BIN': str(self.base),
                                   'OPENSWARM_FLEET_BUNDLE_BIN': str(self.bundle)})

    def test_bundle_uses_metadata_and_helper_on_executor(self):
        result = self.invoke('fleet', '_bundle', 'run-1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'bundle': 'eA==', 'source_repo': '/executor-only/worktree'})

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
