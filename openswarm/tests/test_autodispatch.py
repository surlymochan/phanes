import contextlib
import builtins
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
import warnings
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)

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
            "ssh: connect to host worker port 22: Connection reset by peer",
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


class SwitchAuditTests(unittest.TestCase):
    """Refs #1827: the AD audits who wrote the PAUSE*/EXCLUDE* switches.

    The switch files of the autodispatch directory are written by unknown sources: the audit
    records every change (created / deleted / changed) and flags the changes without an
    authorization entry. It only audits: it never removes nor restores a switch.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-switch-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.dir = self.root / "autodispatch"
        self.dir.mkdir()
        self.brain = self.root / "brain-log.md"
        self.brain.write_text("")
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.root / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        marker_dir = mock.patch.object(autodispatch, "D", str(self.dir))
        marker_dir.start()
        self.addCleanup(marker_dir.stop)
        brain_log = mock.patch.object(autodispatch, "BRAIN_LOG", str(self.brain))
        brain_log.start()
        self.addCleanup(brain_log.stop)

    def audit(self):
        return autodispatch.audit_switches()

    def rows(self):
        path = self.dir / "switch-audit.jsonl"
        if not path.exists(): return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def alerts(self):
        path = self.dir / "health.alert"
        if not path.exists(): return []
        return path.read_text(encoding="utf-8").splitlines()

    def tick(self):
        """One tick of the real loop, with the GitHub queue and the refill stubbed out."""
        with mock.patch.object(autodispatch, "queue", return_value=[]), \
                mock.patch.object(autodispatch, "replenish", return_value=[]):
            autodispatch.tick({}, {}, 0)

    def test_a_created_switch_writes_one_row(self):
        (self.dir / "PAUSE-DISPATCH").touch()

        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], "PAUSE-DISPATCH")
        self.assertEqual(rows[0]["action"], "created")
        self.assertEqual(rows[0]["before"], "")
        self.assertIn("owner", rows[0])
        self.assertIsNotNone(rows[0]["mtime"])
        self.assertEqual(len(self.rows()), 1)

    def test_a_deleted_switch_writes_one_row(self):
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()

        (self.dir / "PAUSE-M19").unlink()
        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "deleted")
        self.assertEqual(rows[0]["before"], "manual hold")
        self.assertEqual(rows[0]["after"], "")
        self.assertEqual(len(self.rows()), 2)

    def test_a_changed_switch_writes_one_row(self):
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()

        (self.dir / "PAUSE-M19").write_text("another hold\n")
        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "changed")
        self.assertEqual(rows[0]["before"], "manual hold")
        self.assertEqual(rows[0]["after"], "another hold")
        self.assertEqual(len(self.rows()), 2)

    def test_an_unchanged_switch_writes_no_row(self):
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()

        self.assertEqual(self.audit(), [])
        self.assertEqual(len(self.rows()), 1)

    def test_a_switch_with_auto_content_is_not_unattributed(self):
        (self.dir / "PAUSE-HOST-ib1").write_text("auto:cooldown:until=123\n")

        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["unattributed"])
        self.assertEqual(rows[0]["authorization"], "auto:")
        self.assertEqual(self.alerts(), [])

    def test_a_brain_order_attributes_the_row(self):
        self.brain.write_text("- 16:00 UTC+8 operator: 用户令：暂停 M19\n")
        (self.dir / "PAUSE-M19").touch()

        rows = self.audit()

        self.assertFalse(rows[0]["unattributed"])
        self.assertIn("用户令", rows[0]["authorization"])
        self.assertEqual(self.alerts(), [])

    def test_an_unattributed_switch_writes_the_health_alert(self):
        (self.dir / "PAUSE-M19").touch()

        rows = self.audit()

        self.assertTrue(rows[0]["unattributed"])
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("PAUSE-M19", alerts[0])
        self.assertIn("unattributed", alerts[0])

    def test_a_corrupt_baseline_rebuilds_without_faking_a_change(self):
        (self.dir / "PAUSE-M19").touch()
        (self.dir / "switch-audit-state.json").write_text("{not json")

        self.assertEqual(self.audit(), [])
        self.assertEqual(self.rows(), [])

        (self.dir / "PAUSE-NEW").touch()
        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], "PAUSE-NEW")
        self.assertEqual(rows[0]["action"], "created")

    def test_a_broken_audit_file_does_not_stop_the_tick(self):
        (self.dir / "switch-audit.jsonl").mkdir()
        (self.dir / "PAUSE-M19").touch()

        self.tick()

        self.assertTrue((self.dir / "lanes-status.json").exists())

    def test_an_unreadable_switch_directory_is_survivable(self):
        broken = self.root / "not-a-directory"
        broken.write_text("x")

        self.assertEqual(autodispatch.audit_switches(directory=str(broken)), [])

    def unreadable(self, name):
        """An `open` that fails for one switch file: the switch exists but cannot be read."""
        real_open = builtins.open

        def guarded(path, *args, **kwargs):
            if str(path).endswith(name): raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)
        return guarded

    def counting_stat(self, name, fail_from=None):
        """A counting `os.stat` for one switch file.

        The calls of the switch file are recorded; from the `fail_from`-th one on the stat
        fails (a permission error that must not be swallowed)."""
        real_stat = os.stat
        calls = []

        def guarded(path, *args, **kwargs):
            if str(path).endswith(name):
                calls.append(str(path))
                if fail_from is not None and len(calls) >= fail_from:
                    raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)
        return guarded, calls

    def baseline(self):
        path = self.dir / "switch-audit-state.json"
        return json.loads(path.read_text(encoding="utf-8"))["switches"]

    def test_a_read_failure_does_not_fake_a_deletion(self):
        """Refs #1827: a switch that cannot be read is not a deleted switch."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()  # the baseline knows the switch
        before = len(self.rows())
        alerts = len(self.alerts())

        with mock.patch("builtins.open", side_effect=self.unreadable("PAUSE-M19")):
            self.assertEqual(self.audit(), [])

        self.assertEqual(len(self.rows()), before)      # no fake `deleted` row
        self.assertEqual(len(self.alerts()), alerts)    # and no health alert about a fake change
        self.assertIn("PAUSE-M19", self.baseline())     # the baseline is kept as it was

    def test_the_recovery_after_a_read_failure_does_not_fake_a_creation(self):
        """Refs #1827: the recovery compares against the kept baseline, not against a gap."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()
        before = len(self.rows())

        with mock.patch("builtins.open", side_effect=self.unreadable("PAUSE-M19")):
            self.audit()
        self.assertEqual(len(self.rows()), before)

        # the switch is readable again and it did not change: there is nothing to audit
        self.assertEqual(self.audit(), [])
        self.assertEqual(len(self.rows()), before)

        # a real change after the recovery is audited against the preserved baseline
        (self.dir / "PAUSE-M19").write_text("another hold\n")
        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "changed")
        self.assertEqual(rows[0]["before"], "manual hold")
        self.assertEqual(rows[0]["after"], "another hold")

    def test_a_deletion_during_a_read_failure_is_audited_after_the_recovery(self):
        """Refs #1827: a real deletion is audited once, after the reads work again."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()

        with mock.patch("builtins.open", side_effect=self.unreadable("PAUSE-M19")):
            self.assertEqual(self.audit(), [])
        (self.dir / "PAUSE-M19").unlink()

        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], "PAUSE-M19")
        self.assertEqual(rows[0]["action"], "deleted")
        self.assertEqual(rows[0]["before"], "manual hold")

    def test_an_unreadable_directory_does_not_fake_a_deletion(self):
        """Refs #1827: a directory that cannot be listed is not an empty switch set."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()
        before = len(self.rows())
        alerts = len(self.alerts())

        with mock.patch("os.listdir", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(self.audit(), [])

        self.assertEqual(len(self.rows()), before)
        self.assertEqual(len(self.alerts()), alerts)
        self.assertIn("PAUSE-M19", self.baseline())

    def test_the_audit_runs_even_when_the_loop_is_paused(self):
        (self.dir / "PAUSE").touch()  # the `all` scope pauses the whole loop
        (self.dir / "PAUSE-M19").touch()

        self.tick()

        self.assertEqual([row["file"] for row in self.rows()], ["PAUSE", "PAUSE-M19"])

    def test_a_stat_failure_does_not_fake_a_deletion(self):
        """Refs #1827: a switch whose stat fails is not a deleted switch."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()  # the baseline knows the switch
        before = len(self.rows())
        alerts = len(self.alerts())
        guarded, calls = self.counting_stat("PAUSE-M19", fail_from=1)

        with mock.patch("os.stat", side_effect=guarded):
            self.assertEqual(self.audit(), [])

        self.assertEqual(len(calls), 1)                   # the set is unknown after one failed stat
        self.assertEqual(len(self.rows()), before)        # no fake `deleted` row
        self.assertEqual(len(self.alerts()), alerts)      # and no health alert about a fake change
        self.assertIn("PAUSE-M19", self.baseline())       # the baseline is kept as it was

    def test_the_recovery_after_a_stat_failure_does_not_fake_a_creation(self):
        """Refs #1827: the recovery compares against the kept baseline, not against a gap."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        self.audit()
        before = len(self.rows())
        guarded, _ = self.counting_stat("PAUSE-M19", fail_from=1)

        with mock.patch("os.stat", side_effect=guarded):
            self.audit()
        self.assertEqual(len(self.rows()), before)

        # the switch is stat-able again and it did not change: there is nothing to audit
        self.assertEqual(self.audit(), [])
        self.assertEqual(len(self.rows()), before)

        # a real change after the recovery is audited against the preserved baseline
        (self.dir / "PAUSE-M19").write_text("another hold\n")
        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "changed")
        self.assertEqual(rows[0]["before"], "manual hold")
        self.assertEqual(rows[0]["after"], "another hold")

    def test_a_switch_is_stat_ed_only_once_per_tick(self):
        """Refs #1827: the file type comes from the stat that was already done.

        A second stat (`os.path.isfile`) swallows its own OSError: the living switch would
        drop out of the set, which fakes a deletion and breaks the baseline."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        guarded, calls = self.counting_stat("PAUSE-M19")

        with mock.patch("os.stat", side_effect=guarded):
            self.audit()

        self.assertEqual(len(calls), 1)  # one stat per switch: there is no second one

    def test_a_living_switch_is_audited_when_a_second_stat_would_fail(self):
        """Refs #1827: a stat error of the second call must not drop the switch."""
        (self.dir / "PAUSE-M19").write_text("manual hold\n")
        guarded, calls = self.counting_stat("PAUSE-M19", fail_from=2)

        with mock.patch("os.stat", side_effect=guarded):
            rows = self.audit()

        self.assertEqual(len(calls), 1)   # the failing second stat is never made
        self.assertEqual(len(rows), 1)    # the switch is audited, not dropped
        self.assertEqual(rows[0]["action"], "created")

        (self.dir / "PAUSE-M19").write_text("another hold\n")
        rows = self.audit()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "changed")


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


