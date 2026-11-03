"""The patrol of the control plane produces the queued diagnosis (Refs #1687).

The lane autodispatcher is the resident patrol of the box: every tick it asks
the openswarm CLI for the queued diagnosis of the fleet.  The command writes
the section into the health snapshot of the control plane and appends it to
the brain log, so a run that waits for hours always says which layer blocks
it.  This module proves the call chain: patrol -> CLI -> health.json + brain
log.
"""
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

AUTODISPATCH = HERE.parents[1] / "tools" / "lane-autodispatch" / "autodispatch.py"
_spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autodispatch)


class PatrolCallChainTests(unittest.TestCase):
    """The patrol asks the CLI for the diagnosis, and the CLI writes the files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-queued-patrol-")
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "runs").mkdir()
        self.ctl = self.root / "ctl"
        (self.ctl / "state").mkdir(parents=True)
        previous = os.environ.get("OPENSWARM_CTL_STATE_DIR")
        os.environ["OPENSWARM_CTL_STATE_DIR"] = str(self.ctl / "state")
        self.addCleanup(self.restore_ctl_state, previous)
        # The state directory of the patrol stays inside the sandbox.
        self.state = self.root / "autodispatch"
        self.state.mkdir()
        patcher = mock.patch.object(autodispatch, "D", str(self.state))
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore_ctl_state(self, previous):
        if previous is None:
            os.environ.pop("OPENSWARM_CTL_STATE_DIR", None)
        else:
            os.environ["OPENSWARM_CTL_STATE_DIR"] = previous

    def queued_run(self, run_id, started):
        """A run of the box that waits in the queued state."""
        directory = self.home / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "status.json").write_text(json.dumps(
            {"id": run_id, "state": "queued", "lane": "M17", "backend": "pi",
             "provider": "minimax-cn", "started": started}), encoding="utf-8")
        return run_id

    # ------------------------------------------------------------- the patrol

    def test_the_patrol_runs_the_queued_diagnosis_command(self):
        calls = []
        with mock.patch.object(autodispatch, "sh",
                               side_effect=lambda cmd, timeout=120, inp=None: (
                                   calls.append(cmd), (0, "{}", ""))[1]):
            autodispatch.queued_diagnosis()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ["fleet", "queued-diagnosis", "--json"])

    def test_the_patrol_uses_the_configured_cli(self):
        with mock.patch.dict(os.environ, {"AD_OPENSWARM_CLI": "/opt/openswarm"}):
            self.assertEqual(autodispatch.openswarm_cli(), "/opt/openswarm")

    def test_a_failing_diagnosis_never_breaks_the_patrol(self):
        with mock.patch.object(autodispatch, "sh", side_effect=OSError("no such file")):
            with mock.patch.object(autodispatch, "log") as logged:
                self.assertIsNone(autodispatch.queued_diagnosis())
        self.assertTrue(logged.called)

    def test_the_tick_of_the_patrol_produces_the_diagnosis(self):
        done = threading.Event()
        calls = []
        patches = {
            "log": mock.Mock(),
            "save_state": mock.Mock(),
            "status_snapshot": mock.Mock(return_value={}),
            "queue": mock.Mock(return_value=[]),
            "prefetch_labels": mock.Mock(),
            "replenish": mock.Mock(return_value=0),
            "queued_diagnosis": mock.Mock(side_effect=lambda: (calls.append("queued"), done.set())),
        }
        with mock.patch.multiple(autodispatch, **patches):
            autodispatch.tick({}, {}, 0)
        self.assertTrue(done.wait(5), "the patrol did not ask for the queued diagnosis")
        self.assertEqual(calls, ["queued"])

    # ---------------------------------------------------------------- the CLI

    def test_the_command_writes_the_control_plane_files(self):
        """The command of the patrol fills health.json and the brain log."""
        run_id = self.queued_run("20261102-213000-m17aa", 1700000000.0 - 1800)
        args = mock.Mock(hosts=None, json=False, health_json=None, brain_log=None)
        with mock.patch.object(sys, "stdout", new=io.StringIO()):
            section = xs.cmd_fleet_queued_diagnosis(
                args, policy={}, hosts={"worker-a": {"name": "worker-a", "kind": "local"}},
                now=1700000000.0, home=self.home)
        payload = json.loads((self.ctl / "autodispatch" / "health.json").read_text())
        self.assertEqual(section["runs"][0]["run_id"], run_id)
        self.assertEqual(payload["queued"]["runs"][0]["run_id"], run_id)
        self.assertIn(run_id, (self.ctl / "brain" / "log.md").read_text())

    def test_the_command_is_wired_in_the_cli(self):
        """`openswarm fleet queued-diagnosis` exists."""
        result = subprocess.run([sys.executable, str(HERE.parent / "bin" / "openswarm"),
                                 "fleet", "queued-diagnosis", "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("queued-diagnosis", result.stdout)


if __name__ == "__main__":
    unittest.main()
