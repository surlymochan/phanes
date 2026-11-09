"""Offline regression tests for the <host> disk watermark guard (Refs #1666).

#1666 asks for a disk usage probe and a watermark guard around the <host> lane
host; the reviews added four further requirements:

* the guard measures the *remote* <host> filesystem, not the filesystem of the
  control plane box (the box and <host> have very different watermarks);
* a failed probe is handled and is never reported as a healthy host;
* the dispatch gate stays closed from the block threshold until the recovery
  threshold is reached, the guard restores its own marker when another sweep
  clears it, and it never takes over or releases a marker owned by somebody
  else;
* the guard, its probe dependency and the resident loop are really deployed
  and started by the live control plane, and a re-install replaces the guard
  that is still running.

Every test is offline: a fake ``ssh`` on ``PATH`` answers the remote ``df``
probe and the control plane tree lives in a scratch directory inside the
worktree. The suite stays red until the #1666 implementation lands.
"""

import contextlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
GUARD = TOOLS / "disk-watermark-guard.py"
PROBE = TOOLS / "disk-usage-probe.py"
INSTALLER = TOOLS / "install-disk-watermark-guard.sh"
START_EXAMPLE = TOOLS / "lane-autodispatch" / "start-control-plane.example"
CTL_BOOT = ROOT / "docs" / "ctl-boot.md"
AUTODISPATCH = TOOLS / "lane-autodispatch" / "autodispatch.py"

HOST = "lane-host-a"
# The guard owns a separate namespace; health/operator markers remain shared.
PAUSE_MARKER = f".disk-watermark/PAUSE-HOST-{HOST}"

# df -Pk output of the fake remote host: blocked (95%), inside the
# block -> warn band (91%), recovery boundary (87%) and healthy (50%).
DF_HEADER = "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
DF_BLOCKED = DF_HEADER + "/dev/sda1 1000000 950000 50000 95% /\n"
DF_HELD = DF_HEADER + "/dev/sda1 1000000 910000 90000 91% /\n"
DF_RECOVERED = DF_HEADER + "/dev/sda1 1000000 870000 130000 87% /\n"
DF_HEALTHY = DF_HEADER + "/dev/sda1 1000000 500000 500000 50% /\n"


def require(path, what):
    """Fail with a readable message while the #1666 artefact is still missing."""
    path = Path(path)
    if not path.is_file():
        raise AssertionError(
            f"missing {path.relative_to(ROOT)}: {what} is not implemented yet (Refs #1666)")
    return path


def scratch_root():
    """The scratch directory of this suite, inside the worktree."""
    root = ROOT / ".tmp" / "disk-watermark-tests"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def scratch():
    """A scratch directory inside the worktree (never the system temp dir)."""
    with tempfile.TemporaryDirectory(dir=str(scratch_root())) as name:
        yield Path(name)


def log_lines(path):
    """The non-empty lines of a log file."""
    path = Path(path)
    if not path.is_file():
        return []
    return [line for line in path.read_text(errors="replace").splitlines() if line.strip()]


def pick_option(help_text, needle, skip=()):
    """The first --option of the help text that carries ``needle``."""
    for token in re.findall(r"--[A-Za-z][A-Za-z0-9-]*", help_text):
        if needle in token and not any(part in token for part in skip):
            return token
    return None


def guard_options():
    """The CLI options of the guard: the pause directory, the brain log, the path.

    The option names are discovered from ``--help`` instead of being pinned, so
    the tests pin the behaviour of the guard and not the spelling of its flags.
    """
    done = subprocess.run([sys.executable, str(GUARD), "--help"],
                          capture_output=True, text=True, timeout=60)
    text = done.stdout + done.stderr
    return {"pause": pick_option(text, "pause"),
            "brain": pick_option(text, "brain") or pick_option(text, "log", skip=("level",)),
            "path": pick_option(text, "path"),
            "help": text}


def run_guard(pause_dir, brain_log, env, extra=(), timeout=120):
    """Run the guard once against the fake remote host."""
    require(GUARD, "the disk watermark guard")
    options = guard_options()
    if not options["pause"] or not options["brain"]:
        raise AssertionError("the guard must accept a pause directory and a brain log "
                             f"option (help: {options['help']})")
    argv = [sys.executable, str(GUARD), options["pause"], str(pause_dir),
            options["brain"], str(brain_log)]
    if options["path"]:
        argv += [options["path"], "/"]
    argv.extend(extra)
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)


