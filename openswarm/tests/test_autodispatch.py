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


class DispatchSelectionTests(unittest.TestCase):
    """Refs #1359: the push-mode dispatch decisions, aligned with the box autodispatch."""

    def test_dispatch_sorts_priority_then_issue_number(self):
        issues = [
            {"number": 9, "labels": ["lane-ready", "p1"]},
            {"number": 4, "labels": ["lane-ready", "p0"]},
            {"number": 2, "labels": ["lane-ready", "p1"]},
        ]
        planned = autodispatch.select_dispatches(issues, [{"name": "a", "backend": "codex"}], {})
        self.assertEqual(planned[0][0]["number"], 4)

    def test_dispatch_lane_order_follows_priority_then_number(self):
        issues = [
            {"number": 9, "labels": ["lane-ready", "p1"]},
            {"number": 4, "labels": ["lane-ready", "p0"]},
            {"number": 2, "labels": ["lane-ready", "p1"]},
        ]
        lanes = [{"name": "a", "backend": "codex"}, {"name": "b", "backend": "codex"}]
        planned = autodispatch.select_dispatches(issues, lanes, {})
        self.assertEqual([(entry[0]["number"], entry[1]["name"]) for entry in planned],
                         [(4, "a"), (2, "b")])

    def test_dispatch_prefers_the_lane_that_owns_the_backend_pin(self):
        issues = [
            {"number": 1, "labels": ["lane-ready"]},
            {"number": 2, "labels": ["lane-ready", "backend:pi"]},
        ]
        lanes = [{"name": "codex-lane", "backend": "codex"}, {"name": "pi-lane", "backend": "pi"}]
        planned = autodispatch.select_dispatches(issues, lanes, {})
        self.assertEqual([(entry[0]["number"], entry[1]["name"]) for entry in planned],
                         [(1, "codex-lane"), (2, "pi-lane")])

    def test_dispatch_falls_back_to_another_backend_like_the_box_ad(self):
        issue = {"number": 4, "labels": ["lane-ready", "backend:pi"]}
        lanes = [{"name": "codex-lane", "backend": "codex"}]
        planned = autodispatch.select_dispatches([issue], lanes, {})
        self.assertEqual([entry[1]["name"] for entry in planned], ["codex-lane"])

    def test_dispatch_refuses_cross_backend_for_strict_backend_cards(self):
        issue = {"number": 4, "labels": ["lane-ready", "backend:pi", "strict-backend"]}
        lanes = [{"name": "codex-lane", "backend": "codex"}, {"name": "pi-lane", "backend": "pi"}]
        self.assertEqual(autodispatch.select_dispatches([issue], [lanes[0]]), [])
        planned = autodispatch.select_dispatches([issue], lanes, {})
        self.assertEqual([entry[1]["name"] for entry in planned], ["pi-lane"])

    def test_dispatch_strict_label_name_is_configurable(self):
        issue = {"number": 4, "labels": ["lane-ready", "backend:pi", "keep-backend"]}
        lanes = [{"name": "codex-lane", "backend": "codex"}]
        self.assertEqual([entry[1]["name"] for entry in autodispatch.select_dispatches([issue], lanes, {})],
                         ["codex-lane"])
        self.assertEqual(autodispatch.select_dispatches([issue], lanes, {"strict_label": "keep-backend"}), [])

    def test_dispatch_uses_configured_label_names(self):
        issue = {"number": 4, "labels": ["ready-now", "urgent"]}
        planned = autodispatch.select_dispatches([issue], [{"name": "lane", "backend": "codex"}], {
            "ready_label": "ready-now", "priorities": {"urgent": 0}})
        self.assertEqual([entry[0]["number"] for entry in planned], [4])

    def test_dispatch_skips_claimed_issues(self):
        issues = [{"number": 1, "labels": ["lane-ready", "in-progress"]}]
        self.assertEqual(autodispatch.select_dispatches(issues, [{"name": "lane", "backend": "codex"}]), [])

    def test_dispatch_with_no_available_lanes_returns_no_assignments(self):
        issue = {"number": 1, "labels": ["lane-ready"]}
        self.assertEqual(autodispatch.select_dispatches([issue], []), [])

    def test_dispatch_waits_for_open_dependencies(self):
        issue = {"number": 1, "labels": ["lane-ready"], "body": "依赖：#12"}
        lanes = [{"name": "lane", "backend": "codex"}]
        self.assertEqual(autodispatch.select_dispatches([issue], lanes, {}), [])
        planned = autodispatch.select_dispatches([issue], lanes, {"closed_issues": {12}})
        self.assertEqual([entry[0]["number"] for entry in planned], [1])

    def test_dispatch_dependencies_are_read_from_the_card_body(self):
        self.assertEqual(autodispatch.dispatch_dependencies("依赖：#12 #34"), [12, 34])
        self.assertEqual(autodispatch.dispatch_dependencies("Depends on: #5"), [5])
        self.assertEqual(autodispatch.dispatch_dependencies("no dependency here"), [])

    def test_dispatch_skip_reason_explains_why_a_card_waited(self):
        config = {"ready_label": "lane-ready"}
        issue = {"number": 1, "labels": ["lane-ready"], "body": "依赖：#12"}
        item = autodispatch.dispatch_item(issue, config)
        self.assertEqual(autodispatch.dispatch_skip_reason(item, [], {**config, "closed_issues": set()}),
                         "waiting on dependencies")
        self.assertEqual(autodispatch.dispatch_skip_reason(item, [], {**config, "closed_issues": {12}}),
                         "no idle lane")

    def test_dispatch_labels_accept_strings_and_objects(self):
        issue = {"number": 1, "labels": [{"name": "lane-ready"}, "p0"]}
        self.assertEqual(autodispatch.dispatch_labels(issue), {"lane-ready", "p0"})


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
