import json
import os
import pathlib
import plistlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"
FLEET_CAPACITY = ROOT / "bin" / "fleet-capacity"
FIXTURES = ROOT / "tests" / "fixtures"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
sys.modules["openswarm"] = xs


class LanePureFunctionTests(unittest.TestCase):
    def lane_status_row(self, state):
        lane = {"name": "L1", "backend": "codex", "model": "fake-model"}
        with mock.patch.object(xs, "lane_config", return_value=[lane]), \
                mock.patch.object(xs, "jload", return_value=state), \
                mock.patch.object(xs, "pid_alive", return_value=True), \
                mock.patch.object(xs, "execution_provider", return_value="fake-provider"), \
                mock.patch.object(xs, "load_policy", return_value={}), \
                mock.patch.object(xs, "NOW", return_value=110.0):
            return xs.lane_status_rows()[0]

    def assert_json_lane_status(self, row):
        decoded = json.loads(json.dumps([row], ensure_ascii=False))[0]
        self.assertEqual(decoded, row)
        self.assertEqual(
            list(row),
            ["name", "state", "backend", "model", "pid", "supervisor_pid",
             "quota_reset_at", "current_run", "run_state", "provider",
             "uptime_seconds", "last_error"],
        )
        self.assertIsInstance(row["name"], str)
        self.assertIsInstance(row["state"], str)
        self.assertIsInstance(row["backend"], str)
        self.assertIsInstance(row["model"], str)
        self.assertIsInstance(row["provider"], str)

    def test_lane_status_rows_missing_optional_metadata_is_stable(self):
        row = self.lane_status_row({"state": "idle", "supervisor_pid": 42})

        self.assert_json_lane_status(row)
        self.assertEqual(row["state"], "idle")
        self.assertEqual(row["supervisor_pid"], 42)
        self.assertIsNone(row["pid"])
        self.assertIsNone(row["current_run"])
        self.assertIsNone(row["run_state"])
        self.assertIsNone(row["uptime_seconds"])
        self.assertIsNone(row["last_error"])

    def test_lane_status_rows_none_optional_metadata_is_stable(self):
        row = self.lane_status_row({
            "state": "running", "supervisor_pid": 42, "child_pid": None,
            "resolved_model": None, "quota_until": None, "current_run": None,
            "turn_state": None, "started": None, "last_error": None,
        })

        self.assert_json_lane_status(row)
        self.assertEqual(row["model"], "fake-model")
        for key in ("pid", "quota_reset_at", "current_run", "run_state", "uptime_seconds", "last_error"):
            self.assertIsNone(row[key])

    def test_lane_status_rows_ignores_unknown_metadata(self):
        row = self.lane_status_row({
            "state": "idle", "supervisor_pid": 42, "started": 100.0,
            "unknown_metadata": {"future": True},
        })

        self.assert_json_lane_status(row)
        self.assertEqual(row["uptime_seconds"], 10.0)
        self.assertNotIn("unknown_metadata", row)

    def test_pi_retryable_errors_need_whole_status_codes(self):
        for text in ("529 overloaded_error: Overloaded", "HTTP 503 Service Unavailable", "429 Too Many Requests",
                     "fetch failed", "socket hang up", "request timed out"):
            self.assertTrue(xs.PI_RETRYABLE_RE.search(text), text)
        for text in ("tool exceeded 15003 ms budget", "request id 4291838 rejected", "invalid_request_error: bad schema"):
            self.assertFalse(xs.PI_RETRYABLE_RE.search(text), text)

    def test_systemd_quote_plain_string(self):
        self.assertEqual(xs.systemd_quote("plain"), '"plain"')
        self.assertEqual(xs.systemd_quote("lane_worker_1"), '"lane_worker_1"')
        self.assertEqual(xs.systemd_quote(""), '""')

    def test_systemd_quote_percent_doubled(self):
        self.assertEqual(xs.systemd_quote("%"), '"%%"')
        self.assertEqual(xs.systemd_quote("100%"), '"100%%"')
        self.assertEqual(xs.systemd_quote("user%d@%h"), '"user%%d@%%h"')

    def test_systemd_quote_backslash_escaped(self):
        self.assertEqual(xs.systemd_quote(r"path\to\bin"), r'"path\\to\\bin"')
        self.assertEqual(xs.systemd_quote(r"\n\t\\"), r'"\\n\\t\\\\"')

    def test_systemd_quote_double_quote_escaped(self):
        self.assertEqual(xs.systemd_quote('cmd "arg"'), r'"cmd \"arg\""')
        self.assertEqual(xs.systemd_quote('"quoted"'), r'"\"quoted\""')

    def test_systemd_quote_spaces_and_chinese_path(self):
        self.assertEqual(
            xs.systemd_quote("/home/user/lane dir/测试 路径/openswarm"),
            '"/home/user/lane dir/测试 路径/openswarm"',
        )
        self.assertEqual(
            xs.systemd_quote('/path with space/中文 目录/%lane%/"ctl.sock"\\sub'),
            '"/path with space/中文 目录/%%lane%%/\\"ctl.sock\\"\\\\sub"',
        )

    def test_lane_validate_socket_path_normal(self):
        normal_path = "/tmp/lanes/worker-1/ctl.sock"
        with mock.patch("openswarm.lane_socket_path", return_value=normal_path) as mock_lsp:
            self.assertEqual(xs.lane_validate_socket_path("worker-1"), normal_path)
            mock_lsp.assert_called_once_with("worker-1")

        with mock.patch.object(xs, "DATA", "/tmp/d"):
            expected = xs.lane_socket_path("L1")
            self.assertEqual(xs.lane_validate_socket_path("L1"), expected)

    def test_lane_validate_socket_path_overlength_raises_with_mock(self):
        long_path = "/tmp/" + ("x" * 120) + "/ctl.sock"
        expected_capacity = 104 if sys.platform == "darwin" else 108
        with mock.patch("openswarm.lane_socket_path", return_value=long_path):
            with self.assertRaises(RuntimeError) as cm:
                xs.lane_validate_socket_path("too_long")
            msg = str(cm.exception)
            self.assertIn("AF_UNIX", msg)
            self.assertIn(f"{expected_capacity}-byte", msg)
            self.assertIn(long_path, msg)

    def test_lane_validate_socket_path_platform_limits(self):
        for platform, capacity in [("darwin", 104), ("linux", 108)]:
            limit = capacity - 1
            boundary_ok = "/p/" + ("a" * (limit - 3))
            self.assertEqual(len(os.fsencode(boundary_ok)), limit)

            boundary_overflow = boundary_ok + "b"
            self.assertEqual(len(os.fsencode(boundary_overflow)), capacity)

            with mock.patch.object(sys, "platform", platform):
                with mock.patch("openswarm.lane_socket_path", return_value=boundary_ok):
                    self.assertEqual(xs.lane_validate_socket_path("ok_lane"), boundary_ok)

                with mock.patch("openswarm.lane_socket_path", return_value=boundary_overflow):
                    with self.assertRaises(RuntimeError) as cm:
                        xs.lane_validate_socket_path("overflow_lane")
                    msg = str(cm.exception)
                    self.assertIn("AF_UNIX", msg)
                    self.assertIn(f"{capacity}-byte", msg)
                    self.assertIn(boundary_overflow, msg)

    def test_lane_validate_socket_path_multibyte_utf8_overflow(self):
        multibyte_path = "/tmp/" + ("测" * 40) + "/ctl.sock"
        self.assertGreater(len(os.fsencode(multibyte_path)), 108)
        expected_capacity = 104 if sys.platform == "darwin" else 108
        with mock.patch("openswarm.lane_socket_path", return_value=multibyte_path):
            with self.assertRaises(RuntimeError) as cm:
                xs.lane_validate_socket_path("multibyte")
            msg = str(cm.exception)
            self.assertIn("AF_UNIX", msg)
            self.assertIn(f"{expected_capacity}-byte", msg)
            self.assertIn(multibyte_path, msg)

    def test_lane_validate_socket_path_real_long_name_raises(self):
        ultra_long_name = "lane_" + ("z" * 150)
        expected_capacity = 104 if sys.platform == "darwin" else 108
        full_path = xs.lane_socket_path(ultra_long_name)
        with self.assertRaises(RuntimeError) as cm:
            xs.lane_validate_socket_path(ultra_long_name)
        msg = str(cm.exception)
        self.assertIn("AF_UNIX", msg)
        self.assertIn(f"{expected_capacity}-byte", msg)
        self.assertIn(full_path, msg)

    def test_parse_memory_bytes_uses_systemd_units(self):
        self.assertEqual(xs.parse_memory_bytes("900M"), 900 * 1024 * 1024)
        self.assertEqual(xs.parse_memory_bytes("700M"), 700 * 1024 * 1024)
        self.assertEqual(xs.parse_memory_bytes("2G"), 2 * 1024 ** 3)
        self.assertEqual(xs.parse_memory_bytes("512K"), 512 * 1024)
        self.assertEqual(xs.parse_memory_bytes("1048576"), 1048576)

    def test_parse_memory_bytes_rejects_unusable_values(self):
        for value in ("", "0", "900MB", "-700M", "700 m", "1.5G", "50%", None, 900):
            self.assertIsNone(xs.parse_memory_bytes(value), value)

    def test_lane_memory_limit_defaults_to_high_ceiling(self):
        value, source = xs.lane_memory_limit({"name": "L1"}, {})
        self.assertEqual(value, "900M")
        self.assertEqual(source, "default")
        self.assertGreater(xs.parse_memory_bytes(value), 700 * 1024 * 1024)

    def test_lane_memory_limit_prefers_lane_then_policy(self):
        value, source = xs.lane_memory_limit({"memory_max": "700M"}, {"lane": {"memory_max": "640M"}})
        self.assertEqual((value, source), ("700M", "lane"))
        value, source = xs.lane_memory_limit({}, {"lane": {"memory_max": "640M"}})
        self.assertEqual((value, source), ("640M", "policy"))
        value, source = xs.lane_memory_limit({}, {"lane": {"memory_max": None}})
        self.assertEqual((value, source), ("900M", "default"))

    def test_lane_memory_limit_rejects_invalid_size(self):
        with self.assertRaises(ValueError) as cm:
            xs.lane_memory_limit({"memory_max": "700MB"}, {})
        self.assertIn("memory_max", str(cm.exception))
        with self.assertRaises(ValueError):
            xs.lane_memory_limit({}, {"lane": {"memory_max": "0"}})


