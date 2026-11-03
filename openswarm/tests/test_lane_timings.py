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


class FakeClock:
    def __init__(self, start=1000):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeChild:
    def __init__(self, clock):
        self.clock = clock
        self.lane = {"backend": "pi"}
        self.pid = self.pgid = None
        self.pending = []
        self.alive_flag = True

    def alive(self):
        return self.alive_flag

    def send(self, obj):
        pass

    def wait_response(self, *args):
        return {}

    def next_event(self, timeout=0):
        self.clock.advance(2)
        if self.pending:
            return self.pending.pop(0)
        report = {"status": "done", "summary": "ok", "files_changed": [], "tests": "none", "followups": []}
        message = {"role": "assistant", "content": [{"type": "text", "text": json.dumps(report)}],
                   "usage": {}, "stopReason": "stop"}
        self.pending = [{"type": "message_end", "message": message}, {"type": "agent_end", "messages": [message]}]
        return self.pending.pop(0)


class LaneTimingTests(unittest.TestCase):
    def test_tools_top_five_are_sorted_and_redacted(self):
        timings = {"tools_s": 0, "tools_top": []}
        with mock.patch.object(xs, "bus_redact", side_effect=lambda value: value.replace("ghp_secret", "[REDACTED]")):
            for seconds in (1, 8, 3, 7, 2, 6):
                xs.lane_timing_record_tool(timings, "run ghp_secret", seconds)
        self.assertEqual([row["seconds"] for row in timings["tools_top"]], [8, 7, 6, 3, 2])
        self.assertEqual(timings["tools_top"][0]["command"], "run [REDACTED]")
        self.assertEqual(timings["tools_s"], 27)

    def test_turn_timings_use_fake_executor_stream_and_clock(self):
        clock = FakeClock()
        child = FakeChild(clock)
        with tempfile.TemporaryDirectory() as temp:
            run_dir = pathlib.Path(temp)
            spec = {"run_id": "timing", "task": "test", "started": clock.now,
                    "budget_minutes": 5}
            with mock.patch.object(xs, "NOW", clock), mock.patch.object(xs, "load_policy", return_value={}):
                xs.lane_run_turn({"name": "L", "backend": "pi", "model": "fake"}, child, spec,
                                 "prompt", {"path": temp}, str(run_dir), None, {}, [])
            timing = spec["timings"]
            self.assertEqual(timing["turns"], 1)
            self.assertGreater(timing["first_event_s"], 0)
            self.assertGreaterEqual(timing["model_s"], timing["first_event_s"])

    def test_run_digest_old_result_has_null_timings(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = pathlib.Path(temp)
            run = runs / "old"
            run.mkdir()
            (run / "result.json").write_text(json.dumps({"status": "done"}))
            (run / "status.json").write_text(json.dumps({"state": "finished", "started": 100, "finished": 101}))
            with mock.patch.object(xs, "RUNS", str(runs)), mock.patch.object(xs, "NOW", return_value=101):
                self.assertIsNone(xs.run_digest("old")["timings"])

    def test_slowest_details_are_written_to_result_and_status(self):
        # A budget finalization with no workspace changes still persists the actual diagnostic.
        with tempfile.TemporaryDirectory() as temp:
            run_dir = pathlib.Path(temp) / "run"
            run_dir.mkdir()
            old = {key: getattr(xs, key) for key in ("RUNS", "LEDGER")}
            xs.RUNS = str(run_dir.parent)
            xs.LEDGER = str(run_dir.parent / "ledger.jsonl")
            spec = {"run_id": "run", "task": "test", "started": 1, "budget_minutes": 1,
                    "timings": {"prepare_s": 0, "first_event_s": 2, "model_s": 1, "tools_s": 9,
                                "verify_s": 0, "push_s": 0, "turns": 1, "tools_top": []}}
            ws = {"root": temp, "path": temp, "repo": temp, "mode": "in-place"}
            with mock.patch.object(xs, "NOW", return_value=30), \
                 mock.patch.object(xs, "git", return_value=(0, "", "")), \
                 mock.patch.object(xs, "finalize_workspace", return_value={"branch": None, "commit": None,
                     "diffstat": "", "excluded_files": [], "patch": None, "worktree": temp, "merge_hint": None}), \
                 mock.patch.object(xs, "append_ledger"), mock.patch.object(xs, "cleanup_executor_tmp"):
                result = xs.lane_finalize_run({"name": "L", "backend": "pi", "model": "m"}, spec, ws,
                                              None, {}, "budget_exceeded", 124, str(run_dir / "events.jsonl"),
                                              attempts=[{"seconds": 20, "error_tail": "deadline"}])
            self.assertTrue(result["error"].endswith("slowest=tools 9.0s"))
            self.assertIn("slowest=tools 9.0s", json.loads((run_dir / "status.json").read_text())["error"])
            self.assertEqual(json.loads((run_dir / "result.json").read_text())["timings"]["tools_s"], 9)
            for key, value in old.items():
                setattr(xs, key, value)


if __name__ == "__main__":
    unittest.main()
