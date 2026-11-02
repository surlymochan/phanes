import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _load  # noqa: E402  (the CLI, loaded with an isolated OPENSWARM_HOME)

ENGINE = pathlib.Path(__file__).resolve().parent.parent / "lib" / "dispatch" / "engine.py"


def load_engine():
    """The openswarm dispatch engine, loaded by path (it loads its own modules lazily)."""
    spec = importlib.util.spec_from_file_location("dispatch_engine_under_test", str(ENGINE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SICK_DISK = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
             "/dev/root 1000 970 30 97% /\n")
HEALTHY_DISK = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/root 1000 500 500 50% /\n")
INODES = ("Filesystem Inodes IUsed IFree IUse% Mounted on\n"
          "/dev/root 1000 100 900 10% /\n")
MEMORY = ("              total used free shared buff/cache available\n"
          "Mem: 8000 3000 2000 100 3000 5000\n")


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


class FleetHealthPauseIntegrationTests(unittest.TestCase):
    """Refs #1367: `openswarm fleet health --apply` holds a host the AD honours.

    The health check writes the host pause marker into the health marker dir
    below the openswarm home (``$OPENSWARM_HOME/health``); the lane
    autodispatcher reads the marker from the same place, so a sick host gets no
    lane dispatched to it.
    """

    def setUp(self):
        self.xs = _load.load()
        self.now = 1700000000.0
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-health-ad-")
        self.addCleanup(self.tmp.cleanup)
        old_dir = autodispatch.D
        autodispatch.D = self.tmp.name
        self.addCleanup(setattr, autodispatch, "D", old_dir)
        # Every test gets an openswarm home of its own: the holds of one test
        # (the pause records, the markers) never reach the next one.
        self.home = tempfile.TemporaryDirectory(prefix="openswarm-fleet-health-home-")
        self.addCleanup(self.home.cleanup)
        previous = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = self.home.name
        self.addCleanup(self.restore_home, previous)

    def restore_home(self, previous):
        if previous is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = previous

    def runner(self, disk):
        """A fake probe runner: the host answers with the given disk usage."""
        outputs = {"df -P /": disk, "df -Pi /": INODES, "free -m": MEMORY}

        def run(host, command):
            return outputs[command]
        return run

    def health(self, disk, now, apply=True):
        """Run `fleet health --apply` against the fake runner and return the report."""
        args = argparse.Namespace(hosts=None, json=True, apply=apply)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.xs.cmd_fleet_health(args, runner=self.runner(disk),
                                            hosts={"worker-a": {"name": "worker-a", "kind": "local"}},
                                            now=now, home=self.home.name)

    def marker(self, host="worker-a"):
        return pathlib.Path(self.home.name, "health", "PAUSE-HOST-" + host)

    def shared_marker(self, host="worker-a"):
        return pathlib.Path(self.home.name, "PAUSE-HOST-" + host)

    def store(self):
        return self.xs.dispatch_pause_module().PauseStore(self.home.name)

    def test_a_host_paused_by_the_fleet_health_check_is_not_dispatched_to(self):
        report = self.health(SICK_DISK, self.now)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertTrue(self.marker().exists())
        self.assertTrue(self.marker().read_text(encoding="utf-8").startswith("auto:health"))
        self.assertTrue(autodispatch.host_paused("worker-a"))
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        self.assertTrue(autodispatch.host_dispatchable("worker-b"))

    def test_the_pause_survives_the_pause_store_ttl_until_the_host_recovers(self):
        store = self.store()
        self.health(SICK_DISK, self.now)
        self.assertTrue(store.is_paused(host="worker-a", now=self.now))
        # The host is still sick at the next check: the pause stays in force.
        later = self.now + 3600
        report = self.health(SICK_DISK, later)
        self.assertEqual(report["hosts"][0]["action"], "renewed")
        self.assertTrue(store.is_paused(host="worker-a", now=later))
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        # The host recovered: the pause ends, the host is dispatchable again.
        recovered = later + 3600
        report = self.health(HEALTHY_DISK, recovered)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(store.is_paused(host="worker-a", now=recovered))
        self.assertFalse(self.marker().exists())
        self.assertTrue(autodispatch.host_dispatchable("worker-a"))

    def test_the_pause_does_not_expire_before_the_host_recovers(self):
        # The health check owns the pause: the record has no expiry, so the
        # pause cannot lapse while the host is still sick - not even when the
        # health check does not run again.
        store = self.store()
        self.health(SICK_DISK, self.now)
        self.assertIsNone(store.active(self.now)[0]["expires_at"])
        later = self.now + 30 * 24 * 3600
        self.assertTrue(store.is_paused(host="worker-a", now=later))
        self.assertTrue(self.marker().exists())
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        # The host recovered: the pause ends.
        report = self.health(HEALTHY_DISK, later)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(store.is_paused(host="worker-a", now=later))
        self.assertFalse(self.marker().exists())
        self.assertTrue(autodispatch.host_dispatchable("worker-a"))

    def test_a_foreign_marker_does_not_hide_the_health_pause(self):
        # Another program holds the host (its own marker) and the host is sick:
        # the health check takes a hold of its own.  When that other program
        # removes its marker, the health hold still keeps the host out of the
        # dispatch until the host recovered.
        self.shared_marker().write_text("operator hold\n", encoding="utf-8")
        report = self.health(SICK_DISK, self.now)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertEqual(self.shared_marker().read_text(encoding="utf-8"), "operator hold\n")
        self.assertTrue(self.marker().exists())
        # The other program removes its own marker: the health hold stays.
        self.shared_marker().unlink()
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        # The host recovered: the health check releases its hold.
        report = self.health(HEALTHY_DISK, self.now + 3600)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(self.marker().exists())
        self.assertTrue(autodispatch.host_dispatchable("worker-a"))

    def test_the_health_hold_outlives_the_manual_pause_of_the_host(self):
        # The operator pauses the host through the pause store and the health
        # check finds the host sick: the health check keeps a hold of its own.
        # The operator removes their pause: the host stays out of the dispatch
        # and the pause store keeps reporting it as paused until it recovers.
        store = self.store()
        store.pause("host:worker-a", reason="operator: maintenance", now=self.now)
        report = self.health(SICK_DISK, self.now)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        scopes = {row["scope"]: row["reason"] for row in store.active(self.now)}
        self.assertEqual(scopes["host:worker-a"], "operator: maintenance")
        self.assertTrue(scopes["host:worker-a@health"].startswith("auto:health"))
        # The operator releases their own pause: the health hold stays.
        store.resume("host:worker-a")
        self.assertTrue(store.is_paused(host="worker-a", now=self.now))
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        self.assertTrue(self.marker().exists())
        # The host recovered: the health check releases its hold.
        report = self.health(HEALTHY_DISK, self.now + 3600)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(store.is_paused(host="worker-a", now=self.now + 3600))
        self.assertFalse(self.marker().exists())
        self.assertTrue(autodispatch.host_dispatchable("worker-a"))


class FleetHealthPauseDirTests(unittest.TestCase):
    """Refs #1367: OPENSWARM_PAUSE_DIR names one marker directory for every program.

    The health check writes the host marker into it, and the openswarm dispatch
    engine and the lane autodispatcher read it from there.
    """

    def setUp(self):
        self.xs = _load.load()
        self.now = 1700000000.0
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-health-dir-")
        self.addCleanup(self.tmp.cleanup)
        self.pause_dir = os.path.join(self.tmp.name, "pauses")
        os.makedirs(self.pause_dir)
        old_dir = autodispatch.D
        autodispatch.D = os.path.join(self.tmp.name, "autodispatch")
        self.addCleanup(setattr, autodispatch, "D", old_dir)
        previous = os.environ.get("OPENSWARM_PAUSE_DIR")
        os.environ["OPENSWARM_PAUSE_DIR"] = self.pause_dir
        self.addCleanup(self.restore_pause_dir, previous)

    def restore_pause_dir(self, previous):
        if previous is None:
            os.environ.pop("OPENSWARM_PAUSE_DIR", None)
        else:
            os.environ["OPENSWARM_PAUSE_DIR"] = previous

    def runner(self, disk):
        outputs = {"df -P /": disk, "df -Pi /": INODES, "free -m": MEMORY}

        def run(host, command):
            return outputs[command]
        return run

    def health(self, disk, now):
        args = argparse.Namespace(hosts=None, json=True, apply=True)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.xs.cmd_fleet_health(args, runner=self.runner(disk),
                                            hosts={"worker-a": {"name": "worker-a", "kind": "local"}},
                                            now=now)

    def marker(self, host="worker-a"):
        return pathlib.Path(self.pause_dir, "health", "PAUSE-HOST-" + host)

    def shared_marker(self, host="worker-a"):
        return pathlib.Path(self.pause_dir, "PAUSE-HOST-" + host)

    def dispatcher(self, engine):
        """A dispatch engine over a temporary state dir."""
        config = {"repo": "acme/project", "local_repo": self.tmp.name,
                  "state_dir": os.path.join(self.tmp.name, "state"),
                  "ssh": {"control_dir": os.path.join(self.tmp.name, "cm")}, "local_hosts": []}
        path = os.path.join(self.tmp.name, "cfg.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
        out = open(os.devnull, "w")
        self.addCleanup(out.close)
        return engine.Dispatcher(engine.load_config(path),
                                 runner=lambda *args, **kwargs: (0, "", ""), out=out)

    def test_the_health_check_holds_the_host_in_the_shared_pause_dir(self):
        report = self.health(SICK_DISK, self.now)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertTrue(self.marker().exists())
        # The lane autodispatcher reads the marker from the same directory.
        self.assertTrue(autodispatch.host_paused("worker-a"))
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        # So does the openswarm dispatch engine.
        engine = load_engine()
        dispatcher = self.dispatcher(engine)
        lane = {"host": "worker-a", "backend": "codex"}
        self.assertTrue(dispatcher.paused_lane("CX1", lane))
        # The host recovered: the health check releases the hold everywhere.
        report = self.health(HEALTHY_DISK, self.now + 3600)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(self.marker().exists())
        self.assertFalse(dispatcher.paused_lane("CX1", lane))
        self.assertTrue(autodispatch.host_dispatchable("worker-a"))

    def test_a_foreign_marker_in_the_shared_pause_dir_does_not_hide_the_health_hold(self):
        # The marker of another program lives in the shared pause dir; the
        # health check writes its own marker below it and releases only that
        # one when the host recovered.
        self.shared_marker().write_text("operator hold\n", encoding="utf-8")
        report = self.health(SICK_DISK, self.now)
        self.assertEqual(report["hosts"][0]["action"], "paused")
        self.assertEqual(self.shared_marker().read_text(encoding="utf-8"), "operator hold\n")
        engine = load_engine()
        dispatcher = self.dispatcher(engine)
        lane = {"host": "worker-a", "backend": "codex"}
        self.assertTrue(dispatcher.paused_lane("CX1", lane))
        # The other program removes its marker: the health hold keeps the host
        # out of the dispatch.
        self.shared_marker().unlink()
        self.assertTrue(dispatcher.paused_lane("CX1", lane))
        self.assertFalse(autodispatch.host_dispatchable("worker-a"))
        # The host recovered: the health hold ends, the other marker is gone
        # already and is never touched.
        report = self.health(HEALTHY_DISK, self.now + 3600)
        self.assertEqual(report["hosts"][0]["action"], "resumed")
        self.assertFalse(self.marker().exists())
        self.assertFalse(dispatcher.paused_lane("CX1", lane))
        self.assertTrue(autodispatch.host_dispatchable("worker-a"))


class HostPauseTests(unittest.TestCase):
    """Refs #1367: a health check holds a host with PAUSE-HOST-<host>; the AD skips it."""

    def test_a_host_held_by_a_pause_host_marker_is_not_dispatchable(self):
        with tempfile.TemporaryDirectory() as directory:
            old = autodispatch.D
            autodispatch.D = directory
            try:
                self.assertTrue(autodispatch.host_dispatchable("ib1"))
                pathlib.Path(directory, "PAUSE-HOST-ib1").write_text("auto:health disk 97% used\n")
                self.assertTrue(autodispatch.host_paused("ib1"))
                self.assertFalse(autodispatch.host_dispatchable("ib1"))
                self.assertTrue(autodispatch.host_dispatchable("ib2"))
            finally:
                autodispatch.D = old

    def test_a_host_held_in_the_openswarm_home_is_not_dispatchable(self):
        with tempfile.TemporaryDirectory() as directory, \
                tempfile.TemporaryDirectory() as openswarm_home:
            old = autodispatch.D
            autodispatch.D = directory
            previous = os.environ.get("OPENSWARM_HOME")
            os.environ["OPENSWARM_HOME"] = openswarm_home
            try:
                self.assertTrue(autodispatch.host_dispatchable("ib1"))
                pathlib.Path(openswarm_home, "PAUSE-HOST-ib1").write_text("auto:health disk 97% used\n")
                self.assertTrue(autodispatch.host_paused("ib1"))
                self.assertFalse(autodispatch.host_dispatchable("ib1"))
                self.assertTrue(autodispatch.host_dispatchable("ib2"))
            finally:
                autodispatch.D = old
                if previous is None:
                    os.environ.pop("OPENSWARM_HOME", None)
                else:
                    os.environ["OPENSWARM_HOME"] = previous

    def test_the_pause_dir_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as directory:
            old = autodispatch.D
            autodispatch.D = directory
            previous = os.environ.get(autodispatch.PAUSE_DIR_ENV)
            os.environ[autodispatch.PAUSE_DIR_ENV] = directory
            try:
                pathlib.Path(directory, "PAUSE-HOST-ib1").write_text("auto:health disk 97% used\n")
                self.assertTrue(autodispatch.host_paused("ib1"))
                self.assertFalse(autodispatch.host_dispatchable("ib1"))
            finally:
                autodispatch.D = old
                if previous is None:
                    os.environ.pop(autodispatch.PAUSE_DIR_ENV, None)
                else:
                    os.environ[autodispatch.PAUSE_DIR_ENV] = previous

    def test_a_host_in_ssh_backoff_is_not_dispatchable(self):
        with tempfile.TemporaryDirectory() as directory:
            old = autodispatch.D
            autodispatch.D = directory
            try:
                autodispatch.BACKOFF["ib1"] = {"n": 1, "until": time.time() + 600}
                self.assertFalse(autodispatch.host_dispatchable("ib1"))
                self.assertTrue(autodispatch.host_dispatchable("ib2"))
            finally:
                autodispatch.BACKOFF.pop("ib1", None)
                autodispatch.D = old


if __name__ == "__main__":
    unittest.main()
