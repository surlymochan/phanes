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
            with self.assertRaisesRegex(AssertionError, 'real checkout'):
                xs.cleanup_executor_tmp('synthetic')

    def test_cleanup_rejects_unregistered_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            _remove_owned_root(directory, os.getpid())
            self.assertTrue(Path(directory).is_dir())
