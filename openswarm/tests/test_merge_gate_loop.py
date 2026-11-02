"""Long-running merge gate behavior."""
import argparse
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_merge_gate as gate
xs = gate.xs


class MergeGateLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openswarm-merge-gate-loop-")
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_reviews(self, records):
        (self.home / "reviews.json").write_text(json.dumps(records))

    def run_loop(self, runner, heartbeat, pause_file=None, interval=17, json_output=True):
        args = argparse.Namespace(repo=gate.REPO, pr=7, dry_run=False, json=json_output,
                                  loop=True, interval=interval, max_iterations=3,
                                  heartbeat=str(heartbeat), pause_file=str(pause_file) if pause_file else None)
        sleeps = []
        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1 and pause_file:
                pathlib.Path(pause_file).unlink()
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            result = xs.cmd_merge_gate(args, runner=runner, sleep=fake_sleep)
        return result, sleeps, errors.getvalue()

    def test_three_iterations_write_heartbeat_and_use_injected_sleep(self):
        self.write_reviews({f"{gate.REPO}#7": {"sha": gate.HEAD_SHA}})
        gh = gate.FakeGh({7: gate.pr_payload(7, checks=[gate.check("build")])})
        heartbeat = self.home / "heartbeat.json"
        result, sleeps, _ = self.run_loop(gh, heartbeat, interval=9)
        record = json.loads(heartbeat.read_text())
        self.assertEqual(result, 0)
        self.assertEqual(record["iteration"], 3)
        self.assertEqual(record["merged"], 1)
        self.assertEqual(record["skipped"], 0)
        self.assertIn("ts", record)
        self.assertEqual(sleeps, [9, 9])
        self.assertEqual(len(gh.merge_calls()), 3)

    def test_text_mode_heartbeat_counts_merges(self):
        self.write_reviews({f"{gate.REPO}#7": {"sha": gate.HEAD_SHA}})
        gh = gate.FakeGh({7: gate.pr_payload(7, checks=[gate.check("build")])})
        heartbeat = self.home / "heartbeat.json"
        result, _, _ = self.run_loop(gh, heartbeat, json_output=False)
        record = json.loads(heartbeat.read_text())
        self.assertEqual(result, 0)
        self.assertEqual(record["merged"], 1)

    def test_pause_and_one_iteration_error_do_not_stop_loop(self):
        pause_file = self.home / "pause"
        pause_file.touch()
        heartbeat = self.home / "heartbeat.json"

        self.write_reviews({f"{gate.REPO}#7": {"sha": gate.HEAD_SHA}})

        class IntermittentRunner:
            def __init__(self):
                self.calls = 0

            def __call__(self, argv):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary runner failure")
                return 0, json.dumps(gate.pr_payload(7, checks=[gate.check("build")])), ""

        runner = IntermittentRunner()
        result, sleeps, errors = self.run_loop(runner, heartbeat, pause_file)
        self.assertEqual(result, 0)
        self.assertEqual(runner.calls, 3)  # exception round and final view/merge; pause invoked none
        self.assertEqual(sleeps, [17, 17])
        record = json.loads(heartbeat.read_text())
        self.assertEqual(record["iteration"], 3)
        self.assertEqual(record["merged"], 1)
        self.assertIn("iteration 2 failed: temporary runner failure", errors)


if __name__ == "__main__":
    unittest.main()
