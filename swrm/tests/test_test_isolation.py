"""Run the shared fixture guard through the existing suite entry point."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "test_isolation_guard", Path(__file__).resolve().parents[2] / "tools/test_isolation_guard.py")
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
IsolationGuardTests = _guard.IsolationGuardTests

import os
import tempfile
import unittest
from unittest import mock

import _load
import _broker_wait
from _broker_wait import short_tmp_root, _remove_owned_root


class OwnedScratchTests(unittest.TestCase):
    def test_cleanup_preserves_sibling_and_ignores_forked_owner(self):
        first, second = short_tmp_root(), short_tmp_root()
        self.assertNotEqual(first, second)
        marker = Path(second) / 'live'
        marker.touch()
        _remove_owned_root(first, os.getpid() + 1)
        self.assertTrue(Path(first).is_dir())
        _remove_owned_root(first, os.getpid())
        self.assertFalse(Path(first).exists())
        self.assertTrue(marker.exists())
        _remove_owned_root(second, os.getpid())

    def test_unversioned_fixture_cannot_clean_the_enclosing_checkout(self):
        xs = _load.load()
        with tempfile.TemporaryDirectory(dir=short_tmp_root()) as directory:
            self.assertEqual(xs.executor_worktree_root(directory), os.path.realpath(directory))
            # The production helper really runs; a sibling stands in for a live victim.
            marker = Path(directory).parent / 'victim'
            marker.touch()
            xs.configure_executor_environment(directory)
            xs.cleanup_executor_tmp(directory)
            self.assertTrue(marker.exists())

    def test_real_checkout_cleanup_is_rejected_even_if_git_discovery_is_mocked(self):
        xs = _load.load()
        with mock.patch.object(xs, 'executor_worktree_root', return_value=_load._ceiling):
            scratch = Path(_load._ceiling) / '.swrm-tmp'
            before = scratch.exists()
            with mock.patch.object(_load.shutil, 'rmtree') as remove:
                xs.cleanup_executor_tmp('synthetic')   # refused: nothing is removed
                remove.assert_not_called()
            self.assertEqual(scratch.exists(), before)

    def test_cleanup_rejects_unregistered_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            _remove_owned_root(directory, os.getpid())
            self.assertTrue(Path(directory).is_dir())

    def test_cleanup_resolves_once_before_deleting(self):
        xs = _load.load()
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / '.swrm-tmp'
            scratch.mkdir()
            with mock.patch.object(xs, 'executor_worktree_root',
                                   side_effect=[directory, _load._ceiling]) as resolve:
                with mock.patch.object(_load.shutil, 'rmtree', wraps=_load.shutil.rmtree) as remove:
                    xs.cleanup_executor_tmp('synthetic')
                    remove.assert_called_once_with(os.path.realpath(str(scratch)), ignore_errors=True)
                resolve.assert_called_once_with('synthetic')
            self.assertFalse(scratch.exists())

    def test_cleanup_rejects_foreign_and_unregistered_roots(self):
        xs = _load.load()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(xs, 'executor_worktree_root', return_value=directory):
                (Path(directory) / '.swrm-tmp').mkdir()
                with mock.patch.object(_load, '_owned_fixture_roots', set()):
                    xs.cleanup_executor_tmp('synthetic')
                    self.assertTrue((Path(directory) / '.swrm-tmp').exists())
                with mock.patch.object(_load, '_owned_fixture_roots',
                                       {(directory, os.getpid() + 1)}):
                    xs.cleanup_executor_tmp('synthetic')
                    self.assertTrue((Path(directory) / '.swrm-tmp').exists())

    def test_cleanup_unlinks_scratch_symlink_without_touching_sibling(self):
        xs = _load.load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / 'victim'
            victim.mkdir()
            marker = victim / 'live'
            marker.touch()
            (root / '.swrm-tmp').symlink_to(victim, target_is_directory=True)
            xs.cleanup_executor_tmp(directory)
            self.assertFalse((root / '.swrm-tmp').exists())
            self.assertTrue(marker.exists())

    def test_short_root_falls_back_when_tmpdir_is_long(self):
        with mock.patch.object(_broker_wait.tempfile, 'gettempdir',
                               return_value='/long/' + 'fixture' * 20):
            with mock.patch.object(_broker_wait.tempfile, 'mkdtemp',
                                   return_value='/tmp/private-root') as allocate:
                with mock.patch.object(_broker_wait.atexit, 'register'):
                    root = short_tmp_root()
        allocate.assert_called_once_with(prefix='', dir='/tmp')
        _broker_wait._owned_roots.discard((root, os.getpid()))

    def test_socket_root_leaves_room_for_socket_filename(self):
        root = short_tmp_root()
        self.addCleanup(_remove_owned_root, root, os.getpid())
        self.assertLess(len(os.fsencode(root + '/fixture/broker.sock')), 100)
