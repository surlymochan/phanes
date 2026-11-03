import importlib.util
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)

MINT_SOURCE = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "mint_source.py"

# A fake `gh` for the seed-mint hook: it files the cards and remembers their labels.
GH_STUB = '''import json, os, sys

record = os.environ["STUB_RECORD"]
args = sys.argv[1:]


def load():
    try:
        with open(record) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


if args[:2] == ["issue", "create"]:
    labels = [args[i + 1] for i, a in enumerate(args) if a == "-l" and i + 1 < len(args)]
    data = load()
    number = 501 + len(data)
    data[str(number)] = labels
    with open(record, "w") as stream:
        json.dump(data, stream)
    print("https://github.com/acme/x/issues/%d" % number)
elif args[:2] == ["issue", "view"]:
    labels = load().get(args[2], [])
    print(json.dumps({"state": "OPEN", "labels": [{"name": name} for name in labels]}))
else:
    print("[]")
'''


class SeedMintSourceHookTests(unittest.TestCase):
    """The phanes seed-mint dispatch source hook (Refs #1371).

    The hook is an external command of ``sources.hooks``: it prints one JSON
    task per line (issue, priority, backend, reason) and stays quiet while the
    operator pause is up.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mint-source-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = pathlib.Path(self.tmp, "ad")
        (self.state / "backlog").mkdir(parents=True)
        self.record = pathlib.Path(self.tmp, "gh-record.json")
        stub = pathlib.Path(self.tmp, "gh_stub.py")
        stub.write_text(GH_STUB)
        gh = pathlib.Path(self.tmp, "bin", "gh")
        gh.parent.mkdir(parents=True, exist_ok=True)
        gh.write_text('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, stub))
        gh.chmod(0o755)

    def run_hook(self, target="1"):
        env = dict(os.environ)
        env["PATH"] = str(pathlib.Path(self.tmp, "bin")) + os.pathsep + env.get("PATH", "")
        env["STUB_RECORD"] = str(self.record)
        return subprocess.run(
            [sys.executable, str(MINT_SOURCE), "--state-dir", str(self.state), "--repo", "acme/x",
             "--repo-path", str(pathlib.Path(self.tmp, "no-repo")), "--target", target],
            capture_output=True, text=True, env=env, timeout=120)

    def filed_cards(self):
        """The cards the fake gh filed: issue number -> labels."""
        try:
            with open(self.record) as stream:
                return json.load(stream)
        except (OSError, ValueError):
            return {}

    def test_hook_files_a_backlog_card_and_reports_it_in_the_protocol(self):
        (self.state / "backlog" / "card.md").write_text(
            "# Seed card\nlabels: p2,backend:pi\nRefill the queue with one bounded card.\n")

        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        task = json.loads(lines[0])
        self.assertEqual(sorted(task), ["backend", "issue", "priority", "reason"])
        self.assertEqual(task["issue"], 501)
        self.assertEqual(task["priority"], "p2")
        self.assertEqual(task["backend"], "pi")
        self.assertIn("seed-mint", task["reason"])
        self.assertEqual(self.filed_cards()["501"], ["lane-ready", "p2", "backend:pi"])
        # the filed card is consumed: it moved to backlog/filed/
        self.assertTrue((self.state / "backlog" / "filed" / "card.md").exists())

    def test_hook_files_the_planner_card_when_the_backlog_is_empty(self):
        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        task = json.loads(lines[0])
        self.assertEqual(task["issue"], 501)
        self.assertEqual(task["priority"], "p1")
        self.assertEqual(task["backend"], "codex")
        self.assertIn("planner", self.filed_cards()["501"])

    def test_import_history_is_shared_across_hook_and_autodispatch(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mint_source_test", MINT_SOURCE)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        (self.state / "state.json").write_text(json.dumps({"_imported": ["old.md"], "CX1": {"run": 1}}))
        self.assertEqual(hook.load_state(str(self.state))["_imported"], ["old.md"])
        hook.save_state(str(self.state), {"_imported": ["new.md"]})
        with mock.patch.object(autodispatch, "D", str(self.state)):
            autodispatch.save_state({"_imported": ["old.md"], "CX1": {"run": 2}})
        state = hook.load_state(str(self.state))
        self.assertEqual(state["_imported"], ["old.md", "new.md"])
        self.assertEqual(state["CX1"], {"run": 2})

    def test_hook_is_silent_and_files_nothing_while_pause_mint_is_up(self):
        (self.state / "backlog" / "card.md").write_text("# Seed card\nlabels: p2,backend:pi\nBody.\n")
        (self.state / "PAUSE-MINT").write_text("operator pause\n")

        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")
        self.assertEqual(self.filed_cards(), {})
        self.assertTrue((self.state / "backlog" / "card.md").exists())


class GitSSHCommandTests(unittest.TestCase):
    def test_replenish_respects_pause_mint_without_touching_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            pause = pathlib.Path(directory, "PAUSE-MINT")
            pause.write_text("operator pause\n")
            with mock.patch.object(autodispatch, "D", directory), \
                    mock.patch.object(autodispatch, "file_issue") as file_issue:
                self.assertEqual(autodispatch.replenish(3, {}), [])
            file_issue.assert_not_called()
            self.assertEqual({path.name for path in pathlib.Path(directory).iterdir()}, {"PAUSE-MINT"})

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


if __name__ == "__main__":
    unittest.main()
