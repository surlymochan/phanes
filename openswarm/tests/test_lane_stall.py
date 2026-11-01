"""A lane turn has to show life: without child events and without provider requests it is aborted.

The stall watchdog lives inside `lane serve`, so it works for every backend. These tests drive
`lane_run_turn` with a fake child instead of a real executor: the fake child answers with
heartbeats only, which is the "run is attached but nothing happens" shape the MiniMax watchdog
used to detect from the outside.
"""
import json
import pathlib
import subprocess
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


class FakeClock:
    """A clock the fake child advances, so the lane loop sees time pass without waiting."""

    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class FakeChild:
    """A lane child that stays alive and reports no event: heartbeats without progress.

    `event_every` turns the silence into a slow event stream, `finish_after` ends the turn with a
    normal report, `request_log` records a provider request per poll, and `stop_on_abort` decides
    whether the child honours the stall interrupt.
    """

    def __init__(self, clock, step=60.0, event_every=0, finish_after=None, stop_on_abort=True,
                 request_log=None):
        self.clock = clock
        self.step = step
        self.event_every = event_every
        self.finish_after = finish_after
        self.stop_on_abort = stop_on_abort
        self.request_log = request_log
        self.lane = {"backend": "pi"}
        self.pid = None
        self.pgid = None
        self.sent = []
        self.pending = []
        self.polls = 0
        self.aborted = False
        self.closed = False
        self.alive_flag = True

    # The LaneChild surface used by lane_run_turn.
    def alive(self):
        return self.alive_flag

    def send(self, obj):
        self.sent.append(obj)
        if obj.get("type") == "abort":
            self.aborted = True
            if self.stop_on_abort:
                self.alive_flag = False

    def wait_response(self, req_id, timeout=30, event_file=None):
        return {}

    def stderr_text(self):
        return ""

    def close(self):
        self.closed = True
        self.alive_flag = False

    def next_event(self, timeout=0):
        self.clock.advance(self.step)
        if self.request_log:
            # The broker still receives requests, so the lane must not call that a stall.
            with open(self.request_log, "a") as stream:
                stream.write(json.dumps({"ts": self.clock.now}) + "\n")
        if self.pending:
            return self.pending.pop(0)
        self.polls += 1
        if self.finish_after is not None and self.polls >= self.finish_after:
            self.pending.extend(self.report_events())
            return self.pending.pop(0)
        if self.event_every and self.polls % self.event_every == 0:
            return {"type": "message_update", "message": {"role": "assistant", "content": []}}
        return False

    def report_events(self):
        report = {"status": "done", "summary": "fake child finished", "files_changed": [],
                  "tests": "none", "followups": []}
        message = {"role": "assistant", "content": [{"type": "text", "text": json.dumps(report)}],
                   "usage": {"input": 1, "output": 1}, "stopReason": "stop"}
        return [{"type": "message_end", "message": message}, {"type": "agent_end", "messages": [message]}]


class LaneStallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-stall-")
        self.root = pathlib.Path(self.tmp.name)
        self.rdir = self.root / "run"
        self.rdir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_turn(self, child, clock, policy, budget_minutes=60):
        lane = {"name": "L1", "backend": "pi", "model": "fake-model", "provider": "fake-provider",
                "repo": str(self.root)}
        spec = {"run_id": "stall-test", "task": "wait for the executor", "title": "stall test",
                "started": clock.now, "budget_minutes": budget_minutes}
        ws = {"path": str(self.root), "root": str(self.root)}
        with mock.patch.object(xs, "NOW", clock), \
                mock.patch.object(xs, "load_policy", return_value=policy):
            final, usage, error_class, rc, event_file, detail = xs.lane_run_turn(
                lane, child, spec, "do the task", ws, str(self.rdir), None, {}, [])
        return final, usage, error_class, rc, event_file, detail

    @staticmethod
    def events(event_file):
        return [json.loads(line) for line in pathlib.Path(event_file).read_text().splitlines() if line.strip()]

    def stalled_events(self, event_file):
        return [event for event in self.events(event_file) if event.get("type") == "stalled"]

    def test_turn_without_events_is_aborted_as_stalled(self):
        clock = FakeClock()
        child = FakeChild(clock, step=60.0)
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, {"lane": {"stall_after_s": 120}})

        self.assertEqual(error_class, "stalled")
        self.assertEqual(rc, 124)
        self.assertTrue(child.aborted, "the stalled turn must interrupt the executor")
        self.assertIn("lane stalled", detail)
        stalled = self.stalled_events(event_file)
        self.assertEqual(len(stalled), 1)
        self.assertIn("last_activity_at", stalled[0])
        self.assertGreater(stalled[0]["idle_seconds"], 120)

    def test_executor_that_ignores_the_stall_interrupt_is_still_stalled(self):
        clock = FakeClock()
        child = FakeChild(clock, step=60.0, stop_on_abort=False)
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, {"lane": {"stall_after_s": 120}})

        self.assertEqual(error_class, "stalled")
        self.assertEqual(rc, 124)
        self.assertTrue(child.closed, "an executor that ignores the interrupt is closed")
        self.assertEqual(len(self.stalled_events(event_file)), 1)

    def test_turn_with_events_is_not_aborted(self):
        clock = FakeClock()
        child = FakeChild(clock, step=60.0, event_every=2, finish_after=6)
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, {"lane": {"stall_after_s": 300}})

        self.assertIsNone(error_class)
        self.assertEqual(rc, 0)
        self.assertFalse(child.aborted)
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(self.stalled_events(event_file), [])

    def test_stall_after_zero_disables_the_check(self):
        clock = FakeClock()
        child = FakeChild(clock, step=60.0, stop_on_abort=False)
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, {"lane": {"stall_after_s": 0}}, budget_minutes=1)

        self.assertNotEqual(error_class, "stalled")
        self.assertEqual(error_class, "budget_exceeded")
        self.assertEqual(self.stalled_events(event_file), [])

    def test_provider_request_log_keeps_a_silent_turn_alive(self):
        clock = FakeClock()
        log = self.root / "broker-requests.jsonl"
        child = FakeChild(clock, step=60.0, stop_on_abort=False, request_log=str(log))
        final, usage, error_class, rc, event_file, detail = self.run_turn(
            child, clock, {"lane": {"stall_after_s": 120, "provider_request_log": str(log)}},
            budget_minutes=5)

        self.assertNotEqual(error_class, "stalled")
        self.assertEqual(self.stalled_events(event_file), [])
        self.assertTrue(log.exists())


