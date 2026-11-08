"""Tests for proof-based executor boundary handling."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()
procboundary = xs.procboundary


class ProcessBoundaryTests(unittest.TestCase):
    def make_cgroup(self, root):
        path = Path(root) / "scope"
        path.mkdir()
        (path / "cgroup.procs").write_text("")
        return procboundary.CgroupBoundary(path)

    def test_empty_boundary_is_proven_only_after_a_member_was_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            self.assertEqual(boundary.finish(), (False, []))

    def test_boundary_retains_while_any_descendant_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            (boundary.path / "cgroup.procs").write_text("123\n")
            self.assertEqual(boundary.finish(), (False, [123]))
            self.assertTrue(boundary.path.exists())

    def test_observed_boundary_can_release_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            (boundary.path / "cgroup.procs").write_text("123\n")
            self.assertEqual(boundary.finish(), (False, [123]))
            (boundary.path / "cgroup.procs").write_text("")
            self.assertEqual(boundary.finish(), (True, []))

    def test_unreadable_boundary_cannot_prove_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            (boundary.path / "cgroup.procs").unlink()
            self.assertEqual(boundary.finish(), (False, None))

    def test_create_returns_none_when_host_scope_is_not_writable(self):
        with mock.patch.object(procboundary, "_cgroup2_mount", return_value=None):
            self.assertIsNone(procboundary.CgroupBoundary.create())


if __name__ == "__main__":
    unittest.main()
