"""Tests for the cgroup v2 process boundary helper."""
import builtins
import os
import pathlib
import signal
import tempfile
import unittest
from unittest import mock

from lib.procboundary import detect, release_decision


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
        self.assertIsNone(self.detect_here())

    def test_attach_members_and_cgroup_kill(self):
        boundary = self.detect_here().create('run-1')
        (pathlib.Path(boundary.path) / 'cgroup.procs').write_text('')
        boundary.attach(1234)
        self.assertEqual(boundary.members(), {1234})
        killfile = pathlib.Path(boundary.path) / 'cgroup.kill'
        killfile.write_text('0\n')
        boundary.kill_all(signal.SIGTERM)
        self.assertEqual(killfile.read_text(), '1\n')

    def test_read_failure_and_nonempty_boundary_retain(self):
        self.assertEqual(release_decision(None, set()), 'retain')
        self.assertEqual(release_decision(object(), None), 'retain')
        self.assertEqual(release_decision(object(), {42}), 'retain')
        self.assertEqual(release_decision(object(), set()), 'release')


_REAL_BOUNDARY = detect()


@unittest.skipUnless(_REAL_BOUNDARY is not None,
                     'current process has no writable delegated cgroup v2')
class RealProcessBoundaryTests(unittest.TestCase):
    def test_tracks_and_kills_detached_descendant(self):
        import subprocess
        boundary = _REAL_BOUNDARY.create('procboundary-test-{}'.format(os.getpid()))
        try:
            proc = subprocess.Popen([
                'python3', '-c',
                'import os,time; p=os.fork(); '
                'os._exit(0) if p else (os.setsid(), time.sleep(60))',
            ])
            boundary.attach(proc.pid)
            proc.wait(timeout=5)
            self.assertIn(proc.pid, boundary.members())
            boundary.kill_all(signal.SIGKILL)
        finally:
            try:
                boundary.kill_all(signal.SIGKILL)
            finally:
                boundary.remove()
