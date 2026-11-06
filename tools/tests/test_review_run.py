import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'review_run.py'


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)


def repo(path):
    path.mkdir()
    run('git', 'init', '-q', str(path))
    run('git', 'config', 'user.email', 'test@example.invalid', cwd=path)
    run('git', 'config', 'user.name', 'Test', cwd=path)


def commit(path):
    run('git', 'add', 'test_visible.py', 'implementation.py', cwd=path)
    run('git', 'commit', '-qm', 'fixture', cwd=path)


class ReviewRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        repo(self.project)
        (self.project / 'test_visible.py').write_text('import unittest\nfrom implementation import value\nclass TestValue(unittest.TestCase):\n def test_value(self): self.assertEqual(value, 1)\n')
        (self.project / 'implementation.py').write_text('value = 0\n')
        commit(self.project)
        self.ev = self.base / 'evalid'
        repo(self.ev)
        atoms = self.ev / 'xflow' / 'atoms'
        atoms.mkdir(parents=True)
        (atoms / 'i6a_tdd_run.py').write_text('''import argparse,json,pathlib,subprocess,sys
p=argparse.ArgumentParser()
for x in ('project-root','change-id','phase','test-command'): p.add_argument('--'+x,required=True)
a=p.parse_args(); root=pathlib.Path(a.project_root); r=subprocess.run(a.test_command,shell=True,cwd=root,capture_output=True)
d=root/'specs'/'changes'/a.change_id/'tdd'; (d/'snapshots'/(a.phase+'-0')).mkdir(parents=True,exist_ok=True)
(d/(a.phase+'-0.json')).write_text(json.dumps({'failure_kind':'assertion' if r.returncode else 'pass'}))
print(json.dumps({'ok': (r.returncode != 0) if a.phase=='red' else (r.returncode == 0)}))
''')
        (atoms / 'i6b_tdd_proof_validate.py').write_text("import json,os,pathlib,sys\np=os.environ.get('FAKE_REPLAY_FAIL')\nfail=bool(p and pathlib.Path(p).exists())\nif p: pathlib.Path(p).touch()\nprint(json.dumps({'ok': not fail}))\nsys.exit(fail)\n")
        run('git', 'add', '.', cwd=self.ev)
        run('git', 'commit', '-qm', 'fixture', cwd=self.ev)
        self.holdout_dir = self.base / 'holdout'
        self.holdout_dir.mkdir()
        self.holdout = self.holdout_dir / 'holdout.py'
        self.holdout.write_text('import unittest\nclass Holdout(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\nunittest.main()\n')
        self.out = self.base / 'evidence'

    def invoke(self, phase, env=None, ev=None, visible=None, holdout=None, extra=()):
        cmd = [sys.executable, str(RUNNER), '--project', str(self.project), '--evalid', str(ev or self.ev),
               '--evidence', str(self.out), '--change-id', 'sample', '--cmd-visible', visible or 'python3 -m unittest test_visible',
               '--cmd-holdout', holdout or 'python3 {holdout}/holdout.py', '--holdout-dir', str(self.holdout_dir), '--phase', phase, '--timeout', '30', *extra]
        r = subprocess.run(cmd, text=True, capture_output=True, env=env)
        return r, json.loads(r.stdout)

    def green(self):
        (self.project / 'implementation.py').write_text('value = 1\n')
        commit(self.project)

    def test_red_green(self):
        r, data = self.invoke('red'); self.assertEqual((r.returncode, data['verdict']), (0, 'PASS'))
        self.green()
        r, data = self.invoke('green'); self.assertEqual((r.returncode, data['verdict'], data['classification'], data['replay']['ok']), (0, 'PASS', 'tdd', True))

    def test_red_unexpected_pass(self):
        self.green()
        r, data = self.invoke('red'); self.assertNotEqual(r.returncode, 0); self.assertEqual(data['verdict'], 'FAIL')

    def test_holdout_failure(self):
        self.invoke('red'); self.green(); self.holdout.write_text('import unittest\nclass Holdout(unittest.TestCase):\n def test_fail(self): self.fail()\nunittest.main()\n')
        _, data = self.invoke('green'); self.assertEqual(data['verdict'], 'FAIL'); self.assertEqual(data['holdout']['exit'], 1)

    def test_no_tests_detected(self):
        self.green()
        self.holdout.write_text('import unittest\nunittest.main()\n')
        _, data = self.invoke('posthoc')
        self.assertEqual((data['verdict'], data['error']), ('INFRA', 'no_tests_detected'))

    def test_visible_no_tests_detected(self):
        _, data = self.invoke('red', visible="python3 -c 'import unittest; unittest.main()'")
        self.assertEqual((data['verdict'], data['error']), ('INFRA', 'no_tests_detected'))

    def test_holdout_placeholder_required(self):
        _, data = self.invoke('red', holdout='python3 ' + str(self.holdout))
        self.assertEqual(data['verdict'], 'INFRA')
        self.assertIn('{holdout}', data['error'])

    def test_min_holdout_tests(self):
        self.green()
        _, data = self.invoke('posthoc', extra=('--min-holdout-tests', '2'))
        self.assertEqual((data['verdict'], data['error']), ('INFRA', 'no_tests_detected'))

    def test_original_swap_does_not_change_executed_copy(self):
        self.green()
        original = str(self.holdout)
        self.holdout.write_text("import unittest\nfrom pathlib import Path\np=Path(" + repr(original) + ")\nold=p.read_text()\np.write_text('raise SystemExit(1)')\np.write_text(old)\nclass Holdout(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\nunittest.main()\n")
        _, data = self.invoke('posthoc')
        self.assertEqual(data['verdict'], 'PASS', data)

    def test_dirty(self):
        (self.project / 'implementation.py').write_text('value = 1\n')
        _, data = self.invoke('red'); self.assertEqual(data['verdict'], 'INFRA')

    def test_replay_failure(self):
        self.green()
        env = dict(os.environ, FAKE_REPLAY_FAIL=str(self.base / 'replay-marker'))
        _, data = self.invoke('posthoc', env); self.assertEqual(data['verdict'], 'FAIL'); self.assertFalse(data['replay']['ok'])

    def test_holdout_replay_failure(self):
        self.green()
        atom = self.ev / 'xflow' / 'atoms' / 'i6b_tdd_proof_validate.py'
        atom.write_text("import json,sys\nfail='sample-holdout' in sys.argv\nprint(json.dumps({'ok': not fail}))\nsys.exit(fail)\n")
        run('git', 'add', '.', cwd=self.ev)
        run('git', 'commit', '-qm', 'fail holdout replay', cwd=self.ev)
        _, data = self.invoke('posthoc')
        self.assertEqual(data['verdict'], 'FAIL')
        self.assertFalse(data['replay']['ok'])
        self.assertIn('holdout', data['error'])

    def test_holdout_changed_during_run(self):
        self.green()
        self.holdout.write_text("import unittest\nfrom pathlib import Path\nPath(__file__).write_text('changed')\nclass Holdout(unittest.TestCase):\n def test_ok(self): pass\nunittest.main()\n")
        _, data = self.invoke('posthoc')
        self.assertEqual((data['verdict'], data['error']), ('INFRA', 'holdout_changed'))
        self.assertIn('holdout.py', data['holdout']['files_sha256'])

    def test_holdout_inside_project_rejected(self):
        self.holdout_dir = self.project
        _, data = self.invoke('red')
        self.assertEqual(data['verdict'], 'INFRA')
        self.assertIn('outside project', data['error'])

    def test_posthoc(self):
        self.green()
        _, data = self.invoke('posthoc'); self.assertEqual((data['verdict'], data['classification']), ('PASS', 'posthoc'))
        self.assertIn('holdout.py', data['holdout']['files_sha256'])
        self.assertTrue((self.out / 'sample-visible' / 'record.json').is_file())
        self.assertTrue((self.out / 'sample-holdout' / 'record.json').is_file())
        self.assertEqual((self.out / 'sample-holdout' / 'holdout-files' / 'holdout.py').read_bytes(), self.holdout.read_bytes())

    def test_missing_evalid(self):
        _, data = self.invoke('red', ev=self.base / 'missing'); self.assertEqual(data['verdict'], 'INFRA')

    @unittest.skipUnless(os.environ.get('EVALID_CHECKOUT'), 'EVALID_CHECKOUT not set')
    def test_real_evalid(self):
        ev = Path(os.environ['EVALID_CHECKOUT'])
        r, data = self.invoke('red', ev=ev); self.assertEqual(r.returncode, 0, data)
        self.green()
        r, data = self.invoke('green', ev=ev); self.assertEqual(r.returncode, 0, data)
        self.assertEqual((data['classification'], data['replay']['ok']), ('tdd', True))


if __name__ == '__main__':
    unittest.main()