def run_probe(root, top=5):
    """Run the usage probe on ``root`` (the CLI spelling is discovered, not pinned)."""
    require(PROBE, "the disk usage probe")
    attempts = []
    for path_args in ([str(root)], ["--path", str(root)], ["--root", str(root)]):
        attempts.append(path_args + ["--top", str(top)])
        attempts.append(path_args)
    last = None
    for args in attempts:
        done = subprocess.run([sys.executable, str(PROBE)] + args,
                              capture_output=True, text=True, timeout=120)
        if done.returncode == 0:
            return done
        last = done
    raise AssertionError(f"the disk usage probe must run on {root}: {last.stderr}")


def parse_sizes(stdout):
    """The name -> bytes map of the probe rows ('<bytes> <path>')."""
    sizes = {}
    for line in stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\S+)\s*$", line)
        if match:
            sizes[Path(match.group(2)).name] = int(match.group(1))
    return sizes


@contextlib.contextmanager
def control_plane_home(home):
    """Point the dispatcher at ``home`` as its control plane root."""
    previous = {name: os.environ.get(name) for name in ("HOME", "OPENSWARM_HOME")}
    os.environ["HOME"] = str(home)
    os.environ["OPENSWARM_HOME"] = str(Path(home) / "state")
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def load_autodispatch():
    """Import the dispatcher (its control plane root comes from the environment)."""
    spec = importlib.util.spec_from_file_location("autodispatch_watermark_probe", AUTODISPATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSsh:
    """A fake ``ssh`` that answers the remote ``df`` probe of the guard."""

    def __init__(self, base):
        self.base = Path(base)
        self.bin = self.base / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.df_file = self.base / "df.out"
        self.fail_file = self.base / "ssh.fail"
        self.log_file = self.base / "ssh.log"
        self.set_df(DF_BLOCKED)
        script = self.bin / "ssh"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.log_file}"\n'
            f'if [ -f "{self.fail_file}" ]; then exit 255; fi\n'
            f'cat "{self.df_file}"\n'
        )
        script.chmod(0o755)

    def set_df(self, text):
        self.df_file.write_text(text)

    def set_failing(self, failing=True):
        if failing:
            self.fail_file.write_text("fail\n")
        elif self.fail_file.exists():
            self.fail_file.unlink()

    @property
    def env(self):
        env = dict(os.environ)
        env["DISK_WATERMARK_HOST"] = HOST
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return env

    def calls(self):
        return log_lines(self.log_file)


class DiskUsageProbeTests(unittest.TestCase):
    """The probe reports the filesystem use and the largest directories."""

    def test_probe_reports_the_usage_and_the_largest_directories(self):
        with scratch() as tmp:
            root = Path(tmp)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            (root / "alpha" / "payload").write_bytes(b"x" * 4096)
            done = run_probe(root)
            self.assertEqual(done.returncode, 0, done.stderr)
            for field in ("used_pct=", "used_bytes=", "free_bytes="):
                self.assertIn(field, done.stdout,
                              f"the probe must report {field}: {done.stdout!r}")
            self.assertIn(str(root / "alpha"), done.stdout,
                          f"the probe must list the largest directories: {done.stdout!r}")
            self.assertIn(str(root / "beta"), done.stdout,
                          f"the probe must list the largest directories: {done.stdout!r}")

    def test_probe_ranks_by_allocated_blocks_not_by_apparent_size(self):
        with scratch() as tmp:
            root = Path(tmp)
            sparse = root / "sparse"
            allocated = root / "allocated"
            sparse.mkdir()
            allocated.mkdir()
            with (sparse / "hole").open("wb") as handle:
                handle.truncate(64 * 1024 * 1024)
            with (allocated / "real").open("wb") as handle:
                handle.write(b"x" * (8 * 1024 * 1024))
            blocks = getattr(os.stat(sparse / "hole"), "st_blocks", 0) * 512
            if blocks > 8 * 1024 * 1024:
                self.skipTest("this filesystem does not support sparse files")
            done = run_probe(root)
            self.assertEqual(done.returncode, 0, done.stderr)
            sizes = parse_sizes(done.stdout)
            self.assertIn("sparse", sizes, f"the probe must report the directories: {done.stdout!r}")
            self.assertIn("allocated", sizes, f"the probe must report the directories: {done.stdout!r}")
            self.assertGreater(sizes["allocated"], sizes["sparse"],
                               f"the ranking must use the allocated blocks, not the apparent "
                               f"size: {sizes}")

    def test_probe_counts_files_below_the_old_depth_limit(self):
        with scratch() as tmp:
            root = Path(tmp)
            deep = root / "large" / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            (deep / "payload").write_bytes(b"x" * (1024 * 1024))
            (root / "small").mkdir()
            (root / "small" / "payload").write_bytes(b"x" * 65536)
            done = run_probe(root)
            self.assertEqual(done.returncode, 0, done.stderr)
            sizes = parse_sizes(done.stdout)
            self.assertGreaterEqual(sizes["large"], (deep / "payload").stat().st_blocks * 512)
            self.assertGreater(sizes["large"], sizes["small"])

    def test_probe_output_is_reproducible(self):
        with scratch() as tmp:
            root = Path(tmp)
            (root / "alpha").mkdir()
            (root / "alpha" / "payload").write_bytes(b"x" * 4096)
            first = parse_sizes(run_probe(root).stdout)
            second = parse_sizes(run_probe(root).stdout)
            self.assertEqual(first, second, "the probe output must be reproducible")


