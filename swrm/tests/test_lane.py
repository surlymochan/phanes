import hashlib
import json
import os
import pathlib
import signal
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "swrm"
FIXTURES = ROOT / "tests" / "fixtures"
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Lane Test", "GIT_AUTHOR_EMAIL": "lane-test@example.invalid",
                "GIT_COMMITTER_NAME": "Lane Test", "GIT_COMMITTER_EMAIL": "lane-test@example.invalid"}


class CodexSandboxTests(unittest.TestCase):
    """#1832: policy unit coverage and real, model-free Codex sandbox commits.

    Host integration command: python3 -m pytest -q swrm/tests/test_lane.py -k CodexSandbox
    B1-B4 evidence and delivery mapping are retained in the companion host checker.
    """

    def setUp(self):
        from _load import load
        self.swrm = load()
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-gitdir-", dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "config", "user.name", "Sandbox Test")
        self.git(self.repo, "config", "user.email", "sandbox@example.invalid")
        self.git(self.repo, "commit", "--allow-empty", "-qm", "base")
        self.worktrees = [self.root / "lanes" / name / "wt" for name in ("CX5", "CX10")]
        for wt in self.worktrees:
            # main requires standalone lane clones (lane_worktree_matches rejects
            # linked worktrees), so the gitdir and common dir are the same path.
            wt.parent.mkdir(parents=True)
            subprocess.run(["git", "clone", "-q", str(self.repo), str(wt)], check=True,
                           capture_output=True, env={**os.environ, **GIT_IDENTITY})
        self.policy = {"codex": {"sandbox": "workspace-write"}}
        patch = mock.patch.object(self.swrm, "load_policy", return_value=self.policy)
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True, env={**os.environ, **GIT_IDENTITY}).stdout.strip()

    def test_policy_modes_and_network_restrictions(self):
        class RecordingChild:
            def send(self, request):
                self.request = request

            def wait_response(self, request_id, timeout, event_file):
                return {"thread": {"id": "thread"}, "turn": {"id": "turn"}}

        for mode, kind in (("read-only", "readOnly"), ("danger-full-access", "dangerFullAccess"),
                           ("workspace-write", "workspaceWrite")):
            for network in ({}, {"network_access": False}, {"networkAccess": False},
                            {"network_access": True}, {"networkAccess": True},
                            {"network_access": False, "networkAccess": True}):
                with self.subTest(mode=mode, network=network):
                    self.policy["codex"] = dict(network, sandbox=mode)
                    child = RecordingChild()
                    self.swrm.lane_begin_turn(
                        {"backend": "codex", "model": "test", "effort": "high"}, child,
                        "test", str(self.worktrees[0]), None, {})
                    policy = child.request["params"]["sandboxPolicy"]
                    self.assertIsInstance(policy, dict)
                    self.assertEqual(policy["type"], kind)
                    if mode != "danger-full-access":
                        self.assertEqual(policy["networkAccess"],
                                         network.get("network_access", network.get("networkAccess", False)))
                    if mode == "workspace-write":
                        self.assertEqual(policy["writableRoots"], [
                            os.path.realpath(self.worktrees[0] / ".git")])
                    else:
                        self.assertNotIn("writableRoots", policy)

    @unittest.skipUnless(shutil.which("codex"), "real Codex binary required")
    def test_real_codex_sandbox_two_lanes_commit(self):
        """Execute git inside real app-server command/exec sandboxes, without inference.

        A negative control proves the common gitdir is protected without the fix;
        each positive run uses the same policy builder as lane_begin_turn.
        Namespace/mount failures are failures, never converted into successful skips.
        """
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            for wt in self.worktrees:
                lane = {"name": wt.parent.name, "backend": "codex", "binary": shutil.which("codex")}
                child = self.swrm.LaneChild(lane, str(wt), self.policy)
                try:
                    def execute(command, policy, request_id):
                        child.send({"jsonrpc": "2.0", "id": request_id, "method": "command/exec",
                                    "params": {"cwd": str(wt), "command": command,
                                               "sandboxPolicy": policy, "timeoutMs": 10000}})
                        try:
                            reply = child.wait_response(request_id, 15)
                        except RuntimeError as error:
                            warnings = [event.get("params", {}).get("summary", "")
                                        for event in child.backlog if event.get("method") == "configWarning"]
                            self.fail(f"real Codex sandbox RPC failed: {error}; {'; '.join(warnings)}")
                        self.assertNotIn("error", reply, reply)
                        return reply

                    gitdir = self.git(wt, "rev-parse", "--absolute-git-dir")
                    denied = execute(["touch", str(pathlib.Path(gitdir) / "sandbox-probe")],
                                     {"type": "workspaceWrite", "networkAccess": False}, "negative")
                    self.assertNotEqual(denied["exitCode"], 0, denied)
                    self.assertFalse((pathlib.Path(gitdir) / "sandbox-probe").exists())
                    before = self.git(wt, "rev-parse", "HEAD")
                    # File creation, index, shared objects/refs and commit all occur in the sandbox.
                    result = execute(["sh", "-c", "printf 'sandbox commit\\n' > result.txt && "
                                      "git add result.txt && git commit -qm sandbox-commit"],
                                     self.swrm.lane_codex_sandbox_policy(str(wt)), "commit")
                    self.assertEqual(result["exitCode"], 0, result)
                    self.assertNotEqual(self.git(wt, "rev-parse", "HEAD"), before)
                    self.assertEqual(self.git(wt, "show", "HEAD:result.txt"), "sandbox commit")
                finally:
                    child.close()

    def test_host_checker_uses_actual_lane_paths(self):
        data = self.root
        (data / "lanes.json").write_text(json.dumps({"lanes": [
            {"name": wt.parent.name, "backend": "codex", "model": "test", "effort": "high",
             "repo": str(self.repo)} for wt in self.worktrees]}))
        checker = ROOT.parent / "tools" / "check-codex-sandbox-gitdir.py"
        result = subprocess.run([sys.executable, str(checker)], capture_output=True, text=True,
                                env=dict(os.environ, OPENSWARM_HOME=str(data)), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["inventory_only"])
        self.assertEqual(len(report["lanes"]), 2)
        for wt, lane in zip(self.worktrees, report["lanes"]):
            self.assertEqual(lane["worktree"], str(wt))
            self.assertIn(self.git(wt, "rev-parse", "--absolute-git-dir"),
                          lane["sandboxPolicy"]["writableRoots"])
            self.assertFalse(lane["verified_commits"])


@unittest.skipUnless(hasattr(socket, "AF_UNIX"),
                     "lane control plane is AF_UNIX-based; Windows named-pipe transport is not implemented")
class LaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-", dir=ROOT.parent)
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
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data),
                        OPENSWARM_FLEET_GUARD_CONFIG=str(self.data / "no-fleet-guard.json"),
                        OPENSWARM_FLEET_GUARD_REQUIRED="0")
        self.env["FAKE_PI_RPC_LOG"] = str(self.root / "fake-pi-rpc.jsonl")
        self.lane_name = "L1"
        self.backend = "codex"
        self.set_config()

    def tearDown(self):
        if self.data.exists():
            try:
                self.cli("lane", "down", check=False, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.tmp.cleanup()

    def set_config(self, backend=None, model=None, fallback_models=None, thinking=None):
        if backend:
            self.backend = backend
        fixture = {"codex": "fake_app_server.py", "cursor": "fake_cursor_acp.py",
                   "agy": "fake_agy.py", "pi": "fake_pi_rpc.py",
                   "hermes": "fake_cursor_acp.py", "copilot": "fake_cursor_acp.py"}[self.backend]
        binary = FIXTURES / fixture
        if model is None:
            model = "composer-2.5" if self.backend in ("cursor", "hermes", "copilot") else (
                "gemini-3.8-flash-medium" if self.backend == "agy" else "fake-model")
        (self.data / "lanes").mkdir(parents=True, exist_ok=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": self.lane_name, "backend": self.backend, "model": model,
            **({"fallback_models": fallback_models} if fallback_models is not None else {}),
            **({"effort": "xhigh"} if self.backend == "codex" else
               ({"provider": "fake-provider"} if self.backend == "pi" else {})),
            **({"thinking": thinking} if thinking is not None else {}),
            "repo": str(self.repo), "binary": str(binary),
        }]}))
        if self.backend in ("cursor", "hermes", "copilot"):
            self.env["FAKE_CURSOR_ACP_LOG"] = str(self.root / "fake-cursor-acp.jsonl")
            self.env["FAKE_CURSOR_SESSION_DIR"] = str(self.root / "sessions")
        if self.backend == "hermes":
            self.env["HERMES_HOME"] = str(self.root / "hermes")
        if self.backend == "agy":
            self.env["FAKE_AGY_LOG"] = str(self.root / "fake-agy.jsonl")

    def git(self, *args):
        # The lane workspace is an independent clone without the repo's local identity.
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
                              env={**os.environ, **GIT_IDENTITY}).stdout.strip()

    def cli(self, *args, check=True, timeout=15):
        if self.backend == "pi":
            policy_file = self.data / "policy.local.json"
            policy = json.loads(policy_file.read_text()) if policy_file.exists() else {}
            policy.setdefault("pi", {})["require_isolation"] = False
            policy_file.write_text(json.dumps(policy))
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env, text=True,
                                capture_output=True, timeout=timeout)
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

    def lane_turn_state(self, lane_name):
        # Same source `lane status` reports as run_state: the lane's
        # state.json turn_state. Read the file directly so the queued
        # observation can be taken in the same poll as the run status.
        try:
            return json.loads((self.data / "lanes" / lane_name / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    @unittest.skipUnless(sys.platform == "darwin", "Darwin socket path fallback")
    def test_pi_lane_long_socket_path(self):
        self.data = self.root / ("long-path-" * 12)
        self.env["OPENSWARM_HOME"] = str(self.data)
        self.set_config("pi")
        original = self.data / "lanes" / self.lane_name / "ctl.sock"
        self.assertGreater(len(os.fsencode(original)), 103)
        digest = hashlib.sha256(os.fsencode(os.path.realpath(original))).hexdigest()[:32]
        short = pathlib.Path(f"/tmp/swrm-{os.getuid()}-{digest}.sock")
        self.start_lane()
        self.assertTrue(short.exists())
        self.assertTrue(json.loads(self.cli("lane", "status").stdout))
        self.cli("lane", "down", self.lane_name)
        self.wait_for(lambda: not short.exists(), description="short socket cleanup")

    @unittest.skipUnless(sys.platform == "darwin", "Darwin socket path fallback")
    def test_long_socket_path_symlink_rejects_second_supervisor(self):
        self.data = self.root / ("long-path-" * 12)
        self.env["OPENSWARM_HOME"] = str(self.data)
        self.set_config("pi")
        alias = self.root / ("alias-path-" * 11)
        alias.symlink_to(self.data, target_is_directory=True)
        original = self.data / "lanes" / self.lane_name / "ctl.sock"
        alias_socket = alias / "lanes" / self.lane_name / "ctl.sock"
        self.assertGreater(len(os.fsencode(alias_socket)), 103)
        digest = hashlib.sha256(os.fsencode(os.path.realpath(original))).hexdigest()[:32]
        short = pathlib.Path(f"/tmp/swrm-{os.getuid()}-{digest}.sock")
        self.start_lane()
        self.assertTrue(short.exists())
        alias_env = dict(self.env, OPENSWARM_HOME=str(alias))
        second = subprocess.run([sys.executable, str(CLI), "lane", "serve", self.lane_name],
                                env=alias_env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("lane supervisor is already listening", second.stderr)
        self.assertTrue(json.loads(self.cli("lane", "status").stdout))

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
        self.assertEqual(result["branch"], f"swrm/{run_id}")
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
        self.assertEqual(self.git("show", f"swrm/{run_id}:lane_result.txt"), "completed")
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
        shutil.rmtree(wt / ".git")
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

    def child_command(self, pid=None):
        """The command line of a lane process (what `ps` shows)."""
        pid = pid or self.lane_state().get("child_pid")
        self.assertTrue(pid, "the lane has no resident executor process")
        # `ps` cuts the command line at 80 columns when its output is not a terminal, so the wide
        # form is the one that shows the whole command line of a lane process.
        command = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], text=True,
                                 capture_output=True, check=False).stdout
        self.assertTrue(command.strip(), f"no process command line for pid {pid}")
        return command

    def pi_commands(self, level=None):
        """The command lines of the pi processes of this lane - what `ps` shows on the host."""
        listing = subprocess.run(["ps", "-ww", "-eo", "pid=,command="], text=True, capture_output=True,
                                 check=False).stdout
        binary = str(FIXTURES / "fake_pi_rpc.py")
        found = []
        for line in listing.splitlines():
            # The pi process is the one whose second argument is the fixture itself: the pid, the
            # interpreter and the binary are the first three fields of the line.
            fields = line.split()
            if len(fields) < 3 or fields[2] != binary:
                continue
            if level is None or f"--thinking {level}" in line:
                found.append(line.strip())
        return found

    def test_pi_lane_starts_pi_at_the_thinking_level_of_its_lane(self):
        """A lane with a thinking level starts pi with it (Refs #1847)."""
        self.set_config("pi", thinking="high")
        self.start_lane()
        self.wait_for(lambda: self.lane_state().get("child_pid"), description="the lane child to start")
        self.assertIn("--thinking high", self.child_command())

    def test_pi_run_uses_the_thinking_level_of_the_run(self):
        """A run that asks for a thinking level starts pi with it (Refs #1847)."""
        self.set_config("pi")
        self.start_lane()
        # The prompt keeps the turn open, so the process of the run stays visible.
        self.assign("LANE_TEST_WAIT pi thinking level", "--thinking", "high")
        # pi reads its thinking level once, at start-up: the level of the run is in the command
        # line of the process the run uses.
        self.wait_for(lambda: self.pi_commands("high") or None, timeout=15,
                      description="a pi process started with --thinking high")
        # The lane keeps its own level: the resident child of the lane runs without one, so the
        # review tasks of the same lane are not touched by the fix round.
        self.assertNotIn("--thinking", self.child_command())

    def test_serve_restart_resumes_the_in_flight_run(self):
        self.set_config("pi")
        self.start_lane()
        run_id = self.assign("LANE_TEST_WAIT resume this run after supervisor restart")
        self.wait_running(run_id)
        log = pathlib.Path(self.env["FAKE_PI_RPC_LOG"])
        self.wait_for(
            lambda: log.exists() and any(
                row.get("type") == "prompt" and "resume this run" in row.get("message", "")
                for row in (json.loads(line) for line in log.read_text().splitlines())
            ),
            description="the in-flight prompt to reach the executor",
        )

        supervisor_pid = self.lane_state()["supervisor_pid"]
        os.kill(supervisor_pid, signal.SIGKILL)
        # SIGKILL cannot unlink a filesystem socket; lane up must handle that
        # stale socket. Observe the dead supervisor rather than its pathname.
        self.wait_for(lambda: json.loads(self.cli("lane", "status").stdout)[0]["state"] == "crashed",
                      description="the stopped supervisor to be reported crashed")
        restarted = self.cli("lane", "up", self.lane_name)
        self.assertTrue(json.loads(restarted.stdout)["lanes"][0]["started"])

        state = self.wait_for(
            lambda: (row if (row := self.lane_state()).get("current_run") == run_id else None),
            timeout=3,
            description=f"restarted lane to resume run {run_id}",
        )
        self.assertEqual(state["state"], "busy")
        self.wait_for(
            lambda: log.exists() and any(row.get("type") == "switch_session"
                                         for row in (json.loads(line) for line in log.read_text().splitlines())),
            description="the restarted executor to restore the saved Pi session",
        )
        events = [json.loads(line) for line in (self.data / "runs" / run_id / "events.1.pi.jsonl").read_text().splitlines()]
        self.assertTrue(any(event.get("type") == "recovery" and event.get("run_id") == run_id for event in events))

    def restart_supervisor(self):
        state = self.lane_state()
        os.kill(state["supervisor_pid"], signal.SIGKILL)
        # The stale control socket is removed by lane up's ownership probe.
        self.cli("lane", "up", self.lane_name)

    def prepare_interrupted_issue(self, backend="pi"):
        self.set_config(backend)
        self.env["FAKE_APP_SERVER_LOG"] = str(self.root / "codex.jsonl")
        self.env["OPENSWARM_AD_STATE"] = str(self.root / "ad.json")
        self.env["OPENSWARM_BRAIN_LOG"] = str(self.root / "brain.log")
        gh = self.root / "gh"
        gh.write_text("#!/bin/sh\ncat " + str(self.root / "issue.json") + "\n")
        gh.chmod(0o755)
        self.env["OPENSWARM_GH_BIN"] = str(gh)
        (self.root / "issue.json").write_text(json.dumps({"state": "open", "labels": [{"name": "in-progress"}]}))
        self.start_lane()
        rid = self.assign("LANE_TEST_WAIT broker interrupted task", "--title", "#1720 recovery")
        self.wait_running(rid)
        assignment = self.data / "runs" / rid / "assignment.json"
        key = {"pi": "session_file", "codex": "thread_id", "agy": "conversation_id"}.get(backend, "session_id")
        self.wait_for(lambda: json.loads(assignment.read_text()).get(key), description="persisted session")
        (self.root / "ad.json").write_text(json.dumps({self.lane_name: {"issue": 1720, "run": rid}}))
        return rid

    def test_broker_disconnect_restart_preserves_workspace_and_rejects_duplicate_dispatch(self):
        # A short relative AF_UNIX path keeps the broker fixture inside the
        # worktree without exceeding macOS's sockaddr_un limit.
        broker_path = pathlib.Path(os.path.relpath(self.root / "broker.sock"))
        broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        broker.bind(str(broker_path))
        broker.listen(2)
        broker.settimeout(12)
        self.addCleanup(broker.close)
        # The fixture connects from the lane cwd; use a relative path from it.
        self.env["FAKE_PI_BROKER_SOCKET"] = "../../../../broker.sock"
        rid = self.prepare_interrupted_issue()
        connection, _ = broker.accept()
        self.assertEqual(connection.recv(32), b"inference\n")
        connection.close()
        log = pathlib.Path(self.env["FAKE_PI_RPC_LOG"])
        self.wait_for(lambda: '"broker_disconnected"' in log.read_text(), description="broker transport loss")
        wt = self.data / "lanes" / self.lane_name / "wt"
        (wt / "partial.txt").write_text("partial work")
        self.restart_supervisor()
        connection, _ = broker.accept()
        self.assertEqual(connection.recv(32), b"inference\n")
        connection.sendall(b"ready")
        connection.close()
        self.wait_for(lambda: self.lane_state().get("current_run") == rid, description="same run restored")
        log = pathlib.Path(self.env["FAKE_PI_RPC_LOG"])
        self.wait_for(lambda: '"switch_session"' in log.read_text(), description="session restored")
        duplicate = self.cli("lane", "assign", self.lane_name, "--prompt", "duplicate", check=False)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual((wt / "partial.txt").read_text(), "partial work")
        self.assertEqual(len(list((self.data / "runs").iterdir())), 1)
        events = [json.loads(line) for line in (self.data / "runs" / rid / "events.1.pi.jsonl").read_text().splitlines()]
        self.assertTrue(any(e.get("type") == "recovery" and e["level"] == "workspace" for e in events))
        self.assertIn(rid, (self.root / "brain.log").read_text())

    def check_pushed_restart(self, missing=False):
        rid = self.prepare_interrupted_issue()
        wt = self.data / "lanes" / self.lane_name / "wt"
        base = self.git("rev-parse", "HEAD")
        (wt / "saved.txt").write_text("pushed progress")
        self.git("-C", str(wt), "add", "saved.txt")
        self.git("-C", str(wt), "commit", "-qm", "progress")
        tip = self.git("-C", str(wt), "rev-parse", "HEAD")
        remote = self.root / "remote.git"
        self.git("init", "--bare", "-q", str(remote))
        self.git("remote", "add", "origin", str(remote))
        # The lane workspace is an independent clone: the run branch lives there,
        # not in the shared repository, so it is pushed from the workspace.
        self.git("-C", str(wt), "push", "-q", str(remote), f"HEAD:refs/heads/swrm/{rid}")
        state = self.lane_state()
        os.kill(state["supervisor_pid"], signal.SIGKILL)
        os.kill(state["child_pid"], signal.SIGKILL)
        if missing:
            shutil.rmtree(wt)
        else:
            self.git("-C", str(wt), "reset", "--hard", base)
        self.cli("lane", "up", self.lane_name)
        self.wait_for(lambda: self.lane_state().get("current_run") == rid, description="pushed run restored")
        self.assertEqual(self.git("-C", str(wt), "rev-parse", "HEAD"), tip)
        self.assertEqual((wt / "saved.txt").read_text(), "pushed progress")
        self.wait_for(lambda: (self.root / "brain.log").exists() and tip in (self.root / "brain.log").read_text(),
                      description="commit recovery brain log")

    def test_restart_restores_remote_tip_ahead_of_local_head(self):
        self.check_pushed_restart()

    def test_restart_restores_missing_workspace_from_pushed_tip(self):
        self.check_pushed_restart(missing=True)

    def test_restart_abandons_issue_reassigned_under_new_run_id(self):
        rid = self.prepare_interrupted_issue()
        (self.root / "ad.json").write_text(json.dumps({self.lane_name: {"issue": 1720, "run": rid},
                                                       "L2": {"issue": 1720, "run": "replacement"}}))
        self.restart_supervisor()
        self.wait_for(lambda: self.lane_state().get("state") == "idle", description="abandoned run yields idle lane")
        self.wait_for(lambda: self.run_state(rid).get("recovery_abandoned"), description="abandonment reason")
        self.assertIn("reassigned", self.run_state(rid)["recovery_abandoned"])
        self.assertNotIn('"switch_session"', pathlib.Path(self.env["FAKE_PI_RPC_LOG"]).read_text())
        self.assertEqual(self.wait_run(self.assign("new task"))["status"], "done")

    def check_abandoned_restart(self, condition, reason):
        rid = self.prepare_interrupted_issue()
        wt = self.data / "lanes" / self.lane_name / "wt"
        (wt / "partial.txt").write_text("unfinished work")
        if condition == "unavailable":
            (self.root / "ad.json").unlink()
        elif condition == "not-progress":
            (self.root / "issue.json").write_text(json.dumps({"state": "open", "labels": []}))
        else:
            assignment = self.data / "runs" / rid / "assignment.json"
            spec = json.loads(assignment.read_text())
            spec.pop("session_file")
            assignment.write_text(json.dumps(spec))
        self.restart_supervisor()
        self.wait_for(lambda: self.run_state(rid).get("recovery_abandoned"), description="recovery abandoned")
        self.assertIn(reason, self.run_state(rid)["recovery_abandoned"])
        self.assertNotIn('"switch_session"', pathlib.Path(self.env["FAKE_PI_RPC_LOG"]).read_text())
        self.assertIn("recovery_abandoned", (self.root / "brain.log").read_text())
        self.assertEqual(self.wait_run(self.assign("new task after abandonment"))["status"], "done")
        archive = pathlib.Path(self.run_state(rid)["preserved_workspace"])
        self.assertEqual((archive / "partial.txt").read_text(), "unfinished work")

    def test_restart_abandons_unavailable_ad_authority(self):
        self.check_abandoned_restart("unavailable", "unavailable")

    def test_restart_abandons_issue_without_in_progress(self):
        self.check_abandoned_restart("not-progress", "not explicitly in-progress")

    def test_restart_abandons_missing_pi_session(self):
        self.check_abandoned_restart("session", "session file is missing")

    def test_restart_codex_uses_saved_thread(self):
        rid = self.prepare_interrupted_issue("codex")
        self.restart_supervisor()
        log = self.root / "codex.jsonl"
        self.wait_for(lambda: '"thread/resume"' in log.read_text(), description="Codex resumes persisted thread")
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        # Count task sessions separately from the independent health sessions.
        self.assertEqual(sum(m.get("method") == "thread/start" and not str(m.get("id", "")).startswith("health-")
                             for m in messages), 1)
        resumed = next(m for m in messages if m.get("method") == "thread/resume")
        self.assertEqual(resumed["params"]["threadId"], "fake-thread")
        self.assertEqual(self.lane_state()["current_run"], rid)

    def check_acp_restart_uses_saved_session(self, backend):
        rid = self.prepare_interrupted_issue(backend)
        wt = self.data / "lanes" / self.lane_name / "wt"
        (wt / "partial.txt").write_text("unfinished work")
        self.restart_supervisor()
        log = pathlib.Path(self.env["FAKE_CURSOR_ACP_LOG"])
        self.wait_for(lambda: '"session/load"' in log.read_text(), description=f"{backend} restores saved session")
        event_file = self.data / "runs" / rid / f"events.1.{backend}.jsonl"
        self.wait_for(lambda: '"type": "recovery"' in event_file.read_text(), description="successful session recovery")
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(sum(m.get("method") == "session/new" for m in messages), 1)
        loaded = next(m for m in messages if m.get("method") == "session/load")
        self.assertEqual(loaded["params"]["sessionId"], "fake-cursor-session")
        self.assertEqual(self.lane_state()["current_run"], rid)
        self.assertEqual((wt / "partial.txt").read_text(), "unfinished work")
        duplicate = self.cli("lane", "assign", self.lane_name, "--prompt", "duplicate", check=False)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(len(list((self.data / "runs").iterdir())), 1)

    def test_restart_cursor_uses_saved_session(self):
        self.check_acp_restart_uses_saved_session("cursor")

    def test_restart_hermes_uses_saved_session(self):
        self.check_acp_restart_uses_saved_session("hermes")

    def test_restart_copilot_uses_saved_session(self):
        self.check_acp_restart_uses_saved_session("copilot")

    def test_restart_agy_uses_saved_conversation(self):
        rid = self.prepare_interrupted_issue("agy")
        self.restart_supervisor()
        log = pathlib.Path(self.env["FAKE_AGY_LOG"])
        self.wait_for(lambda: '"--conversation", "conv-1"' in log.read_text(), description="agy resumes saved conversation")
        event_file = self.data / "runs" / rid / "events.1.agy.jsonl"
        self.wait_for(lambda: '"type": "recovery"' in event_file.read_text(), description="successful agy recovery")
        self.assertEqual(self.lane_state()["current_run"], rid)
        duplicate = self.cli("lane", "assign", self.lane_name, "--prompt", "duplicate", check=False)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(len(list((self.data / "runs").iterdir())), 1)
    def test_lane_git_metadata_is_inside_executor_writable_worktree(self):
        wt = self.data / "lanes" / self.lane_name / "wt"
        wt.parent.mkdir(parents=True)
        lane_rebuild_worktree = runpy.run_path(str(CLI))["lane_rebuild_worktree"]
        lane_rebuild_worktree(str(self.repo), str(wt), self.git("rev-parse", "HEAD"))
        git_dir = pathlib.Path(self.git("-C", str(wt), "rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = wt / git_dir

        self.assertEqual(
            git_dir.resolve(),
            wt.resolve() / ".git",
            "lane worktree git metadata must be writable from the executor's sandbox; "
            "a linked worktree points .git outside its writable root",
        )

    def test_lane_history_survives_source_object_pruning(self):
        original = self.git("rev-parse", "HEAD")
        (self.repo / "temporary.txt").write_text("temporary source object\n")
        self.git("add", "temporary.txt")
        self.git("commit", "-q", "-m", "temporary source commit")
        base = self.git("rev-parse", "HEAD")
        wt = self.root / "wt"
        functions = runpy.run_path(str(CLI))
        functions["lane_rebuild_worktree"](str(self.repo), str(wt), base)
        (wt / "result.txt").write_text("in-flight result\n")
        self.git("-C", str(wt), "add", "result.txt")
        self.git("-C", str(wt), "-c", "user.name=Lane Test", "-c",
                 "user.email=lane-test@example.invalid", "commit", "-q", "-m", "lane result")
        head = self.git("-C", str(wt), "rev-parse", "HEAD")

        self.git("reset", "--hard", original)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        missing = subprocess.run(["git", "cat-file", "-e", base], cwd=self.repo, capture_output=True,
                                 env={**os.environ, **GIT_IDENTITY})
        self.assertNotEqual(missing.returncode, 0, "source must actually prune the lane's base")

        self.git("-C", str(wt), "fsck", "--full")
        self.assertEqual(self.git("-C", str(wt), "show", f"{head}:temporary.txt"), "temporary source object")
        self.assertEqual(self.git("-C", str(wt), "show", f"{head}:result.txt"), "in-flight result")
        self.assertTrue(functions["lane_worktree_matches"](str(self.repo), str(wt)))

    def test_lane_matches_only_an_independent_clone_of_configured_repo(self):
        wt = self.root / "wt"
        self.git("clone", "--shared", "-q", str(self.repo), str(wt))
        functions = runpy.run_path(str(CLI))
        self.assertFalse(functions["lane_worktree_matches"](str(self.repo), str(wt)))
        functions["lane_rebuild_worktree"](str(self.repo), str(wt), self.git("rev-parse", "HEAD"))
        self.assertTrue(functions["lane_worktree_matches"](str(self.repo), str(wt)))
        other = self.root / "other"
        self.git("clone", "-q", str(self.repo), str(other))
        self.assertFalse(functions["lane_worktree_matches"](str(other), str(wt)))

    def test_lane_clone_dissociates_source_alternates(self):
        source = self.root / "shared-source"
        self.git("clone", "--shared", "-q", str(self.repo), str(source))
        wt = self.root / "wt"
        functions = runpy.run_path(str(CLI))
        functions["lane_rebuild_worktree"](str(source), str(wt), self.git("rev-parse", "HEAD"))
        self.assertFalse((wt / ".git" / "objects" / "info" / "alternates").exists())
        shutil.rmtree(self.repo / ".git" / "objects")
        self.git("-C", str(wt), "fsck", "--full")
        self.assertEqual(self.git("-C", str(wt), "show", "HEAD:README.md"), "base")

    def test_independent_lane_fetches_new_base_without_rebuilding(self):
        with mock.patch.dict(os.environ, self.env):
            prepare = runpy.run_path(str(CLI))["lane_prepare_workspace"]
            lane = {"repo": str(self.repo), "name": self.lane_name}
            first = prepare(lane, "first")
            wt = pathlib.Path(first["root"])
            self.git("-C", str(wt), "config", "lane.test-marker", "keep")
            (self.repo / "new-base.txt").write_text("new base\n")
            self.git("add", "new-base.txt")
            self.git("commit", "-q", "-m", "advance source")

            second = prepare(lane, "second")

        self.assertEqual(second["base"], self.git("rev-parse", "HEAD"))
        self.assertEqual((wt / "new-base.txt").read_text(), "new base\n")
        self.assertEqual(self.git("-C", str(wt), "config", "lane.test-marker"), "keep")
        self.assertEqual(list(wt.parent.glob("wt.backup-*")), [])

    def test_lane_handoff_publishes_reported_commit_after_executor_checkout(self):
        with mock.patch.dict(os.environ, self.env):
            functions = runpy.run_path(str(CLI))
            lane = {"repo": str(self.repo), "name": self.lane_name, "backend": "pi", "model": "fake-model"}
            for checkout in ("original", "branch", "detached"):
                with self.subTest(checkout=checkout):
                    run_id = f"handoff-{checkout}"
                    ws = functions["lane_prepare_workspace"](lane, run_id)
                    wt = pathlib.Path(ws["root"])
                    if checkout == "branch":
                        self.git("-C", str(wt), "checkout", "-q", "-b", "executor-result")
                    elif checkout == "detached":
                        self.git("-C", str(wt), "checkout", "-q", "--detach")
                    (wt / "result.txt").write_text(f"{checkout}\n")
                    self.git("-C", str(wt), "add", "result.txt")
                    self.git("-C", str(wt), "-c", "user.name=Lane Test", "-c",
                             "user.email=lane-test@example.invalid", "commit", "-q", "-m", run_id)
                    head = self.git("-C", str(wt), "rev-parse", "HEAD")
                    missing = subprocess.run(["git", "cat-file", "-e", head], cwd=self.repo,
                                             capture_output=True, env={**os.environ, **GIT_IDENTITY})
                    self.assertNotEqual(missing.returncode, 0)
                    spec = {"run_id": run_id, "task": "deliver executor commit", "started": time.time(),
                            "budget_minutes": 1}

                    result = functions["lane_finalize_run"](
                        lane, spec, ws, {"status": "done", "files_changed": ["result.txt"]}, {}, None, 0, None)

                    self.assertEqual(result["status"], "done")
                    self.assertEqual(result["commit"], head)
                    self.assertEqual(result["branch"], ws["branch"])
                    self.assertEqual(self.git("rev-parse", ws["branch"]), head)
                    self.assertEqual(self.git("show", f"{head}:result.txt"), checkout)
                    self.assertIn(f"merge --no-ff {ws['branch']}", result["merge_hint"])
                    self.assertEqual(self.git("rev-parse", "HEAD"), ws["base"])
                    self.assertEqual(self.git("status", "--porcelain"), "")
                    self.assertEqual(self.git("-C", str(wt), "branch", "--show-current"), "")
                    ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
                    entry = next(row for row in ledger if row["id"] == run_id)
                    self.assertEqual(entry["commit"], head)
                    self.assertEqual(entry["area"], ["result.txt"])

    def test_upgrade_preserves_shared_clone_history_and_index_after_source_gc(self):
        base = self.git("rev-parse", "HEAD")
        (self.repo / "temporary.txt").write_text("borrowed object\n")
        self.git("add", "temporary.txt")
        self.git("commit", "-q", "-m", "temporary source commit")
        wt = self.root / "wt"
        self.git("clone", "--shared", "-q", str(self.repo), str(wt))
        head = self.git("-C", str(wt), "rev-parse", "HEAD")
        (wt / "README.md").write_text("staged\n")
        self.git("-C", str(wt), "add", "README.md")
        (wt / "README.md").write_text("unstaged\n")
        (wt / "untracked.txt").write_text("unfinished\n")
        functions = runpy.run_path(str(CLI))
        functions["lane_rebuild_worktree"](str(self.repo), str(wt), base)
        backup, = self.root.glob("wt.backup-*/wt")

        self.git("reset", "--hard", base)
        self.git("reflog", "expire", "--expire=now", "--all")
        self.git("gc", "--prune=now")
        self.git("-C", str(backup), "fsck", "--full")
        self.assertEqual(self.git("-C", str(backup), "show", f"{head}:temporary.txt"), "borrowed object")
        self.assertEqual(self.git("-C", str(backup), "show", ":README.md"), "staged")
        self.assertEqual((backup / "README.md").read_text(), "unstaged\n")
        self.assertEqual((backup / "untracked.txt").read_text(), "unfinished\n")
        self.assertFalse((backup / ".git" / "objects" / "info" / "alternates").exists())

    def test_rebuild_preserves_legacy_linked_worktree_changes(self):
        wt = self.root / "wt"
        self.git("worktree", "add", "-q", "-b", "legacy-lane", str(wt))
        (wt / "committed.txt").write_text("in-flight commit\n")
        self.git("-C", str(wt), "add", "committed.txt")
        self.git("-C", str(wt), "commit", "-q", "-m", "in-flight")
        head = self.git("-C", str(wt), "rev-parse", "HEAD")
        (wt / "README.md").write_text("staged\n")
        self.git("-C", str(wt), "add", "README.md")
        (wt / "README.md").write_text("unstaged\n")
        (wt / "untracked.txt").write_text("unfinished\n")
        (wt / ".gitignore").write_text("ignored.txt\n")
        (wt / "ignored.txt").write_text("ignored result\n")
        status = self.git("-C", str(wt), "status", "--porcelain")

        functions = runpy.run_path(str(CLI))
        self.assertFalse(functions["lane_worktree_matches"](str(self.repo), str(wt)))
        functions["lane_rebuild_worktree"](str(self.repo), str(wt), self.git("rev-parse", "HEAD"))

        backup, = self.root.glob("wt.backup-*/wt")
        self.assertEqual(self.git("-C", str(backup), "status", "--porcelain"), status)
        self.assertEqual(self.git("-C", str(backup), "rev-parse", "HEAD"), head)
        self.assertEqual(self.git("-C", str(backup), "show", ":README.md"), "staged")
        self.assertEqual((backup / "README.md").read_text(), "unstaged\n")
        self.assertEqual((backup / "untracked.txt").read_text(), "unfinished\n")
        self.assertEqual((backup / "ignored.txt").read_text(), "ignored result\n")
        self.assertIn(str(backup), self.git("worktree", "list", "--porcelain"))
        self.assertTrue(functions["lane_worktree_matches"](str(self.repo), str(wt)))

    def test_rebuild_refuses_to_discard_locked_legacy_worktree(self):
        wt = self.root / "wt"
        self.git("worktree", "add", "-q", "--detach", str(wt))
        self.git("worktree", "lock", str(wt))
        (wt / "untracked.txt").write_text("unfinished\n")
        rebuild = runpy.run_path(str(CLI))["lane_rebuild_worktree"]
        with self.assertRaisesRegex(RuntimeError, "cannot preserve lane worktree"):
            rebuild(str(self.repo), str(wt), self.git("rev-parse", "HEAD"))
        self.assertEqual((wt / "untracked.txt").read_text(), "unfinished\n")

    def test_failed_rebuild_preserves_existing_local_workspace(self):
        wt = self.root / "wt"
        rebuild = runpy.run_path(str(CLI))["lane_rebuild_worktree"]
        rebuild(str(self.repo), str(wt), self.git("rev-parse", "HEAD"))
        (wt / "untracked.txt").write_text("unfinished\n")
        (wt / "README.md").write_text("staged\n")
        self.git("-C", str(wt), "add", "README.md")

        with self.assertRaisesRegex(RuntimeError, "lane worktree checkout failed"):
            rebuild(str(self.repo), str(wt), "nonexistent-base")

        backup, = self.root.glob("wt.backup-*/wt")
        self.assertEqual((backup / "untracked.txt").read_text(), "unfinished\n")
        self.assertEqual(self.git("-C", str(backup), "show", ":README.md"), "staged")

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
        first = self.assign("LANE_TEST_WAIT first provider task")
        self.wait_running(first)
        second = json.loads(self.cli("lane", "assign", "L2", "--prompt", "second provider task").stdout)["id"]
        # The first turn waits 20s (LANE_TEST_WAIT), so the queue window
        # survives a slow second `lane assign` under load: with the old
        # 1s LANE_TEST_WAIT_SHORT turn, a loaded suite could return from
        # the second assign only after the first turn had finished, and
        # the queued state was never observed at all (#68 §1). Poll the
        # run status and the lane turn state in the same wait_for iteration
        # and only accept the pair while both still show queued; accepting
        # "running" here would mask a cap that never queued the turn.
        queued = self.wait_for(
            lambda: ((state, turn_state)
                     if (state := self.run_state(second)).get("queue_reason") == "provider concurrency limit"
                     and (turn_state := self.lane_turn_state("L2").get("turn_state")) == "queued"
                     else None),
            description="second provider turn to queue")
        self.assertEqual(queued[0]["queue_reason"], "provider concurrency limit")
        self.assertEqual(queued[1], "queued")
        self.assertEqual(self.wait_run(first, timeout=30)["status"], "done")
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
        first = self.assign("LANE_TEST_WAIT first total-cap task")
        self.wait_running(first)
        second = json.loads(self.cli("lane", "assign", "L2", "--prompt", "second backend task").stdout)["id"]
        # Same 20s first turn as the provider-cap test above: the old 1s
        # LANE_TEST_WAIT_SHORT turn could finish before a slow second
        # assign returned under load, so the queued state was never
        # observed (#68 §1). The assertion still requires observing the
        # queued state; "running" is never accepted as a substitute.
        queued = self.wait_for(lambda: (state if (state := self.run_state(second)).get("queue_reason") else None),
                               description="second backend turn to queue on total cap")
        self.assertEqual(queued["queue_reason"], "provider concurrency limit")
        self.assertEqual(self.wait_run(first, timeout=30)["status"], "done")
        self.assertEqual(self.wait_run(second)["status"], "done")

    def test_codex_assign_commits_and_writes_normal_result(self):
        self.start_lane()
        run_id = self.assign("add the fixture change")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertTrue(result["commit"])
        self.assertEqual(result["branch"], f"swrm/{run_id}")
        self.assertEqual(self.git("show", f"swrm/{run_id}:lane_result.txt"), "completed")

    def test_codex_two_lanes_allow_writes_to_each_worktree_gitdir(self):
        """#1832: same-named lane worktrees must grant each actual gitdir to Codex."""
        worktrees = [self.root / "lanes" / lane / "wt" for lane in ("L1", "L2")]
        for worktree in worktrees:
            subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach", "-q",
                            str(worktree), "HEAD"], check=True, capture_output=True, text=True,
                           env={**os.environ, **GIT_IDENTITY})

        class RecordingChild:
            def __init__(self):
                self.requests = []

            def send(self, request):
                self.requests.append(request)

            def wait_response(self, request_id, timeout, event_file):
                if request_id.startswith("thread-"):
                    return {"thread": {"id": "fake-thread"}}
                return {"turn": {"id": "fake-turn"}}

        from _load import load
        swrm = load()
        pathlib.Path(swrm.DATA, "policy.local.json").write_text(json.dumps(
            {"codex": {"network_access": False}}))
        child = RecordingChild()
        for worktree in worktrees:
            swrm.lane_begin_turn({"backend": "codex", "model": "fake-model", "effort": "xhigh"},
                                 child, "test", str(worktree), None, {})
        turns = [request["params"] for request in child.requests if request.get("method") == "turn/start"]
        gitdirs = {str(wt): subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
            check=True, capture_output=True, text=True,
            env={**os.environ, **GIT_IDENTITY}).stdout.strip() for wt in worktrees}
        self.assertEqual(len(set(gitdirs.values())), 2)
        for worktree, params in zip(worktrees, turns):
            sandbox = params["sandboxPolicy"]
            self.assertIsInstance(sandbox, dict)
            self.assertEqual(sandbox["type"], "workspaceWrite")
            self.assertIn(gitdirs[str(worktree)], sandbox["writableRoots"])
            self.assertFalse(sandbox["networkAccess"])

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

    def test_codex_thread_start_failure_is_exposed_as_run_diagnostic(self):
        error = ("Failed to create session: failed to load AGENTS.md instructions for environment local: "
                 "fs sandbox helper failed: bwrap: Can't mkdir /var/lib/gorelay-codex/control: Permission denied")
        self.env["FAKE_APP_SERVER_THREAD_START_ERROR"] = error
        self.start_lane()
        run_id = self.assign("reproduce <host> codex sandbox initialization failure")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(result["error_class"], "exec_error")
        self.assertIn(error, result["attempts"][0]["error_tail"] or "",
                      "thread/start failure must be visible in the run's executor diagnostic")

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
        self.assertEqual(result["branch"], f"swrm/{run_id}")
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
        self.assertEqual(self.git("show", f"swrm/{run_id}:lane_result.txt"),
                         (self.data / "lanes" / self.lane_name / "wt" / "lane_result.txt").read_text().rstrip("\n"))
        watched = self.cli("watch", run_id).stdout
        self.assertIn("Plan: Apply the requested change", watched)
        self.assertIn("CMD: Edit lane file", watched)
        self.assertIn("MSG:", watched)
        ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
        self.assertTrue(any(entry["id"] == run_id and entry["status"] == "done" for entry in ledger))

    def test_cursor_agent_commit_is_reported_with_branch_and_diffstat(self):
        self.set_config("cursor")
        git_config = self.root / "gitconfig"
        git_config.write_text("[user]\n\tuseConfigOnly = true\n")
        self.env.update(GIT_CONFIG_GLOBAL=str(git_config), GIT_CONFIG_NOSYSTEM="1")
        self.start_lane()
        # Independent lane clones do not inherit the source repo's local identity.
        # Give the committing fixture its own identity, without relying on the host.
        wt = self.data / "lanes" / self.lane_name / "wt"
        self.git("-C", str(wt), "config", "user.name", "Lane Test")
        self.git("-C", str(wt), "config", "user.email", "lane-test@example.invalid")
        run_id = self.assign("LANE_TEST_AGENT_COMMIT commit the result in the worktree")
        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(result["branch"], f"swrm/{run_id}")
        self.assertTrue(result["commit"])
        self.assertIn("lane_result.txt", result["diffstat"])
        self.assertEqual(self.git("show", f"swrm/{run_id}:lane_result.txt"),
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
        self.assertEqual(self.git("show", f"swrm/{run_id}:lane_result.txt"),
                         "completed by the fake Antigravity CLI")
        calls = [json.loads(line) for line in pathlib.Path(self.env["FAKE_AGY_LOG"]).read_text().splitlines()]
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "gemini-3.8-flash-medium")
        self.assertEqual(calls[0][calls[0].index("--output-format") + 1], "stream-json")

    def test_agy_startup_crash_restarts_once_and_succeeds(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "startup-crash-once"
        self.env["FAKE_AGY_STARTUP_CRASH_MARKER"] = str(self.root / "agy-startup-count")
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["executor_restarts"], 1)
        self.assertEqual((self.root / "agy-startup-count").read_text(), "2")
        self.assertEqual(result["backend"], "agy")

    def test_agy_repeated_startup_crash_has_distinct_error_class(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "startup-crash-always"
        self.env["FAKE_AGY_STARTUP_CRASH_MARKER"] = str(self.root / "agy-startup-count")
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["error_class"], "executor_start_failed")
        self.assertIn("startup failed before executor event", result["attempts"][-1]["error_tail"])
        self.assertEqual((self.root / "agy-startup-count").read_text(), "2")

    def test_agy_early_event_crash_restarts_and_preserves_conversation(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "early-event-crash-once"
        self.env["FAKE_AGY_STARTUP_CRASH_MARKER"] = str(self.root / "agy-startup-count")
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id)
        calls = [json.loads(line) for line in pathlib.Path(self.env["FAKE_AGY_LOG"]).read_text().splitlines()]
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["executor_restarts"], 1)
        self.assertIn("--conversation", calls[1])
        self.assertEqual(calls[1][calls[1].index("--conversation") + 1], "conv-early")

    def test_agy_early_event_repeated_crash_is_executor_start_failed(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "early-event-crash-always"
        self.env["FAKE_AGY_STARTUP_CRASH_MARKER"] = str(self.root / "agy-startup-count")
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id)
        result = json.loads((self.data / "runs" / run_id / "result.json").read_text())
        self.assertEqual(state["error_class"], "executor_start_failed")
        error_tail = result["attempts"][-1]["error_tail"]
        self.assertEqual(error_tail.count("exit code 2"), 2)
        self.assertIn("early event then startup crash", error_tail)
        self.assertEqual((self.root / "agy-startup-count").read_text(), "2")

    def test_agy_silent_exit_after_start_grace_is_start_failed(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "late-silent-crash"
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id, timeout=20)
        self.assertEqual(state["error_class"], "executor_start_failed")
        self.assertEqual(state["executor_restarts"], 1)

    def test_agy_exit_after_event_and_start_grace_is_exec_error(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "late-event-crash"
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id, timeout=20)
        self.assertEqual(state["error_class"], "exec_error")
        self.assertNotIn("executor_restarts", state)

    def test_agy_auth_failure_is_not_retried_as_startup_crash(self):
        self.set_config("agy")
        self.env["FAKE_AGY_MODE"] = "auth"
        self.start_lane()
        run_id = self.assign("add the fixture change")

        state = self.wait_run(run_id)
        calls = [json.loads(line) for line in pathlib.Path(self.env["FAKE_AGY_LOG"]).read_text().splitlines()]
        self.assertEqual(state["error_class"], "auth")
        self.assertNotIn("executor_restarts", state)
        self.assertEqual(sum(call[:1] == ["-p"] for call in calls), 1)

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
        self.assertIn("lane_result.txt", self.git("ls-tree", "--name-only", f"swrm/{run_id}"))
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
        (self.repo / "later-commit.txt").write_text("must not enter the running turn\n")
        self.git("add", "later-commit.txt")
        self.git("commit", "-q", "-m", "advance repo while lane is busy")
        second = self.cli("lane", "assign", self.lane_name, "--prompt", "should be rejected", check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("busy", json.loads(second.stdout)["error"])
        wt = self.data / "lanes" / self.lane_name / "wt"
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, check=True,
                                        capture_output=True, text=True,
                                        env={**os.environ, **GIT_IDENTITY}).stdout.strip(), original_head)
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

    def test_codex_lane_status_reports_app_server_health(self):
        self.start_lane()
        self.wait_for(lambda: self.lane_state().get("server_alive"))
        status = json.loads(self.cli("lane", "status").stdout)[0]
        self.assertIs(status.get("server_alive"), True)
        self.assertIsInstance(status.get("server_last_success"), (int, float))
        self.assertEqual(status.get("server_restart_count"), 0)

    def test_codex_app_server_probe_restarts_unresponsive_process_and_recovers(self):
        # Issue-described replay: initialize answers, then a live process drops probes.
        self.env["FAKE_APP_SERVER_IGNORE_PROBE_ONCE_FILE"] = str(self.root / "ignored-probe")
        (self.data / "policy.local.json").write_text(json.dumps({"lane_health_probe": {
            "interval_seconds": 0.1, "timeout_seconds": 0.1, "failure_threshold": 2,
            "smoke_test_timeout_seconds": 2,
        }}))
        self.start_lane()
        pid_before = self.lane_state()["child_pid"]
        recovered = self.wait_for(
            lambda: (state if (state := self.lane_state()).get("child_pid") not in (None, pid_before)
                     and state.get("server_validation_passed") is True else None),
            timeout=12, description="Codex app-server probe restart and recovery")
        self.assertTrue(recovered.get("server_alive"))
        self.assertIsInstance(recovered.get("server_last_success"), (int, float))
        self.assertNotEqual(recovered.get("state"), "paused")

    def test_codex_app_server_failed_smoke_validation_keeps_lane_paused_and_alerts(self):
        # Fail only the post-restart smoke turn; the first server ignores health probes.
        self.env["FAKE_APP_SERVER_IGNORE_PROBE_ONCE_FILE"] = str(self.root / "ignored-probe")
        self.env["FAKE_APP_SERVER_FAIL_TURNS"] = "1"
        (self.data / "policy.local.json").write_text(json.dumps({"lane_health_probe": {
            "interval_seconds": 0.1, "timeout_seconds": 0.1, "failure_threshold": 2,
            "smoke_test_timeout_seconds": 2,
        }}))
        self.start_lane()
        paused = self.wait_for(
            lambda: (state if (state := self.lane_state()).get("state") == "paused" else None),
            timeout=12, description="lane pause after failed app-server smoke validation")
        self.assertTrue(paused.get("server_health_alert"))
        self.assertFalse(paused.get("server_validation_passed", True))
        self.wait_for(lambda: f"ALERT lane {self.lane_name} paused:" in
                      (self.data / "lanes" / self.lane_name / "supervisor.log").read_text(),
                      description="active health alert delivery")
        self.assertNotEqual(self.cli("lane", "assign", self.lane_name, "--prompt", "no dispatch",
                                     check=False).returncode, 0)

    def test_codex_probe_restarts_server_hung_during_active_task(self):
        (self.data / "policy.local.json").write_text(json.dumps({"lane_health_probe": {
            "interval_seconds": 0.1, "timeout_seconds": 0.1, "failure_threshold": 2,
            "smoke_test_timeout_seconds": 2,
        }}))
        self.start_lane()
        pid_before = self.lane_state()["child_pid"]
        run_id = self.assign("LANE_TEST_HANG during an active task")
        self.assertEqual(self.wait_run(run_id)["status"], "failed")
        recovered = self.wait_for(
            lambda: (s if (s := self.lane_state()).get("server_validation_passed")
                     and s.get("state") == "idle" else None), description="active task recovery")
        self.assertNotEqual(recovered["child_pid"], pid_before)
        self.assertEqual(self.wait_run(self.assign("task after recovery"))["status"], "done")



