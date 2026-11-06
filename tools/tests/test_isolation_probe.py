import json
import os
from pathlib import Path
import subprocess
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
        self.assertIn(proc.returncode, (0, 1))  # Other host checks can fail.

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


if __name__ == '__main__':
    unittest.main()
