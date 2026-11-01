"""Codex lane version gates use executable version output and semantic ordering."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()


class CodexMinVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__))
        self.addCleanup(self.tmp.cleanup)
        self.cli = self.executable("codex", "0.166.0")
        self.host = self.executable("codex-code-mode-host", "0.166.0")
        self.policy = {"codex": {"min_cli_version": "0.166.0"}}
        self.lane = {"backend": "codex", "binary": self.cli}

    def executable(self, name, version):
        return self.executable_output(name, f"{name} {version}")

    def executable_output(self, name, banner):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as stream:
            stream.write(f"#!/bin/sh\necho '{banner}'\n")
        os.chmod(path, 0o755)
        return path

    def check(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_CODEX_CODE_MODE_HOST_BIN": self.host}):
            return xs.codex_min_version_error(self.policy, self.lane)

    def test_below_minimum_is_blocked(self):
        self.executable("codex", "0.165.9")
        self.assertIn("blocked: cli_too_old (have 0.165.9, need 0.166.0)", self.check())

    def test_equal_and_higher_versions_pass(self):
        self.assertIsNone(self.check())
        self.executable("codex", "0.167.0")
        self.executable("codex-code-mode-host", "0.167.0")
        self.assertIsNone(self.check())

    def test_host_version_must_match_cli(self):
        self.executable("codex-code-mode-host", "0.166.1")
        self.assertIn("code-mode-host 0.166.1", self.check())

    def test_empty_minimum_skips_checks(self):
        self.policy["codex"]["min_cli_version"] = ""
        self.executable("codex", "0.1.0")
        self.assertIsNone(xs.codex_min_version_error(self.policy, self.lane))

    def test_prerelease_sorts_below_release(self):
        self.executable("codex", "0.166.0-alpha.2")
        self.assertIn("cli_too_old", self.check())
        self.assertLess(xs.semver_key("0.166.0-alpha.2"), xs.semver_key("0.166.0"))
        self.assertGreater(xs.semver_key("0.166.0-alpha.10"), xs.semver_key("0.166.0-alpha.2"))

    def test_version_banner_with_suffix_is_parsed(self):
        self.executable_output("codex", "codex-cli 0.166.0 (build 123)")
        self.assertIsNone(self.check())
        self.assertEqual(xs.codex_version_from_output("codex-cli 0.166.0 (build 123)"), "0.166.0")

    def test_missing_cli_is_reported_as_unavailable(self):
        with mock.patch.object(xs, "codex_resolved_binary", return_value=None):
            reason, error_class = xs.codex_version_gate(self.policy, self.lane)
        self.assertIn("blocked: cli_unavailable", reason)
        self.assertEqual(error_class, "cli_unavailable")

    def test_missing_code_mode_host_is_blocked(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_CODEX_CODE_MODE_HOST_BIN": ""}), \
             mock.patch.object(xs.shutil, "which", return_value=None):
            reason, error_class = xs.codex_version_gate(self.policy, self.lane)
        self.assertIn("code-mode-host not found", reason)
        self.assertEqual(error_class, "cli_unavailable")

    def test_blocking_error_carries_the_error_class(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_CODEX_CODE_MODE_HOST_BIN": self.host}):
            self.assertIsNone(xs.codex_min_version_block(self.policy, self.lane))
            self.executable("codex", "0.165.9")
            block = xs.codex_min_version_block(self.policy, self.lane)
        self.assertIsInstance(block, xs.CodexVersionBlockedError)
        self.assertEqual(block.error_class, "cli_too_old")
        self.assertIn("blocked: cli_too_old", str(block))

    def test_blocked_state_is_reported_like_a_crashed_lane(self):
        state = {"state": "blocked: cli_too_old (have 0.165.0, need 0.166.0)",
                 "last_error": "blocked: cli_too_old (have 0.165.0, need 0.166.0)"}
        self.assertTrue(xs.lane_state_blocked(state["state"]))
        self.assertFalse(xs.lane_state_blocked("crashed"))
        self.assertIn("cli_too_old", xs.lane_state_error(state))
        self.assertIsNone(xs.lane_state_error({"state": "idle"}))

    def test_lane_up_reports_the_blocking_reason(self):
        reason = "blocked: cli_too_old (have 0.165.0, need 0.166.0)"
        state = {"supervisor_pid": 4242, "state": reason, "last_error": reason}
        args = mock.Mock(names=["codex-lane"])
        with mock.patch.object(xs, "lanes_by_name", return_value=[{"name": "codex-lane", "backend": "codex"}]), \
             mock.patch.object(xs, "jload", return_value=state), \
             mock.patch.object(xs, "pid_alive", return_value=True), \
             mock.patch.object(xs, "lane_last_log_error", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                xs.cmd_lane_up(args, {})
        payload = json.loads(out.getvalue())
        error = payload["lanes"][0]["error"]
        self.assertIn("cli_too_old", error)
        self.assertNotIn("control socket is not responding", error)

    def test_lane_up_startup_reports_the_blocking_reason(self):
        reason = "blocked: cli_too_old (have 0.165.0, need 0.166.0)"
        state = {"supervisor_pid": 777, "state": reason, "last_error": reason}
        args = mock.Mock(names=["codex-lane"])
        proc = mock.Mock(pid=777, poll=mock.Mock(return_value=None))
        with mock.patch.object(xs, "lanes_by_name", return_value=[{"name": "codex-lane", "backend": "codex"}]), \
             mock.patch.object(xs, "jload", return_value=state), \
             mock.patch.object(xs, "pid_alive", return_value=True), \
             mock.patch.object(xs, "subprocess", mock.Mock(Popen=mock.Mock(return_value=proc))), \
             mock.patch.object(xs, "lane_last_log_error", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                xs.cmd_lane_up(args, {})
        payload = json.loads(out.getvalue())
        self.assertIn("cli_too_old", payload["lanes"][0]["error"])
        self.assertNotIn("supervisor did not start", payload["lanes"][0]["error"])

    def test_blocked_run_ends_with_cli_too_old(self):
        runs = os.path.join(self.tmp.name, "runs")
        rdir = os.path.join(runs, "run-cli-too-old")
        os.makedirs(rdir, exist_ok=True)
        lane = {"name": "codex-lane", "backend": "codex", "model": "gpt-6-luna"}
        spec = {"run_id": "run-cli-too-old", "title": "gate"}
        block = xs.CodexVersionBlockedError("blocked: cli_too_old (have 0.165.0, need 0.166.0)")
        with mock.patch.object(xs, "RUNS", runs), \
             mock.patch.object(xs, "LEDGER", os.path.join(self.tmp.name, "ledger.jsonl")), \
             mock.patch.object(xs, "lane_write_state"), \
             mock.patch.object(xs, "codex_min_version_block", return_value=block):
            status = xs.lane_do_run(lane, spec, None, None, {})
        self.assertEqual(status["error_class"], "cli_too_old")
        with open(os.path.join(rdir, "result.json")) as stream:
            result = json.load(stream)
        self.assertEqual(result["attempts"][0]["error_class"], "cli_too_old")


if __name__ == "__main__":
    unittest.main()
