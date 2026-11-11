"""A run may only write its result back while it still holds the fence.

These tests drive the real coordinator (swrm/bin/fleet-capacity) through a real
run: the run proves its fencing token before the result is written back, a run
that lost its reservation is fenced instead of reported as done, and the fence
never repeats across runs.
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin' / 'fleet-capacity'
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


FAKE_AGY = '''#!/usr/bin/env python3
import sys
if sys.argv[1:2] == ["models"]:
    print("gemini-3.8-flash-medium")
print("{}")
'''


def ns(**kw):
    base = dict(type="impl", difficulty=3, context_tokens=0, backend="agy", model=None, effort=None,
                prompt="add a function add(a, b)", task_file=None, title="fence test", cwd=".",
                no_worktree=True, verify=None, verify_baseline=False, keep_worktree=False, no_fallback=False,
                budget_minutes=5, detach=False, run_id=None, dry_run=False, no_auto_review=True)
    base.update(kw)
    return argparse.Namespace(**base)


class SafeBoundary:
    """A boundary that proves the executor left no descendants behind."""

    def finish(self):
        return True, None

    def attach_current_process(self):
        pass


def plain_executor(_decision, _state, _prompt, wd, _rdir, _timeout, _event_file):
    (pathlib.Path(wd) / "fence.txt").write_text("fence test\n")
    final = {"status": "done", "summary": "fence test", "files_changed": ["fence.txt"],
             "tests": "none", "followups": []}
    return 0, final, {"usage": {}}, ""


def losing_executor(_decision, _state, _prompt, wd, rdir, _timeout, _event_file):
    """An executor whose reservation is gone before the result is written back."""
    saved = json.loads((pathlib.Path(rdir) / "fleet-reservation.json").read_text())
    payload = saved["payload"]
    subprocess.run([sys.executable, str(BIN), "broker"], text=True, capture_output=True, timeout=15,
                   env=dict(os.environ),
                   input=json.dumps({"op": "release", "run_id": payload["run_id"],
                                     "token": payload["token"]}))
    return plain_executor(_decision, _state, _prompt, wd, rdir, _timeout, _event_file)


class FleetFencingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-fencing-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for name, value in (("DATA", str(self.root / "data")),
                            ("RUNS", str(self.root / "data" / "runs")),
                            ("LEDGER", str(self.root / "data" / "ledger.jsonl")),
                            ("BREAKERS", str(self.root / "data" / "breakers.json"))):
            patch = mock.patch.object(xs, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.guard = self.root / "guard.json"
        self.guard.write_text(json.dumps({"command": [sys.executable, str(BIN), "broker"]}))
        self.capacity = self.root / "capacity"
        self.agy = self.root / "agy"
        self.agy.write_text(FAKE_AGY)
        self.agy.chmod(0o755)
        xs._AGY_LOGIN_CACHE.clear()
        self.addCleanup(self.drop_reservations)

    def drop_reservations(self):
        """Leave no reservation of this test behind in the shared cache."""
        prefix = os.path.realpath(str(self.root))
        for key in [key for key in xs._FLEET_RESERVATIONS
                    if os.path.realpath(key).startswith(prefix)]:
            xs._FLEET_RESERVATIONS.pop(key, None)

    def coordinator(self, **payload):
        env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=str(self.capacity))
        return json.loads(subprocess.run([sys.executable, str(BIN), "broker"], text=True,
                                         input=json.dumps(payload), capture_output=True,
                                         env=env, timeout=15).stdout)

    def dispatch(self, executor=plain_executor, finalize_hook=None, verify_hook=None):
        finalize = xs.finalize_workspace

        def finish(ws, title, backend, rdir, keep):
            if finalize_hook:
                finalize_hook(pathlib.Path(rdir))
            return finalize(ws, title, backend, rdir, keep)

        def verify(*args):
            run_dir = next(pathlib.Path(xs.RUNS).iterdir())
            verify_hook(run_dir)
            return {"rc": 0, "output": "passed", "failing": []}

        env = {"OPENSWARM_FLEET_GUARD_CONFIG": str(self.guard),
               "OPENSWARM_FLEET_CAPACITY_DIR": str(self.capacity),
               "OPENSWARM_STATE_FILE": _load.FIXTURE_STATE,
               "OPENSWARM_AGY_BIN": str(self.agy)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict(xs.EXECUTORS, {"agy": executor}), \
                mock.patch.object(xs.procboundary.CgroupBoundary, "create", return_value=SafeBoundary()), \
                mock.patch.object(xs, "start_fleet_release_worker"), \
                mock.patch.object(xs, "finalize_workspace", side_effect=finish), \
                mock.patch.object(xs, "run_verify", side_effect=verify), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                xs.cmd_run(ns(cwd=str(self.cwd), verify="check" if verify_hook else None), xs.load_policy())
            except SystemExit:
                pass
        with open(xs.LEDGER) as stream:
            entry = json.loads(stream.readlines()[-1])
        run_dir = pathlib.Path(xs.RUNS) / entry["id"]
        result = json.loads((run_dir / "result.json").read_text())
        status = json.loads((run_dir / "status.json").read_text())
        return run_dir, result, status

    def reservation_of(self, run_dir):
        return json.loads((run_dir / "fleet-reservation.json").read_text())["payload"]

    def fence_of(self, run_dir):
        return self.reservation_of(run_dir).get("fence")

    def test_a_run_that_holds_its_fence_writes_the_result_back(self):
        run_dir, result, status = self.dispatch()
        fence = self.fence_of(run_dir)
        self.assertIsInstance(fence, int)
        self.assertGreater(fence, 0)
        self.assertEqual(result["status"], "done")
        self.assertNotIn("fence_rejected", result)
        self.assertNotEqual(status.get("status"), "fenced")
        # The coordinator still holds the reservation this run proved.
        rows = self.coordinator(op="status")["reservations"]
        self.assertEqual([row["run_id"] for row in rows], [run_dir.name])
        held = self.coordinator(op="validate", run_id=run_dir.name,
                                token=self.reservation_of(run_dir)["token"], fence=fence)
        self.assertTrue(held.get("ok"), held)

    def test_a_run_that_lost_its_reservation_is_fenced_instead_of_done(self):
        run_dir, result, status = self.dispatch(executor=losing_executor)
        self.assertNotEqual(result["status"], "done")
        self.assertEqual(result["status"], "fenced")
        self.assertEqual(result["error_class"], "fenced")
        self.assertEqual(result["fence_rejected"], "missing")
        self.assertEqual(status.get("status"), "fenced")
        self.assertEqual(status.get("fence_rejected"), "missing")
        traces = "".join(path.read_text(errors="replace") for path in sorted(run_dir.iterdir())
                         if path.is_file() and path.name not in
                         ("result.json", "status.json", "fleet-reservation.json"))
        self.assertIn("fence", traces.lower(), "the refusal left no trace in the run event log")

    def release_at_writeback(self, run_dir):
        saved = self.reservation_of(run_dir)
        self.assertTrue(self.coordinator(op="release", run_id=saved["run_id"],
                                         token=saved["token"])["released"])
        refused = self.coordinator(op="validate", run_id=saved["run_id"],
                                   token=saved["token"], fence=saved["fence"])
        self.assertFalse(refused["ok"])

    def assert_fenced_writeback(self, run_dir, result, status, reason="missing"):
        self.assertEqual(result["status"], "fenced")
        self.assertEqual(result["error_class"], "fenced")
        self.assertEqual(result["fence_rejected"], reason)
        self.assertEqual(status["status"], "fenced")
        self.assertEqual(status["fence_rejected"], reason)
        events = [json.loads(line) for path in run_dir.glob("*.jsonl")
                  for line in path.read_text().splitlines()]
        self.assertTrue(any(event.get("type") == "FENCE_REJECTED" for event in events))
        entry = json.loads(pathlib.Path(xs.LEDGER).read_text().splitlines()[-1])
        self.assertEqual(entry["status"], "fenced")
        self.assertFalse(entry["exec_pass"])

    def test_release_during_finalize_is_rejected_at_result_writeback(self):
        self.assert_fenced_writeback(*self.dispatch(finalize_hook=self.release_at_writeback))

    def test_release_during_verify_is_rejected_at_result_writeback(self):
        self.assert_fenced_writeback(*self.dispatch(verify_hook=self.release_at_writeback))

    def test_local_release_during_finalize_cannot_erase_writeback_identity(self):
        def release(run_dir):
            xs.release_fleet_slot(str(run_dir))
            self.assertTrue(xs.retry_fleet_release(str(run_dir)))
            self.assertFalse((run_dir / "fleet-reservation.json").exists())

        self.assert_fenced_writeback(*self.dispatch(finalize_hook=release))

    def test_expiry_during_verify_is_rejected_at_result_writeback(self):
        def expire(run_dir):
            saved = self.reservation_of(run_dir)
            with sqlite3.connect(self.capacity / "capacity.sqlite") as db:
                db.execute("UPDATE reservations SET lease_expires=0 WHERE token=?", (saved["token"],))

        self.assert_fenced_writeback(*self.dispatch(verify_hook=expire), reason="expired")

    def test_normal_release_follows_result_publication(self):
        release = xs.release_fleet_slot
        published = []

        def checked_release(rdir):
            result = json.loads((pathlib.Path(rdir) / "result.json").read_text())
            published.append(result["status"])
            saved = self.reservation_of(pathlib.Path(rdir))
            held = self.coordinator(op="validate", run_id=saved["run_id"],
                                    token=saved["token"], fence=saved["fence"])
            self.assertTrue(held["ok"])
            return release(rdir)

        with mock.patch.object(xs, "release_fleet_slot", side_effect=checked_release):
            run_dir, result, _status = self.dispatch()
        self.assertEqual(result["status"], "done")
        self.assertEqual(published, ["done"])
        self.assertTrue((run_dir / "fleet-release-pending.json").exists())

    def test_the_fence_is_monotonic_and_never_reused_across_runs(self):
        first_dir, _first, _first_status = self.dispatch()
        first = self.fence_of(first_dir)
        # Give the coordinator the slot back so the next run can be admitted.
        saved = self.reservation_of(first_dir)
        self.assertTrue(self.coordinator(op="release", run_id=saved["run_id"],
                                         token=saved["token"])["released"])
        second_dir, _second, _second_status = self.dispatch()
        second = self.fence_of(second_dir)
        self.assertGreater(second, first)
        self.assertNotEqual(second, first)


if __name__ == '__main__':
    unittest.main()
