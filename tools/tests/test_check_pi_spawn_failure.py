"""Offline contracts for the Pi spawn-failure forensics (Refs #1747)."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "check-pi-spawn-failure.py"
SPEC = importlib.util.spec_from_file_location("check_pi_spawn_failure", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def proc_tree(root, pid, argv, env=None):
    """A /proc-like directory with one process in it."""
    directory = Path(root) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
    if env is not None:
        (directory / "environ").write_bytes(
            b"\0".join(f"{key}={value}".encode() for key, value in env.items()) + b"\0")
    return directory


class SupervisorTests(unittest.TestCase):
    """The evidence comes from the lane supervisor, not from the shell that runs the tool."""

    def test_the_supervisor_of_the_lane_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_tree(directory, 41, ["python3", "/opt/openswarm/bin/openswarm", "lane", "serve", "M9"])
            proc_tree(directory, 42, ["python3", "/opt/openswarm/bin/openswarm", "lane", "serve", "M8"])
            self.assertEqual(MODULE.supervisor_pid("M9", Path(directory)), 41)
            self.assertIsNone(MODULE.supervisor_pid("M19", Path(directory)))

    def test_only_the_pi_variables_of_the_supervisor_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_tree(directory, 7, ["python3", "/opt/openswarm/bin/openswarm", "lane", "serve", "M9"], {
                "OPENSWARM_PI_BROKER_SOCKET": "/run/openswarm-pi-local/broker.sock",
                "MINIMAX_CN_API_KEY": "secret",
            })
            env = MODULE.process_environment(7, Path(directory), MODULE.PI_ENV_VARS)
            self.assertEqual(env, {"OPENSWARM_PI_BROKER_SOCKET": "/run/openswarm-pi-local/broker.sock"})

    def test_the_wrapper_is_the_one_next_to_the_supervisor_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "releases" / "a9ac8f33" / "openswarm" / "bin"
            binary.mkdir(parents=True)
            (binary / "openswarm").write_text("#!/bin/sh\n")
            wrapper = binary / "pi-isolated"
            wrapper.write_text("#!/usr/bin/env python3\n# --preflight\n")
            proc_tree(directory, 9, ["python3", str(binary / "openswarm"), "lane", "serve", "M9"])
            found = MODULE.supervisor_wrapper(9, Path(directory))
            self.assertEqual(found, wrapper)
            info = MODULE.wrapper_info(found)
            self.assertTrue(info["exists"])
            self.assertTrue(info["supports_preflight"])
            self.assertEqual(len(info["sha256"]), 64)

    def test_a_wrapper_without_preflight_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "pi-isolated"
            wrapper.write_text("#!/usr/bin/env python3\nprint('old release')\n")
            info = MODULE.wrapper_info(wrapper)
            self.assertFalse(info["supports_preflight"])


class GuardMirrorTests(unittest.TestCase):
    """The local mirror of the wrapper's guards, for a wrapper that predates --preflight."""

    def environment(self, root):
        stubs = root / "bin"
        stubs.mkdir(exist_ok=True)
        for name in ("bwrap", "socat"):
            stub = stubs / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        runtime = root / "runtime"
        binary = runtime / "node_modules" / ".bin" / "pi"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        return {"PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
                "OPENSWARM_PI_RUNTIME": str(runtime)}

    def test_an_unset_broker_socket_is_the_failing_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            findings = MODULE.guard_mirror(self.environment(root), root, "minimax-cn", "MiniMax-M3.1-Flash")
            self.assertEqual(findings["guard"], "broker_socket")
            self.assertIn("OPENSWARM_PI_BROKER_SOCKET", findings["error"])

    def test_a_stale_broker_socket_is_not_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.environment(root)
            path = root / "broker.sock"
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            sock.close()
            env["OPENSWARM_PI_BROKER_SOCKET"] = str(path)
            findings = MODULE.guard_mirror(env, root, "minimax-cn", "MiniMax-M3.1-Flash")
            self.assertEqual(findings["guard"], "broker_socket")
            self.assertIn("not live", findings["error"])

    def test_a_live_broker_socket_passes_the_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.environment(root)
            sock = socket.socket(socket.AF_UNIX)
            try:
                path = str(root / "broker.sock")
                sock.bind(path)
                sock.listen()
                env["OPENSWARM_PI_BROKER_SOCKET"] = path
                findings = MODULE.guard_mirror(env, root, "minimax-cn", "MiniMax-M3.1-Flash")
                self.assertIsNone(findings["guard"])
                self.assertTrue(findings["broker"]["live"])
            finally:
                sock.close()