class LaneStallResultTests(unittest.TestCase):
    """The finished run records the stall and the last activity time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-stall-result-")
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Lane Stall Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "lane-stall@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_stalled_result_records_error_class_and_last_activity(self):
        rid = "stalled-run"
        rdir = self.runs / rid
        rdir.mkdir()
        lane = {"name": "L1", "backend": "pi", "model": "fake-model", "provider": "fake-provider"}
        spec = {"run_id": rid, "task": "wait", "title": "stall", "started": self.clock.now,
                "budget_minutes": 60, "stalled_last_activity_at": 1_700_000_000.0}
        ws = {"mode": "repo", "path": str(self.repo), "root": str(self.repo), "repo": str(self.repo),
              "base": "HEAD"}
        with mock.patch.object(xs, "RUNS", str(self.runs)), mock.patch.object(xs, "NOW", self.clock):
            out = xs.lane_finalize_run(lane, spec, ws, None, {}, "stalled", 124, str(rdir / "events.jsonl"))

        self.assertEqual(out["error_class"], "stalled")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["last_activity_at"], 1_700_000_000.0)
        saved = json.loads((rdir / "result.json").read_text())
        self.assertEqual(saved["error_class"], "stalled")
        self.assertEqual(saved["last_activity_at"], 1_700_000_000.0)


class LaneStallPolicyTests(unittest.TestCase):
    def test_default_stall_after_is_900_seconds(self):
        self.assertEqual(xs.lane_stall_after_seconds({}), 900)
        self.assertEqual(xs.lane_stall_after_seconds({"lane": {}}), 900)

    def test_policy_overrides_the_stall_threshold(self):
        self.assertEqual(xs.lane_stall_after_seconds({"lane": {"stall_after_s": 60}}), 60)
        self.assertEqual(xs.lane_stall_after_seconds({"lane": {"stall_after_s": 0}}), 0)

    def test_unusable_stall_threshold_falls_back_to_the_default(self):
        for value in ("", "abc", -1, None, True):
            with self.subTest(value=value):
                self.assertEqual(xs.lane_stall_after_seconds({"lane": {"stall_after_s": value}}), 900)

    def test_provider_request_log_reads_the_last_request_time(self):
        with tempfile.TemporaryDirectory(prefix="oslane-stall-log-") as root:
            path = pathlib.Path(root, "requests.jsonl")
            path.write_text(json.dumps({"ts": 1_700_000_000.5}) + "\n"
                            + json.dumps({"ts": 1_700_000_060.0}) + "\n")
            self.assertEqual(xs.lane_provider_last_request(str(path)), 1_700_000_060.0)

    def test_provider_request_log_accepts_epoch_millis_and_plain_lines(self):
        with tempfile.TemporaryDirectory(prefix="oslane-stall-log-") as root:
            path = pathlib.Path(root, "requests.log")
            path.write_text("1700000000.5 request\n1700000060000 request\n")
            self.assertEqual(xs.lane_provider_last_request(str(path)), 1_700_000_060.0)

    def test_missing_provider_request_log_is_not_an_error(self):
        with tempfile.TemporaryDirectory(prefix="oslane-stall-log-") as root:
            missing = str(pathlib.Path(root, "missing.log"))
            self.assertIsNone(xs.lane_provider_last_request(missing))
            self.assertIsNone(xs.lane_provider_last_request(None))


if __name__ == "__main__":
    unittest.main()
