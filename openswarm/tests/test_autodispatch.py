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

FALLBACK = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "fallback.py"
fallback_spec = importlib.util.spec_from_file_location("dispatch_fallback", FALLBACK)
fallback = importlib.util.module_from_spec(fallback_spec)
fallback_spec.loader.exec_module(fallback)

ENGINE = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "engine.py"
engine_spec = importlib.util.spec_from_file_location("dispatch_engine_under_test", ENGINE)
engine = importlib.util.module_from_spec(engine_spec)
engine_spec.loader.exec_module(engine)

CLI = pathlib.Path(__file__).parents[1] / "bin" / "openswarm"


def load_cli(install, data):
    """The openswarm CLI of an install directory, imported as a module.

    The CLI derives its policy directory from its own path (`SKILL_DIR` in
    `openswarm/bin/openswarm`), so the script is copied into `<install>/bin/` before it is
    imported: the module then reads the policy of that install and the data directory of the
    test, exactly like the CLI on the box.
    """
    script = pathlib.Path(install) / "bin" / "openswarm_cli.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CLI, script)
    spec = importlib.util.spec_from_file_location(f"openswarm_cli_{install}", script)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(data)}):
        spec.loader.exec_module(module)
    return module


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


class CrossBackendFallbackTests(unittest.TestCase):
    """Policy `dispatch.fallback`: a pinned card may fall back to another backend (Refs #1361)."""

    def policy(self, **section):
        return {"dispatch": section}

    def card(self, backend="codex"):
        return {"n": 1, "title": "t", "body": "", "backend": backend, "strict": False}

    def test_before_the_wait_threshold_only_the_pinned_backend_runs_the_card(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        self.assertEqual(autodispatch.fallback_chain("codex", 599, policy, set()), ["codex"])

    def test_after_the_wait_threshold_the_chain_is_tried_in_order(self):
        policy = self.policy(fallback={"agy": ["codex", "pi"]}, fallback_wait_s=600)

        self.assertEqual(autodispatch.fallback_chain("agy", 600, policy, set()), ["agy", "codex", "pi"])

    def test_a_paused_backend_is_skipped(self):
        policy = self.policy(fallback={"agy": ["codex", "pi", "gemini"]}, fallback_wait_s=600)

        self.assertEqual(autodispatch.fallback_chain("agy", 900, policy, {"pi"}), ["agy", "codex", "gemini"])

    def test_a_strict_policy_never_falls_back(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600, fallback_strict=True)

        self.assertEqual(autodispatch.fallback_chain("codex", 900, policy, set()), ["codex"])

    def test_a_backend_without_a_mapping_stays_on_its_own_backend(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        self.assertEqual(autodispatch.fallback_chain("pi", 900, policy, set()), ["pi"])

    def test_without_a_policy_the_plain_cross_pin_rule_stays_in_place(self):
        self.assertTrue(autodispatch.can_take("agy", self.card("pi")))

    def test_a_cross_pin_card_is_not_taken_before_the_wait_threshold(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        self.assertFalse(autodispatch.can_take("pi", self.card("codex"), policy, 599, set()))
        self.assertTrue(autodispatch.can_take("pi", self.card("codex"), policy, 600, set()))

    def test_a_paused_backend_does_not_take_the_card(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        self.assertFalse(autodispatch.can_take("pi", self.card("codex"), policy, 900, {"pi"}))

    def test_the_own_pin_and_unpinned_cards_are_never_affected(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        self.assertTrue(autodispatch.can_take("codex", self.card("codex"), policy, 0, set()))
        self.assertTrue(autodispatch.can_take("agy", self.card(None), policy, 0, set()))

    def test_the_policy_is_read_from_the_configured_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "policy.json")
            with open(path, "w") as stream:
                json.dump(self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600), stream)
            with mock.patch.dict(os.environ, {autodispatch.FALLBACK_POLICY_ENV: path}):
                policy = autodispatch.fallback_policy()

        self.assertEqual(policy["dispatch"]["fallback"], {"codex": ["pi"]})

    def test_the_shared_module_and_the_autodispatch_helper_agree(self):
        policy = self.policy(fallback={"codex": ["pi"], "agy": ["codex", "pi"]}, fallback_wait_s=600)
        cfg = fallback.policy_config(policy)

        for pinned in ("codex", "agy", "pi"):
            for waited in (0, 599, 600, 900):
                for paused in (set(), {"pi"}, {"codex", "pi"}):
                    with self.subTest(pinned=pinned, waited=waited, paused=paused):
                        self.assertEqual(autodispatch.fallback_chain(pinned, waited, policy, paused),
                                         fallback.fallback_backends(pinned, waited, cfg, paused))

    def test_the_shipped_policy_carries_the_mapping_and_the_threshold(self):
        with open(pathlib.Path(__file__).parents[1] / "policy.json") as stream:
            policy = json.load(stream)

        section = policy.get("dispatch") or {}
        self.assertIn("codex", section.get("fallback") or {})
        self.assertGreater(int(section.get("fallback_wait_s") or 0), 0)


class CrossBackendFallbackSchedulerTests(unittest.TestCase):
    """The legacy scheduler follows the policy chain and skips paused backends (Refs #1361)."""

    def policy(self, **section):
        return {"dispatch": section}

    def card(self, backend="agy", n=1, body="", waited=0):
        return {"n": n, "title": f"card {n}", "body": body, "prio": 1, "backend": backend,
                "strict": False, "waited_s": waited}

    def test_the_chain_order_decides_the_backend_of_a_pinned_card(self):
        policy = self.policy(fallback={"agy": ["codex", "pi"]}, fallback_wait_s=0)

        target, restricted = autodispatch.fallback_target(self.card("agy"), policy, {"codex": 1, "pi": 1})

        self.assertTrue(restricted)
        self.assertEqual(target, "codex")

    def test_a_backend_that_cannot_take_the_card_is_skipped(self):
        policy = self.policy(fallback={"agy": ["codex", "pi"]}, fallback_wait_s=0)

        target, _ = autodispatch.fallback_target(self.card("agy", body="needs AF_UNIX socket"),
                                                policy, {"codex": 1, "pi": 1})

        self.assertEqual(target, "pi")     # codex may not take Unix socket work

    def test_before_the_wait_threshold_the_card_waits_for_its_own_backend(self):
        policy = self.policy(fallback={"agy": ["codex", "pi"]}, fallback_wait_s=600)
        card = self.card("agy", waited=599)

        target, restricted = autodispatch.fallback_target(card, policy, {"codex": 1, "pi": 1})

        self.assertTrue(restricted)
        self.assertIsNone(target)
        self.assertFalse(autodispatch.chain_allows_lane("codex", card, policy, {"codex": 1, "pi": 1}))

    def test_after_the_wait_threshold_the_chain_decides_the_backend(self):
        policy = self.policy(fallback={"agy": ["codex", "pi"]}, fallback_wait_s=600)
        card = self.card("agy", waited=600)

        target, _ = autodispatch.fallback_target(card, policy, {"codex": 1, "pi": 1})

        self.assertEqual(target, "codex")
        self.assertTrue(autodispatch.chain_allows_lane("codex", card, policy, {"codex": 1, "pi": 1}))
        self.assertFalse(autodispatch.chain_allows_lane("pi", card, policy, {"codex": 1, "pi": 1}))

    def test_a_paused_backend_takes_no_card(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        self.assertFalse(autodispatch.can_take("codex", self.card("codex"), policy, 0, {"codex"}))
        self.assertFalse(autodispatch.can_take("agy", self.card(None), policy, 0, {"agy"}))

    def test_an_unpinned_card_may_go_to_every_backend(self):
        policy = self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600)

        target, restricted = autodispatch.fallback_target(self.card(None), policy, {"pi": 1})

        self.assertFalse(restricted)
        self.assertIsNone(target)
        self.assertTrue(autodispatch.chain_allows_lane("pi", self.card(None), policy, {"pi": 1}))

    def test_the_base_policy_and_the_local_override_are_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            home = os.path.join(directory, "home")
            os.makedirs(install); os.makedirs(home)
            # the base policy of the openswarm install (the file the openswarm CLI reads)
            with open(os.path.join(install, "policy.json"), "w") as stream:
                json.dump(self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600), stream)
            # the local override of the data directory
            with open(os.path.join(home, "policy.local.json"), "w") as stream:
                json.dump({"resources": {"memory_high": "80%"}}, stream)
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": home, autodispatch.FALLBACK_POLICY_ENV: "",
                                              autodispatch.SKILL_DIR_ENV: install}):
                policy = autodispatch.fallback_policy()

        self.assertEqual(policy["dispatch"]["fallback"]["codex"], ["pi"])
        self.assertEqual(policy["dispatch"]["fallback_wait_s"], 600)
        self.assertEqual(policy["resources"], {"memory_high": "80%"})

    def test_a_local_override_without_a_dispatch_section_keeps_the_base_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            home = os.path.join(directory, "home")
            os.makedirs(install); os.makedirs(home)
            with open(os.path.join(install, "policy.json"), "w") as stream:
                json.dump(self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600), stream)
            with open(os.path.join(home, "policy.local.json"), "w") as stream:
                json.dump({"resources": {"memory_high": "80%"}}, stream)
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": home, autodispatch.FALLBACK_POLICY_ENV: "",
                                              autodispatch.SKILL_DIR_ENV: install}):
                policy = autodispatch.fallback_policy()
                card = self.card("codex")

                # the mapping of the base policy is still in force
                self.assertFalse(autodispatch.can_take("pi", card, policy, 599, set()))
                self.assertTrue(autodispatch.can_take("pi", card, policy, 600, set()))

    def test_the_policy_of_the_cli_install_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            os.makedirs(install)
            with open(os.path.join(install, "policy.json"), "w") as stream:
                json.dump(self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600), stream)
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": directory, autodispatch.FALLBACK_POLICY_ENV: "",
                                              autodispatch.SKILL_DIR_ENV: install}):
                policy = autodispatch.fallback_policy()

        self.assertIn("codex", (policy.get("dispatch") or {}).get("fallback") or {})

    def test_the_install_of_the_cli_is_the_only_base_policy(self):
        """Only the policy of the install of the openswarm CLI is a base policy (Refs #1361).

        The CLI knows the policy of its own install only.  A second install (the openswarm tree
        of the repository, for instance) must not be merged in: the CLI and the dispatcher would
        dispatch differently, and the fallback mapping must not depend on where the dispatcher
        runs from.
        """
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            with mock.patch.dict(os.environ, {autodispatch.SKILL_DIR_ENV: install}):
                paths = autodispatch.policy_base_paths()

        self.assertEqual(paths, [os.path.join(install, "policy.json")])

    def test_the_cli_and_the_dispatcher_read_the_same_policy_without_a_dispatch_section(self):
        """An install without a `dispatch` section knows no fallback (Refs #1361).

        The CLI reads the policy of its install, the dispatcher reads the same file.  When that
        policy has no dispatch section neither of them may fall back: otherwise a card would
        fall back in one dispatcher and not in the other.
        """
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            home = os.path.join(directory, "home")
            os.makedirs(install); os.makedirs(home)
            with open(os.path.join(install, "policy.json"), "w") as stream:
                json.dump({"routing": {"default_order": ["codex"]}}, stream)
            cli = load_cli(install, home)
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": home, autodispatch.FALLBACK_POLICY_ENV: "",
                                              autodispatch.SKILL_DIR_ENV: install}):
                policy = autodispatch.fallback_policy()
                card = self.card("codex")

                # the CLI and the dispatcher read the same policy ...
                self.assertEqual(policy, cli.load_policy())
                # ... and neither of them knows a fallback
                self.assertNotIn("dispatch", policy)
                self.assertTrue(autodispatch.can_take("pi", card, policy, 900, set()))

    def test_the_cli_and_the_dispatcher_read_the_same_policy_with_a_dispatch_section(self):
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            home = os.path.join(directory, "home")
            os.makedirs(install); os.makedirs(home)
            with open(os.path.join(install, "policy.json"), "w") as stream:
                json.dump(self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600), stream)
            cli = load_cli(install, home)
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": home, autodispatch.FALLBACK_POLICY_ENV: "",
                                              autodispatch.SKILL_DIR_ENV: install}):
                policy = autodispatch.fallback_policy()

            self.assertEqual(policy, cli.load_policy())
            self.assertEqual(policy["dispatch"]["fallback"], {"codex": ["pi"]})
            self.assertEqual(policy["dispatch"]["fallback_wait_s"], 600)

    def test_a_stale_policy_of_the_data_directory_does_not_shadow_the_published_rules(self):
        """The data directory is not a base policy source (Refs #1361).

        The openswarm CLI reads the policy of its install and merges the local override of the
        data directory.  A stale `policy.json` of the data directory must not drop the published
        fallback mapping: the dispatcher would fall back to any backend without a wait.
        """
        with tempfile.TemporaryDirectory() as directory:
            install = os.path.join(directory, "install")
            home = os.path.join(directory, "home")
            os.makedirs(install); os.makedirs(home)
            # the policy of the install of the openswarm CLI, with the published rules
            with open(os.path.join(install, "policy.json"), "w") as stream:
                json.dump(self.policy(fallback={"codex": ["pi"]}, fallback_wait_s=600), stream)
            # an old policy of the data directory, without a dispatch section
            with open(os.path.join(home, "policy.json"), "w") as stream:
                json.dump({"routing": {"default_order": ["agy"]}}, stream)
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": home, autodispatch.FALLBACK_POLICY_ENV: "",
                                              autodispatch.SKILL_DIR_ENV: install}):
                policy = autodispatch.fallback_policy()
                card = self.card("codex")

                # the published rules are still in force: the mapping and the wait threshold
                self.assertIn("codex", (policy.get("dispatch") or {}).get("fallback") or {})
                self.assertFalse(autodispatch.can_take("pi", card, policy, 599, set()))
                self.assertTrue(autodispatch.can_take("pi", card, policy, 600, set()))
                # the stale file of the data directory is not read at all
                self.assertNotIn("routing", policy)

    def test_the_queue_wait_grows_over_the_ticks_and_is_not_reset(self):
        state = {}
        items = [self.card("codex", n=7)]

        self.assertTrue(autodispatch.queue_arrivals(items, state, now=1000.0))
        self.assertEqual(items[0]["waited_s"], 0.0)

        items = [self.card("codex", n=7)]
        self.assertFalse(autodispatch.queue_arrivals(items, state, now=1600.0))
        self.assertEqual(items[0]["waited_s"], 600.0)

    def test_the_queue_arrival_survives_the_state_round_trip(self):
        state = {}
        autodispatch.queue_arrivals([self.card("codex", n=7)], state, now=1000.0)

        state = json.loads(json.dumps(state))    # the state file round trip
        items = [self.card("codex", n=7)]
        autodispatch.queue_arrivals(items, state, now=1600.0)

        self.assertEqual(items[0]["waited_s"], 600.0)
        self.assertEqual(list(state[autodispatch.QUEUE_SINCE_KEY]), ["7"])

    def test_the_queue_never_reads_the_timestamp_of_the_issue(self):
        calls = []

        def fake_gh(args, timeout=60):
            calls.append(args)
            return 0, json.dumps([]), ""

        with mock.patch.object(autodispatch, "gh", fake_gh):
            autodispatch.queue()

        self.assertNotIn("updatedAt", " ".join(calls[0]))

    def test_a_card_that_left_the_queue_waits_again_when_it_comes_back(self):
        state = {}
        autodispatch.queue_arrivals([self.card("codex", n=7)], state, now=1000.0)

        # the card left the queue (it was dispatched): its arrival time is forgotten
        self.assertTrue(autodispatch.note_queue_departure(7, state))

        items = [self.card("codex", n=7)]
        self.assertTrue(autodispatch.queue_arrivals(items, state, now=1600.0))
        self.assertEqual(items[0]["waited_s"], 0.0)     # the grace period starts again

    def test_the_cards_that_are_no_more_in_the_queue_are_forgotten(self):
        state = {}
        autodispatch.queue_arrivals([self.card("codex", n=7), self.card("codex", n=8)], state, now=1000.0)

        self.assertTrue(autodispatch.prune_queue_arrivals(state, [self.card("codex", n=7)]))
        self.assertEqual(list(state[autodispatch.QUEUE_SINCE_KEY]), ["7"])
        self.assertFalse(autodispatch.prune_queue_arrivals(state, [self.card("codex", n=7)]))

    def test_the_departure_survives_the_state_round_trip(self):
        state = {}
        autodispatch.queue_arrivals([self.card("codex", n=7)], state, now=1000.0)
        autodispatch.note_queue_departure(7, state)

        state = json.loads(json.dumps(state))    # the state file round trip (a restart)
        items = [self.card("codex", n=7)]
        self.assertTrue(autodispatch.queue_arrivals(items, state, now=1600.0))
        self.assertEqual(items[0]["waited_s"], 0.0)

    def legacy_tick(self, directory, cards, stale=False, lanes=None):
        """One tick of the legacy scheduler with a faked queue and GitHub."""
        def fake_gh(args, timeout=60):
            if args[:2] == ["issue", "view"]:
                return 0, json.dumps({"state": "OPEN", "labels": [{"name": "lane-ready"}]}), ""
            return 0, "", ""

        read = ("stale", {}) if stale else ("fresh", {"state": "OPEN", "labels": [{"name": "lane-ready"}]})
        state_path = os.path.join(directory, "state.json")
        state = {}
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as stream:
                state = json.load(stream)
        with mock.patch.object(autodispatch, "D", directory), \
                mock.patch.object(autodispatch, "BRAIN_LOG", os.path.join(directory, "brain.log")), \
                mock.patch.object(autodispatch, "LOW", 0), \
                mock.patch.object(autodispatch, "gh", fake_gh), \
                mock.patch.object(autodispatch, "read_issue", lambda n: read), \
                mock.patch.object(autodispatch, "remote", lambda lane, cfg, run="": {"lane": "idle"}), \
                mock.patch.object(autodispatch, "replenish", lambda k, state: 0), \
                mock.patch.object(autodispatch, "queue", lambda: list(cards)):
            autodispatch.tick(lanes or {}, state, 0.0)
        return state

    def test_the_tick_forgets_the_cards_that_left_the_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            card = self.card("codex", n=7)
            self.legacy_tick(directory, [card])       # the card enters the queue
            self.assertEqual(card["waited_s"], 0.0)

            state = self.legacy_tick(directory, [])    # the card left the queue
            self.assertEqual(state[autodispatch.QUEUE_SINCE_KEY], {})

            back = self.card("codex", n=7)
            self.legacy_tick(directory, [back])        # the card is back
            self.assertEqual(back["waited_s"], 0.0)    # it waits for the grace period again

    def test_a_card_that_was_claimed_by_another_lane_forgets_its_arrival_time(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {}
            autodispatch.queue_arrivals([self.card("codex", n=7)], state, now=1000.0)

            state = self.legacy_tick(directory, [self.card("codex", n=7)], stale=True,
                                     lanes={"L1": {"host": "box", "backend": "codex"}})

            self.assertNotIn("7", state[autodispatch.QUEUE_SINCE_KEY])


class CrossBackendFallbackEngineTests(unittest.TestCase):
    """The dispatch engine hands a pinned card to the backend of the policy chain (Refs #1361)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fallback-engine-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = 1000.0

    def make(self, chain):
        cfg = engine.deep_merge(engine.DEFAULTS, {
            "repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
            "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "fallback_chain": chain})
        dispatcher = engine.Dispatcher(cfg, runner=lambda cmd, timeout=120, inp=None, env=None: (0, "", ""),
                                       out=open(os.devnull, "w"), clock=lambda: self.now)
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    def card(self, backend="agy", n=1, body="", waited=0):
        return {"n": n, "title": f"card {n}", "body": body, "prio": 1, "backend": backend,
                "strict": False, "waited_s": waited}

    def lanes(self, *backends):
        # the lane names decide the order the lanes are served in (LRU picker)
        return {f"{index}-{backend}": {"host": "box", "backend": backend}
                for index, backend in enumerate(backends)}

    def assign(self, dispatcher, lanes, cards):
        dispatcher.main_sha = lambda: "a" * 40
        dispatcher.label_state = lambda n: "fresh"
        dispatcher.dispatch = lambda lane, lc, item, base: (f"run-{item['n']}", "verify")
        state = {}
        dispatcher.assign_issues(lanes, {lane: {"lane": "idle"} for lane in lanes}, state, list(cards))
        return state

    def test_the_chain_order_decides_the_backend(self):
        dispatcher = self.make({"map": {"agy": ["codex", "pi"]}, "wait_s": 0})
        lanes = self.lanes("pi", "codex")     # the pi lane is served first

        state = self.assign(dispatcher, lanes, [self.card("agy")])

        self.assertEqual(state.get("1-codex", {}).get("issue"), 1)
        self.assertNotIn("0-pi", state)

    def test_a_backend_that_cannot_take_the_card_is_skipped(self):
        dispatcher = self.make({"map": {"agy": ["codex", "pi"]}, "wait_s": 0})
        lanes = self.lanes("pi", "codex")     # the pi lane is served first

        state = self.assign(dispatcher, lanes, [self.card("agy", body="needs AF_UNIX socket")])

        self.assertEqual(state.get("0-pi", {}).get("issue"), 1)
        self.assertNotIn("1-codex", state)

    def test_before_the_wait_threshold_the_card_waits_for_its_own_backend(self):
        dispatcher = self.make({"map": {"agy": ["codex", "pi"]}, "wait_s": 600})
        lanes = self.lanes("pi", "codex")

        state = self.assign(dispatcher, lanes, [self.card("agy", waited=599)])

        self.assertEqual(state, {})

    def test_after_the_wait_threshold_the_card_falls_back(self):
        dispatcher = self.make({"map": {"agy": ["codex", "pi"]}, "wait_s": 600})
        lanes = self.lanes("pi", "codex")

        state = self.assign(dispatcher, lanes, [self.card("agy", waited=600)])

        self.assertEqual(state.get("1-codex", {}).get("issue"), 1)

    def test_the_queue_arrival_survives_the_state_round_trip(self):
        dispatcher = self.make({})
        state = {}
        card = self.card("agy")

        self.assertTrue(dispatcher.mark_queue_arrivals([card], state))
        self.assertEqual(card["waited_s"], 0.0)

        state = json.loads(json.dumps(state))    # the state file round trip
        self.now = 1600.0
        card = self.card("agy")
        self.assertFalse(dispatcher.mark_queue_arrivals([card], state))
        self.assertEqual(card["waited_s"], 600.0)
        self.assertEqual(list(state[engine.QUEUE_SINCE_KEY]), ["1"])

    def test_the_tick_persists_the_queue_arrivals(self):
        dispatcher = self.make({})
        dispatcher.queue = lambda: [self.card("agy")]
        dispatcher.deps_met = lambda item: True
        dispatcher.probe = lambda lanes, state: {}
        dispatcher.harvest = lambda lanes, res, state: None
        dispatcher.sweep_leaked_inprogress = lambda state: None
        dispatcher.dispatch_reviews = lambda lanes, res, state, q: None

        dispatcher.tick({}, {})

        with open(os.path.join(dispatcher.D, "state.json"), encoding="utf-8") as stream:
            saved = json.load(stream)
        self.assertEqual(saved[engine.QUEUE_SINCE_KEY], {"1": 1000.0})

    def test_a_card_that_left_the_queue_waits_again_when_it_comes_back(self):
        dispatcher = self.make({})
        state = {}
        dispatcher.mark_queue_arrivals([self.card("agy", n=7)], state)

        # the card left the queue (it was dispatched): its arrival time is forgotten
        self.assertTrue(dispatcher.note_queue_departure(7, state))

        card = self.card("agy", n=7)
        self.assertTrue(dispatcher.mark_queue_arrivals([card], state))
        self.assertEqual(card["waited_s"], 0.0)     # the grace period starts again

    def test_the_cards_that_are_no_more_in_the_queue_are_forgotten(self):
        dispatcher = self.make({})
        state = {}
        dispatcher.mark_queue_arrivals([self.card("agy", n=7), self.card("agy", n=8)], state)

        self.assertTrue(dispatcher.prune_queue_arrivals(state, [self.card("agy", n=7)]))
        self.assertEqual(list(state[engine.QUEUE_SINCE_KEY]), ["7"])
        self.assertFalse(dispatcher.prune_queue_arrivals(state, [self.card("agy", n=7)]))

    def test_the_departure_survives_the_state_round_trip(self):
        dispatcher = self.make({})
        state = {}
        dispatcher.mark_queue_arrivals([self.card("agy", n=7)], state)
        dispatcher.note_queue_departure(7, state)

        state = json.loads(json.dumps(state))    # the state file round trip (a restart)
        card = self.card("agy", n=7)
        self.assertTrue(dispatcher.mark_queue_arrivals([card], state))
        self.assertEqual(card["waited_s"], 0.0)

    def engine_tick(self, dispatcher):
        """One tick of the serve loop: the state is reloaded from the state file."""
        path = os.path.join(dispatcher.D, "state.json")
        state = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as stream:
                state = json.load(stream)
        dispatcher.tick({}, state)
        return state

    def test_a_card_that_comes_back_waits_again_across_the_ticks(self):
        dispatcher = self.make({})
        dispatcher.deps_met = lambda item: True
        dispatcher.probe = lambda lanes, state: {}
        dispatcher.harvest = lambda lanes, res, state: None
        dispatcher.sweep_leaked_inprogress = lambda state: None
        dispatcher.dispatch_reviews = lambda lanes, res, state, q: None
        queue = [self.card("agy", n=7)]
        dispatcher.queue = lambda: list(queue)

        self.engine_tick(dispatcher)              # the card enters the queue
        self.now = 1600.0
        queue.clear()                             # the card left the queue
        state = self.engine_tick(dispatcher)
        self.assertEqual(state[engine.QUEUE_SINCE_KEY], {})

        self.now = 1700.0
        back = self.card("agy", n=7)
        queue.append(back)
        dispatcher.queue = lambda: [back]
        self.engine_tick(dispatcher)              # the card is back
        self.assertEqual(back["waited_s"], 0.0)   # it waits for the grace period again

    def test_the_tick_forgets_the_cards_that_left_the_queue(self):
        dispatcher = self.make({})
        dispatcher.queue = lambda: []
        dispatcher.deps_met = lambda item: True
        dispatcher.probe = lambda lanes, state: {}
        dispatcher.harvest = lambda lanes, res, state: None
        dispatcher.sweep_leaked_inprogress = lambda state: None
        dispatcher.dispatch_reviews = lambda lanes, res, state, q: None

        dispatcher.tick({}, {engine.QUEUE_SINCE_KEY: {"7": 1000.0}})

        with open(os.path.join(dispatcher.D, "state.json"), encoding="utf-8") as stream:
            saved = json.load(stream)
        self.assertEqual(saved[engine.QUEUE_SINCE_KEY], {})

    def test_a_card_that_was_claimed_by_another_lane_forgets_its_arrival_time(self):
        dispatcher = self.make({})
        state = {}
        dispatcher.mark_queue_arrivals([self.card("agy", n=7)], state)
        dispatcher.label_state = lambda n: "stale"
        lanes = self.lanes("codex")

        dispatcher.assign_issues(lanes, {lane: {"lane": "idle"} for lane in lanes}, state,
                                 [self.card("agy", n=7)])

        self.assertNotIn("7", state[engine.QUEUE_SINCE_KEY])


if __name__ == "__main__":
    unittest.main()
