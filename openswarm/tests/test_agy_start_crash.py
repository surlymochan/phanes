"""A resident executor that dies before it starts is restarted once (Refs #1595).

The agy CLI can come up and exit right away (exit code 2) instead of running the
task: the lane then ended with `resident executor stdout closed (executor exit code 2)`
although nothing was executed.  These tests drive `lane_run_turn` with a fake agy
executor instead of the real binary: the fake dies before its first event, so the
lane can tell a start crash from a task failure.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]

RETRY_PATH = ROOT / "lib" / "dispatch" / "retry.py"
_spec = importlib.util.spec_from_file_location("dispatch_retry", RETRY_PATH)
retry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retry)

REPORT = {"status": "done", "summary": "fake agy finished", "files_changed": [],
          "tests": "none", "followups": []}


def result_event():
    """The agy stream event of a finished turn: the JSON report."""
    return {"type": "result", "status": "SUCCESS", "response": json.dumps(REPORT),
            "conversation_id": "conv-1", "usage": {"input": 1, "output": 1}}


class FakeClock:
    """A clock the fake executor advances, so the lane loop sees time pass without waiting."""

    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeAgyChild:
    """A fake agy executor whose starts are scripted.

    Every entry of `starts` is one start: an integer means the executor exits with
    that code before its first event, a tuple ``(exit code, seconds)`` does the same
    but only after `seconds` of virtual time (a late start crash), a list of events
    means it emits them and then closes stdout.  `prompts` records the prompt of every
    start and `conversations` the session it was started with, so a test can see that
    the same prompt and session were re-sent after a start crash.
    """

    def __init__(self, clock, starts, stderr="agy: cannot start\n", step=0.01):
        self.clock = clock
        self.starts = list(starts)
        self.stderr = stderr
        self.step = step
        self.prompts = []
        self.conversations = []
        self.events = []
        self.exited = False
        self.exit_code = None
        self.closed = False
        self.lane = {"backend": "agy"}
        self.pid = None
        self.pgid = None
        self.delay = 0.0

    # The AgyChild surface used by lane_run_turn.
    def start(self, prompt, model=None, effort=None, conversation=None):
        self.prompts.append(prompt)
        self.conversations.append(conversation)
        plan = self.starts.pop(0) if self.starts else None
        self.events = []
        self.delay = 0.0
        if isinstance(plan, tuple):
            # The executor died before its first event, but only after `delay` seconds.
            self.exit_code, self.delay = plan[0], float(plan[1])
            self.exited = True
            self.events.append(None)
            return None
        if isinstance(plan, int):
            # The executor died before its first event: stdout closes right away.
            self.exit_code = plan
            self.exited = True
            self.events.append(None)
            return None
        self.exit_code = 0
        self.events.extend(plan or [])
        self.events.append(None)  # stdout closes after the events
        return None

    def alive(self):
        return not self.exited

    def next_event(self, timeout=0):
        self.clock.advance(self.step)
        if self.delay:
            # The executor took its time to come up before it died.
            self.clock.advance(self.delay)
            self.delay = 0.0
        if not self.events:
            return False
        event = self.events.pop(0)
        if event is None:
            self.exited = True
        return event

    def send(self, obj):
        pass  # the headless print mode has no control channel

    def stderr_text(self):
        return self.stderr

    def reap_after_stdout_eof(self, timeout=1.0):
        return self.exit_code

    def close(self):
        self.closed = True
        self.exited = True


class AgyStartCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-agystart-", dir=ROOT)
        self.root = pathlib.Path(self.tmp.name)
        self.rdir = self.root / "run"
        self.rdir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_turn(self, child, clock, policy=None, turn=None, continuation=False, spec_extra=None):
        lane = {"name": "L1", "backend": "agy", "model": "fake-model", "provider": "fake-provider",
                "repo": str(self.root)}
        spec = {"run_id": "agy-start-test", "task": "do the task", "title": "agy start test",
                "started": clock.now, "budget_minutes": 60}
        spec.update(spec_extra or {})
        ws = {"path": str(self.root), "root": str(self.root)}
        with mock.patch.object(xs, "NOW", clock), \
                mock.patch.object(xs, "load_policy", return_value=policy or {}):
            final, usage, error_class, rc, event_file, detail = xs.lane_run_turn(
                lane, child, spec, "do the task", ws, str(self.rdir), None, turn or {}, [],
                continuation=continuation)
        return final, usage, error_class, rc, event_file, detail

    @staticmethod
    def events(event_file):
        return [json.loads(line) for line in pathlib.Path(event_file).read_text().splitlines() if line.strip()]

    def status(self):
        return json.loads((self.rdir / "status.json").read_text())

    def test_executor_that_dies_at_start_is_restarted_once(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[2, [result_event()]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertIsNone(error_class)
        self.assertEqual(rc, 0)
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(len(child.prompts), 2, "the executor is started again after the start crash")
        self.assertEqual(child.prompts[0], child.prompts[1], "the same prompt is re-sent")
        self.assertEqual(self.status().get("executor_restarts"), 1)
        restarts = [event for event in self.events(event_file) if event.get("type") == "executor_restart"]
        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0].get("exit_code"), 2)

    def test_the_stderr_tail_is_recorded_in_the_status(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[2, [result_event()]])
        self.run_turn(child, clock)

        status = self.status()
        self.assertEqual(status.get("executor_stderr_tail"), "agy: cannot start\n")
        self.assertEqual(status.get("executor_exit_code"), 2)

    def test_two_start_crashes_are_an_executor_start_failure(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[2, 2])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertEqual(error_class, "executor_start_failed")
        self.assertEqual(rc, 2)
        self.assertIn("exit code 2", detail)
        self.assertEqual(detail.count("exit code 2"), 2, "both exit codes are reported")
        self.assertIn("agy: cannot start", detail)
        self.assertEqual(len(child.prompts), 2, "the executor is restarted at most once")
        self.assertEqual(self.status().get("executor_restarts"), 1)

    def test_an_executor_that_dies_after_the_start_grace_is_restarted(self):
        # The executor comes up late (> AGY_START_GRACE_S) and dies without a single event:
        # the task never ran, so the turn is restarted instead of failing as a task error.
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[(2, 12.0), [result_event()]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertIsNone(error_class)
        self.assertEqual(rc, 0)
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(len(child.prompts), 2, "the executor is started again after the start crash")
        self.assertEqual(child.prompts[0], child.prompts[1], "the same prompt is re-sent")
        self.assertEqual(self.status().get("executor_restarts"), 1)

    def test_two_late_start_crashes_are_an_executor_start_failure(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[(2, 12.0), (2, 12.0)])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertEqual(error_class, "executor_start_failed")
        self.assertEqual(rc, 2)
        self.assertEqual(detail.count("exit code 2"), 2, "both exit codes are reported")
        self.assertEqual(len(child.prompts), 2, "the executor is restarted at most once")
        self.assertEqual(self.status().get("executor_restarts"), 1)

    def test_a_continuation_keeps_its_session_when_the_executor_is_restarted(self):
        # A rate limited turn continues in the same session; the restart of a start crash has to
        # keep that session, otherwise the continuation cannot finish the interrupted task.
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[2, [result_event()]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, turn={"conversation_id": "conv-7"}, continuation=True)

        self.assertIsNone(error_class)
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(len(child.prompts), 2, "the executor is started again after the start crash")
        self.assertEqual(child.prompts[0], child.prompts[1], "the same prompt is re-sent")
        self.assertEqual(child.conversations, ["conv-7", "conv-7"], "the session survives the restart")

    def test_an_aborted_turn_does_not_restart_the_executor(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[2])
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, spec_extra={"abort_requested": True})

        self.assertEqual(error_class, "exec_error")
        self.assertEqual(len(child.prompts), 1, "an aborted turn is not restarted")
        self.assertEqual(self.status().get("executor_restarts", 0), 0)

    def test_a_disconnect_after_the_first_event_stays_an_exec_error(self):
        # Once the executor has run beyond the grace window, a disconnect is a task failure.
        clock = FakeClock()
        class LateEventChild(FakeAgyChild):
            def next_event(self, timeout=0):
                if not getattr(self, "delayed", False):
                    self.delayed = True
                    self.clock.advance(xs.AGY_START_GRACE_S + 1)
                return super().next_event(timeout)

        child = LateEventChild(clock, starts=[[{"type": "step_update", "text": "working"}]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertEqual(error_class, "exec_error")
        self.assertEqual(len(child.prompts), 1, "a running executor is not restarted")
        self.assertEqual(self.status().get("executor_restarts", 0), 0)
        restarts = [event for event in self.events(event_file) if event.get("type") == "executor_restart"]
        self.assertEqual(restarts, [])

    def test_disconnect_after_an_early_event_restarts_once(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[
            [{"type": "step_update", "text": "starting"}], [result_event()]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertIsNone(error_class)
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(len(child.prompts), 2)
        self.assertEqual(self.status().get("executor_restarts"), 1)

    def test_two_early_event_disconnects_are_executor_start_failure(self):
        clock = FakeClock()
        event = {"type": "step_update", "text": "starting"}
        child = FakeAgyChild(clock, starts=[[event], [event]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertEqual(error_class, "executor_start_failed")
        self.assertEqual(len(child.prompts), 2)
        self.assertEqual(self.status().get("executor_restarts"), 1)

    def test_event_drained_after_stdout_eof_marks_executor_as_started(self):
        clock = FakeClock()

        class DrainRaceChild(FakeAgyChild):
            def start(self, *args, **kwargs):
                super().start(*args, **kwargs)
                self.exited = True  # the process exits before the first poll is observed

            def next_event(self, timeout=0):
                if not getattr(self, "polled", False):
                    self.polled = True
                    self.clock.advance(xs.AGY_START_GRACE_S + 1)
                    return False  # the event remains in the pipe for the EOF drain
                return super().next_event(timeout)

        child = DrainRaceChild(clock, starts=[[{"type": "step_update", "text": "working"}]])
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertEqual(error_class, "exec_error")
        self.assertEqual(self.status().get("executor_restarts", 0), 0)

    def test_an_auth_error_is_not_turned_into_a_start_crash(self):
        clock = FakeClock()
        child = FakeAgyChild(clock, starts=[2], stderr="FatalAuthenticationError: not logged in\n")
        final, usage, error_class, rc, event_file, detail = self.run_turn(child, clock)

        self.assertEqual(error_class, "auth")
        self.assertEqual(len(child.prompts), 1, "an executor that reports an auth error is not restarted")


class ExecutorStartFailedRetryTests(unittest.TestCase):
    """The retry table knows the new error class (#1595)."""

    def test_start_failure_is_not_counted_and_pauses_the_lane(self):
        action, counted = retry.decide("executor_start_failed", 0, retry.DEFAULT_TABLE)
        self.assertEqual(action, "pause_lane")
        self.assertFalse(counted)

    def test_start_failure_pauses_the_lane_for_ten_minutes(self):
        self.assertEqual(retry.lane_pause_ttl("executor_start_failed"), 600)

    def test_other_classes_pause_no_lane(self):
        self.assertIsNone(retry.lane_pause_ttl("exec_error"))
        self.assertIsNone(retry.lane_pause_ttl("quota"))

    def test_executor_start_failure_is_in_the_default_policy(self):
        self.assertEqual(retry.DEFAULT_TABLE["executor_start_failed"], "pause_lane")


if __name__ == "__main__":
    unittest.main()
