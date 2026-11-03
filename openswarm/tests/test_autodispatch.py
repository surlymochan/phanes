import contextlib
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
READY_TARGET = pathlib.Path(__file__).parents[2] / "tools" / "check-ad-ready-target.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)


def load_ready_target_contract():
    """Load the shared card-shape contract of tools/check-ad-ready-target.py."""
    spec = importlib.util.spec_from_file_location("ad_ready_target_contract", READY_TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

CLI = pathlib.Path(__file__).parents[1] / "bin" / "openswarm"

ENGINE = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "engine.py"
engine_spec = importlib.util.spec_from_file_location("openswarm_dispatch_engine_under_test", ENGINE)
engine = importlib.util.module_from_spec(engine_spec)
engine_spec.loader.exec_module(engine)

cli_loader = importlib.machinery.SourceFileLoader("openswarm_cli_under_test", str(CLI))
cli_spec = importlib.util.spec_from_loader("openswarm_cli_under_test", cli_loader)
cli = importlib.util.module_from_spec(cli_spec)
cli_loader.exec_module(cli)


class GitSSHCommandTests(unittest.TestCase):
    def test_ssh_command_quotes_proxy_command_as_one_option(self):
        options = ["-o", "ConnectTimeout=20", "-o", "ProxyCommand=ssh -W %h:%p bastion"]

        command = autodispatch.git_fetch_command(options, "/repo", "host:repo", "refs/heads/main", "lane")

        self.assertEqual(
            shlex.split(command[2].split("=", 1)[1]),
            ["ssh", *options],
        )
        self.assertIn("'ProxyCommand=ssh -W %h:%p bastion'", command[2])

    def test_unquoted_ssh_proxy_command_is_split_and_can_trigger_destination_usage(self):
        options = ["-o", "ProxyCommand=ssh -W %h:%p bastion"]

        broken = shlex.split("ssh " + " ".join(options))

        self.assertNotEqual(broken, ["ssh", *options])
        self.assertEqual(broken[1:3], ["-o", "ProxyCommand=ssh"])
        self.assertIn("bastion", broken[3:])

    def test_ssh_opts_injects_a_configured_proxy_command_as_one_argv_item(self):
        # The jump topology is runtime configuration, not code (docs/dual-mode-ssh.md).
        opts = autodispatch.ssh_opts("ib1", jump_hosts={"ib1": "ssh -W %h:%p bastion"})
        self.assertEqual([o for o in opts if o.startswith("ProxyCommand=")], ["ProxyCommand=ssh -W %h:%p bastion"])
        command = autodispatch.git_ssh_command(opts)
        self.assertIn("'ProxyCommand=ssh -W %h:%p bastion'", command)
        self.assertEqual(shlex.split(command), ["ssh", *opts])

    def test_ssh_opts_expands_a_bare_jump_host_into_a_non_interactive_command(self):
        opts = autodispatch.ssh_opts("gocc", jump_hosts={"gocc": "jumpbox"})
        self.assertEqual(
            [o for o in opts if o.startswith("ProxyCommand=")],
            ["ProxyCommand=ssh -o BatchMode=yes -o ConnectTimeout=20 -W %h:%p jumpbox"],
        )

    def test_ssh_opts_without_a_configured_jump_host_has_no_proxy_command(self):
        opts = autodispatch.ssh_opts("gorelay")
        self.assertFalse(any("ProxyCommand=" in opt for opt in opts))
        self.assertEqual(shlex.split(autodispatch.git_ssh_command(opts)), ["ssh", *opts])

    def test_jump_host_table_is_read_from_the_runtime_configuration(self):
        old = os.environ.get(autodispatch.JUMP_HOSTS_ENV)
        try:
            os.environ[autodispatch.JUMP_HOSTS_ENV] = json.dumps({"ib1": "ssh -W %h:%p bastion"})
            self.assertEqual(autodispatch.load_jump_hosts(), {"ib1": "ssh -W %h:%p bastion"})
            os.environ[autodispatch.JUMP_HOSTS_ENV] = "not json"
            self.assertEqual(autodispatch.load_jump_hosts(), {})
        finally:
            if old is None:
                os.environ.pop(autodispatch.JUMP_HOSTS_ENV, None)
            else:
                os.environ[autodispatch.JUMP_HOSTS_ENV] = old
    def test_autodispatch_pid_state_restarts_stale_pid_and_skips_live_pid(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"OPENSWARM_CTL_STATE_DIR": directory}):
            state_path = pathlib.Path(autodispatch.worker_state_path())
            state_path.write_text(json.dumps({"pid": exited.pid, "started_at": 1}))
            first = autodispatch.start_autodispatch(command)
            def stop_worker():
                if autodispatch.worker_alive(first["pid"], first["started_at"]):
                    os.kill(first["pid"], 15)
                    os.waitpid(first["pid"], 0)
            self.addCleanup(stop_worker)
            self.assertTrue(autodispatch.worker_alive(first["pid"], first["started_at"]))
            self.assertGreater(first["started_at"], 1)
            self.assertEqual(autodispatch.start_autodispatch(command)["pid"], first["pid"])

    def test_autodispatch_clears_nonpositive_pid_and_starts_worker(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"OPENSWARM_CTL_STATE_DIR": directory}):
            state_path = pathlib.Path(autodispatch.worker_state_path())
            for invalid_pid in (0, -1):
                state_path.write_text(json.dumps({"pid": invalid_pid, "started_at": 1}))
                state = autodispatch.start_autodispatch(command)
                self.assertGreater(state["pid"], 1)
                self.assertGreater(state["started_at"], 1)
                os.kill(state["pid"], 15)
                os.waitpid(state["pid"], 0)

    def test_autodispatch_rejects_reused_pid_with_different_start_time(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        live = subprocess.Popen(command)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, {"OPENSWARM_CTL_STATE_DIR": directory}):
            state_path = pathlib.Path(autodispatch.worker_state_path())
            state_path.write_text(json.dumps({"pid": live.pid, "started_at": 1}))
            state = autodispatch.start_autodispatch(command)
            self.assertNotEqual(state["pid"], live.pid)
            os.kill(state["pid"], 15)
            os.waitpid(state["pid"], 0)
        live.terminate()
        live.wait()


