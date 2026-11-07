"""Tests for the cgroup v2 process boundary helper."""
import builtins
import os
import pathlib
import signal
import tempfile
import unittest
from unittest import mock

from lib.procboundary import detect, release_decision


def process_dead(pid):
    """Treat an unreaped zombie as dead, as well as a vanished process."""
    try:
        stat = pathlib.Path('/proc/{}/stat'.format(pid)).read_text()
    except FileNotFoundError:
        return True
    # The parenthesized command can itself contain spaces and parentheses.
    return stat.rsplit(')', 1)[1].split()[0] == 'Z'


class ProcessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.child = self.root / 'delegated'
        self.child.mkdir()
        (self.root / 'cgroup.controllers').write_text('cpu memory\n')
        (self.child / 'cgroup.procs').write_text('')
        self.original_open = builtins.open

    def detect_here(self):
        def fake_open(path, *args, **kwargs):
            if os.fspath(path) == '/proc/self/cgroup':
                import io
                return io.StringIO('0::/delegated\n')
            return self.original_open(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=fake_open):
            return detect(self.root)

    def test_detects_v2_writable_delegation_and_creates_child(self):
        boundary = self.detect_here()
        self.assertIsNotNone(boundary)
        created = boundary.create('run-1')
        self.assertTrue(pathlib.Path(created.path).is_dir())

    def test_rejects_non_v2_or_nonwritable_delegation(self):
        (self.root / 'cgroup.controllers').unlink()
        self.assertIsNone(self.detect_here())
        (self.root / 'cgroup.controllers').write_text('cpu\n')
        self.child.chmod(0o555)
        self.addCleanup(self.child.chmod, 0o755)
        self.assertIsNone(self.detect_here())

    def test_attach_members_and_cgroup_kill(self):
        boundary = self.detect_here().create('run-1')
        (pathlib.Path(boundary.path) / 'cgroup.procs').write_text('')
        boundary.attach(1234)
        self.assertEqual(boundary.members(), {1234})
        killfile = pathlib.Path(boundary.path) / 'cgroup.kill'
        killfile.write_text('0\n')
        with mock.patch('os.kill') as kill:
            boundary.kill_all(signal.SIGTERM)
            kill.assert_called_once_with(1234, signal.SIGTERM)
        self.assertEqual(killfile.read_text(), '0\n')
        boundary.kill_all(signal.SIGKILL)
        self.assertEqual(killfile.read_text(), '1\n')

    def test_read_failure_and_nonempty_boundary_retain(self):
        self.assertEqual(release_decision(None, set()), 'retain')
        self.assertEqual(release_decision(object(), None), 'retain')
        self.assertEqual(release_decision(object(), {42}), 'retain')
        self.assertEqual(release_decision(object(), set()), 'release')

    def test_process_dead_recognizes_unreaped_zombie(self):
        with mock.patch.object(pathlib.Path, 'read_text', return_value='42 (a ) b) Z 1'):
            self.assertTrue(process_dead(42))
        with mock.patch.object(pathlib.Path, 'read_text', return_value='42 (a) S 1'):
            self.assertFalse(process_dead(42))
        with mock.patch.object(pathlib.Path, 'read_text', side_effect=FileNotFoundError):
            self.assertTrue(process_dead(42))


_REAL_BOUNDARY = detect()


@unittest.skipUnless(_REAL_BOUNDARY is not None,
                     'current process has no writable delegated cgroup v2')
class RealProcessBoundaryTests(unittest.TestCase):
    def test_tracks_and_kills_detached_descendant(self):
        import subprocess
        import sys
        import time
        boundary = _REAL_BOUNDARY.create('procboundary-test-{}'.format(os.getpid()))
        proc = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                marker = pathlib.Path(directory) / 'pid'
                script = (
                    'import os,sys,time\n'
                    'sys.stdin.read(1)\n'
                    'if os.fork(): os._exit(0)\n'
                    'os.setsid()\n'
                    'with open(sys.argv[1] + ".tmp", "w") as f: f.write(str(os.getpid()))\n'
                    'os.replace(sys.argv[1] + ".tmp", sys.argv[1])\n'
                    'time.sleep(60)\n'
                )
                proc = subprocess.Popen([sys.executable, '-c', script, str(marker)],
                                        stdin=subprocess.PIPE)
                boundary.attach(proc.pid)
                proc.stdin.write(b'x')
                proc.stdin.close()
                proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while not marker.exists():
                    if time.monotonic() >= deadline:
                        self.fail('descendant did not write its pid')
                    time.sleep(0.01)
                pid = int(marker.read_text())
                self.assertIn(pid, boundary.members())
                boundary.kill_all(signal.SIGKILL)
                deadline = time.monotonic() + 5
                while not process_dead(pid) or pid in boundary.members():
                    if time.monotonic() >= deadline:
                        self.fail('descendant still alive or in cgroup after kill')
                    time.sleep(0.01)
        finally:
            try:
                boundary.kill_all(signal.SIGKILL)
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        boundary.remove()
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
