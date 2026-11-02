import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)


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


class DispatchBudgetTests(unittest.TestCase):
    """Refs #1686: the dispatch budget is tiered per card — large cards get 90 minutes."""

    def item(self, **kwargs):
        card = {"n": 1686, "title": "budget card", "body": "", "labels": ["lane-ready"],
                "prio": 1, "backend": None, "strict": False}
        card.update(kwargs)
        return card

    def dispatch_command(self, card):
        """Run dispatch() against a fake shell and return the remote command it built."""
        calls = []

        def fake_sh(cmd, timeout=120, inp=None):
            calls.append(cmd)
            return 0, '{"id":"run-1"}', ""

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(autodispatch, "D", directory), \
                mock.patch.object(autodispatch, "sh", fake_sh):
            autodispatch.dispatch("C1", {"host": "instabox", "fleet": "fleet", "repo": "repo"},
                                  card, "b" * 40)
        return " ".join(calls[-1])

    def test_a_large_card_is_dispatched_with_90_minutes(self):
        command = self.dispatch_command(self.item(labels=["lane-ready", "budget:large"]))

        self.assertIn("--budget-minutes 90", command)

    def test_a_card_without_a_marker_keeps_the_default_budget(self):
        command = self.dispatch_command(self.item())

        self.assertIn("--budget-minutes 45", command)

    def test_a_body_marker_selects_the_large_budget(self):
        command = self.dispatch_command(self.item(body="实现大卡。\n预算：90\n"))

        self.assertIn("--budget-minutes 90", command)

    def test_a_malformed_marker_falls_back_to_45_and_warns(self):
        card = self.item(body="实现大卡。\n预算：九十\n")
        decision = autodispatch.budget_for(card)

        self.assertEqual(decision["minutes"], 45)
        self.assertTrue(decision["warn"])
        self.assertIn("--budget-minutes 45", self.dispatch_command(card))

    def test_an_unknown_budget_label_falls_back_to_45_and_warns(self):
        decision = autodispatch.budget_for(self.item(labels=["lane-ready", "budget:huge"]))

        self.assertEqual(decision["minutes"], 45)
        self.assertTrue(decision["warn"])

    def test_the_budget_decision_records_its_reason(self):
        label = autodispatch.budget_for(self.item(labels=["budget:large"]))
        body = autodispatch.budget_for(self.item(body="预算：90"))
        plain = autodispatch.budget_for(self.item())

        self.assertEqual(label["minutes"], 90)
        self.assertIn("budget:large", label["basis"])
        self.assertEqual(body["minutes"], 90)
        self.assertIn("预算：90", body["basis"])
        self.assertEqual(plain["minutes"], 45)
        self.assertFalse(plain["warn"])

    def test_the_dispatch_log_line_carries_the_issue_the_budget_and_the_reason(self):
        card = self.item(labels=["budget:large"])
        budget = autodispatch.budget_for(card)

        line = autodispatch.dispatch_log_line("C1", {"host": "instabox"}, card, "run-1", "b" * 40, budget)

        self.assertIn("#1686", line)
        self.assertIn("C1", line)
        self.assertIn("budget 90m", line)
        self.assertIn("budget:large", line)

    def test_only_the_budget_differs_between_the_two_tiers(self):
        # The stall watchdog, the verify command and the base stay the same for both tiers:
        # the no-progress kill must hit a 45-minute card and a 90-minute card alike.
        small = self.dispatch_command(self.item())
        large = self.dispatch_command(self.item(labels=["budget:large"]))

        self.assertEqual(small.replace("--budget-minutes 45", "B"), large.replace("--budget-minutes 90", "B"))


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
