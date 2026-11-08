"""The lanes-status health probe of the control plane (Refs #1697).

``~/phanes-ctl/autodispatch/lanes-status.json`` is the queue watermark of the control
plane: the pool level, the number of parallel lanes and every supervisor decision are
taken from it.  On 2026-11-02 the ``_at`` stamp froze at 22:13:20 while the dispatcher
kept dispatching, and no reader noticed.

The reader is ``tools/check-lanes-status-stall.py``: an offline probe that judges the
snapshot (fresh / expired, with the last ``_at``) and the liveness of its writer (the
heartbeat of the dispatcher loop and the pid file), and alarms on both.  The tests below
drive the probe and swrm fleet health as commands, with an isolated home, and
pin the contract the issue asks for:

* a frozen snapshot is reported as expired, with the last ``_at``, and the probe fails;
* a fresh snapshot is not an alarm;
* a dispatcher that is not alive any more is reported, even when its heartbeat is fresh;
* the probe reads the file the dispatcher writes, on the deployed path and on a
  configured one;
* the probe only reads: it never touches the dispatcher.
"""
import importlib.machinery
import importlib.util
import argparse
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "tools" / "check-lanes-status-stall.py"
AUTODISPATCH = REPO_ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

# The stamp the issue reports: the dispatcher froze there on 2026-11-02.
STALLED_AT = "2026-11-02 22:13:20"


def load_probe():
    """Load the probe as a module (its file name carries dashes)."""
    loader = importlib.machinery.SourceFileLoader("lanes_status_stall", str(PROBE))
    spec = importlib.util.spec_from_loader("lanes_status_stall", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_autodispatch(**environ):
    """Load the dispatcher with the given environment (the state dir is read at import)."""
    previous = {name: os.environ.get(name) for name in environ}
    os.environ.update({name: str(value) for name, value in environ.items()})
    try:
        loader = importlib.machinery.SourceFileLoader("autodispatch_health", str(AUTODISPATCH))
        spec = importlib.util.spec_from_loader("autodispatch_health", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_probe(arguments, home, environ=None):
    """Run the probe in an isolated home; returns the completed process."""
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("OPENSWARM_", "XSWARM_"))}
    environment["HOME"] = str(home)
    environment["TMPDIR"] = str(home)
    if environ:
        environment.update(environ)
    return subprocess.run([sys.executable, str(PROBE)] + list(arguments),
                          capture_output=True, text=True, timeout=30,
                          env=environment, cwd=str(REPO_ROOT))


def fixture(directory, stamp, heartbeat=True):
    """The deployed layout of the control plane in an isolated home."""
    home = pathlib.Path(directory)
    state = home / "phanes-ctl" / "autodispatch"
    state.mkdir(parents=True)
    snapshot = state / "lanes-status.json"
    snapshot.write_text(json.dumps({"_at": stamp, "_ready": 4, "_busy": 1, "_phase": "idle"}),
                        encoding="utf-8")
    if heartbeat:
        # the heartbeat of the dispatcher loop: it is fresh
        (state / "heartbeat").write_text(time.strftime("%F %T") + "\n", encoding="utf-8")
    return home, snapshot


