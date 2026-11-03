"""The lane server health probe: a wedged app-server is restarted and verified (Refs #1709).

The 2026-11-02 CX9 incident: four runs on four cards failed without a commit and
without an error class while the ``codex app-server`` of the lane had been running
for hours and the lane serve process was alive - new sessions simply never started.
Nothing watched the lane server itself, so the failure only showed up after a run
had already failed.

These tests cover the decision (the pure module), the supervisor glue (probe,
empty-run verification, restart) and the lane itself (a wedged fake app-server is
restarted, a failed verification keeps the lane paused).
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"
FIXTURES = ROOT / "tests" / "fixtures"
# The scratch root of the worktree: the lane home lives here, so the AF_UNIX
# control socket stays short enough for a lane test.
SCRATCH = ROOT.parent / ".openswarm-tmp"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
sys.modules["openswarm"] = xs

probe_module = xs.lane_probe_module()


class LaneProbeSettingsTests(unittest.TestCase):
    def test_the_defaults_probe_every_two_minutes(self):
        settings = probe_module.settings({})

        self.assertEqual(settings["interval_s"], 120)
        self.assertEqual(settings["timeout_s"], 20)
        self.assertEqual(settings["failures"], 3)
        self.assertEqual(settings["verify_timeout_s"], 180)
        self.assertEqual(settings["method"], "initialize")

    def test_the_policy_overrides_the_thresholds(self):
        settings = probe_module.settings({"lane": {"server_probe": {
            "interval_s": 30, "timeout_s": 5, "failures": 2, "verify_timeout_s": 60,
            "restart_limit": 1, "restart_window_s": 120, "method": "model/list"}}})

        self.assertEqual(settings["interval_s"], 30)
        self.assertEqual(settings["timeout_s"], 5)
        self.assertEqual(settings["failures"], 2)
        self.assertEqual(settings["verify_timeout_s"], 60)
        self.assertEqual(settings["restart_limit"], 1)
        self.assertEqual(settings["restart_window_s"], 120)
        self.assertEqual(settings["method"], "model/list")

    def test_an_interval_of_zero_turns_the_probe_off(self):
        self.assertEqual(probe_module.settings({"lane": {"server_probe": {"interval_s": 0}}})["interval_s"], 0)

    def test_unusable_values_fall_back_to_the_defaults(self):
        settings = probe_module.settings({"lane": {"server_probe": {
            "interval_s": "soon", "timeout_s": -1, "failures": 0, "verify_timeout_s": None}}})

        self.assertEqual(settings["interval_s"], 120)
        self.assertEqual(settings["timeout_s"], 20)
        self.assertEqual(settings["failures"], 3)
        self.assertEqual(settings["verify_timeout_s"], 180)


class LaneProbeStateTests(unittest.TestCase):
    def test_the_server_counts_as_wedged_after_the_threshold(self):
        probe = probe_module.ServerProbe(failures=3)

        probe.record_failure(100.0, "no answer within 20s")
        probe.record_failure(200.0, "no answer within 20s")
        self.assertFalse(probe.wedged())

        probe.record_failure(300.0, "no answer within 20s")
        self.assertTrue(probe.wedged())

    def test_an_answered_probe_clears_the_failure_streak(self):
        probe = probe_module.ServerProbe(failures=2)
        probe.record_failure(100.0, "no answer")
        probe.record_success(200.0)

        self.assertEqual(probe.failures, 0)
        self.assertFalse(probe.wedged())
        self.assertTrue(probe.alive)
        self.assertEqual(probe.last_success_at, 200.0)
        self.assertIsNone(probe.last_error)

    def test_a_restart_counts_and_clears_the_failures(self):
        probe = probe_module.ServerProbe(failures=2)
        probe.record_failure(100.0, "no answer")
        probe.record_failure(200.0, "no answer")

        probe.note_restart(300.0)

        self.assertEqual(probe.restarts, 1)
        self.assertEqual(probe.failures, 0)
        self.assertFalse(probe.wedged())

    def test_a_lane_that_keeps_wedging_stops_restarting(self):
        probe = probe_module.ServerProbe(failures=1, restart_limit=2, restart_window_s=3600)
        probe.note_restart(100.0)
        self.assertFalse(probe.restart_storm(100.0))

        probe.note_restart(200.0)
        self.assertTrue(probe.restart_storm(200.0))

    def test_old_restarts_leave_the_window(self):
        probe = probe_module.ServerProbe(failures=1, restart_limit=1, restart_window_s=100)
        probe.note_restart(100.0)

        self.assertFalse(probe.restart_storm(250.0))

    def test_the_probe_state_survives_a_supervisor_restart(self):
        probe = probe_module.ServerProbe(failures=3)
        probe.record_success(100.0)
        probe.record_failure(200.0, "no answer")
        probe.note_restart(300.0)

        restored = probe_module.ServerProbe.from_dict(probe.to_dict(), failures=3)

        self.assertEqual(restored.restarts, 1)
        self.assertEqual(restored.failures, 0)
        self.assertEqual(restored.last_success_at, 100.0)
        self.assertEqual(restored.restarted_at, [300.0])

    def test_a_damaged_probe_state_starts_over(self):
        restored = probe_module.ServerProbe.from_dict({"failures": "many", "restarts": None})

        self.assertEqual(restored.failures, 0)
        self.assertEqual(restored.restarts, 0)
        self.assertIsNone(restored.alive)

    def test_the_probe_is_restored_with_the_policy_thresholds(self):
        policy = {"lane": {"server_probe": {"failures": 5}}}
        probe = probe_module.probe_from(policy, {"failures": 2})

        self.assertEqual(probe.threshold, 5)
        self.assertEqual(probe.failures, 2)


class LaneProbeBrainLogTests(unittest.TestCase):
    def test_the_brain_line_names_the_lane_and_the_event(self):
        line = probe_module.brain_line("CX9", "lane server wedged", now=1_700_000_000.0)

        self.assertIn("CX9", line)
        self.assertIn("lane-probe", line)
        self.assertTrue(line.startswith("- "))

    def test_the_restart_message_carries_the_uptime_and_the_basis(self):
        message = probe_module.restart_message(4211.4, "no answer within 20s", 3)

        self.assertIn("uptime 4211s", message)
        self.assertIn("no answer within 20s", message)
        self.assertIn("3 failed probes", message)
        self.assertIn("restarting", message)


class LaneProbeStatusTests(unittest.TestCase):
    def test_the_health_output_carries_the_probe_state(self):
        status = probe_module.status({"alive": False, "last_success_at": 100.0, "restarts": 2,
                                      "failures": 3, "last_error": "no answer"})

        self.assertFalse(status["alive"])
        self.assertEqual(status["last_success_at"], 100.0)
        self.assertEqual(status["restarts"], 2)
        self.assertEqual(status["failures"], 3)
        self.assertEqual(status["last_error"], "no answer")

    def test_a_lane_without_a_probe_reports_nothing(self):
        status = probe_module.status(None)

        self.assertIsNone(status["alive"])
        self.assertIsNone(status["last_success_at"])
        self.assertEqual(status["restarts"], 0)


class FakeProbeChild:
    """A lane child that answers (or does not answer) the health probe."""

    def __init__(self, answer=None, alive=True, error=None):
        self.answer = answer
        self.alive_flag = alive
        self.error = error
        self.sent = []
        self.closed = False
        self.started_at = 1_700_000_000.0

    def send(self, obj):
        self.sent.append(obj)

    def alive(self):
        return self.alive_flag

    def close(self):
        self.closed = True
        self.alive_flag = False

    def next_event(self, timeout=0):
        if self.answer is None:
            return False
        answer, self.answer = self.answer, None
        return answer

    def probe(self, timeout=20, method="initialize"):
        """The probe of a real LaneChild, replayed against the fake events."""
        request = {"jsonrpc": "2.0", "id": "probe-1", "method": method, "params": {}}
        self.sent.append(request)
        if not self.alive_flag:
            return False, "executor process exited"
        answer = self.next_event(0)
        if answer is False:
            return False, f"no answer within {float(timeout):g}s"
        if "error" in answer:
            return True, f"answered with an error: {answer['error']}"
        return True, "answered"


class LaneProbeCheckTests(unittest.TestCase):
    def test_an_answered_probe_means_the_server_is_alive(self):
        child = FakeProbeChild(answer={"jsonrpc": "2.0", "id": "probe-1", "result": {"serverInfo": {}}})

        ok, detail = xs.lane_probe_check(child, {"timeout_s": 5, "method": "initialize"})

        self.assertTrue(ok)
        self.assertEqual(detail, "answered")

    def test_a_json_rpc_error_still_proves_the_server_answers(self):
        child = FakeProbeChild(answer={"jsonrpc": "2.0", "id": "probe-1", "error": {"code": -32601}})

        ok, _ = xs.lane_probe_check(child, {"timeout_s": 5, "method": "initialize"})

        self.assertTrue(ok)

    def test_a_silent_server_is_not_alive(self):
        child = FakeProbeChild(answer=None)

        ok, detail = xs.lane_probe_check(child, {"timeout_s": 5, "method": "initialize"})

        self.assertFalse(ok)
        self.assertIn("no answer", detail)

    def test_a_dead_server_is_not_alive(self):
        child = FakeProbeChild(answer=None, alive=False)

        ok, detail = xs.lane_probe_check(child, {"timeout_s": 5, "method": "initialize"})

        self.assertFalse(ok)
        self.assertIn("exited", detail)

    def test_a_missing_server_is_not_alive(self):
        ok, detail = xs.lane_probe_check(None, {"timeout_s": 5, "method": "initialize"})

        self.assertFalse(ok)
        self.assertIn("no lane server", detail)


class FakeVerifyChild:
    """A lane child that answers the empty-run verification."""

    def __init__(self, events=None, thread_reply=None, turn_reply=None):
        self.events = list(events or [])
        self.thread_reply = thread_reply if thread_reply is not None else {"thread": {"id": "t1"}}
        self.turn_reply = turn_reply if turn_reply is not None else {"turn": {"id": "u1"}}
        self.sent = []
        self.closed = False
        self.started_at = 1_700_000_000.0
        self.pid = 4242

    def send(self, obj):
        self.sent.append(obj)

    def alive(self):
        return True

    def close(self):
        self.closed = True

    def wait_response(self, req_id, timeout=30, event_file=None):
        if "thread" in req_id:
            return self.thread_reply
        return self.turn_reply

    def next_event(self, timeout=0):
        if not self.events:
            return False
        return self.events.pop(0)


class LaneProbeVerifyTests(unittest.TestCase):
    def lane(self, name="L1"):
        return {"name": name, "backend": "codex", "model": "fake-model"}

    def test_a_completed_empty_run_verifies_the_server(self):
        child = FakeVerifyChild(events=[{"jsonrpc": "2.0", "method": "turn/completed",
                                         "params": {"turn": {"id": "u1", "status": "completed"}}}])

        ok, detail = xs.lane_probe_verify(self.lane(), child, timeout=5, policy={})

        self.assertTrue(ok, detail)
        self.assertEqual(detail, "empty run completed")

    def test_a_failed_turn_does_not_verify_the_server(self):
        child = FakeVerifyChild(events=[{"jsonrpc": "2.0", "method": "turn/failed",
                                         "params": {"error": {"message": "no session"}}}])

        ok, detail = xs.lane_probe_verify(self.lane(), child, timeout=5, policy={})

        self.assertFalse(ok)
        self.assertIn("no session", detail)

    def test_a_server_that_never_finishes_the_turn_does_not_verify(self):
        child = FakeVerifyChild(events=[])

        ok, detail = xs.lane_probe_verify(self.lane(), child, timeout=1, policy={})

        self.assertFalse(ok)
        self.assertIn("did not finish", detail)

    def test_a_server_that_cannot_start_a_session_does_not_verify(self):
        child = FakeVerifyChild(thread_reply={"error": {"message": "no threads"}})

        ok, detail = xs.lane_probe_verify(self.lane(), child, timeout=5, policy={})

        self.assertFalse(ok)
        self.assertIn("thread/start failed", detail)


class LaneProbeRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-probe-restart-")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "lanes" / "L1").mkdir(parents=True)
        self.lane = {"name": "L1", "backend": "codex", "model": "fake-model"}

    def tearDown(self):
        self.tmp.cleanup()

    def restart(self, wedged_child, new_child, verify=(True, "empty run completed")):
        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "load_policy", return_value={"lane": {}}), \
                mock.patch.object(xs, "lane_child", return_value=new_child), \
                mock.patch.object(xs, "lane_probe_verify", return_value=verify) as verify_call:
            outcome = xs.lane_probe_restart(self.lane, {}, wedged_child,
                                            probe_module.ServerProbe(failures=2), {"verify_timeout_s": 30})
        return outcome, verify_call

    def test_a_wedged_server_is_replaced_and_verified(self):
        wedged = FakeProbeChild()
        fresh = FakeVerifyChild()
        probe = probe_module.ServerProbe(failures=2)
        probe.record_failure(1.0, "no answer within 20s")
        probe.record_failure(2.0, "no answer within 20s")

        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "load_policy", return_value={"lane": {}}), \
                mock.patch.object(xs, "lane_child", return_value=fresh), \
                mock.patch.object(xs, "lane_probe_verify", return_value=(True, "empty run completed")):
            outcome = xs.lane_probe_restart(self.lane, {}, wedged, probe, {"verify_timeout_s": 30})

        self.assertTrue(outcome["ok"])
        self.assertIs(outcome["child"], fresh)
        self.assertTrue(wedged.closed, "the wedged server has to be stopped")
        self.assertEqual(outcome["server_probe"]["restarts"], 1)

    def test_the_brain_log_names_the_lane_the_uptime_and_the_basis(self):
        wedged = FakeProbeChild()
        probe = probe_module.ServerProbe(failures=2)
        probe.record_failure(1.0, "no answer within 20s")
        probe.record_failure(2.0, "no answer within 20s")

        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "load_policy", return_value={"lane": {}}), \
                mock.patch.object(xs, "lane_child", return_value=FakeVerifyChild()), \
                mock.patch.object(xs, "lane_probe_verify", return_value=(True, "empty run completed")), \
                mock.patch.object(xs, "NOW", return_value=1_700_000_000.0):
            xs.lane_probe_restart(self.lane, {}, wedged, probe, {"verify_timeout_s": 30})

        brain = (self.data / "lanes" / "L1" / "brain.log").read_text()
        self.assertIn("L1", brain)
        self.assertIn("lane-probe", brain)
        self.assertIn("uptime", brain)
        self.assertIn("no answer within 20s", brain)

    def test_a_failed_verification_keeps_the_lane_paused(self):
        wedged = FakeProbeChild()
        fresh = FakeVerifyChild()
        probe = probe_module.ServerProbe(failures=2)
        probe.record_failure(1.0, "no answer within 20s")
        probe.record_failure(2.0, "no answer within 20s")

        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "load_policy", return_value={"lane": {}}), \
                mock.patch.object(xs, "lane_child", return_value=fresh), \
                mock.patch.object(xs, "lane_probe_verify", return_value=(False, "empty run failed: no session")):
            outcome = xs.lane_probe_restart(self.lane, {}, wedged, probe, {"verify_timeout_s": 30})

        self.assertFalse(outcome["ok"])
        self.assertIn("no session", outcome["detail"])
        self.assertEqual(outcome["server_probe"]["restarts"], 1)

    def test_a_server_that_cannot_be_restarted_reports_the_reason(self):
        probe = probe_module.ServerProbe(failures=2)
        probe.record_failure(1.0, "no answer")
        probe.record_failure(2.0, "no answer")

        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "load_policy", return_value={"lane": {}}), \
                mock.patch.object(xs, "lane_child", side_effect=RuntimeError("codex binary not found")):
            outcome = xs.lane_probe_restart(self.lane, {}, FakeProbeChild(), probe, {"verify_timeout_s": 30})

        self.assertFalse(outcome["ok"])
        self.assertIsNone(outcome["child"])
        self.assertIn("codex binary not found", outcome["detail"])


CLI_BOOTSTRAP = (
    "import importlib.machinery, importlib.util;"
    "loader = importlib.machinery.SourceFileLoader('openswarm_cli', {path!r});"
    "spec = importlib.util.spec_from_loader('openswarm_cli', loader);"
    "module = importlib.util.module_from_spec(spec);"
    "loader.exec_module(module);"
    "module.main()"
)


class LaneProbeControlPlaneTests(unittest.TestCase):
    """The control plane reads the probe state of the lane host."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "openswarm_dispatch_engine_probe", os.path.join(ROOT, "lib", "dispatch", "engine.py"))
        self.engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.engine)
        self.tmp = tempfile.TemporaryDirectory(prefix="p", dir=str(SCRATCH)) \
            if SCRATCH.exists() else tempfile.TemporaryDirectory(prefix="openswarm-probe-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_remote_status_forwards_the_probe_state(self):
        home = self.root / "executor"
        lane_dir = home / "lanes" / "CX9"
        lane_dir.mkdir(parents=True)
        (lane_dir / "state.json").write_text(json.dumps({"state": "idle", "server_probe": {
            "alive": False, "last_success_at": 1_700_000_000.0, "restarts": 2, "failures": 3,
            "last_error": "no answer within 20s"}}))
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["remote", "CX9", "", str(home)]), \
                contextlib.redirect_stdout(output):
            exec(self.engine.REMOTE_STATUS, {})
        result = json.loads(output.getvalue())

        self.assertEqual(result["lane"], "idle")
        self.assertFalse(result["server_probe"]["alive"])
        self.assertEqual(result["server_probe"]["restarts"], 2)
        self.assertEqual(result["server_probe"]["failures"], 3)
        self.assertEqual(result["server_probe"]["last_success_at"], 1_700_000_000.0)

    def test_a_lane_without_a_probe_reports_none(self):
        home = self.root / "executor"
        lane_dir = home / "lanes" / "CX9"
        lane_dir.mkdir(parents=True)
        (lane_dir / "state.json").write_text(json.dumps({"state": "idle"}))
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["remote", "CX9", "", str(home)]), \
                contextlib.redirect_stdout(output):
            exec(self.engine.REMOTE_STATUS, {})
        result = json.loads(output.getvalue())

        self.assertEqual(result["lane"], "idle")
        self.assertNotIn("server_probe", result)