class HarvestFetchBackoffTests(unittest.TestCase):
    """Refs #947: a broken harvest fetch is transient until the threshold is exceeded."""

    def test_field_transfer_errors_are_transient(self):
        for error in (
            "fetch-pack: unexpected disconnect while reading sideband packet",
            "fatal: bad pack header at offset 0",
            "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly",
            "ssh: connect to host instabox port 22: Connection reset by peer",
        ):
            self.assertEqual(autodispatch.classify_harvest_fetch_error(error), "transient", error)
            self.assertTrue(autodispatch.should_retry_harvest_fetch(error, 1), error)

    def test_other_errors_are_fatal_and_not_retried(self):
        error = "fatal: could not read from remote repository"

        self.assertEqual(autodispatch.classify_harvest_fetch_error(error), "fatal")
        self.assertFalse(autodispatch.should_retry_harvest_fetch(error, 1))

    def test_backoff_is_exponential_and_capped(self):
        ladder = [autodispatch.harvest_fetch_backoff_seconds(n) for n in range(1, 6)]

        self.assertEqual(ladder, [30, 60, 120, 240, 480])
        self.assertEqual(autodispatch.harvest_fetch_backoff_seconds(0), 0)
        self.assertEqual(autodispatch.harvest_fetch_backoff_seconds(9), 600)

    def test_transient_failures_up_to_the_threshold_stay_transient(self):
        threshold = autodispatch.HARVEST_FETCH_MAX_TRANSIENT_FAILURES

        self.assertEqual(autodispatch.harvest_fetch_outcome(threshold - 1, "transient"), "transient")
        self.assertEqual(autodispatch.harvest_fetch_outcome(threshold, "transient"), "transient")
        self.assertEqual(autodispatch.harvest_fetch_outcome(threshold + 1, "transient"), "lane-failed")
        self.assertEqual(autodispatch.harvest_fetch_outcome(1, "fatal"), "lane-failed")

    def test_lane_backoff_gate_uses_the_stored_deadline(self):
        state = {"fetch_backoff_until": autodispatch.time.time() + 600}

        self.assertTrue(autodispatch.lane_in_fetch_backoff(state))
        self.assertFalse(autodispatch.lane_in_fetch_backoff({"fetch_backoff_until": 1}))
        self.assertFalse(autodispatch.lane_in_fetch_backoff({}))