def dead_pid():
    """A pid that is not running any more."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class LanesStatusHealthProbeTest(unittest.TestCase):
    """The health output of the control plane must judge the snapshot it reads."""

    def test_a_stalled_snapshot_is_reported_as_expired(self):
        """The 22:13 sample: the health output must mark the snapshot as expired."""
        with tempfile.TemporaryDirectory() as directory:
            home, snapshot = fixture(directory, STALLED_AT)
            done = run_probe(["--lanes-status-file", str(snapshot)], home)
            output = done.stdout + done.stderr
            self.assertNotEqual(done.returncode, 0,
                                f"a frozen snapshot must raise an alarm: {output}")
            self.assertIn("stale", output.lower(), f"the snapshot is not marked as expired: {output}")
            self.assertIn(STALLED_AT, output, f"the last _at is not named: {output}")

    def test_a_fresh_snapshot_is_not_an_alarm(self):
        """A snapshot the dispatcher just refreshed is not an alarm."""
        with tempfile.TemporaryDirectory() as directory:
            stamp = time.strftime("%F %T")
            home, snapshot = fixture(directory, stamp)
            done = run_probe(["--lanes-status-file", str(snapshot)], home)
            output = done.stdout + done.stderr
            self.assertEqual(done.returncode, 0, f"a fresh snapshot must not raise an alarm: {output}")
            self.assertIn("fresh", output.lower(), f"the verdict does not name the freshness: {output}")

    def test_a_dead_dispatcher_is_reported_even_with_a_fresh_heartbeat(self):
        """The heartbeat is not proof of life: a dead pid must be reported."""
        with tempfile.TemporaryDirectory() as directory:
            stamp = time.strftime("%F %T")
            home, snapshot = fixture(directory, stamp)
            record = json.dumps({"pid": dead_pid(), "started_at": time.time()})
            for path in (home / "phanes-ctl" / "state" / "autodispatch.pid",
                         home / "phanes-ctl" / "autodispatch" / "autodispatch.pid"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(record, encoding="utf-8")
            done = run_probe(["--lanes-status-file", str(snapshot)], home)
            output = done.stdout + done.stderr
            self.assertNotEqual(done.returncode, 0,
                                f"a dead dispatcher must raise an alarm: {output}")
            self.assertIn("dead", output.lower(), f"the dead dispatcher is not reported: {output}")

    def test_a_missing_snapshot_is_an_alarm(self):
        """Without a watermark the control plane has no pool level: that is an alarm."""
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            done = run_probe(["--lanes-status-file", str(home / "lanes-status.json")], home)
            output = done.stdout + done.stderr
            self.assertNotEqual(done.returncode, 0, f"a missing snapshot must raise an alarm: {output}")
            self.assertIn("missing", output.lower(), f"the missing snapshot is not reported: {output}")

    def test_plain_text_pid_is_checked_even_with_a_fresh_heartbeat(self):
        for pid, status in ((dead_pid(), "dead"), (os.getpid(), "alive")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                home, snapshot = fixture(directory, time.strftime("%F %T"))
                pid_file = snapshot.parent / "autodispatch.pid"
                pid_file.write_text(f"{pid}\n", encoding="utf-8")
                done = run_probe(["--json"], home)
                report = json.loads(done.stdout)
                self.assertEqual(report["writer"]["pid"], pid)
                self.assertIs(report["writer"]["pid_alive"], status == "alive")
                self.assertEqual(report["writer"]["status"], status)
                self.assertEqual(done.returncode, int(status == "dead"))
                self.assertEqual([alert["kind"] for alert in report["alerts"]],
                                 ["autodispatch-writer-dead"] if status == "dead" else [])

    def test_the_json_verdict_carries_the_snapshot_and_the_writer(self):
        """The verdict is machine readable: a monitor consumes the same judgement."""
        with tempfile.TemporaryDirectory() as directory:
            home, snapshot = fixture(directory, STALLED_AT)
            done = run_probe(["--lanes-status-file", str(snapshot), "--json"], home)
            report = json.loads(done.stdout)
            self.assertFalse(report["ok"])
            self.assertEqual(report["snapshot"]["status"], "stale")
            self.assertEqual(report["snapshot"]["at"], STALLED_AT)
            self.assertGreater(report["snapshot"]["age_seconds"], 180)
            self.assertEqual(report["writer"]["status"], "alive")
            self.assertTrue(report["alerts"])
            self.assertEqual(report["alerts"][0]["kind"], "lanes-snapshot-stale")

    def test_the_probe_reads_the_file_the_dispatcher_writes(self):
        """Reader and writer must name the same file: the deployed path by default."""
        probe = load_probe()
        dispatcher = load_autodispatch()
        default = os.path.join(os.path.expanduser("~"), "phanes-ctl", "autodispatch", "lanes-status.json")
        self.assertEqual(dispatcher.lanes_status_path(), default)
        self.assertEqual(str(probe.lanes_status_path()), default)

    def test_a_configured_snapshot_file_is_read_by_both_sides(self):
        """OPENSWARM_LANES_STATUS_FILE names the file for the writer AND the reader."""
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "elsewhere", "lanes-status.json")
            os.makedirs(os.path.dirname(target))
            with open(target, "w", encoding="utf-8") as stream:
                json.dump({"_at": time.strftime("%F %T"), "_ready": 2}, stream)
            dispatcher = load_autodispatch(OPENSWARM_LANES_STATUS_FILE=target)
            probe = load_probe()
            with mock.patch.dict(os.environ, {"OPENSWARM_LANES_STATUS_FILE": target}):
                self.assertEqual(dispatcher.lanes_status_path(), target)
                self.assertEqual(str(probe.lanes_status_path()), target)
                self.assertEqual(probe.snapshot_report(probe.lanes_status_path())["status"], "fresh")
            # the probe as a command reads the configured file, not the default one
            home = pathlib.Path(directory)
            done = run_probe(["--json"], home, {"OPENSWARM_LANES_STATUS_FILE": target})
            report = json.loads(done.stdout)
            self.assertEqual(report["snapshot"]["path"], target)
            self.assertEqual(report["snapshot"]["status"], "fresh")

    def test_the_probe_never_touches_the_dispatcher(self):
        """The probe is read-only: it starts, stops and rewrites nothing."""
        with tempfile.TemporaryDirectory() as directory:
            home, snapshot = fixture(directory, STALLED_AT)
            before = {path: path.stat().st_mtime_ns for path in home.rglob("*") if path.is_file()}
            run_probe(["--lanes-status-file", str(snapshot)], home)
            after = {path: path.stat().st_mtime_ns for path in home.rglob("*") if path.is_file()}
            self.assertEqual(before, after, "the probe wrote into the control plane home")


class FleetHealthIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import _load
        cls.swrm = _load.load()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home, self.snapshot = fixture(self.tmp.name, STALLED_AT)
        self.state = self.snapshot.parent
        self.environment = mock.patch.dict(os.environ, {
            "OPENSWARM_AD_STATE_DIR": str(self.state),
            "OPENSWARM_CTL_STATE_DIR": str(self.home / "state"),
            "OPENSWARM_LANES_STATUS_FILE": str(self.snapshot),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_existing_fleet_status_reports_stale_in_text_and_overall_json(self):
        for as_json in (False, True):
            with self.subTest(json=as_json), mock.patch.object(self.swrm, "fleet_config", return_value={}), \
                    mock.patch.object(self.swrm, "instabox_states", return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.swrm.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=as_json))
                if as_json:
                    result = json.loads(output.getvalue())
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "degraded")
                    self.assertEqual(result["control_plane"]["snapshot"]["at"], STALLED_AT)
                else:
                    self.assertIn("WARNING", output.getvalue())
                    self.assertIn("stale", output.getvalue())
                    self.assertIn(STALLED_AT, output.getvalue())

    def test_health_cli_detects_dead_writer_with_fresh_heartbeat(self):
        self.snapshot.write_text(json.dumps({"_at": time.strftime("%F %T")}))
        (self.state / "autodispatch.pid").write_text(json.dumps({"pid": dead_pid()}))
        environment = dict(os.environ, HOME=str(self.home), OPENSWARM_HOME=str(self.home / "swrm"))
        result = subprocess.run([sys.executable, str(REPO_ROOT / "swrm/bin/swrm"),
                                 "fleet", "health", "--json"], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["writer"]["status"], "dead")

    def test_health_cli_detects_plain_text_dead_pid_with_fresh_heartbeat(self):
        self.snapshot.write_text(json.dumps({"_at": time.strftime("%F %T")}))
        (self.state / "autodispatch.pid").write_text(f"{dead_pid()}\n")
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit_status:
            self.swrm.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=True, watch=None))
        self.assertEqual(exit_status.exception.code, 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report["ok"])
        self.assertIs(report["writer"]["pid_alive"], False)
        self.assertEqual(report["alerts"][0]["kind"], "autodispatch-writer-dead")

    def test_periodic_health_rechecks_after_snapshot_stalls_and_writer_exits(self):
        self.snapshot.write_text(json.dumps({"_at": time.strftime("%F %T")}))
        pid_file = self.state / "autodispatch.pid"
        pid_file.write_text(json.dumps({"pid": os.getpid()}))
        calls = []

        def next_period(seconds):
            calls.append(seconds)
            if len(calls) == 2:
                raise KeyboardInterrupt
            self.snapshot.write_text(json.dumps({"_at": STALLED_AT}))
            pid_file.write_text(json.dumps({"pid": dead_pid()}))

        with mock.patch.object(self.swrm.time, "sleep", side_effect=next_period), \
                contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(KeyboardInterrupt):
            self.swrm.cmd_fleet(argparse.Namespace(fleet_cmd="health", json=True, watch=60))
        first, second = map(json.loads, output.getvalue().splitlines())
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["snapshot"]["status"], "stale")
        self.assertEqual(second["writer"]["status"], "dead")
        self.assertEqual(calls, [60, 60])


class StandaloneFleetHealthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = pathlib.Path(cls.tmp.name)
        install = cls.root / "standalone"
        shutil.copytree(REPO_ROOT / "swrm", install,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        cls.binary = cls.root / "swrm"
        cls.binary.symlink_to(install / "bin" / "swrm")

    def test_installed_fleet_commands_without_repository_tools(self):
        self.assertFalse((self.root / "tools").exists())
        for status in ("fresh", "stale", "dead", "missing"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.root) as directory:
                stamp = STALLED_AT if status == "stale" else time.strftime("%F %T")
                home, snapshot = fixture(directory, stamp)
                if status == "missing":
                    snapshot.unlink()
                if status == "dead":
                    (snapshot.parent / "autodispatch.pid").write_text(f"{dead_pid()}\n")
                runtime = home / "runtime"
                runtime.mkdir()
                (runtime / "fleet.json").write_text('{"hosts": []}')
                environment = {name: value for name, value in os.environ.items()
                               if not name.startswith(("OPENSWARM_", "XSWARM_", "SWRM_", "PYTHONPATH"))}
                environment.update(HOME=str(home), TMPDIR=str(home), OPENSWARM_HOME=str(runtime))
                for command in ("health", "status"):
                    for as_json in (False, True):
                        with self.subTest(command=command, json=as_json):
                            argv = [sys.executable, str(self.binary), "fleet", command]
                            if as_json:
                                argv.append("--json")
                            result = subprocess.run(argv, env=environment, cwd=home,
                                                    capture_output=True, text=True, timeout=30)
                            expected_code = int(status != "fresh") if command == "health" else 0
                            self.assertEqual(result.returncode, expected_code, result.stderr)
                            self.assertNotIn("Traceback", result.stderr)
                            if as_json:
                                report = json.loads(result.stdout)
                                self.assertEqual(report["ok"], status == "fresh")
                                control = report if command == "health" else report["control_plane"]
                                expected_snapshot = "fresh" if status == "dead" else status
                                self.assertEqual(control["snapshot"]["status"], expected_snapshot)
                                if status == "dead":
                                    self.assertEqual(control["writer"]["status"], "dead")
                            elif status in ("stale", "dead"):
                                self.assertIn("WARNING", result.stdout)
                                self.assertIn(status, result.stdout)
                                if status == "stale":
                                    self.assertIn(STALLED_AT, result.stdout)


class mock_environ:
    """Temporarily set environment variables (a tiny stand-in for unittest.mock)."""

    def __init__(self, values):
        self.values = values
        self.previous = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.previous[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *exception):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


if __name__ == "__main__":
    unittest.main()