class GuardRemoteMeasurementTests(unittest.TestCase):
    """The guard measures the remote <host> filesystem, not the control plane."""

    def test_guard_measures_the_remote_host_and_pauses_it(self):
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            fake.set_df(DF_BLOCKED)
            pause_dir = base / "pause"
            brain_log = base / "brain.log"
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            calls = fake.calls()
            self.assertTrue(calls, "the guard must probe the remote host over ssh")
            self.assertTrue(any(HOST in call for call in calls),
                            f"the guard must measure {HOST}, the ssh calls were {calls}")
            self.assertTrue((pause_dir / PAUSE_MARKER).exists(),
                            f"a remote 95% watermark must pause {HOST}: "
                            f"stdout={done.stdout!r} stderr={done.stderr!r}")

    def test_a_healthy_remote_host_is_not_paused(self):
        # The control plane box and <host> have different watermarks: the remote
        # host reports 50% while the box may be nearly full, and the guard must
        # follow the remote number.
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            fake.set_df(DF_HEALTHY)
            pause_dir = base / "pause"
            brain_log = base / "brain.log"
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertFalse((pause_dir / PAUSE_MARKER).exists(),
                             f"a healthy remote host must not be paused: "
                             f"stdout={done.stdout!r} stderr={done.stderr!r}")

    def test_failed_probe_is_not_reported_as_healthy(self):
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            fake.set_failing(True)
            pause_dir = base / "pause"
            brain_log = base / "brain.log"
            done = run_guard(pause_dir, brain_log, fake.env)
            for stream in (done.stdout, done.stderr):
                self.assertNotIn("recovered", stream.lower(),
                                 f"a failed probe must not be reported as recovered: {stream!r}")
            rows = log_lines(brain_log)
            self.assertTrue(any("disk-watermark" in row.lower() for row in rows),
                            f"a failed probe must leave a brain log row: {rows}")
            paused = (pause_dir / PAUSE_MARKER).exists() or done.returncode != 0
            self.assertTrue(paused,
                            "a failed probe must fail closed (pause the host or fail loudly), "
                            f"stdout={done.stdout!r} stderr={done.stderr!r}")