class LaneProbeCx9ReplayTests(unittest.TestCase):
    """Replay the CX9 sample of 2026-11-02: the app-server answers nobody since 01:04.

    Four runs on four cards failed between 22:42 and 22:55 without a commit and
    without an error class, while the lane serve process was alive and the
    app-server of the lane had been running since 01:04.  The probe has to judge
    that server wedged and take the restart path.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-probe-cx9-")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "lanes" / "CX9").mkdir(parents=True)
        self.lane = {"name": "CX9", "backend": "codex", "model": "fake-model"}
        self.started = time.mktime((2026, 11, 2, 1, 4, 0, 0, 0, -1))
        self.failed = time.mktime((2026, 11, 2, 22, 42, 0, 0, 0, -1))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_cx9_sample_is_judged_wedged_and_takes_the_restart_path(self):
        settings = probe_module.settings({"lane": {"server_probe": {"failures": 3, "timeout_s": 1}}})
        probe = probe_module.ServerProbe(failures=settings["failures"])
        child = FakeProbeChild(answer=None)
        child.started_at = self.started

        for moment in (self.failed, self.failed + 120, self.failed + 240):
            ok, detail = xs.lane_probe_check(child, settings)
            self.assertFalse(ok, "the wedged server of the CX9 sample answers nobody")
            probe.record_failure(moment, detail)

        self.assertTrue(probe.wedged(), "three unanswered probes judge the server wedged")

        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "load_policy", return_value={"lane": {}}), \
                mock.patch.object(xs, "lane_child", return_value=FakeVerifyChild()), \
                mock.patch.object(xs, "lane_probe_verify", return_value=(True, "empty run completed")), \
                mock.patch.object(xs, "NOW", return_value=self.failed):
            outcome = xs.lane_probe_restart(self.lane, {}, child, probe, settings)

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["server_probe"]["restarts"], 1)
        brain = (self.data / "lanes" / "CX9" / "brain.log").read_text()
        self.assertIn("CX9", brain)
        self.assertIn("uptime 77880s", brain)
        self.assertIn("3 failed probes", brain)
        self.assertIn("no answer", brain)


class LaneProbeLaneTests(unittest.TestCase):
    """A lane whose app-server stops answering restarts it and verifies the new one."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="p", dir=str(SCRATCH))
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Probe Test")
        self.git("config", "user.email", "lane-probe@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        (self.data / "lanes").mkdir(parents=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": "L1", "backend": "codex", "model": "fake-model", "effort": "high",
            "repo": str(self.repo), "binary": str(FIXTURES / "fake_app_server.py"),
        }]}))
        (self.data / "policy.local.json").write_text(json.dumps({"lane": {"server_probe": {
            "interval_s": 1, "timeout_s": 1, "failures": 2, "verify_timeout_s": 30}}}))
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        self.env.pop("OPENSWARM_LANES_FILE", None)

    def tearDown(self):
        # `lane down` waits for the supervisor pid to disappear; in a container the
        # supervisor stays a zombie of the exited CLI, so the test stops it directly.
        state = self.lane_state()
        for pid in (state.get("child_pid"), state.get("supervisor_pid")):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (OSError, TypeError, ValueError):
                pass
        time.sleep(0.2)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def cli(self, *args, check=True, timeout=30):
        result = subprocess.run([sys.executable, "-c", CLI_BOOTSTRAP.format(path=str(CLI)), *map(str, args)],
                                env=self.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result

    def lane_state(self):
        try:
            return json.loads((self.data / "lanes" / "L1" / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, predicate, timeout=30, description="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.05)
        self.fail(f"timed out waiting for {description}; last={last!r}")

    def brain_log(self):
        path = self.data / "lanes" / "L1" / "brain.log"
        return path.read_text() if path.exists() else ""

    def test_a_wedged_app_server_is_restarted_and_verified(self):
        marker = self.root / "wedged.marker"
        self.env["FAKE_APP_SERVER_WEDGE_PROBE"] = "1"
        self.env["FAKE_APP_SERVER_WEDGE_MARKER"] = str(marker)

        self.cli("lane", "up", "L1")

        state = self.wait_for(lambda: (s if (s := self.lane_state()).get("state") == "idle"
                                       and (s.get("server_probe") or {}).get("restarts") else None),
                              description="the lane to restart its wedged server")
        probe = state["server_probe"]
        self.assertEqual(probe["restarts"], 1)
        self.assertTrue(probe["alive"])
        self.assertIsNotNone(probe["last_success_at"])
        brain = self.brain_log()
        self.assertIn("lane server wedged", brain)
        self.assertIn("uptime", brain)
        self.assertIn("restarted and verified", brain)

    def test_a_failed_verification_keeps_the_lane_paused_and_alerts(self):
        marker = self.root / "wedged.marker"
        self.env["FAKE_APP_SERVER_WEDGE_PROBE"] = "1"
        self.env["FAKE_APP_SERVER_WEDGE_MARKER"] = str(marker)
        self.env["FAKE_APP_SERVER_FAIL_VERIFY"] = "1"

        self.cli("lane", "up", "L1")

        state = self.wait_for(lambda: (s if (s := self.lane_state()).get("state") == "server_wedged" else None),
                              description="the lane to pause after a failed verification")
        self.assertTrue(state["last_error"])
        self.assertEqual((state.get("server_probe") or {}).get("restarts"), 1)
        brain = self.brain_log()
        self.assertIn("verification failed", brain)
        # A paused lane takes no work: the dispatcher only picks an idle lane.
        answer = json.loads(self.cli("lane", "assign", "L1", "--prompt", "do something", check=False).stdout)
        self.assertFalse(answer.get("ok"))
        self.assertIn("paused", answer.get("error", ""))


if __name__ == "__main__":
    unittest.main()
