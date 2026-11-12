"""A fleet run may only write its result back while it still holds the fence.

The fleet guard learns the fencing token when it acquires the reservation.
Before the run result is written back the guard has to ask the coordinator
whether that (run_id, token, fence) still holds the reservation. A refused
fence must not be written back as a success: the run is marked fenced and the
refusal is recorded, never dropped silently. Without a fleet guard (single
machine) nothing changes.
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

FAKE_AGY = '''#!/usr/bin/env python3
import sys
if sys.argv[1:2] == ["models"]:
    print("gemini-3.8-flash-medium")
print("{}")
'''

COORDINATOR = '''#!/usr/bin/env python3
"""A fake fleet coordinator: it records every request and answers the fence protocol."""
import json, os, pathlib, sys, time

request = json.load(sys.stdin)
with pathlib.Path(os.environ["FENCE_LOG"]).open("a") as stream:
    stream.write(json.dumps(request) + "\\n")

op = request.get("op")
fence = int(os.environ.get("FENCE_VALUE", "7"))
if op == "acquire":
    answer = {"ok": True, "admitted": True, "existing": False, "fence": fence,
              "lease_expires": time.time() + 300, "lease_ttl_seconds": 300}
elif op == "validate":
    verdict = os.environ.get("FENCE_VALIDATE", "ok")
    if verdict == "ok":
        answer = {"ok": True, "valid": True, "fence": fence}
    else:
        reason = verdict.split(":", 1)[1] if ":" in verdict else "fence_mismatch"
        answer = {"ok": False, "valid": False, "admitted": False,
                  "reason": reason, "error": reason, "fence_rejected": reason}
elif op == "renew":
    answer = {"ok": True, "lease_expires": time.time() + 300, "lease_ttl_seconds": 300}
else:
    answer = {"ok": True, "released": True}
print(json.dumps(answer))
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


def fake_executor(_decision, _state, _prompt, wd, _rdir, _timeout, _event_file):
    (pathlib.Path(wd) / "fence.txt").write_text("fence test\n")
    final = {"status": "done", "summary": "fence test", "files_changed": ["fence.txt"],
             "tests": "none", "followups": []}
    return 0, final, {"usage": {}}, ""


class FleetFenceWriteBackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-fence-")
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
        self.log = self.root / "coordinator.jsonl"
        self.script = self.root / "coordinator.py"
        self.script.write_text(COORDINATOR)
        self.guard = self.root / "guard.json"
        self.guard.write_text(json.dumps({"command": [sys.executable, str(self.script)]}))
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

    def requests(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def dispatch(self, verdict="ok", fleet=True):
        env = {"OPENSWARM_FLEET_GUARD_CONFIG":
               str(self.guard if fleet else self.root / "no-guard.json"),
               "OPENSWARM_FLEET_CAPACITY_DIR": str(self.root / "capacity"),
               "OPENSWARM_STATE_FILE": _load.FIXTURE_STATE,
               "FENCE_LOG": str(self.log),
               "FENCE_VALIDATE": verdict,
               "FENCE_VALUE": "7",
               "OPENSWARM_AGY_BIN": str(self.agy)}
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict(xs.EXECUTORS, {"agy": fake_executor}), \
                mock.patch.object(xs.procboundary.CgroupBoundary, "create", return_value=SafeBoundary()), \
                mock.patch.object(xs, "start_fleet_release_worker"), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                xs.cmd_run(ns(cwd=str(self.cwd)), xs.load_policy())
            except SystemExit:
                pass
        with open(xs.LEDGER) as stream:
            entry = json.loads(stream.readlines()[-1])
        run_dir = pathlib.Path(xs.RUNS) / entry["id"]
        result = json.loads((run_dir / "result.json").read_text())
        status = json.loads((run_dir / "status.json").read_text())
        return run_dir, result, status

    def test_a_held_fence_writes_the_result_back(self):
        run_dir, result, _status = self.dispatch()
        calls = self.requests()
        self.assertTrue(any(call.get("op") == "acquire" for call in calls), calls)
        validations = [call for call in calls if call.get("op") == "validate"]
        self.assertTrue(validations, "the result was written back without asking the coordinator: %r" % (calls,))
        self.assertEqual(validations[0]["fence"], 7)
        self.assertEqual(validations[0]["run_id"], run_dir.name)
        self.assertEqual(validations[0]["token"], calls[0]["token"])
        self.assertEqual(result["status"], "done")
        self.assertNotIn("fence_rejected", result)

    def test_a_refused_fence_is_not_written_back_as_a_success(self):
        run_dir, result, status = self.dispatch(verdict="fence_mismatch")
        validations = [call for call in self.requests() if call.get("op") == "validate"]
        self.assertTrue(validations, "the refused fence was never reported: %r" % (self.requests(),))
        self.assertNotEqual(result["status"], "done")
        markers = {result.get("status"), result.get("error_class"), status.get("status"),
                   status.get("state"), status.get("error_class")}
        self.assertIn("fenced", markers)
        self.assertIn("fence_mismatch", str(result.get("fence_rejected") or ""))
        traces = "".join(path.read_text(errors="replace") for path in sorted(run_dir.iterdir())
                         if path.is_file() and path.name not in
                         ("result.json", "status.json", "fleet-reservation.json"))
        self.assertIn("fence", traces.lower(), "the refusal left no trace in the run event log")

    def test_a_single_machine_run_is_unaffected(self):
        run_dir, result, _status = self.dispatch(fleet=False)
        self.assertEqual(result["status"], "done")
        self.assertNotIn("fence_rejected", result)
        self.assertFalse((run_dir / "fleet-reservation.json").exists())
        self.assertEqual(self.requests(), [])


if __name__ == "__main__":
    unittest.main()
