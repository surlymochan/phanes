"""`lane assign --idempotency-key`: a retry after a broken transport must not create a second run.

The key lives in the lane state, so the same key always resolves to the run the
first attempt created, even when the lane is busy with that very run.
"""
import json
import os
import pathlib
import shutil
import socket
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

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]
# Other suites patch the CLI through ``sys.modules["openswarm"]``; a second
# registration here would point those patches at this module instead.
if not hasattr(sys.modules.get("openswarm"), "lane_config"):
    sys.modules["openswarm"] = xs

CLI_BOOTSTRAP = (
    "import importlib.machinery, importlib.util;"
    "loader = importlib.machinery.SourceFileLoader('openswarm_cli', {path!r});"
    "spec = importlib.util.spec_from_loader('openswarm_cli', loader);"
    "module = importlib.util.module_from_spec(spec);"
    "loader.exec_module(module);"
    "module.main()"
)


class LaneIdempotencyTableTests(unittest.TestCase):
    """The key table itself: it keeps the most recent keys and nothing else."""

    def record(self, path, keys):
        with mock.patch.object(xs, "lane_state_path", return_value=path):
            for key in keys:
                xs.lane_idempotency_record("L1", key, f"run-{key}")
            return xs.lane_idempotency_table(xs.jload(path, {}))

    def test_table_keeps_only_the_most_recent_keys(self):
        with tempfile.TemporaryDirectory(prefix="osidem-table-") as tmp:
            path = os.path.join(tmp, "state.json")
            keys = [f"key-{index}" for index in range(xs.LANE_IDEMPOTENCY_LIMIT + 50)]
            table = self.record(path, keys)
        self.assertEqual(xs.LANE_IDEMPOTENCY_LIMIT, 200)
        self.assertEqual(len(table), xs.LANE_IDEMPOTENCY_LIMIT)
        self.assertNotIn("key-0", table)
        self.assertNotIn("key-49", table)
        self.assertIn("key-50", table)
        self.assertEqual(table["key-249"], "run-key-249")

    def test_recording_a_key_again_keeps_one_entry_and_refreshes_it(self):
        with tempfile.TemporaryDirectory(prefix="osidem-table-") as tmp:
            path = os.path.join(tmp, "state.json")
            table = self.record(path, [f"key-{index}" for index in range(10)])
            self.assertEqual(table["key-3"], "run-key-3")
            with mock.patch.object(xs, "lane_state_path", return_value=path):
                xs.lane_idempotency_record("L1", "key-3", "run-again")
                table = xs.lane_idempotency_table(xs.jload(path, {}))
        self.assertEqual(len(table), 10)
        self.assertEqual(table["key-3"], "run-again")

    def test_lookup_finds_a_recorded_key_and_ignores_an_unknown_one(self):
        with tempfile.TemporaryDirectory(prefix="osidem-table-") as tmp:
            path = os.path.join(tmp, "state.json")
            self.record(path, ["key-1"])
            with mock.patch.object(xs, "lane_state_path", return_value=path):
                self.assertEqual(xs.lane_idempotency_lookup("L1", "key-1"), "run-key-1")
                self.assertIsNone(xs.lane_idempotency_lookup("L1", "key-2"))
                self.assertIsNone(xs.lane_idempotency_lookup("L1", None))

    def test_validate_idempotency_key_rejects_an_unusable_key(self):
        self.assertIsNone(xs.validate_idempotency_key(None))
        self.assertEqual(xs.validate_idempotency_key("task-1"), "task-1")
        for bad in ("", "   ", "x" * 201, "line\nbreak"):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError):
                    xs.validate_idempotency_key(bad)


class LaneIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="osidem-")
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
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data), TMPDIR=str(self.root))
        self.lane_name = "L1"
        self.set_config()

    def tearDown(self):
        supervisor = self.lane_state().get("supervisor_pid")
        if self.data.exists():
            try:
                self.cli("lane", "down", check=False, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if supervisor:
            deadline = time.monotonic() + 10
            while self.process_alive(supervisor) and time.monotonic() < deadline:
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

    def set_config(self):
        (self.data / "lanes").mkdir(parents=True, exist_ok=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": self.lane_name, "backend": "codex", "model": "fake-model", "effort": "xhigh",
            "repo": str(self.repo), "binary": str(FIXTURES / "fake_app_server.py"),
        }]}))

    @staticmethod
    def process_alive(pid):
        try:
            os.kill(int(pid), 0)
        except (OSError, TypeError, ValueError):
            return False
        return True

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def cli(self, *args, check=True, timeout=60):
        result = subprocess.run([sys.executable, "-c", CLI_BOOTSTRAP.format(path=str(CLI)), *map(str, args)],
                                env=self.env, text=True, capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result

    def start_lane(self):
        result = self.cli("lane", "up", self.lane_name)
        self.assertTrue(json.loads(result.stdout)["lanes"][0]["state"] in ("idle", "starting"))

    def assign_payload(self, task, *extra):
        result = self.cli("lane", "assign", self.lane_name, "--prompt", task, *extra)
        return json.loads(result.stdout)

    def assign(self, task, *extra):
        return self.assign_payload(task, *extra)["id"]

    def wait_for(self, predicate, timeout=20, description="condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = predicate()
            if last:
                return last
            time.sleep(0.05)
        self.fail(f"timed out waiting for {description}; last={last!r}")

    def run_state(self, run_id):
        try:
            return json.loads((self.data / "runs" / run_id / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def wait_run(self, run_id, timeout=20):
        return self.wait_for(lambda: (s if (s := self.run_state(run_id)).get("state") == "finished" else None),
                             timeout=timeout, description=f"run {run_id} to finish")

    def run_dirs(self):
        runs = self.data / "runs"
        return sorted(path.name for path in runs.iterdir()) if runs.exists() else []

    def lane_state(self):
        try:
            return json.loads((self.data / "lanes" / self.lane_name / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def lane_request(self, payload):
        """Send one raw request to the lane control socket."""
        path = self.data / "lanes" / self.lane_name / "ctl.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(str(path))
            sock.sendall((json.dumps(payload) + "\n").encode())
            data = bytearray()
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
        return json.loads(bytes(data).split(b"\n", 1)[0].decode())

    def test_same_key_twice_creates_one_run(self):
        self.start_lane()
        first = self.assign("idempotent task", "--idempotency-key", "task-1")
        self.assertEqual(self.wait_run(first)["status"], "done")
        result = self.cli("lane", "assign", self.lane_name, "--prompt", "idempotent task",
                          "--idempotency-key", "task-1")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(payload["reused"])
        self.assertEqual(payload["run_id"], first)
        self.assertEqual(payload["id"], first)
        self.assertEqual(payload["status"], "assigned")
        self.assertEqual(payload["idempotency_key"], "task-1")
        self.assertEqual(self.run_dirs(), [first])

    def test_different_keys_create_two_runs(self):
        self.start_lane()
        first = self.assign_payload("first task", "--idempotency-key", "task-1")
        self.assertNotIn("reused", first)
        self.assertEqual(self.wait_run(first["id"])["status"], "done")
        second = self.assign_payload("second task", "--idempotency-key", "task-2")
        self.assertNotIn("reused", second)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.wait_run(second["id"])["status"], "done")
        self.assertEqual(self.run_dirs(), sorted([first["id"], second["id"]]))

    def test_same_key_while_the_lane_is_busy_returns_the_existing_run(self):
        self.start_lane()
        first = self.assign("LANE_TEST_WAIT busy lane task", "--idempotency-key", "busy-key")
        self.wait_for(lambda: (state if (state := self.lane_state()).get("state") == "busy" else None),
                      description="lane to become busy")
        result = self.cli("lane", "assign", self.lane_name, "--prompt", "LANE_TEST_WAIT busy lane task",
                          "--idempotency-key", "busy-key", check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["reused"])
        self.assertEqual(payload["run_id"], first)
        self.assertEqual(self.run_dirs(), [first])

    def test_lane_answers_a_known_key_with_the_existing_run_while_busy(self):
        self.start_lane()
        first = self.assign("LANE_TEST_WAIT supervisor reuse", "--idempotency-key", "reuse-key")
        answer = self.lane_request({"op": "assign", "run_id": "second-run-id", "task": "again",
                                    "idempotency_key": "reuse-key"})
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["reused"])
        self.assertEqual(answer["id"], first)
        self.assertEqual(self.run_dirs(), [first])

    def test_fleet_lane_assign_forwards_the_idempotency_key(self):
        self.start_lane()
        card = self.root / "card.md"
        card.write_text("fleet idempotency task\n")
        first = json.loads(self.cli("fleet", "exec", "local", "--", "lane", "assign", self.lane_name,
                                    "--task-file", str(card), "--idempotency-key", "fleet-key").stdout)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["idempotency_key"], "fleet-key")
        self.assertEqual(self.wait_run(first["run_id"])["status"], "done")
        second = json.loads(self.cli("fleet", "exec", "local", "--", "lane", "assign", self.lane_name,
                                     "--task-file", str(card), "--idempotency-key", "fleet-key").stdout)
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["reused"], second)
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(self.run_dirs(), [first["run_id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