class PauseScopeTests(unittest.TestCase):
    """Refs #1366: the AD reads the openswarm pause store and the legacy PAUSE markers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-ad-pause-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        self.store = self.home / "pauses"
        self.store.mkdir(parents=True)
        self.markers = self.root / "autodispatch"
        self.markers.mkdir()
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        marker_dir = mock.patch.object(autodispatch, "D", str(self.markers))
        marker_dir.start()
        self.addCleanup(marker_dir.stop)

    def write_pause(self, scope, expires_at=None, reason=""):
        record = {"scope": scope, "created_at": time.time(), "expires_at": expires_at, "reason": reason}
        (self.store / (scope.replace(":", "_") + ".json")).write_text(json.dumps(record))

    def test_expired_pause_is_no_pause(self):
        self.write_pause("lane:CX5", expires_at=time.time() - 1)

        self.assertEqual(autodispatch.paused_scopes(), set())

        self.write_pause("lane:CX5", expires_at=time.time() + 600)

        self.assertEqual(autodispatch.paused_scopes(), {"lane:CX5"})

    def test_scope_matching_covers_lane_host_and_backend(self):
        self.write_pause("lane:CX5")
        self.write_pause("host:ib1")
        self.write_pause("backend:pi")
        scopes = autodispatch.paused_scopes()

        self.assertTrue(autodispatch.lane_paused("CX5", {"host": "ib2", "backend": "codex"}, scopes))
        self.assertTrue(autodispatch.lane_paused("M1", {"host": "ib1", "backend": "codex"}, scopes))
        self.assertTrue(autodispatch.lane_paused("M1", {"host": "ib2", "backend": "pi"}, scopes))
        self.assertFalse(autodispatch.lane_paused("M1", {"host": "ib2", "backend": "codex"}, scopes))

    def test_the_all_scope_pauses_every_lane_and_the_loop(self):
        self.write_pause("all")
        scopes = autodispatch.paused_scopes()

        self.assertTrue(autodispatch.lane_paused("M1", {"host": "ib2", "backend": "codex"}, scopes))
        self.assertEqual(autodispatch.tick({}, {}, 0), 0)

    def test_legacy_markers_are_read_next_to_the_store(self):
        for name in ("PAUSE", "PAUSE-MINT", "PAUSE-M1", "PAUSE-HOST-ib1", "PAUSE-BACKEND-pi"):
            (self.markers / name).touch()
        scopes = autodispatch.paused_scopes()

        self.assertEqual(scopes, {"all", "mint", "lane:M1", "host:ib1", "backend:pi"})
        self.assertTrue(autodispatch.lane_paused("M1", {}, scopes))
        self.assertTrue(autodispatch.mint_paused(scopes))
        self.assertFalse(autodispatch.mint_paused(set()))

    def test_pause_records_list_the_scope_the_reason_and_the_source(self):
        self.write_pause("lane:CX5", expires_at=time.time() + 600, reason="drain")
        (self.markers / "PAUSE-MINT").touch()

        rows = autodispatch.pause_records()

        self.assertEqual([row["scope"] for row in rows], ["lane:CX5", "mint"])
        self.assertEqual(rows[0]["reason"], "drain")
        self.assertEqual(rows[0]["source"], "store")
        self.assertEqual(rows[1]["source"], "marker")
        self.assertEqual(rows[1]["reason"], "legacy marker: PAUSE-MINT")


class PauseCliListTests(unittest.TestCase):
    """Refs #1366: `openswarm pause <scope>` and `openswarm pauses` list the pause records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.home))

    def run_cli(self, *args):
        return subprocess.run([str(CLI), *args], env=self.env, text=True, capture_output=True)

    def test_pause_and_pauses_list_the_active_scopes(self):
        self.assertEqual(self.run_cli("pause", "lane:CX5", "--ttl", "30m", "--reason", "drain").returncode, 0)

        rows = json.loads(self.run_cli("pauses", "--json").stdout)

        self.assertEqual([row["scope"] for row in rows], ["lane:CX5"])
        self.assertEqual(rows[0]["reason"], "drain")
        self.assertGreater(rows[0]["remaining_ttl_s"], 0)
        self.assertIn("lane:CX5", self.run_cli("pauses").stdout)

    def test_pause_without_a_ttl_never_expires(self):
        self.assertEqual(self.run_cli("pause", "all", "--reason", "maintenance").returncode, 0)

        rows = json.loads(self.run_cli("pauses", "--json").stdout)

        self.assertEqual(rows[0]["expires_at"], None)
        self.assertEqual(rows[0]["remaining_ttl_s"], None)

    def test_resume_removes_the_scope(self):
        self.assertEqual(self.run_cli("pause", "lane:CX5").returncode, 0)
        self.assertEqual(self.run_cli("resume", "lane:CX5").returncode, 0)
        self.assertEqual(json.loads(self.run_cli("pauses", "--json").stdout), [])
        self.assertEqual(self.run_cli("resume", "lane:CX5").returncode, 1)

    def test_an_invalid_scope_exits_two(self):
        self.assertEqual(self.run_cli("pause", "bogus").returncode, 2)
        self.assertEqual(self.run_cli("resume", "bogus").returncode, 2)


