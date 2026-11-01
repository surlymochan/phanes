"""The compatibility adapter keeps bundle generation on its executor host."""
import base64
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


class FleetBundleEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'executor-source'
        self.consumer = root / 'hub-consumer'
        self.metadata = root / 'run-info.json'
        self.base_runtime = root / 'old-openswarm'
        self.run_id = 'e2e-run-1'
        identity = ['-c', 'user.name=test', '-c', 'user.email=test@example.invalid']

        def git(repo, *args):
            return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()

        def commit(repo, message):
            git(repo, *identity, 'commit', '-qam', message)
            return git(repo, 'rev-parse', 'HEAD')

        self.source.mkdir()
        git(self.source, 'init', '-q', '-b', 'main')
        (self.source / 'file').write_text('before')
        git(self.source, 'add', 'file')
        git(self.source, *identity, 'commit', '-qm', 'base')
        self.base = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'checkout', '-qb', 'openswarm/' + self.run_id)
        (self.source / 'file').write_text('after')
        self.commit = commit(self.source, 'result')
        git(self.source, 'checkout', '-q', 'main')

        self.consumer.mkdir()
        git(self.consumer, 'init', '-q', '-b', 'main')
        git(self.consumer, 'fetch', '-q', str(self.source), 'refs/heads/main:refs/remotes/source/main')

        run_info = {'id': self.run_id, 'state': 'finished', 'result': {
            'id': self.run_id, 'status': 'done', 'branch': 'openswarm/' + self.run_id,
            'commit': self.commit, 'diffstat': '1 file changed',
            'workspace': {'mode': 'worktree', 'path': str(self.source), 'repo': str(self.source),
                          'base': self.base}}}
        self.metadata.write_text(json.dumps(run_info))
        self.base_runtime.write_text('#!' + sys.executable + '\n'
                                     'import sys\n'
                                     'if sys.argv[1:3] == ["fleet", "_run-info"]:\n'
                                     ' sys.stdout.write(open(' + repr(str(self.metadata)) + ').read())\n'
                                     'else: raise SystemExit(2)\n')
        self.base_runtime.chmod(0o755)

    def test_adapter_bundle_fetches_recorded_commit_into_consumer(self):
        env = {**os.environ, 'OPENSWARM_FLEET_BASE_BIN': str(self.base_runtime),
               'OPENSWARM_FLEET_BUNDLE_BIN': str(RESULT_BUNDLE)}
        result = subprocess.run([str(ADAPTER), 'fleet', '_bundle', self.run_id], text=True,
                                capture_output=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['branch'], 'openswarm/' + self.run_id)
        self.assertEqual(payload['commit'], self.commit)

        bundle_file = Path(self.temp.name) / 'returned.bundle'
        bundle_file.write_bytes(base64.b64decode(payload['bundle'], validate=True))
        subprocess.run(['git', '-C', str(self.consumer), 'fetch', '-q', '--no-tags', str(bundle_file),
                        'refs/heads/' + payload['branch'] + ':refs/openswarm/' + self.run_id],
                       check=True, capture_output=True, timeout=60)
        resolved = subprocess.check_output(['git', '-C', str(self.consumer), 'rev-parse',
                                            'refs/openswarm/' + self.run_id], text=True).strip()
        self.assertEqual(resolved, self.commit)

    def test_push_rejects_different_run_without_damaging_bundle_and_is_idempotent(self):
        source = Path(self.temp.name) / 'source'
        target = Path(self.temp.name) / 'target'
        source.mkdir()
        target.mkdir()
        first = source / 'first.json'
        second = source / 'second.json'
        first.write_text(json.dumps({'id': 'run-one', 'result': 'first'}))
        second.write_text(json.dumps({'id': 'run-two', 'result': 'second'}))
        cli = Path(__file__).resolve().parents[1] / 'bin' / 'openswarm-fleet'
        env = {**os.environ, 'HOME': self.temp.name}
        command = [str(cli), 'push', str(first), str(target)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=15, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = (target / 'result.json').read_bytes()
        conflict = subprocess.run([str(cli), 'push', str(second), str(target)], text=True,
                                  capture_output=True, timeout=15, env=env)
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('conflict', conflict.stdout)
        self.assertEqual((target / 'result.json').read_bytes(), original)
        retry = subprocess.run(command, text=True, capture_output=True, timeout=15, env=env)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        replaced = subprocess.run([str(cli), 'push', '--replace', str(second), str(target)], text=True,
                                  capture_output=True, timeout=15, env=env)
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertEqual(json.loads((target / 'result.json').read_text())['id'], 'run-two')


if __name__ == '__main__':
    unittest.main()
