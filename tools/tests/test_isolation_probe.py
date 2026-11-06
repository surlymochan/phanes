import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'isolation_probe.sh'


class IsolationProbeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'root'
        self.root.mkdir()
        self.identifiers = self.base / 'identifiers'
        self.identifiers.write_text('needle-example-781\n')
        self.home = self.base / 'home'
        self.home.mkdir()

    def run_probe(self, forbidden):
        env = dict(os.environ, HOME=str(self.home), EXAMPLE_SECRET='do-not-print-value')
        proc = subprocess.run(
            ['sh', str(SCRIPT), '--identifiers', str(self.identifiers),
             '--search-root', str(self.root), '--forbid-path', str(forbidden),
             '--no-network'], capture_output=True, text=True, env=env, check=False)
        self.assertNotIn('do-not-print-value', proc.stdout)
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(rows[-1]['check'], 'summary')
        return proc, {row['check']: row for row in rows}

    def test_clean_and_empty_directory(self):
        forbidden = self.base / 'empty'
        forbidden.mkdir()
        proc, rows = self.run_probe(forbidden)
        self.assertEqual(rows['identifiers']['status'], 'PASS')
        self.assertEqual(rows['forbid-path']['status'], 'PASS')
        self.assertEqual(rows['deny-host']['status'], 'SKIP')
        self.assertEqual(rows['allow-host']['status'], 'SKIP')
        self.assertEqual(rows['net-iface']['status'], 'SKIP')
        self.assertIn(proc.returncode, (1, 2))  # Other host checks can fail.

    def test_content_and_nonempty_directory_fail(self):
        (self.root / 'file').write_text('before needle-example-781 after')
        forbidden = self.base / 'nonempty'
        forbidden.mkdir()
        (forbidden / 'entry').touch()
        proc, rows = self.run_probe(forbidden)
        self.assertEqual(rows['identifiers']['status'], 'FAIL')
        self.assertEqual(rows['forbid-path']['status'], 'FAIL')
        self.assertEqual(proc.returncode, 1)

    def test_filename_match_fails(self):
        (self.root / 'needle-example-781').touch()
        proc, rows = self.run_probe(self.base / 'absent')
        self.assertEqual(rows['identifiers']['status'], 'FAIL')
        self.assertEqual(rows['forbid-path']['status'], 'PASS')
        self.assertEqual(proc.returncode, 1)

    def test_missing_explicit_root_fails_both_checks(self):
        self.root.rmdir()
        proc, rows = self.run_probe(self.base / 'absent')
        for check in ('identifiers', 'unix-socket'):
            self.assertEqual(rows[check]['status'], 'FAIL')
            self.assertIn('missing search root', rows[check]['detail'])
        self.assertEqual(proc.returncode, 1)

    def test_walk_permission_error_fails_both_checks(self):
        (self.base / 'sitecustomize.py').write_text(
            "import os\n"
            "original = os.walk\n"
            "def walk(root, *args, **kwargs):\n"
            "    if root == os.environ['TEST_ROOT']:\n"
            "        kwargs['onerror'](PermissionError('denied test root'))\n"
            "        return iter(())\n"
            "    return original(root, *args, **kwargs)\n"
            "os.walk = walk\n")
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(self.base), TEST_ROOT=str(self.root))
        proc = subprocess.run(['sh', str(SCRIPT), '--identifiers', str(self.identifiers),
                               '--search-root', str(self.root), '--no-network'],
                              capture_output=True, text=True, env=env)
        rows = {row['check']: row for row in map(json.loads, proc.stdout.splitlines())}
        for check in ('identifiers', 'unix-socket'):
            self.assertEqual(rows[check]['status'], 'FAIL')
            self.assertIn('denied test root', rows[check]['detail'])
        self.assertEqual(proc.returncode, 1)

    def test_missing_default_root_is_reported_and_skipped(self):
        (self.base / 'sitecustomize.py').write_text(
            "import os\n"
            "original = os.path.lexists\n"
            "os.path.lexists = lambda p: False if p == '/workspace' else original(p)\n"
            "os.walk = lambda *args, **kwargs: iter(())\n")
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(self.base))
        proc = subprocess.run(['/bin/sh', str(SCRIPT), '--identifiers', str(self.identifiers),
                               '--no-network'], capture_output=True, text=True, env=env)
        rows = {row['check']: row for row in map(json.loads, proc.stdout.splitlines())}
        for check in ('identifiers', 'unix-socket'):
            self.assertEqual(rows[check]['status'], 'PASS')
            self.assertIn('skipped missing default root: /workspace', rows[check]['detail'])

    def test_missing_curl_is_skip(self):
        bin_dir = self.base / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'python3').symlink_to(sys.executable)
        env = dict(os.environ, HOME=str(self.home), PATH=str(bin_dir))
        proc = subprocess.run(['/bin/sh', str(SCRIPT), '--identifiers', str(self.identifiers),
                               '--search-root', str(self.root), '--deny-host', 'example.invalid'],
                              capture_output=True, text=True, env=env)
        rows = {row['check']: row for row in map(json.loads, proc.stdout.splitlines())}
        self.assertEqual(rows['deny-host']['status'], 'SKIP')
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_python_is_skip_exit_two(self):
        proc = subprocess.run(['/bin/sh', str(SCRIPT)], capture_output=True, text=True,
                              env={'PATH': str(self.base)})
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(rows[-1]['status'], 'SKIP')
        self.assertEqual(proc.returncode, 2)

    def test_deny_host_curl_exit_codes(self):
        bin_dir = self.base / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'python3').symlink_to(sys.executable)
        curl = bin_dir / 'curl'
        env = dict(os.environ, HOME=str(self.home), PATH=str(bin_dir))
        for key in ('https_proxy', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
            env.pop(key, None)
        for code, expected in ((28, 'FAIL'), (7, 'PASS'), (6, 'PASS')):
            with self.subTest(code=code):
                curl.write_text('#!/bin/sh\nprintf "000 000"\nexit %d\n' % code)
                curl.chmod(0o755)
                proc = subprocess.run(['/bin/sh', str(SCRIPT), '--identifiers', str(self.identifiers),
                                       '--search-root', str(self.root), '--deny-host', 'example.invalid'],
                                      capture_output=True, text=True, env=env)
                rows = {row['check']: row for row in map(json.loads, proc.stdout.splitlines())}
                self.assertEqual(rows['deny-host']['status'], expected)

    def test_skipped_checks_produce_exit_two(self):
        self.identifiers.write_text('')
        proc, rows = self.run_probe(self.base / 'absent')
        if rows['summary']['status'] != 'FAIL':
            self.assertEqual(rows['summary']['status'], 'SKIP')
            self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