class PauseTickRunner:
    """Answers the GitHub and git commands of one dispatch tick and records every call."""

    def __init__(self, issues=()):
        self.issues = list(issues)
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        if "issue list" in text and "lane-ready" in text:
            return 0, json.dumps(self.issues), ""
        if "issue list" in text:
            return 0, "[]", ""
        if "issue view" in text:
            return 0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-ready"}]}), ""
        if "ls-remote" in text:
            return 0, "b" * 40 + "\n", ""
        if "lane assign" in text:
            return 0, '{"id": "run-1"}', ""
        return 0, "", ""

    def called(self, needle):
        return [call for call in self.calls if needle in call]


class PauseDispatchTickTests(unittest.TestCase):
    """Refs #1366: the dispatch tick honours the pause store of the openswarm home.

    The tick is the real one: only the ssh probe of the lanes is stubbed, so the
    pause decision, the assignment and the replenish hook are the shipped ones.
    """

    LANE = {"L1": {"host": "h1", "backend": "pi"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-tick-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "repo").mkdir()
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def write_pause(self, scope, expires_at=None, reason=""):
        store = self.home / "pauses"
        store.mkdir(parents=True, exist_ok=True)
        record = {"scope": scope, "created_at": time.time(), "expires_at": expires_at, "reason": reason}
        (store / (scope.replace(":", "_") + ".json")).write_text(json.dumps(record))

    def clear_pauses(self):
        store = self.home / "pauses"
        for path in store.glob("*.json") if store.exists() else []:
            path.unlink()

    def dispatcher(self, runner):
        cfg = {"repo": "acme/project", "local_repo": str(self.root / "repo"),
               "state_dir": str(self.root / "state"), "remote_home": str(self.root),
               "ssh": {"control_dir": str(self.root / "cm")}, "local_hosts": [],
               "hot_files": {"limits": {}, "union": []},
               "replenish": {"cmd": ["mint"], "low": 2, "target": 3}}
        path = self.root / "cfg.json"
        path.write_text(json.dumps(cfg))
        d = engine.Dispatcher(engine.load_config(str(path)), runner=runner)
        d.probe = lambda lanes, state: {name: {"lane": "idle"} for name in lanes}
        return d

    def tick(self, runner):
        d = self.dispatcher(runner)
        state = {}
        d.tick(dict(self.LANE), state)
        return state

    def runner(self):
        return PauseTickRunner([{"number": 7, "title": "card 7", "body": "", "labels": [{"name": "lane-ready"}]}])

    def test_without_a_pause_the_tick_assigns_the_issue(self):
        runner = self.runner()

        state = self.tick(runner)

        self.assertEqual(state["L1"]["issue"], 7)
        self.assertTrue(runner.called("lane assign"))

    def test_a_lane_pause_stops_the_assignment_and_a_resume_lets_it_through(self):
        self.write_pause("lane:L1")
        runner = self.runner()

        state = self.tick(runner)

        self.assertNotIn("L1", state)
        self.assertFalse(runner.called("lane assign"))

        self.clear_pauses()
        runner = self.runner()

        state = self.tick(runner)

        self.assertEqual(state["L1"]["issue"], 7)

    def test_an_expired_lane_pause_does_not_stop_the_assignment(self):
        self.write_pause("lane:L1", expires_at=time.time() - 1)
        runner = self.runner()

        state = self.tick(runner)

        self.assertEqual(state["L1"]["issue"], 7)

    def test_a_host_pause_stops_the_assignment(self):
        self.write_pause("host:h1")
        runner = self.runner()

        state = self.tick(runner)

        self.assertNotIn("L1", state)
        self.assertFalse(runner.called("lane assign"))

    def test_a_backend_pause_stops_the_assignment(self):
        self.write_pause("backend:pi")
        runner = self.runner()

        state = self.tick(runner)

        self.assertNotIn("L1", state)
        self.assertFalse(runner.called("lane assign"))

    def test_the_all_pause_stops_the_whole_tick(self):
        self.write_pause("all")
        runner = self.runner()

        state = self.tick(runner)

        self.assertNotIn("L1", state)
        self.assertFalse(runner.called("lane assign"))
        self.assertFalse(runner.called("mint"))

    def test_the_mint_pause_stops_the_minting_but_not_the_assignment(self):
        self.write_pause("mint")
        runner = self.runner()

        state = self.tick(runner)

        self.assertEqual(state["L1"]["issue"], 7)
        self.assertFalse(runner.called("mint"))

        self.clear_pauses()
        runner = self.runner()

        self.tick(runner)

        self.assertTrue(runner.called("mint"))

    def test_an_expired_mint_pause_lets_the_minting_through(self):
        self.write_pause("mint", expires_at=time.time() - 1)
        runner = self.runner()

        self.tick(runner)

        self.assertTrue(runner.called("mint"))

    def test_the_default_home_is_read_when_openswarm_home_is_unset(self):
        """The CLI writes into ``~/.claude/openswarm``; the dispatcher must read that home."""
        home = self.root / "fake-home"
        home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(home), "OPENSWARM_HOME": "",
                                                   "XSWARM_HOME": ""})
        environment.start()
        self.addCleanup(environment.stop)
        result = subprocess.run([str(CLI), "pause", "lane:L1"], env=dict(os.environ), text=True,
                                capture_output=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / ".claude" / "openswarm" / "pauses" / "lane_L1.json").exists())
        runner = self.runner()

        state = self.tick(runner)

        self.assertNotIn("L1", state)
        self.assertFalse(runner.called("lane assign"))