# Running bin/openswarm as a script recompiles the whole ~600 KB source on every
# invocation. Loading it as a module reuses the cached bytecode instead, the same
# way tests/_load.py does; the command line, output and exit status are unchanged.
CLI_BOOTSTRAP = (
    "import importlib.machinery, importlib.util;"
    "loader = importlib.machinery.SourceFileLoader('openswarm_cli', {path!r});"
    "spec = importlib.util.spec_from_loader('openswarm_cli', loader);"
    "module = importlib.util.module_from_spec(spec);"
    "loader.exec_module(module);"
    "module.main()"
)


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-", dir="/tmp")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Test")
        self.git("config", "user.email", "lane-test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        for path in FIXTURES.glob("fake_*py"):
            path.chmod(0o755)
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        self.env["OPENSWARM_PI_RETRY_WAIT_SECONDS"] = "2"  # keep retryable-error tests fast
        self.env["FAKE_PI_RPC_LOG"] = str(self.root / "fake-pi-rpc.jsonl")
        self.lane_name = "L1"
        self.backend = "codex"
        self.set_config()

    def tearDown(self):
        supervisor = self.lane_state().get("supervisor_pid")
        if self.data.exists():
            try:
                self.cli("lane", "down", check=False, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                pass
        # The lane supervisor keeps writing into the temporary directory until it
        # exits, and git may still run background maintenance there.  Wait for the
        # supervisor and retry the removal instead of failing the test on that race.
        if supervisor:
            deadline = time.monotonic() + 10
            while process_alive(supervisor) and time.monotonic() < deadline:
                time.sleep(0.05)
        tmp_path = self.tmp.name
        try:
            self.tmp.cleanup()
        except OSError:
            for attempt in range(3):
                if not os.path.exists(tmp_path):
                    break
                try:
                    shutil.rmtree(tmp_path)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)

    def set_config(self, backend=None, model=None, fallback_models=None, memory_max=None):
        if backend:
            self.backend = backend
        fixture = {"codex": "fake_app_server.py", "cursor": "fake_cursor_acp.py",
                   "agy": "fake_agy.py", "pi": "fake_pi_rpc.py"}[self.backend]
        binary = FIXTURES / fixture
        if model is None:
            model = "composer-2.5" if self.backend == "cursor" else (
                "gemini-3.8-flash-medium" if self.backend == "agy" else "fake-model")
        (self.data / "lanes").mkdir(parents=True, exist_ok=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": self.lane_name, "backend": self.backend, "model": model,
            **({"fallback_models": fallback_models} if fallback_models is not None else {}),
            **({"effort": "xhigh"} if self.backend == "codex" else
               ({"provider": "fake-provider"} if self.backend == "pi" else {})),
            **({"memory_max": memory_max} if memory_max is not None else {}),
            "repo": str(self.repo), "binary": str(binary),
        }]}))
        if self.backend == "cursor":
            self.env["FAKE_CURSOR_ACP_LOG"] = str(self.root / "fake-cursor-acp.jsonl")
        if self.backend == "agy":
            self.env["FAKE_AGY_LOG"] = str(self.root / "fake-agy.jsonl")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def cli(self, *args, check=True, timeout=15):
        if self.backend == "pi":
            policy_file = self.data / "policy.local.json"
            policy = json.loads(policy_file.read_text()) if policy_file.exists() else {}
            policy.setdefault("pi", {})["require_isolation"] = False
            policy_file.write_text(json.dumps(policy))
        result = subprocess.run([sys.executable, "-c", CLI_BOOTSTRAP.format(path=str(CLI)), *map(str, args)],
                                env=self.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result

    def start_lane(self):
        result = self.cli("lane", "up", self.lane_name)
        self.assertTrue(json.loads(result.stdout)["lanes"][0]["state"] in ("idle", "starting"))

    def assign(self, task, *extra):
        result = self.cli("lane", "assign", self.lane_name, "--prompt", task, *extra)
        return json.loads(result.stdout)["id"]

    def wait_for(self, predicate, timeout=12, description="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.05)
        self.fail(f"timed out waiting for {description}; last={last!r}")

    def run_state(self, run_id):
        path = self.data / "runs" / run_id / "status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def wait_run(self, run_id, timeout=12):
        return self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "finished" else None),
                             timeout=timeout, description=f"run {run_id} to finish")

    def wait_running(self, run_id):
        return self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "running" else None),
                             description=f"run {run_id} to start")

    def lane_state(self):
        try:
            return json.loads((self.data / "lanes" / self.lane_name / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def enable_fleet_guard(self):
        capacity_dir = self.root / "fleet-capacity"
        guard = self.root / "fleet-guard.json"
        guard.write_text(json.dumps({"command": [str(FLEET_CAPACITY), "gocc"]}))
        self.env["OPENSWARM_FLEET_GUARD_CONFIG"] = str(guard)
        self.env["OPENSWARM_FLEET_CAPACITY_DIR"] = str(capacity_dir)

    def fleet_reservations(self):
        result = self.fleet_call({"op": "status"})
        return json.loads(result.stdout)["reservations"]

    def fleet_call(self, payload):
        return subprocess.run([str(FLEET_CAPACITY), "gocc"], input=json.dumps(payload),
                                text=True, capture_output=True, check=True, env=self.env)

    @staticmethod
    def process_identity(pid):
        try:
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as stream:
                fields = stream.read().rsplit(")", 1)[1].split()
            if len(fields) > 19:
                return f"linux-start:{fields[19]}"
        except (OSError, ValueError, IndexError):
            pass
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], text=True,
                                capture_output=True, check=False)
        return f"ps-lstart:{result.stdout.strip()}" if result.returncode == 0 and result.stdout.strip() else None

    def test_lane_supervisor_recovers_killed_runner_after_ttl_without_new_admission(self):
        self.enable_fleet_guard()
        (self.data / "policy.local.json").write_text(json.dumps({"fleet": {
            "recovery_ttl_seconds": 0.1, "recovery_poll_seconds": 0.02}}))
        self.start_lane()
        self.wait_for(lambda: (state if (state := self.lane_state()).get("supervisor_pid") and
                               state.get("state") in ("idle", "busy") else None),
                      description="resident lane supervisor to become ready")
        run_id, token = "killed-runner", "a" * 32
        answer = json.loads(self.fleet_call({"op": "acquire", "token": token, "run_id": run_id,
                                             "backend": "pi", "provider": "minimax-cn"}).stdout)
        self.assertTrue(answer["admitted"])
        runner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        executor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.kill(runner.pid, signal.SIGKILL)
            runner.wait()
            rdir = self.data / "runs" / run_id
            rdir.mkdir(parents=True)
            (rdir / "status.json").write_text(json.dumps({
                "id": run_id, "state": "running", "runner_pid": runner.pid,
                "executor_pid": executor.pid, "executor_pgid": os.getpgid(executor.pid),
                "executor_identity": self.process_identity(executor.pid),
                "fleet_host": "gocc", "fleet_reserved_at": time.time() - 1,
            }))
            # No later assignment/reserve occurs: the existing lane polling loop must stop only this
            # recorded group and perform recovery after the group is gone.
            self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("fleet_recovery_stop_requested") else None),
                          timeout=15, description="resident lane to stop the recorded executor group")
            # The recovery marker records that SIGTERM/SIGKILL was requested.  On macOS the
            # runner can publish that marker just before the child scheduler observes its
            # SIGTERM/SIGKILL shutdown sequence,
            # so an immediate poll is a race rather than evidence that recovery failed.  Wait
            # for, and assert, the actual forced exit: this must still happen before capacity
            # can be released below.
            self.wait_for(lambda: executor.poll() is not None, timeout=15,
                          description="recorded executor to exit after TTL recovery")
            self.assertIn(executor.returncode, (-signal.SIGTERM, -signal.SIGKILL))
            executor.wait()
            self.wait_for(lambda: not self.fleet_reservations(), timeout=15,
                          description="resident lane to recover a dead runner")
            status = self.run_state(run_id)
            self.assertIn("fleet_recovered_at", status)
            self.assertIsNone(status.get("fleet_reservation_error"))
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if executor.poll() is None:
                os.killpg(os.getpgid(executor.pid), signal.SIGKILL)
                executor.wait()

    def test_lane_supervisor_never_kills_unconfirmed_executor_group(self):
        self.enable_fleet_guard()
        (self.data / "policy.local.json").write_text(json.dumps({"fleet": {
            "recovery_ttl_seconds": 0.1, "recovery_poll_seconds": 0.02}}))
        self.start_lane()
        run_id, token = "unknown-group", "b" * 32
        self.assertTrue(json.loads(self.fleet_call({"op": "acquire", "token": token, "run_id": run_id,
                                                    "backend": "pi", "provider": "minimax-cn"}).stdout)["admitted"])
        runner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        executor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.kill(runner.pid, signal.SIGKILL)
            runner.wait()
            rdir = self.data / "runs" / run_id
            rdir.mkdir(parents=True)
            (rdir / "status.json").write_text(json.dumps({
                "id": run_id, "state": "running", "runner_pid": runner.pid,
                "executor_pid": executor.pid, "executor_pgid": os.getpgid(executor.pid),
                "executor_identity": "not-this-process", "fleet_host": "gocc", "fleet_reserved_at": time.time() - 1,
            }))
            retained = self.wait_for(lambda: (s if "identity cannot be confirmed" in
                                               str((s := self.run_state(run_id)).get("fleet_reservation_error")) else None),
                                     timeout=5, description="unconfirmed executor group to be retained")
            self.assertEqual(len(self.fleet_reservations()), 1)
            self.assertIsNone(executor.poll(), retained)
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if executor.poll() is None:
                os.killpg(os.getpgid(executor.pid), signal.SIGKILL)
                executor.wait()

    def test_assign_commits_and_writes_normal_result(self):
        self.set_config("pi")
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        (knowledge / "card-types.yaml").write_text("card_types:\n  - feature\n")
        (knowledge / "lessons").mkdir()
        (knowledge / "lessons" / "L-0200.yaml").write_text(
            "id: L-0200\nrule: Keep lane edits covered by tests.\nguard_level: L2\n"
            "applies_to:\n  card_types: [feature]\n  paths: [src/*.py]\n  backends: [pi]\n")
        (self.data / "policy.local.json").write_text(json.dumps({"knowledge": {"repo": str(knowledge)}}))
        self.start_lane()
        run_id = self.assign("add the fixture change", "--card-type", "feature", "--paths", "src/lane.py",
                             "--rework-of", "parent-run")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(result["lane"], self.lane_name)
        self.assertTrue(result["commit"])
        self.assertEqual(result["branch"], f"openswarm/{run_id}")
        ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
        entry = next(row for row in ledger if row["id"] == run_id)
        self.assertEqual(entry["card_type"], "feature")
        self.assertEqual(entry["rework_of"], "parent-run")
        self.assertEqual(entry["lessons_injected"], ["L-0200"])
        self.assertEqual(entry["area"], ["lane_result.txt"])
        messages = [json.loads(line) for line in pathlib.Path(self.env["FAKE_PI_RPC_LOG"]).read_text().splitlines()]
        prompt = next(row["message"] for row in messages if row.get("type") == "prompt")
        self.assertIn("[L-0200] Keep lane edits covered by tests.", prompt)
        self.assertEqual((self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text(), "completed")
        self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"), "completed")
        self.assertFalse((self.repo / "lane_result.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("-C", str(self.data / "lanes" / self.lane_name / "wt"), "branch", "--show-current"), "")

    def test_assignment_refreshes_head_and_records_workspace_note(self):
        self.start_lane()
        (self.repo / "package.json").write_text('{"workspaces": ["packages/*"]}\n')
        (self.repo / "bun.lock").write_text("# test lock\n")
        (self.repo / "after-lane-start.txt").write_text("new repo head\n")
        self.git("add", "package.json", "bun.lock", "after-lane-start.txt")
        self.git("commit", "-q", "-m", "move lane base")
        head = self.git("rev-parse", "HEAD")

        run_id = self.assign("refresh to current repo head")
        self.wait_run(run_id)
        status = self.run_state(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["workspace"]["base"], head)
        self.assertIn("Bun workspace detected", result["workspace_note"])
        self.assertEqual(status["workspace_note"], result["workspace_note"])
        self.assertTrue((self.data / "lanes" / self.lane_name / "wt" / "after-lane-start.txt").exists())

    def test_assignment_rebuilds_a_broken_lane_worktree(self):
        self.start_lane()
        wt = self.data / "lanes" / self.lane_name / "wt"
        (wt / ".git").unlink()
        (wt / "stale-worktree-file").write_text("discarded\n")
        expected_head = self.git("rev-parse", "HEAD")

        run_id = self.assign("rebuild broken lane worktree")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["workspace"]["base"], expected_head)
        self.assertFalse((wt / "stale-worktree-file").exists())

    def test_assignment_rebuilds_a_missing_lane_worktree(self):
        self.start_lane()
        wt = self.data / "lanes" / self.lane_name / "wt"
        shutil.rmtree(wt)
        expected_head = self.git("rev-parse", "HEAD")

        run_id = self.assign("rebuild missing lane worktree")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["workspace"]["base"], expected_head)
        self.assertTrue(wt.is_dir())

    def test_pi_auto_retry_after_transient_error_finishes_the_turn(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_AUTO_RETRY transient overload")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertIsNone(result.get("error_class"))
        self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"), "completed")
        messages = [json.loads(line) for line in pathlib.Path(self.env["FAKE_PI_RPC_LOG"]).read_text().splitlines()]
        self.assertEqual(sum(row.get("type") == "prompt" for row in messages), 1)

    def test_pi_auto_retry_exhausted_fails_with_final_error(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_AUTO_RETRY LANE_TEST_AUTO_RETRY_EXHAUSTED transient overload")
        state = self.wait_run(run_id)
        self.assertNotEqual(state["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertIn("overloaded", json.dumps(result))

    def test_pi_rate_limit_retries_in_session_and_watch_shows_alert(self):
        self.set_config("pi")
        (self.data / "policy.local.json").write_text(json.dumps({"rate_limit": {
            "backoff_seconds": [0], "max_retries": 1}}))
        self.start_lane()
        run_id = self.assign("LANE_TEST_RATE_LIMIT_ONCE resume after provider limit")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        events = [json.loads(line) for path in (self.data / "runs" / run_id).glob("events.*.jsonl")
                  for line in path.read_text().splitlines()]
        self.assertEqual(sum(event.get("type") == "RATELIMIT" for event in events), 1)
        messages = [json.loads(line) for line in pathlib.Path(self.env["FAKE_PI_RPC_LOG"]).read_text().splitlines()]
        self.assertEqual(sum(row.get("type") == "new_session" for row in messages), 1)
        prompts = [row["message"] for row in messages if row.get("type") == "prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[1], "You were interrupted by a provider rate limit. Continue the task from where you stopped; finish with the final JSON report.")
        watched = self.cli("watch", run_id, "--quiet").stdout
        self.assertIn("RATELIMIT:", watched)
        self.assertEqual(result["status"], "done")

    def test_repeated_rate_limit_keeps_partial_work_with_note(self):
        self.set_config("pi")
        (self.data / "policy.local.json").write_text(json.dumps({"rate_limit": {
            "backoff_seconds": [0], "max_retries": 1}}))
        self.start_lane()
        run_id = self.assign("LANE_TEST_RATE_LIMIT_PARTIAL keep partial changes")
        self.assertEqual(self.wait_run(run_id)["status"], "partial")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["verify_note"], "rate limited")
        self.assertEqual(result["error_class"], "rate_limit")
        self.assertIsNotNone(result["commit"])
        empty_run = self.assign("LANE_TEST_RATE_LIMIT_ALWAYS no changes")
        self.assertEqual(self.wait_run(empty_run)["status"], "failed")
        empty_result = json.loads((self.data / "runs" / empty_run / "result.json").read_text())
        self.assertEqual(empty_result["verify_note"], "rate limited")
        self.assertIsNone(empty_result["commit"])

    def test_provider_cap_queues_second_lane_until_slot_frees(self):
        self.set_config("pi")
        config_path = self.data / "lanes.json"
        config = json.loads(config_path.read_text())
        config["lanes"].append(dict(config["lanes"][0], name="L2"))
        config_path.write_text(json.dumps(config))
        (self.data / "policy.local.json").write_text(json.dumps({"concurrency": {"fake-provider": 1}}))
        self.cli("lane", "up", "L1")
        self.cli("lane", "up", "L2")
        first = self.assign("LANE_TEST_WAIT_SHORT first provider task")
        self.wait_running(first)
        second = json.loads(self.cli("lane", "assign", "L2", "--prompt", "second provider task").stdout)["id"]
        queued = self.wait_for(lambda: (state if (state := self.run_state(second)).get("queue_reason") else None),
                               description="second provider turn to queue")
        self.assertEqual(queued["queue_reason"], "provider concurrency limit")
        lanes = {row["name"]: row for row in json.loads(self.cli("lane", "status").stdout)}
        self.assertEqual(lanes["L2"]["run_state"], "queued")
        self.assertEqual(self.wait_run(first)["status"], "done")
        self.assertEqual(self.wait_run(second)["status"], "done")

    def test_total_cap_queues_different_backends_until_slot_frees(self):
        self.set_config("pi")
        config_path = self.data / "lanes.json"
        config = json.loads(config_path.read_text())
        second_lane = dict(config["lanes"][0], name="L2", backend="codex", model="fake-model", effort="xhigh")
        second_lane.pop("provider", None)
        second_lane["binary"] = str(FIXTURES / "fake_app_server.py")
        config["lanes"].append(second_lane)
        config_path.write_text(json.dumps(config))
        (self.data / "policy.local.json").write_text(json.dumps({"concurrency": {"total": 1}}))
        self.cli("lane", "up", "L1")
        self.cli("lane", "up", "L2")
        first = self.assign("LANE_TEST_WAIT_SHORT first total-cap task")
        self.wait_running(first)
        second = json.loads(self.cli("lane", "assign", "L2", "--prompt", "second backend task").stdout)["id"]
        queued = self.wait_for(lambda: (state if (state := self.run_state(second)).get("queue_reason") else None),
                               description="second backend turn to queue on total cap")
        self.assertEqual(queued["queue_reason"], "provider concurrency limit")
        self.assertEqual(self.wait_run(first)["status"], "done")
        self.assertEqual(self.wait_run(second)["status"], "done")

    def test_codex_assign_commits_and_writes_normal_result(self):
        self.start_lane()
        run_id = self.assign("add the fixture change")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertTrue(result["commit"])
        self.assertEqual(result["branch"], f"openswarm/{run_id}")
        self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"), "completed")

    def test_pi_session_leak_is_excluded_even_when_staged(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_SESSION_LEAK")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        artifact = "2026-10-26T10-17-04-024Z_12345678-1234-1234-1234-123456789abc.jsonl"
        self.assertEqual(result["excluded_files"], [artifact])
        self.assertNotIn(artifact, self.git("ls-tree", "-r", "--name-only", result["commit"]))
        self.assertTrue((self.data / "lanes" / self.lane_name / "pi-sessions").is_dir())

    def test_budget_refuses_lane_assignment(self):
        self.set_config("pi")
        (self.data / "policy.local.json").write_text(json.dumps({"budget": {"daily_executor_minutes": 0}}))
        self.start_lane()
        response = self.cli("lane", "assign", self.lane_name, "--prompt", "must not start", check=False)
        self.assertEqual(response.returncode, 3, response.stderr)
        self.assertEqual(json.loads(response.stdout)["error"], "budget_exceeded_daily")
        self.assertEqual(self.lane_state()["state"], "idle")

    def test_rm_outside_worktree_is_refused_and_the_turn_continues(self):
        for backend in ("pi", "codex"):
            with self.subTest(backend=backend):
                self.set_config(backend)
                self.start_lane()
                run_id = self.assign("LANE_TEST_RM_OUTSIDE LANE_TEST_WAIT")
                self.assertEqual(self.wait_run(run_id)["status"], "done")
                result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
                self.assertIsNone(result.get("error_class"))
                self.assertFalse(result.get("risk_blocks"))
                self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"), "completed")
                events = (self.data / "runs" / run_id / f"events.1.{backend}.jsonl").read_text()
                self.assertIn('"risk_denied"', events)
                self.assertIn('"risk_resume"', events)
                self.cli("lane", "down", self.lane_name)

    def test_pi_streaming_updates_do_not_repeat_the_message_snapshot(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_STREAM")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        rows = [json.loads(line) for line in (self.data / "runs" / run_id / "events.1.pi.jsonl").read_text().splitlines()]
        update = next(row for row in rows if row.get("type") == "message_update")
        self.assertNotIn("message", update)
        self.assertEqual(update["assistantMessageEvent"], {"type": "text_delta", "delta": "x"})

    def test_risk_aborts_lane_turns_without_watch(self):
        for backend in ("pi", "codex", "cursor"):
            with self.subTest(backend=backend):
                self.set_config(backend)
                self.start_lane()
                run_id = self.assign("LANE_TEST_RISK LANE_TEST_WAIT", "--verify", "true")
                self.assertEqual(self.wait_run(run_id)["status"], "stopped")
                result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
                self.assertEqual(result["error_class"], "risk_blocked")
                self.assertIsNone(result["verify"])
                self.assertEqual(result["risk_blocks"][0]["risk"], "push")
                self.assertIn("git push", result["risk_blocks"][0]["command"])
                self.assertNotIn("secret-value", json.dumps(result))
                if backend == "cursor":
                    log = [json.loads(line) for line in (self.root / "fake-cursor-acp.jsonl").read_text().splitlines()]
                    reply = next(row["permission_response"] for row in log if "permission_response" in row)
                    self.assertEqual(reply["result"]["outcome"]["outcome"], "cancelled")
                    self.assertTrue(any(row.get("method") == "session/cancel" for row in log))
                self.cli("lane", "down", self.lane_name)

    def test_codex_retries_model_unavailable_turn_with_lane_fallback(self):
        self.set_config(model="fake-model", fallback_models=["gpt-6-luna"])
        self.env["FAKE_APP_SERVER_OVERLOAD_MODEL"] = "fake-model"
        self.start_lane()
        run_id = self.assign("retry the overloaded turn")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["model"], "gpt-6-luna")
        self.assertEqual([attempt["model"] for attempt in result["attempts"]], ["fake-model", "gpt-6-luna"])
        self.assertEqual([attempt["error_class"] for attempt in result["attempts"]], ["model_unavailable", None])
        self.assertTrue((self.data / "lanes" / self.lane_name / "wt" / "lane_partial.txt").exists())
        self.assertTrue((self.data / "runs" / run_id / "events.2.codex.jsonl").exists())
        self.assertIn("Selected model is at capacity", result["attempts"][0]["error_tail"])
        self.assertIn("codex/fake-model", json.loads((self.data / "breakers.json").read_text()))

    def cursor_log(self):
        path = pathlib.Path(self.env["FAKE_CURSOR_ACP_LOG"])
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_cursor_assign_selects_model_and_commits_without_verify(self):
        self.set_config("cursor")
        config_path = self.data / "lanes.json"
        config = json.loads(config_path.read_text())
        config["lanes"][0].pop("model")
        config_path.write_text(json.dumps(config))
        self.start_lane()
        run_id = self.assign("add the cursor fixture change")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertIsNone(result["verify"])
        self.assertTrue(result["commit"])
        self.assertEqual(result["branch"], f"openswarm/{run_id}")
        self.assertIn("add the cursor fixture change",
                      (self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text())
        methods = [entry.get("method") for entry in self.cursor_log()]
        self.assertIn("session/new", methods)
        self.assertIn("session/set_config_option", methods)
        self.assertIn("session/prompt", methods)
        argv = next(entry["argv"] for entry in self.cursor_log() if "argv" in entry)
        self.assertEqual(argv[1:], ["--sandbox", "enabled", "acp"])
        self.assertEqual(json.loads((self.data / "lanes" / "L1" / "state.json").read_text())["model"],
                         "composer-2.5")
        choice = next(entry for entry in self.cursor_log() if entry.get("method") == "session/set_config_option")
        self.assertEqual(choice["params"]["value"], "composer-2.5")
        initialize = next(entry for entry in self.cursor_log() if entry.get("method") == "initialize")
        self.assertEqual(initialize["params"]["clientCapabilities"], {})
        new_session = next(entry for entry in self.cursor_log() if entry.get("method") == "session/new")
        self.assertEqual(new_session["params"]["mcpServers"], [])
        self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"),
                         (self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text().rstrip("\n"))
        watched = self.cli("watch", run_id).stdout
        self.assertIn("Plan: Apply the requested change", watched)
        self.assertIn("CMD: Edit lane file", watched)
        self.assertIn("MSG:", watched)
        ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
        self.assertTrue(any(entry["id"] == run_id and entry["status"] == "done" for entry in ledger))

    def test_cursor_agent_commit_is_reported_with_branch_and_diffstat(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_AGENT_COMMIT commit the result in the worktree")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(result["branch"], f"openswarm/{run_id}")
        self.assertTrue(result["commit"])
        self.assertIn("lane_result.txt", result["diffstat"])
        self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"),
                         (self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text().rstrip("\n"))

    def test_cursor_systemd_unit_is_printed_not_installed(self):
        self.set_config("cursor")
        unit = self.cli("lane", "systemd-unit", self.lane_name).stdout
        self.assertIn("[Service]", unit)
        self.assertIn("User=", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("MemoryMax=900M", unit)
        self.assertIn("CPUQuota=150%", unit)
        self.assertIn("Nice=10", unit)
        self.assertIn(os.path.expanduser("~/.local/bin"), unit)
        self.assertIn("lane\" \"serve\" \"L1", unit)

    def memory_max_of(self, unit):
        line = next(row for row in unit.splitlines() if row.startswith("MemoryMax="))
        return line.split("=", 1)[1]

    def test_systemd_unit_memory_max_default_stays_high(self):
        unit = self.cli("lane", "systemd-unit", self.lane_name).stdout
        self.assertEqual(self.memory_max_of(unit), "900M")
        self.assertGreater(xs.parse_memory_bytes(self.memory_max_of(unit)), 700 * 1024 * 1024)

    def test_systemd_unit_memory_max_can_be_lowered_per_lane(self):
        self.set_config(memory_max="700M")
        unit = self.cli("lane", "systemd-unit", self.lane_name).stdout
        self.assertEqual(self.memory_max_of(unit), "700M")
        self.assertNotIn("MemoryMax=900M", unit)
        self.assertLessEqual(xs.parse_memory_bytes(self.memory_max_of(unit)), 700 * 1024 * 1024)

    def test_systemd_unit_memory_max_can_be_lowered_by_host_policy(self):
        (self.data / "policy.local.json").write_text(json.dumps({"lane": {"memory_max": "640M"}}))
        unit = self.cli("lane", "systemd-unit", self.lane_name).stdout
        self.assertEqual(self.memory_max_of(unit), "640M")
        self.assertLessEqual(xs.parse_memory_bytes(self.memory_max_of(unit)), 700 * 1024 * 1024)

    def test_systemd_unit_rejects_invalid_memory_max(self):
        self.set_config(memory_max="700MB")
        result = self.cli("lane", "systemd-unit", self.lane_name, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("memory_max", result.stderr)

    def test_launchd_plist_memory_limit_equivalent(self):
        default = json.loads(self.cli("lane", "launchd-plist", self.lane_name).stdout)["plist"]
        self.assertNotIn("HardResourceLimits", default)
        self.assertNotIn("SoftResourceLimits", default)

        self.set_config(memory_max="700M")
        plist = json.loads(self.cli("lane", "launchd-plist", self.lane_name).stdout)["plist"]
        limits = plistlib.loads(plist.encode())
        self.assertEqual(limits["HardResourceLimits"]["Memory"], 700 * 1024 * 1024)
        self.assertEqual(limits["SoftResourceLimits"]["Memory"], 700 * 1024 * 1024)
        self.assertLessEqual(limits["HardResourceLimits"]["Memory"], 700 * 1024 * 1024)

    def test_cursor_model_missing_fails_with_available_values(self):
        self.set_config("cursor", model="missing-model")
        self.start_lane()
        run_id = self.assign("should fail model selection")
        state = self.wait_run(run_id)
        self.assertEqual(state["status"], "failed")
        events = (self.data / "runs" / run_id / "events.1.cursor.jsonl").read_text()
        self.assertIn("composer-2.5", events)
        self.assertIn("other-model", events)
        self.assertNotIn("session/prompt", [entry.get("method") for entry in self.cursor_log()])

    def test_cursor_base_model_resolves_parameterized_value(self):
        self.set_config("cursor")
        resolved = "composer-2.5[fast=true]"
        self.env["FAKE_CURSOR_ACP_MODELS"] = json.dumps([resolved, "other-model"])
        self.start_lane()
        run_id = self.assign("resolve the base model")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["model"], resolved)
        self.assertEqual(result["resolved_model"], resolved)
        self.assertEqual(self.lane_state()["model"], resolved)
        choice = next(entry for entry in self.cursor_log() if entry.get("method") == "session/set_config_option")
        self.assertEqual(choice["params"]["value"], resolved)

    def test_cursor_exact_model_wins_over_base_matches(self):
        self.set_config("cursor")
        self.env["FAKE_CURSOR_ACP_MODELS"] = json.dumps(["composer-2.5[fast=true]", "composer-2.5"])
        self.start_lane()
        run_id = self.assign("prefer the exact model")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        choice = next(entry for entry in self.cursor_log() if entry.get("method") == "session/set_config_option")
        self.assertEqual(choice["params"]["value"], "composer-2.5")

    def test_cursor_ambiguous_base_model_fails_with_matches(self):
        self.set_config("cursor")
        values = ["composer-2.5[fast=true]", "composer-2.5[fast=false]"]
        self.env["FAKE_CURSOR_ACP_MODELS"] = json.dumps(values)
        self.start_lane()
        run_id = self.assign("reject ambiguous model")
        self.assertEqual(self.wait_run(run_id)["status"], "failed")
        events = (self.data / "runs" / run_id / "events.1.cursor.jsonl").read_text()
        self.assertIn("ambiguous", events)
        for value in values:
            self.assertIn(value, events)
        self.assertNotIn("session/prompt", [entry.get("method") for entry in self.cursor_log()])

    def test_agy_quota_reset_beyond_budget_does_not_retry(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "long-quota"
        quota_file = self.root / "quota.txt"
        quota_file.write_text("0")
        self.env["FAKE_AGY_QUOTA_FILE"] = str(quota_file)
        self.start_lane()
        run_id = self.assign("review the fixture")
        self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["error_class"], "rate_limit")
        self.wait_for(lambda: self.lane_state().get("state") == "idle")
        rows = json.loads(self.cli("lane", "status").stdout)
        self.assertEqual(rows[0]["state"], "quota_exhausted")
        self.assertGreater(rows[0]["quota_reset_at"], time.time())
        self.assertLessEqual(rows[0]["quota_reset_at"], time.time() + 3600)
        state_path = self.data / "lanes" / self.lane_name / "state.json"
        state = json.loads(state_path.read_text())
        state["quota_until"] = 1
        state_path.write_text(json.dumps(state))
        self.wait_for(lambda: self.lane_state().get("quota_until", 0) > time.time())
        self.assertEqual(json.loads(self.cli("lane", "status").stdout)[0]["state"], "quota_exhausted")
        quota_file.write_text("0.5")
        state = json.loads(state_path.read_text())
        state["quota_until"] = 1
        state_path.write_text(json.dumps(state))
        self.wait_for(lambda: self.lane_state().get("quota_model") is None)
        self.assertEqual(json.loads(self.cli("lane", "status").stdout)[0]["state"], "idle")

    def test_agy_assign_commits_and_writes_normal_result(self):
        self.set_config("agy")
        self.start_lane()
        run_id = self.assign("add the fixture change")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(result["backend"], "agy")
        self.assertEqual(result["model"], "gemini-3.8-flash-medium")
        self.assertEqual(result["usage"]["input_tokens"], 120)
        self.assertTrue(result["commit"])
        self.assertEqual(self.git("show", f"openswarm/{run_id}:lane_result.txt"),
                         "completed by the fake Antigravity CLI")
        calls = [json.loads(line) for line in pathlib.Path(self.env["FAKE_AGY_LOG"]).read_text().splitlines()]
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "gemini-3.8-flash-medium")
        self.assertEqual(calls[0][calls[0].index("--output-format") + 1], "stream-json")

    def test_agy_silent_stdout_eof_records_reaped_exit_code(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "silent-eof"
        self.start_lane()
        run_id = self.assign("inspect the fixture")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["executor_exit_code"], 17)
        self.assertEqual(result["attempts"][-1]["error_class"], "exec_error")
        self.assertIn("executor exit code 17", result["attempts"][-1]["error_tail"])

    def test_agy_silent_stdout_eof_keeps_stderr_and_exit_code(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "silent-eof-stderr"
        self.start_lane()
        run_id = self.assign("inspect the fixture")
        self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        tail = result["attempts"][-1]["error_tail"]
        self.assertIn("transient provider transport failure", tail)
        self.assertIn("executor exit code 17", tail)

    def test_agy_steer_aborts_the_process_and_resumes_the_conversation(self):
        self.set_config("agy")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT original task")
        self.wait_running(run_id)
        event_file = self.data / "runs" / run_id / "events.1.agy.jsonl"
        self.wait_for(lambda: event_file.exists() and '"conversation_id": "conv-1"' in event_file.read_text(),
                      description="Antigravity conversation initialization")
        self.cli("lane", "steer", self.lane_name, "--text", "write steered instruction")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        calls = [json.loads(line) for line in pathlib.Path(self.env["FAKE_AGY_LOG"]).read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn("--conversation", calls[1])
        self.assertEqual(calls[1][calls[1].index("--conversation") + 1], "conv-1")
        self.assertIn("write steered instruction", calls[1][calls[1].index("-p") + 1])
        events = (self.data / "runs" / run_id / "events.1.agy.jsonl").read_text()
        self.assertIn('"mode": "abort-and-resume"', events)
        self.assertTrue((self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").exists())

    def test_cursor_permission_is_allowed_and_logged(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_PERMISSION make the change")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        events = [json.loads(line) for line in
                  (self.data / "runs" / run_id / "events.1.cursor.jsonl").read_text().splitlines()]
        permission = next(event for event in events if event.get("type") == "permission_request")
        self.assertEqual(permission["selected"], "allow_always")
        reply = next(event["permission_response"] for event in self.cursor_log() if "permission_response" in event)
        self.assertEqual(reply["result"]["outcome"], {"outcome": "selected", "optionId": "always"})

    def test_cursor_steer_cancels_then_continues_same_session(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT original task")
        self.wait_running(run_id)
        self.cli("lane", "steer", self.lane_name, "--text", "write steered instruction")
        self.assertEqual(self.wait_run(run_id)["status"], "done")
        log = self.cursor_log()
        sessions = [entry for entry in log if entry.get("method") == "session/new"]
        prompts = [entry for entry in log if entry.get("method") == "session/prompt"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0]["params"]["sessionId"], prompts[1]["params"]["sessionId"])
        correction = prompts[1]["params"]["prompt"][0]["text"]
        self.assertIn("Correction to the interrupted task", correction)
        self.assertIn("write steered instruction", correction)
        # The follow-up prompt replaces the cancelled one, so it has to restate the report
        # requirement; without it the agent ends the turn without printing the JSON report.
        self.assertIn("your FINAL message must be ONLY a JSON object", correction)
        self.assertLess(correction.index("write steered instruction"),
                        correction.index("your FINAL message must be ONLY a JSON object"))
        self.assertTrue(any(entry.get("method") == "session/cancel" for entry in log))
        events = (self.data / "runs" / run_id / "events.1.cursor.jsonl").read_text()
        self.assertIn('"mode": "cancel-and-continue"', events)
        self.assertIn("write steered instruction",
                      (self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text())

    def test_cursor_turn_without_report_is_partial(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_NO_REPORT make the change")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["result"])
        self.assertEqual(result["attempts"][0]["error_class"], "no_report")
        self.assertIn("executor ended without a final report", result["verify_note"])
        self.assertTrue(result["commit"])
        self.assertIn("lane_result.txt", self.git("ls-tree", "--name-only", f"openswarm/{run_id}"))
        ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
        self.assertTrue(any(entry["id"] == run_id and entry["status"] == "partial" for entry in ledger))

    def test_cursor_turn_without_report_honours_verify(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_NO_REPORT make the change", "--verify", "true")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertIn("executor ended without a final report", result["verify_note"])

    def test_cursor_turn_without_report_or_change_is_failed(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_NO_REPORT LANE_TEST_NO_CHANGE leave the tree alone")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["commit"])

    def test_cursor_abort_marks_run_stopped(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT abort this")
        self.wait_running(run_id)
        self.cli("lane", "abort", self.lane_name)
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(result["status"], "stopped")

    def test_cursor_budget_interrupt_is_recorded(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT exceed the budget", "--budget-minutes", "0.02")
        self.assertEqual(self.wait_run(run_id)["status"], "budget_exceeded")
        self.assertTrue(any(entry.get("method") == "session/cancel" for entry in self.cursor_log()))

    def test_cursor_crash_fails_run_and_restarts_child(self):
        self.set_config("cursor")
        self.start_lane()
        run_id = self.assign("LANE_TEST_CRASH")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(result["attempts"][0]["error_class"], "exec_error")
        restarted = self.wait_for(lambda: (s if s.get("state") == "idle" and s.get("child_pid") else None)
                                   if (s := self.lane_state()) else None,
                                   timeout=8, description="Cursor ACP child restart")
        self.assertTrue(restarted["child_pid"])

    def test_crashed_lane_child_releases_shared_fleet_slot(self):
        self.set_config("cursor")
        self.enable_fleet_guard()
        self.start_lane()
        run_id = self.assign("LANE_TEST_CRASH")
        state = self.wait_run(run_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.fleet_reservations(), [])

    def test_signal_terminated_lane_child_retains_shared_fleet_slot(self):
        """Do not reuse a lane slot while a SIGKILLed parent's child is still alive."""
        self.set_config("cursor")
        self.enable_fleet_guard()
        child_marker = self.root / "signal-child.pid"
        self.env["FAKE_CURSOR_CHILD_PID"] = str(child_marker)
        self.start_lane()
        run_id = self.assign("LANE_TEST_SIGNAL_WITH_CHILD")
        try:
            state = self.wait_run(run_id)
            self.assertEqual(state["status"], "failed")
            child_pid = int(child_marker.read_text())
            self.assertTrue(process_alive(child_pid))
            reservations = self.fleet_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["run_id"], run_id)
        finally:
            if child_marker.exists():
                try:
                    os.kill(int(child_marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_normally_exited_lane_parent_with_child_retains_shared_fleet_slot(self):
        """A normal wrapper exit must not release a slot held by its child."""
        self.set_config("cursor")
        self.enable_fleet_guard()
        child_marker = self.root / "normal-exit-child.pid"
        self.env["FAKE_CURSOR_CHILD_PID"] = str(child_marker)
        self.start_lane()
        run_id = self.assign("LANE_TEST_EXIT_WITH_CHILD")
        try:
            state = self.wait_run(run_id)
            self.assertEqual(state["status"], "failed")
            self.assertTrue(state.get("executor_pgid"))
            self.assertTrue(process_alive(int(child_marker.read_text())))
            reservations = self.fleet_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["run_id"], run_id)
        finally:
            if child_marker.exists():
                try:
                    os.kill(int(child_marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_normally_exited_lane_parent_with_detached_child_retains_shared_fleet_slot(self):
        """A recorded child session keeps the lane slot after an ordinary parent exit."""
        self.set_config("cursor")
        self.enable_fleet_guard()
        child_marker = self.root / "normal-exit-detached-child.pid"
        self.env["FAKE_CURSOR_CHILD_PID"] = str(child_marker)
        self.start_lane()
        run_id = self.assign("LANE_TEST_EXIT_WITH_DETACHED_CHILD")
        try:
            state = self.wait_run(run_id)
            self.assertEqual(state["status"], "failed")
            self.assertTrue(process_alive(int(child_marker.read_text())))
            self.assertTrue(state.get("executor_descendants"), state)
            reservations = self.fleet_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["run_id"], run_id)
        finally:
            if child_marker.exists():
                try:
                    os.kill(int(child_marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_headless_cursor_parent_with_child_retains_shared_fleet_slot(self):
        """Headless Cursor must record its group before testing an ordinary exit."""
        self.set_config("cursor", model="composer-2.5[fast=false]")
        config_path = self.data / "lanes.json"
        config = json.loads(config_path.read_text())
        config["lanes"][0].update({"mode": "headless",
                                   "binary": str(FIXTURES / "fake_cursor_headless.py")})
        config_path.write_text(json.dumps(config))
        self.enable_fleet_guard()
        child_marker = self.root / "headless-child.pid"
        self.env["FAKE_CURSOR_CHILD_PID"] = str(child_marker)
        self.start_lane()
        run_id = self.assign("LANE_TEST_EXIT_WITH_CHILD")
        try:
            self.assertEqual(self.wait_run(run_id)["status"], "failed")
            self.assertTrue(process_alive(int(child_marker.read_text())))
            reservations = self.fleet_reservations()
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["run_id"], run_id)
        finally:
            if child_marker.exists():
                try:
                    os.kill(int(child_marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_live_lane_turn_keeps_shared_fleet_slot(self):
        self.set_config("cursor")
        self.enable_fleet_guard()
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT retain the running slot")
        self.wait_running(run_id)
        reservations = self.fleet_reservations()
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["run_id"], run_id)
        self.cli("lane", "abort", self.lane_name)
        self.wait_run(run_id)

    def test_steer_reaches_active_codex_turn(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT steer me")
        self.wait_running(run_id)
        self.cli("lane", "steer", self.lane_name, "--text", "write steered instruction")
        self.wait_run(run_id)
        self.assertEqual((self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text(),
                         "write steered instruction")

    def test_steer_reaches_active_pi_turn(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT steer me")
        self.wait_running(run_id)
        self.cli("lane", "steer", self.lane_name, "--text", "write steered instruction")
        self.wait_run(run_id)
        self.assertEqual((self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text(),
                         "write steered instruction")

    def test_abort_marks_lane_run_stopped(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT abort this")
        self.wait_running(run_id)
        self.cli("lane", "abort", self.lane_name)
        state = self.wait_run(run_id)
        self.assertEqual(state["status"], "stopped")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["status"], "stopped")

    def test_abort_marks_codex_lane_run_stopped(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT abort this")
        self.wait_running(run_id)
        self.cli("lane", "abort", self.lane_name)
        state = self.wait_run(run_id)
        self.assertEqual(state["status"], "stopped")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["status"], "stopped")

    def test_stop_command_aborts_lane_run_without_killing_supervisor(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT stop this")
        self.wait_running(run_id)
        supervisor_pid = self.lane_state()["supervisor_pid"]
        self.cli("stop", run_id)
        self.assertEqual(self.wait_run(run_id)["status"], "stopped")
        self.assertTrue(process_alive(supervisor_pid))

    def test_budget_interrupt_is_recorded(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT exceed the budget", "--budget-minutes", "0.02")
        state = self.wait_run(run_id)
        self.assertEqual(state["status"], "budget_exceeded")
        events = (self.data / "runs" / run_id / "events.1.codex.jsonl").read_text()
        self.assertIn("turn/completed", events)

    def test_crashed_child_fails_run_and_restarts_idle(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_CRASH")
        state = self.wait_run(run_id)
        self.assertEqual(state["status"], "failed")
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(result["attempts"][0]["error_class"], "exec_error")
        restarted = self.wait_for(lambda: (s if s.get("state") == "idle" and s.get("child_pid") else None)
                                   if (s := self.lane_state()) else None,
                                   timeout=8, description="lane child restart")
        self.assertTrue(restarted["child_pid"])

    def test_busy_lane_refuses_second_assignment_and_down_leaves_no_process(self):
        self.start_lane()
        original_head = self.git("rev-parse", "HEAD")
        first = self.assign("LANE_TEST_WAIT keep busy")
        self.wait_running(first)
        original_state = self.lane_state()
        (self.repo / "later-commit.txt").write_text("must not enter the running turn\n")
        self.git("add", "later-commit.txt")
        self.git("commit", "-q", "-m", "advance repo while lane is busy")
        second = self.cli("lane", "assign", self.lane_name, "--prompt", "should be rejected", check=False)
        self.assertNotEqual(second.returncode, 0)
        error = json.loads(second.stdout)["error"]
        self.assertIn(self.lane_name, error)
        self.assertRegex(error, r"busy|active")
        unchanged_state = self.lane_state()
        for key in ("state", "current_run", "supervisor_pid", "child_pid"):
            with self.subTest(state_key=key):
                self.assertEqual(unchanged_state.get(key), original_state.get(key))
        wt = self.data / "lanes" / self.lane_name / "wt"
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, check=True,
                                        capture_output=True, text=True).stdout.strip(), original_head)
        self.assertFalse((wt / "later-commit.txt").exists())
        supervisor_pid = self.lane_state()["supervisor_pid"]
        child_pid = self.lane_state()["child_pid"]
        self.cli("lane", "down", self.lane_name, timeout=45)
        self.wait_run(first)
        for pid in (supervisor_pid, child_pid):
            with self.subTest(pid=pid):
                self.wait_for(lambda: not process_alive(pid), timeout=5, description=f"pid {pid} to exit")

    def test_codex_false_done_without_changes_is_partial(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_NO_CHANGE report done without edits")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "partial")
        self.assertEqual(result["status"], "partial")
        self.assertIn("no_change_evidence", result.get("verify_note") or "")
        self.assertIsNone(result["commit"])

    def test_codex_read_only_assign_allows_done_without_changes(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_NO_CHANGE read-only review", "--read-only")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(result["status"], "done")
        self.assertIsNone(result["commit"])

    def test_codex_io_lost_fails_run(self):
        self.start_lane()
        run_id = self.assign("LANE_TEST_IO_LOST stale shell output")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(result["error_class"], "executor_io_lost")
        events = [json.loads(line) for line in (self.data / "runs" / run_id / "events.1.codex.jsonl").read_text().splitlines()]
        self.assertTrue(any(event.get("error") == "executor_io_lost" for event in events if event.get("type") == "error"))

    def test_codex_lane_restarts_app_server_when_binary_changes(self):
        self.start_lane()
        pid_before = self.lane_state()["child_pid"]
        alt = self.root / "fake_app_server_alt.py"
        shutil.copy(FIXTURES / "fake_app_server.py", alt)
        alt.chmod(0o755)
        config = json.loads((self.data / "lanes.json").read_text())
        config["lanes"][0]["binary"] = str(alt)
        (self.data / "lanes.json").write_text(json.dumps(config))
        run_id = self.assign("add the fixture change")
        self.wait_run(run_id)
        events = [json.loads(line) for line in (self.data / "runs" / run_id / "events.1.codex.jsonl").read_text().splitlines()]
        self.assertTrue(any(event.get("type") == "codex_app_server_restarted" for event in events))
        pid_after = self.lane_state()["child_pid"]
        self.assertNotEqual(pid_before, pid_after)


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


if __name__ == "__main__":
    unittest.main()