class GuardHysteresisTests(unittest.TestCase):
    """Block, hold and recovery of the pause marker."""

    def test_the_gate_stays_closed_until_the_recovery_threshold_is_reached(self):
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            pause_dir = base / "pause"
            brain_log = base / "brain.log"
            marker = pause_dir / PAUSE_MARKER

            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(marker.exists(), f"the blocked run must pause {HOST}: {done.stdout!r}")
            body = marker.read_text()
            self.assertTrue(body.startswith("auto:"), f"the pause must be an automatic one: {body!r}")
            rows = log_lines(brain_log)
            self.assertTrue(any("disk-watermark" in row.lower() for row in rows),
                            f"the brain log must record the alert: {rows}")
            self.assertTrue(any("block" in row.lower() for row in rows),
                            f"the brain log must record the block: {rows}")

            # 91% is inside the block -> warn band: the lane stays paused and the
            # state must not be released yet.
            fake.set_df(DF_HELD)
            before = len(log_lines(brain_log))
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(marker.exists(),
                            f"the host must stay paused above the recovery threshold: {done.stdout!r}")
            fresh = log_lines(brain_log)[before:]
            self.assertFalse(any("recovered" in row.lower() for row in fresh),
                             f"the hold must not be reported as recovered: {fresh}")

            # At 87% the recovery threshold is reached: release our own marker.
            fake.set_df(DF_RECOVERED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertFalse(marker.exists(),
                             f"the recovery must release the pause of {HOST}: {done.stdout!r}")
            state = json.loads((pause_dir / f"disk-watermark-{HOST}.json").read_text())
            self.assertEqual(state["state"], "recovered")
            self.assertFalse(state["blocked"])
            self.assertTrue(any("state=recovered" in row for row in log_lines(brain_log)))

    def test_a_marker_cleared_by_another_sweep_is_restored(self):
        # The health sweep clears stale "auto:" host pauses
        # (tools/check-health-pause.py), so the guard has to restore its own
        # marker while the watermark is still too high.
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            pause_dir = base / "pause"
            brain_log = base / "brain.log"
            marker = pause_dir / PAUSE_MARKER

            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(marker.exists(), "the blocked run must pause the lane host")
            marker.unlink()

            fake.set_df(DF_HELD)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(marker.exists(),
                            "the guard must restore its pause marker while the watermark is "
                            f"still too high, otherwise the gate opens: stdout={done.stdout!r}")

    def test_the_marker_of_another_owner_is_neither_taken_over_nor_released(self):
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            pause_dir = base / "pause"
            brain_log = base / "brain.log"
            marker = pause_dir / f"PAUSE-HOST-{HOST}"
            pause_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text("auto:other-guard\n")

            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertTrue(marker.exists(), "the pause of the other guard must survive")
            self.assertIn("other-guard", marker.read_text(),
                          "the guard must not overwrite a pause it does not own")

            fake.set_df(DF_RECOVERED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertTrue(marker.exists(),
                            "the recovery must not release a pause owned by somebody else")
            self.assertIn("other-guard", marker.read_text(),
                          "the recovery must not release a pause owned by somebody else")


class DispatchGateTests(unittest.TestCase):
    """The dispatcher must not hand out <host> while the guard blocks it."""

    def test_state_write_failure_keeps_gate_closed(self):
        with scratch() as tmp, control_plane_home(tmp):
            home = Path(tmp)
            pause_dir = home / "phanes-ctl" / "autodispatch"
            spec = importlib.util.spec_from_file_location("guard_failure", GUARD)
            guard = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(guard)
            usage = {"used_pct": 95, "used_bytes": 95, "free_bytes": 5}
            with mock.patch.object(guard, "measure_remote", return_value=usage), \
                    mock.patch.object(guard, "write_state", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    guard.main(["--host", HOST, "--pause-dir", str(pause_dir),
                                "--brain-log", str(home / "brain.log")])
            dispatcher = load_autodispatch()
            self.assertTrue(dispatcher.lane_paused("CX2", {"host": HOST}))

    def test_recovery_write_failure_preserves_marker(self):
        with scratch() as tmp:
            base = Path(tmp)
            fake = FakeSsh(base)
            pause_dir = base / "pause"
            fake.set_df(DF_BLOCKED)
            self.assertEqual(run_guard(pause_dir, base / "brain.log", fake.env).returncode, 0)
            spec = importlib.util.spec_from_file_location("guard_recovery_failure", GUARD)
            guard = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(guard)
            usage = {"used_pct": 87, "used_bytes": 87, "free_bytes": 13}
            with mock.patch.object(guard, "measure_remote", return_value=usage), \
                    mock.patch.object(guard, "write_state", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    guard.main(["--host", HOST, "--pause-dir", str(pause_dir),
                                "--brain-log", str(base / "brain.log")])
            self.assertTrue((pause_dir / PAUSE_MARKER).exists())

    def test_the_dispatcher_holds_lane_host_while_the_guard_blocks_it(self):
        with scratch() as tmp, control_plane_home(tmp):
            home = Path(tmp)
            pause_dir = home / "phanes-ctl" / "autodispatch"
            brain_log = home / "phanes-ctl" / "brain.log"
            fake = FakeSsh(home)

            dispatcher = load_autodispatch()
            self.assertFalse(dispatcher.lane_paused("CX2", {"host": HOST}),
                             "the gate must be open while nothing blocks the host")

            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            dispatcher = load_autodispatch()
            self.assertIn(f"host:{HOST}", dispatcher.paused_scopes(),
                          "the dispatcher must see the pause of <host>")
            self.assertTrue(dispatcher.lane_paused("CX2", {"host": HOST}),
                            "the dispatcher must hold <host> while the disk watermark guard "
                            f"blocks it (stdout={done.stdout!r})")

            fake.set_df(DF_RECOVERED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            dispatcher = load_autodispatch()
            self.assertFalse(dispatcher.lane_paused("CX2", {"host": HOST}),
                             "the gate must open again after the recovery")

    def test_the_gate_holds_even_when_the_pause_marker_was_cleared(self):
        # The health sweep clears the stale `auto:` host pauses
        # (tools/check-health-pause.py), so the marker alone is not a gate: the
        # independent state file of the guard must hold the host in the window
        # between the clearing and the next guard tick.
        with scratch() as tmp, control_plane_home(tmp):
            home = Path(tmp)
            pause_dir = home / "phanes-ctl" / "autodispatch"
            brain_log = home / "phanes-ctl" / "brain.log"
            marker = pause_dir / PAUSE_MARKER
            fake = FakeSsh(home)

            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue(marker.exists(), "the blocked run must pause the lane host")

            # another sweep removes the marker, the disk is still too full
            marker.unlink()
            dispatcher = load_autodispatch()
            self.assertFalse((pause_dir / PAUSE_MARKER).exists(),
                             "the marker must be gone for this test")
            self.assertIn(f"host:{HOST}", dispatcher.paused_scopes(),
                          "the independent disk gate must hold the host without the marker")
            self.assertTrue(dispatcher.lane_paused("CX2", {"host": HOST}),
                            "the dispatcher must hold the host even without the pause marker")
            self.assertTrue(dispatcher.disk_watermark_blocked(HOST),
                            "the independent disk gate must report the block")

            # and the recovery opens the gate again
            fake.set_df(DF_RECOVERED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            dispatcher = load_autodispatch()
            self.assertFalse(dispatcher.lane_paused("CX2", {"host": HOST}),
                             "the gate must open again after the recovery")


class DispatchTickTests(unittest.TestCase):
    """The real dispatch tick must not hand a card to a host the guard blocks."""

    def test_the_tick_dispatches_no_card_to_the_blocked_host(self):
        with scratch() as tmp, control_plane_home(tmp):
            home = Path(tmp)
            pause_dir = home / "phanes-ctl" / "autodispatch"
            brain_log = home / "phanes-ctl" / "brain.log"
            fake = FakeSsh(home)
            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)

            dispatcher = load_autodispatch()
            dispatcher.BRAIN_LOG = str(brain_log)      # the tick logs into the scratch brain log
            dispatched = []
            card = {"n": 1666, "title": "card", "body": "body", "labels": [], "prio": 3,
                    "backend": None, "strict": False, "verify": False}
            lanes = {"CX2": {"host": HOST, "backend": "pi"},
                     "CX9": {"host": "other-host", "backend": "pi"}}
            # the outside world is faked: no ssh, no gh, no planner backlog
            dispatcher.remote = lambda lane, cfg, run="": {"lane": "idle"}
            dispatcher.queue = lambda: [dict(card)]
            dispatcher.label_state = lambda number, cache=None: ("fresh", "")
            dispatcher.prefetch_labels = lambda numbers, cache=None: {}
            dispatcher.replenish = lambda count, state: []
            dispatcher.gh = lambda *args, **kwargs: (0, "", "")
            dispatcher.sh = lambda *args, **kwargs: (0, "base-sha\n", "")
            dispatcher.dispatch = (lambda lane, cfg, item, base:
                                    (dispatched.append(lane) or "run-1", False))
            state = {}
            dispatcher.tick(lanes, state, time.time())
            self.assertNotIn("CX2", state,
                             "the dispatch tick must not give a card to a host the disk "
                             f"watermark guard blocks: {state}")
            self.assertNotIn("CX2", dispatched,
                             "the dispatch tick must not dispatch to a blocked host")
            self.assertIn("CX9", state,
                          "a lane on a healthy host must still get its card")

    def test_the_tick_dispatches_no_card_after_the_marker_was_cleared(self):
        # The health sweep clears the `auto:` host pause, so the tick must not
        # hand a card out in the window between the clearing and the next tick
        # of the guard: the independent disk gate holds the host.
        with scratch() as tmp, control_plane_home(tmp):
            home = Path(tmp)
            pause_dir = home / "phanes-ctl" / "autodispatch"
            brain_log = home / "phanes-ctl" / "brain.log"
            fake = FakeSsh(home)
            fake.set_df(DF_BLOCKED)
            done = run_guard(pause_dir, brain_log, fake.env)
            self.assertEqual(done.returncode, 0, done.stderr)
            (pause_dir / PAUSE_MARKER).unlink()
            # A stopped guard must keep the gate closed beyond the old 30-minute TTL.
            state_file = pause_dir / f"disk-watermark-{HOST}.json"
            record = json.loads(state_file.read_text())
            record["updated_at"] = time.time() - 1801
            state_file.write_text(json.dumps(record))

            dispatcher = load_autodispatch()
            self.assertTrue(dispatcher.lane_paused("CX2", {"host": HOST}))
            self.assertIn(f"host:{HOST}", dispatcher.paused_scopes())
            dispatcher.BRAIN_LOG = str(brain_log)
            dispatched = []
            card = {"n": 1666, "title": "card", "body": "body", "labels": [], "prio": 3,
                    "backend": None, "strict": False, "verify": False}
            lanes = {"CX2": {"host": HOST, "backend": "pi"},
                     "CX9": {"host": "other-host", "backend": "pi"}}
            dispatcher.remote = lambda lane, cfg, run="": {"lane": "idle"}
            dispatcher.queue = lambda: [dict(card)]
            dispatcher.label_state = lambda number, cache=None: ("fresh", "")
            dispatcher.prefetch_labels = lambda numbers, cache=None: {}
            dispatcher.replenish = lambda count, state: []
            dispatcher.gh = lambda *args, **kwargs: (0, "", "")
            dispatcher.sh = lambda *args, **kwargs: (0, "base-sha\n", "")
            dispatcher.dispatch = (lambda lane, cfg, item, base:
                                    (dispatched.append(lane) or "run-1", False))
            state = {}
            dispatcher.tick(lanes, state, time.time())
            self.assertNotIn("CX2", state,
                             "the tick must not give a card to a blocked host whose pause "
                             f"marker was cleared by another sweep: {state}")
            self.assertNotIn("CX2", dispatched,
                             "the tick must not dispatch to a blocked host without its marker")


class InstallerTests(unittest.TestCase):
    """The installer must deploy the guard, its probe and the resident loop."""

    START_FIXTURE = """#!/usr/bin/env bash
# Fake live start-control-plane of the test: it records the requested loops and
# only the disk watermark loop is really started (the other loops of the real
# script would touch the live control plane).
set -e
ctl="$(cd "$(dirname "$0")/.." && pwd)"
start_one() {
  local name="$1" cmd="$2"
  echo "start_one $name $cmd" >> "$ctl/started.log"
  case "$name" in
    lane-autodispatch)
      if [[ -x "$cmd" ]]; then
        nohup bash -c "exec $cmd" >>"$ctl/dispatcher.log" 2>&1 &
        echo $! >> "$ctl/started.pids"
      fi
      ;;
    *disk-watermark*)
      nohup bash -c "exec $cmd" >>"$ctl/loop.log" 2>&1 &
      echo $! >> "$ctl/started.pids"
      # The installer verifies the resident loop right after this script
      # returns, so the loop has to be visible in the process table by then.
      for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        pgrep -f "disk-watermark-guard" >/dev/null 2>&1 && break
        sleep 0.1
      done
      ;;
  esac
}
start_one ib-refresh-loop "$ctl/bin/ctl-loop"
start_one lane-autodispatch "$ctl/bin/lane-autodispatch"
start_one lane-merge "$ctl/bin/lane-merge"
start_one bus-worker-loop "$ctl/bin/bus-worker-loop"
echo "status:"
"""

    def setUp(self):
        self.addCleanup(self.kill_resident_guards)

    def build_control_plane(self, ctl):
        """The live control plane tree of the test (never the real one)."""
        ctl = Path(ctl)
        (ctl / "bin").mkdir(parents=True, exist_ok=True)
        (ctl / "autodispatch").mkdir(parents=True, exist_ok=True)
        start = ctl / "bin" / "start-control-plane"
        start.write_text(self.START_FIXTURE)
        start.chmod(0o755)
        return ctl

    def run_installer(self, ctl, home, env):
        require(INSTALLER, "the disk watermark guard installer")
        environment = dict(env)
        environment["HOME"] = str(home)
        environment["DISK_WATERMARK_HOST"] = HOST
        environment["DISK_WATERMARK_BRAIN_LOG"] = str(Path(home) / "workspace" / "brain" / "log.md")
        return subprocess.run(["bash", str(INSTALLER), str(ctl)],
                              capture_output=True, text=True, env=environment, timeout=180)

    def test_installer_deploys_the_guard_with_its_probe_and_starts_the_loop(self):
        with scratch() as tmp:
            base = Path(tmp)
            home = base / "home"
            ctl = self.build_control_plane(home / "phanes-ctl")
            fake = FakeSsh(base)
            fake.set_df(DF_BLOCKED)
            done = self.run_installer(ctl, home, fake.env)
            self.assertEqual(done.returncode, 0,
                             f"the installer must succeed: {done.stdout}\n{done.stderr}")
            guard = ctl / "bin" / "disk-watermark-guard.py"
            probe = ctl / "bin" / "disk-usage-probe.py"
            self.assertTrue(guard.is_file(), "the installer must deploy the guard")
            self.assertTrue(probe.is_file(),
                            "the installer must deploy the probe dependency of the guard")
            self.assertTrue(os.access(guard, os.X_OK), "the deployed guard must be executable")
            self.assertTrue(os.access(probe, os.X_OK), "the deployed probe must be executable")
            started = log_lines(ctl / "started.log")
            self.assertTrue(any("disk-watermark" in row for row in started),
                            f"the installer must start the resident guard loop: {started}")
            # The resident loop must really measure the remote host and block it.
            self.assertTrue(self.wait_for(lambda: fake.calls()),
                            "the resident guard must measure the remote host")
            self.assertTrue(self.wait_for(lambda: list(ctl.rglob(PAUSE_MARKER))),
                            f"the resident guard must pause {HOST}: {done.stdout}\n{done.stderr}")
            brain_log = home / "workspace" / "brain" / "log.md"
            self.assertTrue(self.wait_for(lambda: any("state=block" in row
                                                    for row in log_lines(brain_log))),
                            "the resident guard must write the alert into the configured brain log")
            self.assertFalse((ctl / "brain.log").exists())

    def test_installer_replaces_the_guard_that_is_still_running(self):
        with scratch() as tmp:
            base = Path(tmp)
            home = base / "home"
            ctl = self.build_control_plane(home / "phanes-ctl")
            fake = FakeSsh(base)
            fake.set_df(DF_BLOCKED)
            old_guard = ctl / "bin" / "disk-watermark-guard.py"
            old_guard.write_text("import time\ntime.sleep(600)\n")
            old = subprocess.Popen(
                [sys.executable, str(old_guard), "--loop",
                 "--pause-dir", str(ctl / "autodispatch"), "--brain-log", str(ctl / "brain.log")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self.kill_pid, old.pid)
            time.sleep(0.5)
            self.assertTrue(self.alive(old.pid), "the fake old guard must be running")
            done = self.run_installer(ctl, home, fake.env)
            self.assertEqual(done.returncode, 0,
                             f"the installer must succeed: {done.stdout}\n{done.stderr}")
            deadline = time.time() + 20
            while time.time() < deadline and self.alive(old.pid):
                time.sleep(0.2)
            self.assertFalse(self.alive(old.pid),
                             "the installer must stop the guard that is still running, "
                             "otherwise the updated guard never takes over")
            self.assertTrue(self.wait_for(lambda: fake.calls()),
                            "the new resident guard must measure the remote host")

    def test_the_installer_deploys_the_dispatcher_with_the_disk_gate(self):
        # The independent disk gate lives in the dispatcher: the live AD must run
        # the new code, and the AD that is still running must be replaced.
        with scratch() as tmp:
            base = Path(tmp)
            home = base / "home"
            ctl = self.build_control_plane(home / "phanes-ctl")
            fake = FakeSsh(base)
            fake.set_df(DF_BLOCKED)
            live = ctl / "bin" / "lane-autodispatch"
            live.write_text('#!/usr/bin/env python3\n"""Phanes lane autodispatcher (the old copy)."""\n'
                            "import time\ntime.sleep(600)\n")
            live.chmod(0o755)
            # Keep the real deployed dispatcher offline while it runs its loop.
            (ctl / "autodispatch" / "PAUSE").write_text("test fixture\n")
            old = subprocess.Popen([sys.executable, str(live)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self.kill_pid, old.pid)
            time.sleep(0.5)
            self.assertTrue(self.alive(old.pid), "the fake old dispatcher must be running")
            done = self.run_installer(ctl, home, fake.env)
            self.assertEqual(done.returncode, 0,
                             f"the installer must succeed: {done.stdout}\n{done.stderr}")
            self.assertIn("DISK_WATERMARK_STATE_PREFIX", live.read_text(),
                          "the installer must deploy the dispatcher with the independent disk gate")
            deadline = time.time() + 20
            while time.time() < deadline and self.alive(old.pid):
                time.sleep(0.2)
            self.assertFalse(self.alive(old.pid),
                             "the installer must stop the dispatcher that is still running, "
                             "otherwise it keeps the old code without the disk gate")
            self.assertTrue(self.wait_for(lambda: (ctl / "autodispatch" / "heartbeat").exists()),
                            "the updated dispatcher must actually run, not just stop the old one")

    def test_installer_rejects_an_unknown_restart_entry_before_stopping_dispatcher(self):
        with scratch() as tmp:
            base = Path(tmp)
            home = base / "home"
            ctl = self.build_control_plane(home / "phanes-ctl")
            fake = FakeSsh(base)
            live = ctl / "bin" / "autodispatch.py"
            source = ('#!/usr/bin/env python3\n"""Phanes lane autodispatcher (the old copy)."""\n'
                      "import time\ntime.sleep(600)\n")
            live.write_text(source)
            old = subprocess.Popen([sys.executable, str(live)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self.kill_pid, old.pid)
            time.sleep(0.5)
            done = self.run_installer(ctl, home, fake.env)
            self.assertNotEqual(done.returncode, 0,
                                f"an unknown restart entry must fail: {done.stdout}\n{done.stderr}")
            self.assertIn("FAIL:", done.stderr)
            self.assertTrue(self.alive(old.pid),
                            "preflight must preserve the running dispatcher if restart is unknown")
            self.assertEqual(live.read_text(), source)

    def test_installer_fails_if_the_start_script_does_not_restart_dispatcher(self):
        with scratch() as tmp:
            base = Path(tmp)
            home = base / "home"
            ctl = self.build_control_plane(home / "phanes-ctl")
            fake = FakeSsh(base)
            live = ctl / "bin" / "lane-autodispatch"
            live.write_text('#!/usr/bin/env python3\n"""Phanes lane autodispatcher (the old copy)."""\n'
                            "import time\ntime.sleep(600)\n")
            live.chmod(0o755)
            start = ctl / "bin" / "start-control-plane"
            start.write_text(self.START_FIXTURE.replace('if [[ -x "$cmd" ]]; then', 'if false; then'))
            old = subprocess.Popen([sys.executable, str(live)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self.kill_pid, old.pid)
            time.sleep(0.5)
            done = self.run_installer(ctl, home, fake.env)
            self.assertNotEqual(done.returncode, 0,
                                f"a failed restart must fail installation: {done.stdout}\n{done.stderr}")
            self.assertIn("dispatcher did not come back", done.stderr)

    def alive(self, pid):
        # A child that exited stays in the process table as a zombie until its parent
        # reaps it, and os.kill(pid, 0) still succeeds for a zombie: the old guard is
        # reaped here, otherwise "the old guard still runs" would stay true forever.
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def kill_pid(self, pid):
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, signal_number)
            except OSError:
                return
            time.sleep(0.2)
            if not self.alive(pid):
                return

    def wait_for(self, predicate, timeout=20):
        """Wait until the predicate is true; the resident loop needs a moment."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def brain_rows(self, ctl):
        """The brain log rows of the control plane tree that name the guard."""
        rows = []
        for path in Path(ctl).rglob("*"):
            if not path.is_file() or path.stat().st_size > 1000000:
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            rows.extend(line for line in text.splitlines() if "disk-watermark" in line.lower())
        return rows

    def resident_guard_pids(self):
        """The resident guard processes of this test run (never a real one)."""
        found = []
        proc = Path("/proc")
        if not proc.is_dir():
            return found
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if "disk-watermark-guard.py" in cmdline and str(scratch_root()) in cmdline:
                found.append(int(entry.name))
        return found

    def kill_resident_guards(self):
        pids = set(self.resident_guard_pids())
        for entry in scratch_root().rglob("started.pids"):
            for line in log_lines(entry):
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
        for pid in pids:
            self.kill_pid(pid)


class ControlPlaneWiringTests(unittest.TestCase):
    """The control plane must start the guard loop."""

    def test_the_start_script_template_starts_the_disk_watermark_loop(self):
        require(START_EXAMPLE, "the control plane start script template")
        text = START_EXAMPLE.read_text()
        self.assertIn("disk-watermark", text,
                      "the control plane start script must start the disk watermark guard")

    def test_default_alerts_use_the_dispatcher_brain_log(self):
        dispatcher = load_autodispatch()
        spec = importlib.util.spec_from_file_location("disk_guard_default_log", GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        environment = dict(os.environ)
        environment.pop("DISK_WATERMARK_BRAIN_LOG", None)
        environment["DISK_WATERMARK_HOST"] = HOST
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(guard, "run_once", return_value=0) as run_once:
            self.assertEqual(guard.main([]), 0)
        self.assertEqual(run_once.call_args.args[0].brain_log, dispatcher.BRAIN_LOG)
        for path in (INSTALLER, START_EXAMPLE):
            self.assertIn("${DISK_WATERMARK_BRAIN_LOG:-" + dispatcher.BRAIN_LOG + "}",
                          path.read_text(), f"{path} must wire the actual dispatcher brain log")

    def test_the_lane_host_is_required_not_built_in(self):
        environment = dict(os.environ)
        environment.pop("DISK_WATERMARK_HOST", None)
        done = subprocess.run([sys.executable, str(GUARD)], capture_output=True, text=True,
                              env=environment, timeout=60)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--host", done.stderr)
        text = START_EXAMPLE.read_text()
        self.assertNotIn("--host <", text, "a <...> placeholder is a shell redirection")

    def test_ctl_boot_documents_the_guard_and_its_probe(self):
        require(CTL_BOOT, "the control plane boot documentation")
        text = CTL_BOOT.read_text()
        self.assertIn("disk-watermark", text,
                      "the boot documentation must describe the disk watermark guard")
        self.assertIn("disk-usage-probe", text,
                      "the boot documentation must describe the disk usage probe")


if __name__ == "__main__":
    unittest.main()