class WorkClassCapTickTests(unittest.TestCase):
    """Refs #1864: the work class cap of the queue share policy is a real limit.

    The tick is the real one: only the GitHub/ssh/dispatch side effects are faked, so the
    category admission of `tools/lane-autodispatch/autodispatch.py` is the shipped one. The work
    class may hold at most `total - reserve` lanes; with no work slot left the work cards wait."""

    LANES = {"CX2": {"host": "h2", "backend": "codex"}, "CX10": {"host": "h10", "backend": "codex"}}
    WORK_BODY = "普通工作卡"
    VERIFY_BODY = "## Verify Intent\n目标 PR: #1785\n证据要求:\n- [ ] 测试结果\n- [ ] 复现步骤"

    def issue(self, number, body, labels=("lane-ready",)):
        return {"number": number, "title": f"card {number}", "body": body, "labels": list(labels)}

    def tick(self, issues, state=None):
        """One dispatch tick with the GitHub/ssh/dispatch side effects faked."""
        dispatched = []

        def fake_gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(issues), ""
            if args[:2] == ["issue", "view"]:
                number = int(args[2])
                labels = next((row["labels"] for row in issues if row["number"] == number), [])
                return 0, json.dumps({"number": number, "state": "OPEN",
                                      "labels": [{"name": name} for name in labels]}), ""
            return 0, "", ""

        def fake_dispatch(lane, cfg, item, base):
            dispatched.append((lane, item["n"]))
            return "run-1", None

        patched = ("D", "gh", "sh", "log", "host_ok", "host_fail", "host_good", "remote", "deps_met",
                   "dispatch", "replenish", "save_state", "status_snapshot")
        saved = {name: getattr(autodispatch, name) for name in patched}
        old_share = autodispatch.AD_VERIFY_SHARE_ENABLED
        autodispatch.AD_VERIFY_SHARE_ENABLED = True
        try:
            with tempfile.TemporaryDirectory() as scratch:
                autodispatch.D = scratch
                autodispatch.gh = fake_gh
                autodispatch.sh = lambda *a, **k: (0, "0" * 40 + "\n", "")
                autodispatch.log = lambda msg, brain=True: None
                autodispatch.host_ok = lambda host: True
                autodispatch.host_fail = lambda host, why: None
                autodispatch.host_good = lambda host: None
                autodispatch.remote = lambda lane, cfg, run="": {"lane": "idle"}
                autodispatch.deps_met = lambda item: True
                autodispatch.dispatch = fake_dispatch
                autodispatch.replenish = lambda k, state: []
                autodispatch.save_state = lambda state: None
                autodispatch.status_snapshot = lambda *a, **k: {}
                autodispatch.VERIFY_REJECT_LOGGED.clear()
                # tick() writes lanes-status.json without closing it: the warning is pre-existing
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", ResourceWarning)
                    autodispatch.tick(dict(self.LANES), dict(state or {}), 0)
        finally:
            autodispatch.AD_VERIFY_SHARE_ENABLED = old_share
            for name, value in saved.items():
                setattr(autodispatch, name, value)
        return dispatched

    def busy_work_state(self):
        """CX10 holds a work run: the work class is at its cap (2 lanes - 1 reserved = 1 lane)."""
        return {"CX10": {"issue": 9, "run": "run-9", "host": "h10", "base": "b" * 40,
                         "at": "2026-01-01 00:00:00", "verify": None, "class": "work"}}

    def test_a_work_card_is_not_dispatched_when_the_work_class_is_at_its_cap(self):
        dispatched = self.tick([self.issue(10, self.WORK_BODY)], self.busy_work_state())

        self.assertEqual(dispatched, [])

    def test_a_work_card_is_dispatched_while_the_work_class_has_a_slot(self):
        dispatched = self.tick([self.issue(10, self.WORK_BODY)])

        self.assertEqual(dispatched, [("CX2", 10)])

    def test_the_verify_card_still_takes_the_reserved_lane_under_the_cap(self):
        dispatched = self.tick([self.issue(1, self.VERIFY_BODY, ("lane-ready", "verify-intent"))],
                                self.busy_work_state())

        self.assertEqual(dispatched, [("CX2", 1)])


if __name__ == "__main__":
    unittest.main()