class LaneControlSocket:
    """A stand-in for the listening control socket of the lane serve.

    ``select()`` sees the pending request through ``fileno()`` and ``accept()``
    hands the connection over, exactly like the AF_UNIX server of the lane serve.
    """

    def __init__(self, connection):
        self.connection = connection

    def fileno(self):
        return self.connection.fileno()

    def accept(self):
        return self.connection, None


class PauseLaneServeAssignTests(unittest.TestCase):
    """Refs #1366: the lane serve refuses an assign while its scope is paused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lane-pause-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "repo").mkdir()
        state = self.root / "data" / "lanes" / "L1"
        state.mkdir(parents=True)
        (state / "state.json").write_text(json.dumps({"state": "idle"}))
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        # The dispatch configuration names the host of every lane it drives.
        dispatch = self.root / "dispatch-lanes.json"
        dispatch.write_text(json.dumps({"lanes": {"L1": {"host": "ib1", "backend": "codex"}}}))
        lanes_file = mock.patch.dict(os.environ, {"OPENSWARM_DISPATCH_LANES_FILE": str(dispatch)})
        lanes_file.start()
        self.addCleanup(lanes_file.stop)
        # The lane configuration itself names no host: the lane serve has to resolve it.
        self.lane = {"name": "L1", "backend": "codex", "model": "fake-model", "repo": str(self.root / "repo")}

    def write_pause(self, scope, expires_at=None, reason=""):
        store = self.home / "pauses"
        store.mkdir(parents=True, exist_ok=True)
        record = {"scope": scope, "created_at": time.time(), "expires_at": expires_at, "reason": reason}
        (store / (scope.replace(":", "_") + ".json")).write_text(json.dumps(record))

    def clear_pauses(self):
        store = self.home / "pauses"
        for path in store.glob("*.json") if store.exists() else []:
            path.unlink()

    def assign(self, request=None):
        """Send one assign request over the lane control socket and return the answer."""
        client, connection = socket.socketpair()
        payload = request or {"op": "assign", "run_id": "r1", "task": "do it"}
        client.sendall((json.dumps(payload) + "\n").encode())
        try:
            with contextlib.ExitStack() as stack:
                for name, value in (("DATA", str(self.root / "data")), ("RUNS", str(self.root / "runs"))):
                    stack.enter_context(mock.patch.object(cli, name, value))
                for name in ("validate_card_type", "admit_budget", "execution_provider", "lane_write_state",
                             "set_status", "lane_idempotency_record", "concurrency_keys"):
                    stack.enter_context(mock.patch.object(cli, name, lambda *args, **kwargs: None))
                stack.enter_context(mock.patch.object(cli, "load_policy",
                                                     return_value={"run": {"timeout_seconds": 2700}}))
                stack.enter_context(mock.patch.object(cli, "lane_drain_check", return_value=None))
                cli.lane_control_messages(LaneControlSocket(connection), self.lane, {}, None, {}, [])
            client.settimeout(10)
            answer = json.loads(client.recv(65536).decode().splitlines()[0])
        finally:
            client.close()
            connection.close()
        return answer

    def test_without_a_pause_the_lane_takes_the_work(self):
        answer = self.assign()

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["id"], "r1")

    def test_a_lane_pause_refuses_the_assign_and_a_resume_lets_it_through(self):
        self.write_pause("lane:L1")

        answer = self.assign()

        self.assertFalse(answer["ok"])
        self.assertTrue(answer["paused"])
        self.assertIn("is paused", answer["error"])

        self.clear_pauses()

        answer = self.assign()

        self.assertTrue(answer["ok"], answer)

    def test_a_host_pause_refuses_the_assign_although_the_lane_names_no_host(self):
        self.write_pause("host:ib1")

        answer = self.assign()

        self.assertFalse(answer["ok"])
        self.assertTrue(answer["paused"])

    def test_a_backend_pause_refuses_the_assign(self):
        self.write_pause("backend:codex")

        answer = self.assign()

        self.assertFalse(answer["ok"])
        self.assertTrue(answer["paused"])

    def test_the_all_pause_refuses_the_assign(self):
        self.write_pause("all")

        answer = self.assign()

        self.assertFalse(answer["ok"])
        self.assertTrue(answer["paused"])

    def test_an_expired_pause_does_not_refuse_the_assign(self):
        self.write_pause("lane:L1", expires_at=time.time() - 1)

        answer = self.assign()

        self.assertTrue(answer["ok"], answer)

    def test_the_lane_serve_resolves_its_host_from_the_dispatch_configuration(self):
        self.assertEqual(cli.lane_host_identity(self.lane), "ib1")
        self.assertEqual(cli.lane_host_identity({"name": "L1", "host": "explicit"}), "explicit")


class ReplenishTriggerTests(unittest.TestCase):
    """Refs #1662: a pool that runs dry is refilled, and every skipped card leaves a trace.

    The 2026-11-02 19:00 incident: the pool fell from 14 to 5, ``autodispatch.log`` got
    no mint record, and the 19:45 watchdog round found no usable card among the 21 cards
    of the local backlog.  The refill used to give up on the whole batch as soon as one
    card was unusable, it refilled only up to the low-water mark, and a failed queue read
    left no trace at all.
    """

    THICK = ("# Local card\nlabels: p1\nImplement the bounded refill path.\n"
             "验收：python3 tools/check-ready-refill-contract.py\n")
    THIN = "# Thin card\nlabels: p1\n"

    def patch(self, name, value):
        """Replace a module attribute for the duration of one test."""
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        self.patch("LAST_MINT_LOG", 0.0)
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        autodispatch.MINT_EVIDENCE.clear()
        self.filed = []
        self.patch("file_issue", lambda text, extra=(): self.filed.append(text.splitlines()[0]) or "#501")
        self.patch("gh", self.fake_gh)
        self.patch("sh", lambda *args, **kwargs: (0, "", ""))

    def fake_gh(self, args, timeout=60):
        """A planner issue is already open, so no second planner card may be minted."""
        if args[:2] == ["issue", "list"] and "planner" in args:
            return 0, json.dumps([{"number": 42}]), ""
        return 0, json.dumps([]), ""

    def log_text(self):
        return (self.dir / "autodispatch.log").read_text(encoding="utf-8")

    def backlog(self, **cards):
        backlog = self.dir / "backlog"
        backlog.mkdir(exist_ok=True)
        for name, text in cards.items():
            (backlog / f"{name}.md").write_text(text, encoding="utf-8")
        return backlog

    def test_the_pool_is_topped_up_to_the_target_below_the_low_water_mark(self):
        self.assertEqual(autodispatch.refill_need(5), autodispatch.TARGET - 5)
        self.assertEqual(autodispatch.refill_need(autodispatch.LOW), 0)
        self.assertEqual(autodispatch.refill_need(autodispatch.LOW, starved=3), 3)
        self.assertEqual(autodispatch.refill_need(5, starved=25), 25)
        self.assertEqual(autodispatch.refill_need(autodispatch.TARGET + 5), 0)

    def test_an_unusable_card_does_not_block_the_rest_of_the_batch(self):
        backlog = self.backlog(a_thin=self.THIN, b_good=self.THICK)

        filed = autodispatch.replenish(5, {})

        self.assertEqual(filed, ["#501"])
        self.assertEqual(self.filed, ["# Local card"])
        self.assertTrue((backlog / "rejected" / "a_thin.md").is_file())
        self.assertTrue((backlog / "filed" / "b_good.md").is_file())
        self.assertIn("backlog a_thin.md", self.log_text())
        self.assertIn("thin card", self.log_text())

    def test_a_failing_card_does_not_block_the_rest_of_the_batch(self):
        broken = self.THICK.replace("# Local card", "# Broken card")
        self.backlog(a_broken=broken, b_good=self.THICK)

        def file_issue(text, extra=()):
            if text.startswith("# Broken card"):
                raise RuntimeError("gh: could not add label: 'nope' not found")
            return "#502"

        self.patch("file_issue", file_issue)

        filed = autodispatch.replenish(5, {})

        self.assertEqual(filed, ["#502"])
        self.assertIn("backlog a_broken.md", self.log_text())

    def test_the_mint_pause_marker_skips_the_mint_and_says_why(self):
        self.backlog(a_good=self.THICK)
        (self.dir / "PAUSE-MINT").write_text("", encoding="utf-8")

        self.assertEqual(autodispatch.replenish(5, {}), [])
        self.assertIn("replenish skipped", self.log_text())
        self.assertIn("PAUSE-MINT", self.log_text())

    def test_an_open_planner_issue_is_logged_and_blocks_a_second_planner_card(self):
        self.assertEqual(autodispatch.replenish(5, {}), [])
        self.assertIn("planner issue already open (#42)", self.log_text())
        self.assertEqual(self.filed, [])

    def test_the_tick_refills_the_pool_when_it_drops_below_the_threshold(self):
        needs = []
        self.patch("queue", lambda: [])
        self.patch("replenish", lambda k, state: needs.append(k) or [])

        autodispatch.tick({}, {}, 0)

        self.assertEqual(needs, [autodispatch.TARGET])
        self.assertIn("WARNING queue low", self.log_text())
        self.assertIn(f"need {autodispatch.TARGET} card(s)", self.log_text())

    def test_the_card_rules_agree_with_the_shared_thin_card_contract(self):
        contract = load_ready_target_contract()
        samples = [
            "",
            "# Thin card\nlabels: p1\n",
            "No title\nlabels: p1\nImplement the path.\n验收：python3 check.py\n",
            "# Title\nNo labels line\nImplement the path.\n验收：python3 check.py\n",
            "# Title\nlabels: p1\nImplement the path.\nNo acceptance.\n",
            "# Title\nlabels: p1\n验收：python3 check.py\n",
            "# Title\nlabels: p1\nImplement the bounded refill path.\n验收：python3 check.py\n",
        ]
        for text in samples:
            self.assertEqual(autodispatch.card_defect(text) is None, not contract.is_thin_card(text), text)


class ReplenishUnreadableQueueTests(unittest.TestCase):
    """Refs #1662: an unreadable queue must not mint a new batch on every tick.

    The pool level is unknown while the queue read fails.  The AD mints against the last
    known level (plus the cards it minted since) and bounds the number of consecutive
    retries; past the bound it waits for an authoritative read.
    """

    def patch(self, name, value):
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.needs = []
        self.patch("replenish", lambda k, state: self.needs.append(k) or ["#501"])

        def broken_queue():
            raise RuntimeError("gh: API rate limit exceeded")

        self.patch("queue", broken_queue)

    def log_text(self):
        return (self.dir / "autodispatch.log").read_text(encoding="utf-8")

    def test_a_persistent_queue_failure_mints_within_a_bound_and_then_waits(self):
        state = {"_ready": 5}

        for _ in range(autodispatch.QUEUE_UNREADABLE_REFILL_MAX + 2):
            autodispatch.tick({}, state, 0)

        # the retries are bounded, and every minted card raises the estimated pool level
        self.assertEqual(len(self.needs), autodispatch.QUEUE_UNREADABLE_REFILL_MAX)
        self.assertEqual(self.needs[0], autodispatch.TARGET - 5)
        self.assertEqual(self.needs, sorted(self.needs, reverse=True))
        self.assertIn("bounded retries exhausted", self.log_text())
        self.assertEqual(state["_queue_failures"], autodispatch.QUEUE_UNREADABLE_REFILL_MAX + 2)

    def test_the_minted_cards_are_counted_into_the_pool_level(self):
        state = {"_ready": 5}

        autodispatch.tick({}, state, 0)

        self.assertEqual(state["_minted"], 1)
        self.assertEqual(autodispatch.pool_level(state), 6)

    def test_an_unknown_pool_level_mints_nothing(self):
        state = {}

        autodispatch.tick({}, state, 0)

        self.assertEqual(self.needs, [])
        self.assertIn("pool level unknown", self.log_text())

    def test_a_readable_queue_resets_the_failure_counter_and_the_level(self):
        state = {"_ready": 5}
        autodispatch.tick({}, state, 0)

        self.patch("queue", lambda: [])
        autodispatch.tick({}, state, 0)

        self.assertEqual(state["_queue_failures"], 0)
        self.assertEqual(state["_ready"], 0)
        self.assertIn("queue error", self.log_text())


class QuarantineKeepsBothCardsTests(unittest.TestCase):
    """Refs #1662: a quarantined card never overwrites an earlier card of the same name."""

    def patch(self, name, value):
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch("file_issue", lambda text, extra=(): "#501")
        self.patch("gh", lambda args, timeout=60: (0, json.dumps([{"number": 42}]), ""))
        self.patch("sh", lambda *args, **kwargs: (0, "", ""))

    def test_move_without_overwrite_preserves_both_card_contents(self):
        filed = self.dir / "filed"
        filed.mkdir()
        original = filed / "card.md"
        original.write_text("earlier card")
        source = self.dir / "incoming.md"
        source.write_text("new card")

        moved = pathlib.Path(autodispatch.move_without_overwrite(str(source), str(original)))

        self.assertEqual(original.read_text(), "earlier card")
        self.assertEqual(moved.read_text(), "new card")
        self.assertNotEqual(moved, original)

    def test_the_quarantine_keeps_an_earlier_card_of_the_same_name(self):
        backlog = self.dir / "backlog"
        backlog.mkdir()
        (backlog / "a_thin.md").write_text("# Thin card\nlabels: p1\n")
        rejected = backlog / "rejected"
        rejected.mkdir()
        (rejected / "a_thin.md").write_text("earlier card")

        filed = autodispatch.replenish(5, {})

        self.assertEqual(filed, [])
        self.assertEqual((rejected / "a_thin.md").read_text(), "earlier card")
        moved = rejected / "a_thin.1.md"
        self.assertTrue(moved.is_file(), sorted(path.name for path in rejected.iterdir()))
        self.assertIn("Thin card", moved.read_text())


class PlannerGateTests(unittest.TestCase):
    """Refs #1662: an unreadable planner query keeps the single-planner gate closed."""

    def patch(self, name, value):
        self.addCleanup(setattr, autodispatch, name, getattr(autodispatch, name))
        setattr(autodispatch, name, value)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)
        self.patch("D", str(self.dir))
        self.patch("BRAIN_LOG", str(self.dir / "brain.log"))
        self.patch("LAST_MINT_LOG", 0.0)
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch("sh", lambda *args, **kwargs: (0, "", ""))

    def log_text(self):
        return (self.dir / "autodispatch.log").read_text(encoding="utf-8")

    def test_an_unreadable_planner_query_skips_the_creation(self):
        commands = []

        def fake_gh(args, timeout=60):
            commands.append(args)
            if args[:2] == ["issue", "list"] and "planner" in args:
                return 0, "not-json", ""
            return 0, json.dumps([]), ""

        self.patch("gh", fake_gh)

        self.assertEqual(autodispatch.replenish(1, {}), [])
        self.assertEqual(len(commands), 1)
        self.assertIn("planner query unreadable", self.log_text())
        self.assertIn("skip planner creation", self.log_text())


if __name__ == "__main__":
    unittest.main()
