"""Tests for proof-based executor boundary handling."""
import os
import signal
import subprocess
import sys
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
        (path / "cgroup.events").write_text("populated 0\n")
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

    def test_nested_cgroup_member_prevents_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            boundary.proven = True
            nested = boundary.path / "nested"
            nested.mkdir()
            (nested / "cgroup.procs").write_text("456\n")
            self.assertEqual(boundary.finish(), (False, [456]))
            (nested / "cgroup.procs").write_text("")
            self.assertEqual(boundary.finish(), (True, []))

    def test_unreadable_nested_cgroup_prevents_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            boundary.proven = True
            (boundary.path / "nested").mkdir()
            self.assertEqual(boundary.finish(), (False, None))

    def test_recursive_populated_flag_prevents_release_after_empty_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = self.make_cgroup(tmp)
            boundary.proven = True
            (boundary.path / "cgroup.events").write_text("populated 1\n")
            self.assertEqual(boundary.finish(), (False, None))
            (boundary.path / "cgroup.events").unlink()
            self.assertEqual(boundary.finish(), (False, None))

    @unittest.skipIf(os.name == "nt", "fixture requires detached POSIX sessions")
    def test_peer_reviewer_retains_slot_for_detached_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = Path(tmp) / "reviewer.py"
            script.write_text(
                'import os, pathlib, subprocess, sys, time\n'
                'if len(sys.argv) > 1:\n'
                '    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n'
                '    time.sleep(60)\n'
                'else:\n'
                '    subprocess.Popen([sys.executable, __file__, ' + repr(str(pid_file)) + '], '
                'start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                '    deadline = time.monotonic() + 5\n'
                '    while not pathlib.Path(' + repr(str(pid_file)) + ').exists() and time.monotonic() < deadline: '
                'time.sleep(0.01)\n'
                "    print('{\"findings\": [], \"verdict\": \"approve\"}')\n")
            slots = []

            def reserve(rdir, *args, **kwargs):
                slots.append(rdir)
                xs._FLEET_RESERVATIONS[os.path.realpath(rdir)] = ([], {})
                return True

            try:
                with mock.patch.object(xs, "RUNS", tmp), \
                     mock.patch.object(xs, "reserve_provider_slot", side_effect=reserve), \
                     mock.patch.object(xs, "review_command", return_value=[sys.executable, str(script)]), \
                     mock.patch.object(procboundary.CgroupBoundary, "create", return_value=None), \
                     mock.patch.object(xs, "release_fleet_slot") as release:
                    xs.invoke_reviewer({"backend": "agy"}, "review", tmp, str(Path(tmp) / "review"), {})
                    self.assertTrue(xs.pid_alive(int(pid_file.read_text())))
                    release.assert_not_called()
                    self.assertIn("retained", xs.jload(str(Path(slots[0]) / "status.json"), {})[
                        "fleet_reservation_error"])
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                for slot in slots:
                    xs._FLEET_RESERVATIONS.pop(os.path.realpath(slot), None)

    def test_all_lane_children_attach_before_executor_start(self):
        cases = ((xs.LaneChild, "pi", "fake"), (xs.AgyChild, "agy", "fake"),
                 (xs.CursorHeadlessChild, "cursor", "composer-2.5[fast=false]"))
        for child_class, backend, model in cases:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as tmp:
                boundary = mock.Mock()
                boundary.members.return_value = [123]
                with mock.patch.object(procboundary.CgroupBoundary, "create", return_value=boundary) as create, \
                     mock.patch.object(xs, "configure_executor_environment", return_value=(tmp, {})), \
                     mock.patch.object(xs, "lane_root", return_value=tmp), \
                     mock.patch.object(xs.subprocess, "Popen") as popen, \
                     mock.patch.object(xs.threading, "Thread"):
                    child = child_class({"name": "L1", "backend": backend, "model": model,
                                         "binary": "/fake/executor"}, tmp, {"pi": {"require_isolation": False}})
                    if child_class is not xs.LaneChild:
                        child.start("task")
                    self.assertIs(child.boundary, boundary)
                    self.assertEqual(popen.call_args.kwargs["preexec_fn"], boundary.attach_current_process)
                    boundary.members.assert_called_once()
                    self.assertTrue(boundary.proven)
                    if child_class is not xs.LaneChild:
                        child.start("restart")
                        create.assert_called_once()
                        self.assertIs(child.boundary, boundary)
                        boundary.path.is_dir.return_value = False
                        child.start("new scope after empty boundary removal")
                        self.assertEqual(create.call_count, 2)

    def test_headless_restart_cannot_forget_an_uncontained_executor(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(procboundary.CgroupBoundary, "create", return_value=None) as create, \
             mock.patch.object(xs, "configure_executor_environment", return_value=(tmp, {})), \
             mock.patch.object(xs.subprocess, "Popen") as popen, \
             mock.patch.object(xs.threading, "Thread"):
            child = xs.AgyChild({"backend": "agy", "model": "fake", "binary": "/fake/agy"}, tmp, {})
            child.start("first")
            create.return_value = mock.Mock()
            child.start("restart")
            create.assert_called_once()
            self.assertIsNone(child.boundary)
            self.assertNotIn("preexec_fn", popen.call_args.kwargs)

    def test_peer_reviewer_attaches_before_executor_start(self):
        boundary = mock.Mock()
        boundary.members.return_value = [123]
        with procboundary.track(boundary), mock.patch.object(xs.subprocess, "Popen") as popen:
            popen.return_value.communicate.return_value = ("response", "")
            popen.return_value.returncode = 0
            completed = xs.run_peer_reviewer(["/fake/reviewer"], ".", 1)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(popen.call_args.kwargs["preexec_fn"], boundary.attach_current_process)
        boundary.members.assert_called_once()

    def test_peer_reviewer_release_requires_empty_boundary_on_exit_or_timeout(self):
        for proof in (None, (False, [123]), (True, [])):
            for timeout in (False, True):
                with self.subTest(proof=proof, timeout=timeout), tempfile.TemporaryDirectory() as tmp:
                    boundary = mock.Mock() if proof is not None else None
                    if boundary is not None:
                        boundary.finish.return_value = proof
                    slots = []

                    def reserve(rdir, *args, **kwargs):
                        slots.append(rdir)
                        xs._FLEET_RESERVATIONS[os.path.realpath(rdir)] = ([], {})
                        return True

                    completed = subprocess.CompletedProcess([], 0, '{"findings": [], "verdict": "approve"}', '')
                    try:
                        with mock.patch.object(xs, "RUNS", tmp), \
                             mock.patch.object(xs, "reserve_provider_slot", side_effect=reserve), \
                             mock.patch.object(procboundary.CgroupBoundary, "create", return_value=boundary), \
                             mock.patch.object(xs, "run_peer_reviewer", side_effect=(
                                 subprocess.TimeoutExpired([], 1) if timeout else lambda *a: completed)), \
                             mock.patch.object(xs, "release_fleet_slot", return_value=True) as release:
                            if timeout:
                                with self.assertRaisesRegex(RuntimeError, "reviewer timed out"):
                                    xs.invoke_reviewer({"backend": "agy", "binary": "/fake/agy"},
                                                       "review", tmp, str(Path(tmp) / "review"), {})
                            else:
                                xs.invoke_reviewer({"backend": "agy", "binary": "/fake/agy"},
                                                   "review", tmp, str(Path(tmp) / "review"), {})
                            self.assertEqual(release.call_count, int(proof == (True, [])))
                    finally:
                        for slot in slots:
                            xs._FLEET_RESERVATIONS.pop(os.path.realpath(slot), None)

    def test_lane_release_requires_empty_executor_boundary(self):
        for proof in (None, (False, [123]), (True, [])):
            with self.subTest(proof=proof), tempfile.TemporaryDirectory() as tmp:
                lane = {"name": "L1", "backend": "pi", "model": "fake"}
                spec = {"run_id": "run", "task": "task", "started": xs.NOW(), "budget_minutes": 1}
                workspace = {"repo": tmp, "root": tmp, "path": tmp}
                child = mock.Mock()
                child.boundary = mock.Mock() if proof is not None else None
                if proof is not None:
                    child.boundary.finish.return_value = proof
                rdir = os.path.realpath(os.path.join(tmp, "run"))
                xs._FLEET_RESERVATIONS[rdir] = (["guard"], {
                    "run_id": "run", "token": "a" * 32, "fence": 1})
                finalize = xs.lane_finalize_run

                def finalize_after_boundary(*args, **kwargs):
                    # The lane only records readiness; finalization owns release.
                    release.assert_not_called()
                    self.assertEqual(xs.jload(os.path.join(rdir, "status.json"), {})[
                        "fleet_release_ready"], proof == (True, []))
                    return finalize(*args, **kwargs)

                def release_after_publication(path):
                    self.assertEqual(path, rdir)
                    verdict.assert_called_once()
                    self.assertTrue(os.path.isfile(os.path.join(rdir, "result.json")))

                try:
                    with mock.patch.object(xs, "RUNS", tmp), \
                         mock.patch.object(xs, "lane_prepare_workspace", return_value=workspace), \
                         mock.patch.object(xs, "run_workspace_prepare", return_value=None), \
                         mock.patch.object(xs, "add_lessons_to_prompt", side_effect=lambda p, *a: (p, [])), \
                         mock.patch.object(xs, "add_patterns_to_prompt", side_effect=lambda p, *a: (p, [])), \
                         mock.patch.object(xs, "reserve_provider_slot", return_value=True), \
                         mock.patch.object(xs, "lane_control_messages"), \
                         mock.patch.object(xs, "lane_run_turn", return_value=({}, {}, None, 0, "events", None)), \
                         mock.patch.object(xs, "git", return_value=(0, "", "")), \
                         mock.patch.object(xs, "finalize_workspace", return_value={}), \
                         mock.patch.object(xs, "changed_paths", return_value=[]), \
                         mock.patch.object(xs, "append_ledger"), \
                         mock.patch.object(xs, "cleanup_executor_tmp"), \
                         mock.patch.object(xs, "fleet_guard_answer", return_value=(
                             0, {"ok": True, "valid": True, "fence": 1})) as verdict, \
                         mock.patch.object(xs, "lane_finalize_run", side_effect=finalize_after_boundary) as finalized, \
                         mock.patch.object(xs, "release_fleet_slot", side_effect=release_after_publication) as release:
                        xs.lane_do_run(lane, spec, child, None, {})
                        finalized.assert_called_once()
                        verdict.assert_called_once_with(["guard"], {
                            "op": "validate", "run_id": "run", "token": "a" * 32, "fence": 1})
                        self.assertEqual(release.call_count, int(proof == (True, [])))
                finally:
                    xs._FLEET_RESERVATIONS.pop(rdir, None)

    def test_create_returns_none_when_host_scope_is_not_writable(self):
        with mock.patch.object(procboundary, "_cgroup2_mount", return_value=None):
            self.assertIsNone(procboundary.CgroupBoundary.create())


if __name__ == "__main__":
    unittest.main()