class RunRecordTests(unittest.TestCase):
    """The historical evidence: what the failed runs recorded about themselves."""

    def home(self, root):
        (root / "runs" / "20261103-004712-abc123").mkdir(parents=True)
        (root / "runs" / "20261103-004712-abc123" / "result.json").write_text(json.dumps({
            "id": "20261103-004712-abc123", "lane": "M9", "status": "failed", "error_class": "exec_error",
            "seconds": 118.4, "attempts": [{"error_class": "exec_error", "error_tail": None}]}))
        (root / "runs" / "20261103-004712-abc123" / "status.json").write_text(json.dumps({
            "lane": "M9", "started": 1793677632.0, "finished": 1793677750.4, "state": "finished"}))
        (root / "runs" / "20261103-005000-def456").mkdir(parents=True)
        (root / "runs" / "20261103-005000-def456" / "result.json").write_text(json.dumps({
            "id": "20261103-005000-def456", "lane": "M8", "status": "done", "commit": "abc"}))
        return root

    def test_the_lane_runs_are_read_from_the_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.home(Path(directory))
            runs = MODULE.lane_runs(root, "M9")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["run_id"], "20261103-004712-abc123")
            self.assertEqual(runs[0]["error_class"], "exec_error")
            self.assertEqual(runs[0]["seconds"], 118.4)

    def test_a_run_without_an_error_tail_is_reported_as_a_dropped_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.home(Path(directory))
            verdict = MODULE.historical_verdict(MODULE.lane_runs(root, "M9"))
            self.assertIn("never written down", " ".join(verdict["basis"]))
            self.assertIn("118.4", " ".join(verdict["basis"]))

    def test_the_current_state_is_never_a_historical_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.home(Path(directory))
            report = MODULE.build_report("M9", root, root / "lanes.json", {"backend": "pi"},
                                         "minimax-cn", "MiniMax-M3.1-Flash", {"path": "/x/pi-isolated"},
                                         {"OPENSWARM_PI_BROKER_SOCKET": "/run/broker.sock"}, "supervisor process 1",
                                         {"guard": None, "error": None}, "the wrapper's own --preflight",
                                         MODULE.lane_runs(root, "M9"), None)
            self.assertEqual(report["scope"]["kind"], "current-state")
            self.assertIn("not evidence about the runs that already failed", report["scope"]["note"])
            self.assertIn("current state of the host", report["current_verdict"])
            self.assertNotIn("root cause", report["current_verdict"])


class CliTests(unittest.TestCase):
    """The tool as the operator runs it on the host."""

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              timeout=60)

    def home(self, root):
        home = root / "home"
        (home / "lanes").mkdir(parents=True)
        (home / "lanes.json").write_text(json.dumps({"lanes": [{"name": "M9", "backend": "pi"}]}))
        return home

    def test_a_wrapper_without_preflight_does_not_stop_the_investigation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self.home(root)
            wrapper = root / "pi-isolated"
            wrapper.write_text("#!/usr/bin/env python3\nprint('old release')\n")
            wrapper.chmod(0o755)
            result = self.run_tool("--lane", "M9", "--home", str(home), "--wrapper", str(wrapper),
                                   "--supervisor-pid", str(os.getpid()))
            self.assertIn("local guard mirror", result.stdout)
            self.assertIn("guard", result.stdout)
            self.assertIn("not evidence about the runs that already failed", result.stdout)

    def test_explicit_partial_environment_is_not_evaluated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self.home(root)
            env_file = root / "env"
            env_file.write_text("PATH=/usr/bin\n")
            for args in (("--env", "PATH=/usr/bin"), ("--env-file", str(env_file))):
                with mock.patch.dict(os.environ, {"OPENSWARM_PI_BROKER_SOCKET": "/terminal.sock"}):
                    result = self.run_tool("--lane", "M9", "--home", str(home), *args)
                self.assertIn("not evaluated", result.stdout)
                self.assertNotIn("/terminal.sock", result.stdout)

    def test_without_an_environment_the_guards_are_not_evaluated_with_the_shell_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = self.home(root)
            result = self.run_tool("--lane", "M9", "--home", str(home))
            self.assertIn("not evaluated (the supervisor's environment is unknown)", result.stdout)
            self.assertIn("the shell environment is not the lane's", result.stdout)


if __name__ == "__main__":
    unittest.main()