def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False





class CodexHealthProbeTests(unittest.TestCase):
    """Replay issue #1709's live-but-unresponsive server; raw CX9 bundles unavailable."""

    def setUp(self):
        from _load import load
        self.xs = load()
        self.tmp = tempfile.TemporaryDirectory(prefix="health-", dir=ROOT.parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.lane = {"name": "L1", "backend": "codex", "model": "fake-model",
                     "binary": str(FIXTURES / "fake_app_server.py")}
        self.state = {"server_started": time.time() - 3600}
        self.policy = {"lane_health_probe": {"interval_seconds": 0.05, "timeout_seconds": 0.1,
                                            "failure_threshold": 2, "smoke_test_timeout_seconds": 2}}
        for name, value in (("lane_root", str(self.root)), ("lane_state_path", "unused")):
            patch = mock.patch.object(self.xs, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(self.xs, "jload", side_effect=lambda *args: dict(self.state))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(self.xs, "lane_write_state",
                                  side_effect=lambda name, **kw: self.state.update(kw))
        patch.start()
        self.addCleanup(patch.stop)
        self.children = []
        self.addCleanup(lambda: [child.close() for child in self.children])

    def spawn(self, *args):
        child = self.xs.LaneChild(self.lane, str(self.root), self.policy)
        self.children.append(child)
        return child

    def test_live_unresponsive_process_restarts_at_threshold_and_verifies(self):
        marker = self.root / "first-server"
        with mock.patch.dict(os.environ, {"FAKE_APP_SERVER_IGNORE_PROBE_ONCE_FILE": str(marker)}):
            child = self.spawn()
            self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
            self.assertTrue(child.alive())
            self.assertEqual(self.state["server_probe_failures"], 1)
            self.assertNotIn("server_restart_count", self.state)
            self.state["server_probe_at"] = 0
            with mock.patch.object(self.xs, "lane_child", side_effect=self.spawn):
                replacement = self.xs.lane_codex_health_probe(self.lane, child, self.policy)
            self.assertIs(replacement, self.children[1])
            self.assertFalse(child.alive())
            self.assertTrue(self.state["server_validation_passed"])
            self.assertEqual(self.state["server_restart_count"], 1)
            event = json.loads((self.root / "brain.jsonl").read_text())
            self.assertEqual(event["lane"], "L1")
            self.assertGreaterEqual(event["runtime_seconds"], 3600)
            self.assertIn("timed out", event["reason"])

    def test_discovery_answers_but_session_creation_failure_restarts(self):
        for failure in ("timeout", "error"):
            with self.subTest(failure=failure):
                self.state.clear()
                marker = self.root / ("session-" + failure)
                with mock.patch.dict(os.environ, {
                        "FAKE_APP_SERVER_SESSION_FAILURE": failure,
                        "FAKE_APP_SERVER_SESSION_FAILURE_ONCE_FILE": str(marker)}):
                    child = self.spawn()
                    # Prove discovery remains responsive on the broken process.
                    child.send({"id": "health-discovery", "method": "model/list"})
                    self.assertIn("data", child.wait_response("health-discovery", 2))
                    self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
                    self.assertEqual(self.state["server_probe_failures"], 1)
                    self.assertFalse(self.state["server_alive"])
                    self.assertNotIn("server_restart_count", self.state)
                    self.state["server_probe_at"] = 0
                    with mock.patch.object(self.xs, "lane_child", side_effect=self.spawn):
                        replacement = self.xs.lane_codex_health_probe(self.lane, child, self.policy)
                    self.assertIsNotNone(replacement)
                    replacement.close()
                    self.assertFalse(child.alive())
                    self.assertEqual(self.state["server_restart_count"], 1)
                    self.assertTrue(self.state["server_validation_passed"])

    def test_session_creation_success_does_not_restart(self):
        child = self.spawn()
        with mock.patch.object(self.xs, "lane_child") as restart:
            self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
        restart.assert_not_called()
        self.assertTrue(self.state["server_alive"])
        self.assertEqual(self.state["server_probe_failures"], 0)
        self.assertNotIn("server_restart_count", self.state)
        self.assertFalse(child.health_waiters)

    def session_stats(self, child):
        child.send({"id": "health-stats", "method": "fixture/session-stats"})
        return child.wait_response("health-stats", 2)

    def test_repeated_probes_release_unique_sessions(self):
        log = self.root / "requests.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_APP_SERVER_LOG": str(log)}):
            child = self.spawn()
        for _ in range(100):
            self.state["server_probe_at"] = 0
            self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
            self.assertTrue(self.state["server_alive"])
            self.assertEqual(self.session_stats(child)["live"], 0)
        self.assertEqual(self.session_stats(child), {"created": 100, "released": 100, "live": 0})
        requests = [json.loads(line) for line in log.read_text().splitlines()]
        ids = [req["params"]["threadId"] for req in requests
               if req.get("method") == "thread/unsubscribe"]
        self.assertEqual(len(set(ids)), 100)
        self.assertFalse(child.health_waiters)

    def test_failure_after_creation_still_releases_session(self):
        for error in (RuntimeError("probe failed"), TimeoutError("probe timed out")):
            with self.subTest(error=error):
                self.state.clear()
                child = self.spawn()

                def write_state(name, **kw):
                    if kw.get("server_alive"):
                        raise error
                    self.state.update(kw)

                with mock.patch.object(self.xs, "lane_write_state", side_effect=write_state):
                    self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
                self.assertFalse(self.state["server_alive"])
                self.assertEqual(self.state["server_probe_failures"], 1)
                self.assertEqual(self.session_stats(child), {"created": 1, "released": 1, "live": 0})

    def test_creation_failure_without_id_does_not_release(self):
        for reply in ({"error": {"message": "cannot create"}}, {"thread": {}}):
            with self.subTest(reply=reply):
                self.state.clear()
                child = mock.Mock(health_restart_reason=None)
                child.wait_response.side_effect = [{}, reply]
                self.xs.lane_codex_health_probe(self.lane, child, self.policy)
                self.assertFalse(self.state["server_alive"])
                self.assertEqual([call.args[0]["method"] for call in child.send.call_args_list],
                                 ["model/list", "thread/start"])
        for failure in ("error", "timeout"):
            with self.subTest(failure=failure):
                self.state.clear()
                with mock.patch.dict(os.environ, {"FAKE_APP_SERVER_SESSION_FAILURE": failure}):
                    child = self.spawn()
                self.xs.lane_codex_health_probe(self.lane, child, self.policy)
                self.assertFalse(self.state["server_alive"])
                self.assertEqual(self.session_stats(child), {"created": 0, "released": 0, "live": 0})

    def test_release_failure_marks_probe_unhealthy(self):
        for failure in ("error", "timeout"):
            with self.subTest(failure=failure):
                self.state.clear()
                with mock.patch.dict(os.environ, {"FAKE_APP_SERVER_RELEASE_FAILURE": failure}):
                    child = self.spawn()
                with mock.patch("sys.stderr") as stderr:
                    self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
                self.assertFalse(self.state["server_alive"])
                self.assertEqual(self.state["server_release_failures"], 1)
                self.assertEqual(self.state["server_probe_failures"], 0)
                self.assertEqual(self.state["server_probe_error"], "session release failing")
                self.assertIn("thread/unsubscribe failed", "".join(
                    call.args[0] for call in stderr.write.call_args_list))
                self.assertFalse(child.health_waiters)
                for attempt in range(49):
                    self.state["server_probe_at"] = 0
                    with mock.patch("sys.stderr"):
                        self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
                    self.assertFalse(self.state["server_alive"])
                self.assertEqual(self.state["server_release_failures"], 50)
                self.assertIn("session release failing", self.state["server_probe_error"])
                self.assertEqual(self.session_stats(child)["created"], 1)
                child.send({"id": "recover", "method": "fixture/release-recover"})
                child.wait_response("recover", 2)
                self.state["server_probe_at"] = 0
                self.xs.lane_codex_health_probe(self.lane, child, self.policy)
                self.assertEqual(self.state["server_release_failures"], 0)
                self.assertTrue(self.state["server_alive"])
                self.state["server_probe_at"] = 0
                self.xs.lane_codex_health_probe(self.lane, child, self.policy)
                self.assertEqual(self.session_stats(child), {"created": 2, "released": 2, "live": 0})

    def test_failed_validation_stays_paused_and_alerts(self):
        marker = self.root / "first-server"
        self.policy["lane_health_probe"]["failure_threshold"] = 1
        with mock.patch.dict(os.environ, {"FAKE_APP_SERVER_IGNORE_PROBE_ONCE_FILE": str(marker),
                                         "FAKE_APP_SERVER_FAIL_TURNS": "1"}):
            child = self.spawn()
            with mock.patch.object(self.xs, "lane_child", side_effect=self.spawn),                     mock.patch("sys.stderr") as stderr:
                self.assertIsNone(self.xs.lane_codex_health_probe(self.lane, child, self.policy))
            self.assertEqual(self.state["state"], "paused")
            self.assertFalse(self.state["server_validation_passed"])
            self.assertTrue(self.state["server_health_alert"])
            self.assertFalse(self.children[1].alive())
            self.assertTrue(stderr.write.called)
            events = [json.loads(line) for line in (self.root / "brain.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["type"], "codex_app_server_health_alert")

    def test_monitor_detects_active_hang_without_consuming_task_reply(self):
        child = self.spawn()
        child.send({"id": "task", "method": "turn/start", "params": {
            "threadId": "fake-thread", "input": [{"type": "text", "text": "LANE_TEST_HANG"}]}})
        self.xs.lane_start_codex_health_monitor(self.lane, child, self.policy)
        self.assertEqual(child.wait_response("task", 2)["turn"]["id"], "fake-turn")
        child.health_monitor.join(timeout=4)
        self.assertFalse(child.health_monitor.is_alive())
        self.assertIsInstance(child.health_restart_reason, str)
        self.assertFalse(child.alive())
        self.assertEqual(self.state["server_probe_failures"], 2)
        self.assertFalse(child.health_waiters)

    def test_session_probe_during_active_turn_preserves_task_response(self):
        child = self.spawn()
        child.send({"id": "task", "method": "turn/start", "params": {
            "threadId": "fake-thread", "input": [{"type": "text", "text": "LANE_TEST_WAIT"}]}})
        self.xs.lane_start_codex_health_monitor(self.lane, child, self.policy)
        self.assertEqual(child.wait_response("task", 2)["turn"]["id"], "fake-turn")
        deadline = time.monotonic() + 2
        while not self.state.get("server_last_success") and time.monotonic() < deadline:
            time.sleep(0.01)
        child.health_stop.set()
        child.health_monitor.join(timeout=2)
        self.assertFalse(child.health_monitor.is_alive())
        self.assertTrue(self.state.get("server_alive"))
        self.assertEqual(self.state["server_probe_failures"], 0)
        stats = self.session_stats(child)
        self.assertGreater(stats["created"], 0)
        self.assertEqual(stats["created"], stats["released"])
        self.assertEqual(stats["live"], 0)
        self.assertIsNone(child.health_restart_reason)
        self.assertTrue(child.alive())
        self.assertFalse(child.health_waiters)

    def test_health_fields_are_visible_and_dead_pid_overrides_cached_alive(self):
        self.state.update(supervisor_pid=12, child_pid=13, server_alive=True,
                          server_last_success=123, server_restart_count=2)
        with mock.patch.object(self.xs, "lane_config", return_value=[self.lane]),                 mock.patch.object(self.xs, "pid_alive", return_value=True),                 mock.patch.object(self.xs, "lane_state_lock"),                 mock.patch.object(self.xs, "load_policy", return_value={}):
            row = self.xs.lane_status_rows()[0]
            self.assertTrue(row["server_alive"])
            self.assertEqual(row["server_last_success"], 123)
            self.assertEqual(row["server_restart_count"], 2)
            with mock.patch.object(self.xs, "pid_alive", return_value=False):
                self.assertFalse(self.xs.lane_status_rows()[0]["server_alive"])

    def test_rpc_error_is_liveness_and_success_resets_failure_streak(self):
        child = mock.Mock()
        child.health_restart_reason = None
        child.wait_response.side_effect = [{"error": {"code": -32601}},
                                           {"thread": {"id": "probe-thread"}}, {}]
        self.state["server_probe_failures"] = 1
        self.assertIs(self.xs.lane_codex_health_probe(self.lane, child, self.policy), child)
        self.assertEqual(self.state["server_probe_failures"], 0)
        self.assertTrue(self.state["server_alive"])
        self.assertEqual([call.args[0]["method"] for call in child.send.call_args_list],
                         ["model/list", "thread/start", "thread/unsubscribe"])

    def test_smoke_rejects_unrelated_turn_completion(self):
        child = mock.Mock()
        child.health_restart_reason = "no response"
        replacement = mock.Mock()
        replacement.wait_response.side_effect = [{"thread": {"id": "smoke"}},
                                                 {"turn": {"id": "smoke-turn"}}]
        replacement.next_event.return_value = {
            "method": "turn/completed",
            "params": {"threadId": "other", "turn": {"id": "other", "status": "completed"}}}
        self.policy["lane_health_probe"]["smoke_test_timeout_seconds"] = 0.1
        with mock.patch.object(self.xs, "lane_child", return_value=replacement),                 mock.patch("sys.stderr"):
            self.assertIsNone(self.xs.lane_codex_health_probe(self.lane, child, self.policy))
        self.assertEqual(self.state["state"], "paused")
        self.assertFalse(self.state["server_validation_passed"])
        replacement.close.assert_called_once()

    def test_failed_restart_counts_attempt_and_pauses(self):
        child = mock.Mock()
        child.health_restart_reason = "no response"
        self.state["server_probe_failures"] = 2
        with mock.patch.object(self.xs, "lane_child", side_effect=RuntimeError("cannot start")),                 mock.patch("sys.stderr"):
            self.assertIsNone(self.xs.lane_codex_health_probe(self.lane, child, self.policy))
        self.assertEqual(self.state["server_restart_count"], 1)
        self.assertEqual(self.state["state"], "paused")
        self.assertTrue(self.state["server_health_alert"])


class AgyStartupRegressionTests(unittest.TestCase):
    """Drive the production turn and admission paths without a control socket."""

    def setUp(self):
        from unittest import mock
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("agy_start_regression", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.xs = importlib.util.module_from_spec(spec)
        loader.exec_module(self.xs)
        self.mock = mock
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.lane = {"name": "L1", "backend": "agy", "model": "test"}

    def run_turn(self, second_error, late=False, drained=False, early=False):
        xs, mock = self.xs, self.mock
        clock = [100.0]
        child = mock.Mock(pid=123)
        child.proc.poll.return_value = 2
        child.stderr_text.side_effect = lambda: second_error if child.start.call_count > 1 else "first startup crash"
        events = iter([None, {"type": "init", "conversation_id": "conv-1"}, None] if late else [None, None])

        def next_event(timeout):
            event = next(events)
            if late and child.start.call_count > 1 and event is None:
                clock[0] += 6
            return event

        if drained:
            events = iter([None, False, {"type": "init", "conversation_id": "conv-1"}, None])
            child.alive.return_value = False
        if early:
            events = iter([{"type": "init"}, None, {"type": "init"}, None])
            # Reaping must not turn an early EOF into a late running-turn failure.
            def reap():
                clock[0] += 6
            child.reap_after_stdout_eof.side_effect = reap
        child.next_event.side_effect = next_event
        spec = {"started": 100, "budget_minutes": 1}
        with mock.patch.object(xs, "NOW", side_effect=lambda: clock[0]), \
             mock.patch.object(xs, "set_status"), \
             mock.patch.object(xs, "load_policy", return_value={}), \
             mock.patch.object(xs, "lane_control_messages"), \
             mock.patch.object(xs, "enforced_risk", return_value=None):
            result = xs.lane_run_turn(self.lane, child, spec, "same prompt", {"path": self.tmp.name},
                                      self.tmp.name, None, {}, [])
        self.assertEqual(child.start.call_count, 2)
        return result

    def test_start_crash_then_auth_preserves_auth(self):
        result = self.run_turn("FatalAuthenticationError: authentication required")
        self.assertEqual(result[2], "auth")
        self.assertIn("authentication required", result[5])

    def test_start_crash_then_late_disconnect_is_exec_error(self):
        result = self.run_turn("later disconnect", late=True)
        self.assertEqual(result[2], "exec_error")
        self.assertIn("later disconnect", result[5])

    def test_drained_event_marks_restarted_executor_as_running(self):
        result = self.run_turn("later disconnect", late=True, drained=True)
        self.assertEqual(result[2], "exec_error")

    def test_two_start_crashes_include_both_diagnostics(self):
        result = self.run_turn("second startup crash")
        self.assertEqual(result[2], "executor_start_failed")
        self.assertEqual(result[5].count("exit code 2"), 2)
        self.assertIn("first startup crash", result[5])
        self.assertIn("second startup crash", result[5])

    def test_early_event_crash_uses_eof_time_before_reaping(self):
        result = self.run_turn("second startup crash", early=True)
        self.assertEqual(result[2], "executor_start_failed")

    def test_steered_start_retry_preserves_correction_and_conversation(self):
        xs, mock = self.xs, self.mock
        for late_original in (False, True):
            for eof in (None, False):
                with self.subTest(late_original=late_original, eof=eof):
                    clock = [100.0]
                    child = mock.Mock(pid=123)
                    child.alive.return_value = False
                    child.proc.poll.return_value = 2
                    child.stderr_text.return_value = "startup crash"
                    turn = {"conversation_id": "conv-1"}
                    events = iter(([{"type": "init"}] if late_original else []) +
                                  [None, eof] + ([False] if eof is False else []) +
                                  [{"type": "result", "status": "SUCCESS",
                                    "response": '{"status":"done"}'}])

                    def next_event(timeout):
                        if late_original and child.start.call_count == 1:
                            clock[0] = 106.0
                        return next(events)

                    def control(*args):
                        if child.start.call_count == 1:
                            turn["steer_queue"] = ["important correction"]

                    child.next_event.side_effect = next_event
                    with mock.patch.object(xs, "NOW", side_effect=lambda: clock[0]), \
                         mock.patch.object(xs, "set_status"), \
                         mock.patch.object(xs, "load_policy", return_value={}), \
                         mock.patch.object(xs, "lane_control_messages", side_effect=control), \
                         mock.patch.object(xs, "enforced_risk", return_value=None):
                        result = xs.lane_run_turn(
                            self.lane, child, {"started": 100, "budget_minutes": 1},
                            "original task", {"path": self.tmp.name}, self.tmp.name, None, turn, [])
                    self.assertEqual(child.start.call_count, 3)
                    steered, retried = child.start.call_args_list[1:]
                    self.assertIn("important correction", steered.args[0])
                    self.assertEqual(retried, steered, "retry must resend the exact steered prompt and context")
                    self.assertEqual(retried.kwargs["conversation"], "conv-1")
                    self.assertEqual(result[2:4], (None, 0))

    def test_continuation_start_retry_preserves_prompt_and_conversation(self):
        xs, mock = self.xs, self.mock
        child = mock.Mock(pid=123)
        child.proc.poll.return_value = 2
        child.stderr_text.return_value = "startup crash"
        child.next_event.side_effect = [None, {"type": "result", "status": "SUCCESS",
                                              "response": '{"status":"done"}'}]
        with mock.patch.object(xs, "NOW", return_value=100), \
             mock.patch.object(xs, "set_status"), \
             mock.patch.object(xs, "load_policy", return_value={}), \
             mock.patch.object(xs, "lane_control_messages"), \
             mock.patch.object(xs, "enforced_risk", return_value=None):
            result = xs.lane_run_turn(
                self.lane, child, {"started": 100, "budget_minutes": 1}, xs.RATE_LIMIT_CONTINUE_PROMPT,
                {"path": self.tmp.name}, self.tmp.name, None,
                {"prompt": "original task", "conversation_id": "conv-1"}, [], continuation=True)
        self.assertEqual(child.start.call_args_list, [mock.call(
            xs.RATE_LIMIT_CONTINUE_PROMPT, model="test", effort=None, conversation="conv-1")] * 2)
        self.assertEqual(result[2:4], (None, 0))

    def assert_interrupted_startup_exit(self, eof, interruption, after_restart=False):
        xs, mock = self.xs, self.mock
        clock = [100.0]
        spec = {"started": 100, "budget_minutes": 1}
        if interruption == "already_stopped":
            spec["abort_requested"] = True
        child = mock.Mock(pid=123)
        child.alive.return_value = False
        child.proc.poll.return_value = 2
        child.stderr_text.return_value = "startup crash"
        child.next_event.return_value = eof

        def control(*args):
            if child.start.call_count < (2 if after_restart else 1):
                return
            if interruption == "budget":
                clock[0] = 160.0
            else:
                spec["abort_requested"] = True

        with mock.patch.object(xs, "NOW", side_effect=lambda: clock[0]), \
             mock.patch.object(xs, "set_status") as status, \
             mock.patch.object(xs, "load_policy", return_value={}), \
             mock.patch.object(xs, "lane_control_messages", side_effect=control), \
             mock.patch.object(xs, "lane_abort_child") as abort:
            result = xs.lane_run_turn(self.lane, child, spec, "same prompt", {"path": self.tmp.name},
                                      self.tmp.name, None, {}, [])
        self.assertEqual(child.start.call_count, 2 if after_restart else 1)
        abort.assert_called_once()
        self.assertEqual(result[2:4], ("budget_exceeded", 124) if interruption == "budget" else ("stopped", 130))
        if not after_restart:
            self.assertFalse(any("executor_restarts" in call.kwargs for call in status.call_args_list))

    def test_abort_requested_does_not_retry_startup_exit(self):
        for eof in (None, False):
            with self.subTest(eof=eof):
                self.assert_interrupted_startup_exit(eof, "already_stopped")

    def test_stop_during_turn_preserves_stopped_classification(self):
        for eof in (None, False):
            for after_restart in (False, True):
                with self.subTest(eof=eof, after_restart=after_restart):
                    self.assert_interrupted_startup_exit(eof, "stop", after_restart)

    def test_budget_exhaustion_preserves_budget_classification(self):
        for eof in (None, False):
            for after_restart in (False, True):
                with self.subTest(eof=eof, after_restart=after_restart):
                    self.assert_interrupted_startup_exit(eof, "budget", after_restart)

    def test_stdout_eof_reaps_each_child_before_restart_and_diagnosis(self):
        xs, mock = self.xs, self.mock
        child = mock.Mock(pid=123)
        child.proc.poll.return_value = None
        child.stderr_text.return_value = ""
        child.next_event.side_effect = [None, None]

        def start(*args, **kwargs):
            if child.start.call_count == 2:
                self.assertEqual(child.proc.poll(), 2, "previous child is still running")
            child.proc.poll.return_value = None
            child.stderr_text.return_value = ""

        def reap():
            child.proc.poll.return_value = 2
            child.stderr_text.return_value = f"startup crash {child.start.call_count}"

        child.start.side_effect = start
        child.reap_after_stdout_eof.side_effect = reap
        with mock.patch.object(xs, "NOW", return_value=100), \
             mock.patch.object(xs, "set_status"), \
             mock.patch.object(xs, "load_policy", return_value={}), \
             mock.patch.object(xs, "lane_control_messages"):
            result = xs.lane_run_turn(self.lane, child, {"started": 100, "budget_minutes": 1},
                                      "same prompt", {"path": self.tmp.name}, self.tmp.name, None, {}, [])
        self.assertEqual(child.reap_after_stdout_eof.call_count, 2)
        self.assertEqual(result[2], "executor_start_failed")
        self.assertEqual(result[5].count("exit code 2"), 2)
        self.assertIn("startup crash 1", result[5])
        self.assertIn("startup crash 2", result[5])

    def test_reap_after_stdout_eof_waits_for_exit_and_stderr(self):
        xs = self.xs
        # Exercise the real readers: stdout EOF can precede both stderr and exit.
        child = xs.AgyChild.__new__(xs.AgyChild)
        child.events, child.stderr_lines = xs.queue.Queue(), []
        child.proc = subprocess.Popen(
            [sys.executable, "-c", "import os, sys; os.close(1); sys.stdin.readline(); "
             "sys.stderr.write('late startup diagnostic\\n'); sys.exit(2)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.addCleanup(child.close)
        self.addCleanup(child.proc.stdin.close)
        xs.threading.Thread(target=child._read_stdout, args=(child.proc, child.events), daemon=True).start()
        child.stderr_thread = xs.threading.Thread(
            target=child._read_stderr, args=(child.proc, child.stderr_lines), daemon=True)
        child.stderr_thread.start()
        self.assertIsNone(child.next_event(5))
        self.assertIsNone(child.proc.poll())
        child.proc.stdin.write("\n")
        child.proc.stdin.flush()
        child.reap_after_stdout_eof()
        self.assertEqual(child.proc.poll(), 2)
        self.assertEqual(child.stderr_text(), "late startup diagnostic\n")
        self.assertFalse(child.stderr_thread.is_alive())

    def test_reap_after_stdout_eof_terminates_child_that_does_not_exit(self):
        xs, mock = self.xs, self.mock
        child = xs.AgyChild.__new__(xs.AgyChild)
        child.proc = mock.Mock(pid=123)
        child.proc.wait.side_effect = [subprocess.TimeoutExpired("agy", 3), -9]
        child.proc.poll.return_value = None
        child.stderr_thread = mock.Mock()
        with mock.patch.object(xs, "kill_tree") as kill:
            child.reap_after_stdout_eof()
        kill.assert_called_once_with(123, grace=1)
        child.proc.kill.assert_called_once_with()
        self.assertEqual(child.proc.wait.call_args_list, [mock.call(timeout=3), mock.call(timeout=3)])
        child.stderr_thread.join.assert_called_once_with(timeout=3)
        child.proc.stderr.close.assert_called_once_with()

    def test_start_crash_releases_empty_fleet_reservation(self):
        self.assert_start_crash_releases_empty_fleet_reservation(self.lane)

    def test_cursor_headless_start_crash_releases_empty_fleet_reservation(self):
        self.assert_start_crash_releases_empty_fleet_reservation(
            {"name": "L1", "backend": "cursor", "mode": "headless", "model": "test"})

    def test_finalize_releases_ready_reservation_after_fence_verdict(self):
        xs, mock = self.xs, self.mock
        for accepted, ready in ((True, True), (False, True), (False, False)):
            with self.subTest(accepted=accepted, ready=ready):
                rid = f"finalize-{accepted}-{ready}"
                rdir = os.path.join(self.tmp.name, rid)
                os.mkdir(rdir)
                reservation = (["guard"], {"run_id": rid, "token": "a" * 32, "fence": 7})
                spec = {"run_id": rid, "task": "task", "started": 100, "budget_minutes": 1,
                        "_fleet_reservation": reservation}
                ws = {"repo": self.tmp.name, "root": self.tmp.name, "path": self.tmp.name}
                answer = ({"ok": True, "valid": True, "fence": 7} if accepted else
                          {"ok": False, "reason": "expired"})
                calls = []

                def validate(command, payload):
                    self.assertEqual((command, payload), (["guard"], {
                        "op": "validate", "run_id": rid, "token": "a" * 32, "fence": 7}))
                    calls.append("validate")
                    return (0 if accepted else 1), answer

                def release(path):
                    self.assertEqual(path, rdir)
                    self.assertEqual(calls, ["validate"])
                    result = xs.jload(os.path.join(rdir, "result.json"), {})
                    self.assertEqual(result["status"], "failed" if accepted else "fenced")
                    calls.append("release")

                with mock.patch.object(xs, "RUNS", self.tmp.name), \
                     mock.patch.object(xs, "git", return_value=(0, "", "")), \
                     mock.patch.object(xs, "finalize_workspace", return_value={}), \
                     mock.patch.object(xs, "changed_paths", return_value=[]), \
                     mock.patch.object(xs, "append_ledger"), \
                     mock.patch.object(xs, "cleanup_executor_tmp"), \
                     mock.patch.object(xs, "fleet_guard_answer", side_effect=validate) as verdict, \
                     mock.patch.object(xs, "release_fleet_slot", side_effect=release) as released:
                    xs.set_status(rdir, fleet_release_ready=ready)
                    result = xs.lane_finalize_run(
                        self.lane, spec, ws, None, {}, "executor_start_failed", 1,
                        os.path.join(rdir, "events.jsonl"))
                verdict.assert_called_once()
                if ready:
                    released.assert_called_once_with(rdir)
                else:
                    released.assert_not_called()
                if not accepted:
                    self.assertEqual(result["fence_rejected"], "expired")
                    self.assertIn("FENCE_REJECTED", pathlib.Path(rdir, "events.jsonl").read_text())

    def assert_start_crash_releases_empty_fleet_reservation(self, lane):
        xs, mock = self.xs, self.mock
        spec = {"run_id": "startup", "task": "task", "started": 100, "budget_minutes": 1}
        rdir = os.path.join(self.tmp.name, "startup")
        os.mkdir(rdir)
        child = mock.Mock()
        # The executor is alive while the turn is admitted and dead once the
        # turn reports its startup failure.
        turn_ran = []
        child.alive.side_effect = lambda: not turn_ran
        ws = {"repo": self.tmp.name, "root": self.tmp.name, "path": self.tmp.name}
        with mock.patch.object(xs, "RUNS", self.tmp.name), \
             mock.patch.object(xs, "lane_write_state"), \
             mock.patch.object(xs, "lane_prepare_workspace", return_value=ws), \
             mock.patch.object(xs, "run_workspace_prepare", return_value=None), \
             mock.patch.object(xs, "add_lessons_to_prompt", side_effect=lambda p, *a: (p, [])), \
             mock.patch.object(xs, "add_patterns_to_prompt", side_effect=lambda p, *a: (p, [])), \
             mock.patch.object(xs, "load_policy", return_value={}), \
             mock.patch.object(xs, "NOW", return_value=100), \
             mock.patch.object(xs, "set_status") as status, \
             mock.patch.object(xs, "reserve_provider_slot", return_value=True), \
             mock.patch.object(xs, "lane_run_turn", side_effect=lambda *a, **k: turn_ran.append(1) or (
                 None, {}, "executor_start_failed", 1, "events", "two crashes")) as run_turn, \
             mock.patch.object(xs, "executor_boundary_empty", return_value=True), \
             mock.patch.object(xs, "release_fleet_slot") as release, \
             mock.patch.object(xs, "lane_finalize_run") as finalize:
            xs.lane_do_run(lane, spec, child, None, None)
        run_turn.assert_called_once()
        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.args[5], "executor_start_failed")
        # The finalizer owns release, after validating the still-held fence.
        self.assertTrue(any(call.kwargs.get("fleet_release_ready") is True
                            for call in status.call_args_list))
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
