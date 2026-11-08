"""Pi thinking level: the lanes.json field and the launch flag (Refs #1847).

pi reads its thinking level once, from `--thinking <level>` on the command line
(pi-coding-agent README.md "Model Options", dist/cli/args.js VALID_THINKING_LEVELS),
so the level a lane runs at is visible in the process command line.
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

# The levels pi itself accepts (pi-coding-agent dist/cli/args.js VALID_THINKING_LEVELS).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


class LaneConfigThinkingTest(unittest.TestCase):
    """lanes.json carries an optional thinking level; an unknown level is refused."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swrm-thinking-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)

    def lane_config(self, lane):
        config = os.path.join(self.tmp, "lanes.json")
        with open(config, "w", encoding="utf-8") as stream:
            json.dump({"lanes": [lane]}, stream)
        with mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                               config if name == "LANES_FILE" else default), \
                mock.patch.object(xs, "load_policy", return_value={"pi": {"require_isolation": False}}):
            return xs.lane_config()

    def lane(self, **fields):
        return {"name": "L1", "backend": "pi", "model": "MiniMax-M3.1-Flash", "provider": "minimax-cn",
                "repo": self.repo, **fields}

    def test_a_thinking_level_is_kept(self):
        lanes = self.lane_config(self.lane(thinking="high"))
        self.assertEqual(len(lanes), 1)
        self.assertEqual(lanes[0]["thinking"], "high")

    def test_a_lane_without_a_thinking_level_has_none(self):
        lanes = self.lane_config(self.lane())
        self.assertIsNone(lanes[0].get("thinking"))

    def test_an_unknown_thinking_level_is_refused(self):
        for level in ("deeper", "HIGH", "", 3, True):
            with self.subTest(level=level):
                with self.assertRaises(ValueError) as raised:
                    self.lane_config(self.lane(thinking=level))
                message = str(raised.exception)
                self.assertIn("invalid lane config", message)
                self.assertIn("thinking must be one of", message)

    def test_every_pi_thinking_level_is_accepted(self):
        for level in THINKING_LEVELS:
            with self.subTest(level=level):
                lanes = self.lane_config(self.lane(thinking=level))
                self.assertEqual(lanes[0]["thinking"], level)

    def test_the_module_publishes_the_pi_thinking_levels(self):
        self.assertEqual(tuple(getattr(xs, "PI_THINKING_LEVELS", ())), THINKING_LEVELS,
                         "the pi thinking levels are not published by swrm/bin/swrm")


class PiLaunchThinkingTest(unittest.TestCase):
    """The pi RPC launch carries the thinking level of the lane."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swrm-pi-launch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        binary = FIXTURES / "fake_pi_rpc.py"
        binary.chmod(0o755)

    def launch(self, **lane_fields):
        lane = {"name": "L1", "backend": "pi", "model": "fake-model", "provider": "fake-provider",
                "binary": str(FIXTURES / "fake_pi_rpc.py"), **lane_fields}
        policy = {"pi": {"require_isolation": False}}
        with mock.patch.object(xs, "load_policy", return_value=policy):
            child = xs.LaneChild(lane, self.cwd, policy)
        self.addCleanup(child.close)
        return list(child.proc.args)

    def test_the_launch_carries_the_thinking_level(self):
        argv = self.launch(thinking="high")
        self.assertIn("--thinking", argv)
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertLess(argv.index("--model"), argv.index("--thinking"))

    def test_the_launch_without_a_level_has_no_thinking_flag(self):
        argv = self.launch()
        self.assertNotIn("--thinking", argv)
        self.assertIn("--mode", argv)
        self.assertIn("rpc", argv)


class AssignThinkingTest(unittest.TestCase):
    """`lane assign --thinking <level>` carries the level into the run."""

    def assign(self, thinking):
        lane = {"name": "L1", "backend": "pi", "model": "fake-model"}
        args = argparse.Namespace(name="L1", prompt="do it", task_file=None, title=None, verify=None,
                                  verify_baseline=False, budget_minutes=5, base=None, card_type=None,
                                  rework_of=None, paths=None, read_only=False, thinking=thinking,
                                  scope="repo", idempotency_key=None, run_id=None)
        with mock.patch.object(xs, "lanes_by_name", return_value=[lane]), \
                mock.patch.object(xs, "lane_call", return_value={"ok": True, "id": "run-1"}) as call, \
                mock.patch.object(xs, "load_policy", return_value={"run": {"timeout_seconds": 600}}), \
                contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_lane_assign(args)
        return call.call_args[0][1]

    def test_the_level_travels_with_the_assignment(self):
        self.assertEqual(self.assign("high").get("thinking"), "high")

    def test_an_assignment_without_a_level_carries_none(self):
        self.assertIsNone(self.assign(None).get("thinking"))


if __name__ == "__main__":
    unittest.main()
