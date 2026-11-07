"""An idle lane must not keep zombie children.

The lane supervisor is a Linux subreaper, so it adopts the orphans of the executor: a
``git`` command that detaches its own background work (``git gc --auto`` forks a
background ``git gc`` and returns at once) leaves such an orphan behind.  Nobody waits
for it, so it turns into a zombie of the supervisor and the host's process table fills
up until a new ``git`` fails with "unable to create threaded lstat" (Refs #1817).

The tests drive the real idle poll of the lane serve with real ``git`` commands, and
the real ``lane serve`` supervisor runs through repeated poll rounds of the executor,
so the zombie count of the process under test (read out of ``ps``) is the evidence.
"""
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()
xs.__path__ = [str(ROOT)]

CLI = ROOT / "bin" / "openswarm"


def zombie_children(pid=None):
    """The zombie children of this process (the dead children nobody waited for).

    None when the platform cannot report the process table in the needed shape, so the
    caller can skip instead of reading a wrong answer.
    """
    parent = str(os.getpid() if pid is None else pid)
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,stat="], capture_output=True, text=True)
    if result.returncode:
        return None
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if not parts[0].isdigit():
            return None          # a header: this platform does not answer in this shape
        if parts[2].startswith("Z") and parts[1] == parent:
            found.append(int(parts[0]))
    return found


def require_ps(case):
    """Skip the test when the platform cannot report the process table."""
    if zombie_children() is None:
        case.skipTest("this platform cannot report the process table in the needed shape")


def spawn_zombie():
    """Fork a child that exits at once and is never waited for: a zombie of this process."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    for _ in range(200):
        found = zombie_children()
        if found is None:
            raise unittest.SkipTest("this platform cannot report the process table")
        if pid in found:
            return pid
        time.sleep(0.02)
    raise AssertionError(f"child {pid} did not become a zombie")


PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37


def child_subreaper():
    """Whether this process is a Linux subreaper (the lane supervisor is one)."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(PR_GET_CHILD_SUBREAPER, 0, 0, 0, 0))
    except (OSError, AttributeError):
        return False


def set_child_subreaper(enabled):
    """Turn this process into a Linux subreaper, like ``lane_become_subreaper`` does."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1 if enabled else 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def spawn_adopted_zombie():
    """The root cause of #1817: an orphan that the supervisor adopts and nobody waits for.

    A child forks a grandchild and exits at once, so the grandchild is orphaned and
    reparented to this (subreaper) process, exactly like the detached background
    ``git`` of the executor is reparented to the lane supervisor.  The orphan dies and
    stays a zombie of this process until somebody waits for it.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        grandchild = os.fork()
        if grandchild == 0:
            os._exit(0)
        os.write(write_fd, str(grandchild).encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    data = b""
    while True:
        chunk = os.read(read_fd, 32)
        if not chunk:
            break
        data += chunk
    os.close(read_fd)
    os.waitpid(pid, 0)
    grandchild = int(data.decode())
    for _ in range(200):
        found = zombie_children()
        if found is None:
            raise unittest.SkipTest("this platform cannot report the process table")
        if grandchild in found:
            return grandchild
        time.sleep(0.02)
    raise AssertionError(f"the adopted orphan {grandchild} did not become a zombie")


# The hook a real ``git commit`` runs: it leaves a background ``git`` behind, exactly
# like the detached background work of the executor (Refs #1817).
POST_COMMIT_HOOK = """#!/bin/sh
git --version >/dev/null 2>&1 &
exit 0
"""

# The fake pi RPC backend of the real serve test: it polls git in the lane worktree,
# leaves the orphan of #1817 behind on every poll round and then stays alive.
FAKE_PI = '''#!/usr/bin/env python3
"""A fake pi RPC backend: real git poll rounds, the orphan of #1817, then idle."""
import os
import subprocess
import time

MARKER = os.environ["OPENSWARM_ZOMBIE_MARKER"]
INTERVAL = float(os.environ.get("OPENSWARM_ZOMBIE_POLL_INTERVAL", "1.5"))


def poll_round(number):
    """One idle poll of the executor: a real git command that leaves an orphan behind.

    The `git commit` runs the post-commit hook of the lane repo, and the hook detaches a
    background `git` (the shape of `git gc --auto`): nobody waits for it, so the orphan
    dies and stays a zombie of the lane supervisor until the supervisor reaps it
    (Refs #1817).
    """
    subprocess.run(["git", "status", "--porcelain"], capture_output=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "poll %d" % number],
                   capture_output=True)
    with open(MARKER, "a", encoding="utf-8") as marker:
        marker.write("%d\\n" % number)
        marker.flush()


number = 0
while True:
    number += 1
    poll_round(number)
    time.sleep(INTERVAL)
'''


class LaneZombieReapTests(unittest.TestCase):
    """The idle poll of the supervisor reaps the dead children it adopted."""

    def setUp(self):
        require_ps(self)

    def tearDown(self):
        for pid in zombie_children() or []:
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass

    def test_idle_polling_leaves_no_zombie_child(self):
        spawn_zombie()
        self.assertEqual(len(zombie_children()), 1)

        for _ in range(5):
            xs.lane_idle_poll()

        self.assertEqual(zombie_children(), [])

    def test_idle_polling_keeps_a_live_child_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            xs.lane_idle_poll()
            self.assertIsNone(child.poll(), "the idle poll must not reap a live child")
        finally:
            child.kill()
            child.wait()

    def test_reaping_hands_the_exit_status_back_to_the_child_object(self):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
        child.wait()
        # A child object that still owns the status of a dead process keeps it.
        self.assertEqual(child.returncode, 7)

    def test_the_reaper_counts_the_reaped_children(self):
        spawn_zombie()
        self.assertEqual(xs.lane_reap_zombies(), 1)
        self.assertEqual(xs.lane_reap_zombies(), 0)

    def test_the_idle_poll_reaps_an_adopted_orphan(self):
        """The supervisor adopts the orphans of the executor, so it has to wait for them."""
        previous = child_subreaper()
        if not set_child_subreaper(True):
            self.skipTest("this platform has no subreaper")
        try:
            spawn_adopted_zombie()
            self.assertEqual(len(zombie_children()), 1)

            for _ in range(5):
                xs.lane_idle_poll()

            self.assertEqual(zombie_children(), [])
        finally:
            set_child_subreaper(previous)


class LaneIdleServeZombieTests(unittest.TestCase):
    """The real idle poll of the serve reaps the orphans of real git commands."""

    def setUp(self):
        # The subreaper is a Linux feature: without it the orphan of the executor is
        # reparented to the init of the host, so there is no zombie to reap (Refs #1817).
        if not xs.lane_become_subreaper():
            self.skipTest("this platform has no subreaper (Linux only)")
        require_ps(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-zombie-git-")
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", str(self.repo))
        self.git("-C", str(self.repo), "config", "user.email", "lane@example.com")
        self.git("-C", str(self.repo), "config", "user.name", "lane")
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.write_text(POST_COMMIT_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.restore_subreaper, child_subreaper())

    def tearDown(self):
        for pid in zombie_children() or []:
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass

    @staticmethod
    def restore_subreaper(previous):
        set_child_subreaper(previous)

    def git(self, *args):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True)

    def wait_for_zombie(self, timeout=5.0):
        """Wait until the orphan of the last git command died unwaited for."""
        end = time.time() + timeout
        while time.time() < end:
            if zombie_children():
                return True
            time.sleep(0.02)
        return False

    def test_a_real_git_command_leaves_an_orphan_behind(self):
        """The shape of #1817: a real git command leaves an orphan nobody waits for."""
        self.assertTrue(xs.lane_become_subreaper(), "the lane supervisor is a subreaper")
        self.git("-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "first")
        self.assertTrue(self.wait_for_zombie(), "the orphan of the git command never died")
        self.assertTrue(zombie_children())

    def test_the_idle_serve_poll_reaps_the_orphans_of_real_git_commands(self):
        """Every idle poll of the serve waits for the orphans the git commands left."""
        self.assertTrue(xs.lane_become_subreaper(), "the lane supervisor is a subreaper")
        for round_number in range(3):
            self.git("-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", f"round {round_number}")
            self.assertTrue(self.wait_for_zombie(),
                            f"the orphan of the git command of round {round_number} never died")
            self.assertTrue(zombie_children(), "the orphan is a zombie of this process")
            result = xs.lane_serve_poll()
            self.assertGreaterEqual(result["reaped"], 1)
            self.assertEqual(zombie_children(), [], "the idle poll left a zombie behind")

    def test_n_idle_polls_leave_no_zombie_child(self):
        """The acceptance of #1817: after N idle polls this process owns no zombie."""
        self.assertTrue(xs.lane_become_subreaper(), "the lane supervisor is a subreaper")
        rounds = 5
        for round_number in range(rounds):
            self.git("-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", f"round {round_number}")
            self.assertTrue(self.wait_for_zombie())
            xs.lane_serve_poll()
        self.assertEqual(zombie_children(), [])

    def test_the_idle_serve_poll_records_the_host_zombie_count(self):
        """The serve poll also refreshes the host's zombie count in the lane state."""
        with tempfile.TemporaryDirectory(prefix="oslane-zombie-state-") as tmp:
            state = pathlib.Path(tmp) / "state.json"
            with mock.patch.object(xs, "lane_state_path", return_value=str(state)), \
                    mock.patch.object(xs, "host_zombie_count", return_value=4600):
                result = xs.lane_serve_poll(name="M9", policy={"lane": {"zombie_warn_count": 200}},
                                            zombie_check=True)
            self.assertEqual(result["zombie_count"], 4600)
            self.assertIn("4600 zombie processes", result["zombie_warning"])
            recorded = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(recorded["zombie_count"], 4600)


class RealLaneServeZombieTests(unittest.TestCase):
    """The real `lane serve` supervisor keeps no zombie child (Refs #1817).

    The executor of the lane polls git in the lane worktree over and over, and every
    poll leaves the orphan of #1817 behind: a detached background `git` that nobody
    waits for, so the supervisor adopts it and has to reap it.  The test drives the
    real supervisor through several poll rounds and reads the zombie count of the
    supervisor process out of `ps`.
    """

    # The poll rounds the executor runs before the test looks at the process table.
    ROUNDS = 5
    # The seconds between two poll rounds of the fake executor.
    INTERVAL = 0.5

    def setUp(self):
        # The subreaper is a Linux feature: without it the serve never adopts the orphans
        # of the executor, so there is no zombie to reap (Refs #1817).
        if not xs.lane_become_subreaper():
            self.skipTest("this platform has no subreaper (Linux only)")
        require_ps(self)
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-zombie-serve-")
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.marker = self.root / "marker"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", str(self.repo))
        self.git("-C", str(self.repo), "config", "user.email", "lane@example.com")
        self.git("-C", str(self.repo), "config", "user.name", "lane")
        (self.repo / "README").write_text("lane\n", encoding="utf-8")
        self.git("-C", str(self.repo), "add", "-A")
        self.git("-C", str(self.repo), "commit", "-q", "-m", "first")
        # The hook every poll round fires: it detaches a background `git`, the shape of
        # the `git gc --auto` of the executor (Refs #1817).
        hook = self.repo / ".git" / "hooks" / "post-commit"
        hook.write_text(POST_COMMIT_HOOK, encoding="utf-8")
        hook.chmod(0o755)
        self.fake_pi = self.root / "fake-pi"
        self.fake_pi.write_text(FAKE_PI, encoding="utf-8")
        self.fake_pi.chmod(0o755)
        (self.home / "policy.local.json").write_text(
            json.dumps({"pi": {"require_isolation": False}}), encoding="utf-8")
        lanes = {"lanes": [{"name": "ZT", "backend": "pi", "model": "MiniMax-M3.1-Flash",
                            "provider": "minimax-cn", "repo": str(self.repo),
                            "binary": str(self.fake_pi)}]}
        (self.home / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)
        self.serve = None
        self.peak_zombies = 0

    def git(self, *args):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True)

    def start_serve(self):
        """Start the real lane serve supervisor and wait until it is idle."""
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.home)
        env["OPENSWARM_LANES_FILE"] = str(self.home / "lanes.json")
        env["OPENSWARM_ZOMBIE_MARKER"] = str(self.marker)
        env["OPENSWARM_ZOMBIE_POLL_INTERVAL"] = str(self.INTERVAL)
        self.serve = subprocess.Popen([sys.executable, str(CLI), "lane", "serve", "ZT"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, env=env, start_new_session=True)
        self.addCleanup(self.stop_serve)
        state = self.home / "lanes" / "ZT" / "state.json"
        end = time.time() + 60
        while time.time() < end:
            if self.serve.poll() is not None:
                raise AssertionError("the lane serve exited: " + (self.serve.stdout.read() or ""))
            try:
                recorded = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            if recorded.get("state") == "idle" and recorded.get("child_pid"):
                return recorded
            time.sleep(0.1)
        raise AssertionError("the lane serve did not become idle")

    def stop_serve(self):
        if self.serve is None or self.serve.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.serve.pid), signal.SIGTERM)
        except OSError:
            self.serve.terminate()
        try:
            self.serve.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.serve.kill()
        if self.serve.stdout is not None:
            self.serve.stdout.close()
        # The executor runs in its own session, so the supervisor's group does not hold it.
        child = self.lane_state().get("child_pid")
        if child:
            try:
                os.kill(int(child), signal.SIGTERM)
            except (OSError, ValueError):
                pass

    # ---------------------------------------------------------------- fixtures

    def rounds(self):
        """The number of poll rounds the fake executor has run."""
        try:
            return len(self.marker.read_text(encoding="utf-8").split())
        except OSError:
            return 0

    def lane_state(self):
        """The lane state the supervisor writes."""
        try:
            recorded = json.loads((self.home / "lanes" / "ZT" / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return recorded if isinstance(recorded, dict) else {}

    def zombies(self):
        """The zombie children of the supervisor, counted out of `ps`."""
        found = zombie_children(self.serve.pid)
        self.peak_zombies = max(self.peak_zombies, len(found))
        return found

    def wait_for_rounds(self, count, timeout=120):
        """Wait until the executor ran `count` poll rounds; watch the process table."""
        end = time.time() + timeout
        while time.time() < end:
            self.zombies()
            if self.rounds() >= count:
                return self.rounds()
            time.sleep(0.02)
        raise AssertionError(f"the executor ran only {self.rounds()} of {count} poll rounds")

    def wait_for_reaped(self, count, timeout=60):
        """Wait until the supervisor reaped `count` dead children."""
        end = time.time() + timeout
        while time.time() < end:
            reaped = int(self.lane_state().get("zombies_reaped") or 0)
            if reaped >= count:
                return reaped
            time.sleep(0.02)
        raise AssertionError(f"the supervisor reaped only {self.lane_state().get('zombies_reaped')} "
                             f"of {count} dead children")

    def wait_for_no_zombie(self, timeout=30):
        """Wait until the supervisor owns no zombie child any more."""
        end = time.time() + timeout
        while time.time() < end:
            if not self.zombies():
                return True
            time.sleep(0.02)
        return False

    # ---------------------------------------------------------------- tests

    def test_a_real_idle_serve_keeps_no_zombie_child(self):
        """The serve adopts the orphan of the executor and reaps it on its next poll."""
        state = self.start_serve()
        rounds = self.wait_for_rounds(1)
        self.assertGreaterEqual(rounds, 1, "the fake executor never left its orphan")
        # The serve polls every fraction of a second, so the adopted orphan is reaped.
        self.wait_for_reaped(rounds)
        self.assertTrue(self.wait_for_no_zombie(), "the idle lane serve kept a zombie child")
        self.assertEqual(self.zombies(), [])
        recorded = self.lane_state()
        self.assertIsInstance(recorded.get("zombie_count"), int)
        # The warning follows the threshold of the host: no warning below it.
        if recorded["zombie_count"] < 200:
            self.assertIsNone(recorded.get("zombie_warning"))
        self.assertEqual(state["state"], "idle")

    def test_the_idle_serve_reaps_every_round_of_git_polls(self):
        """The real serve keeps no zombie while its executor polls git over and over.

        This is the acceptance of #1817 driven through the real supervisor: several
        poll rounds leave several orphans behind, and the process table of the
        supervisor has to stay free of zombies.
        """
        self.start_serve()
        rounds = self.wait_for_rounds(self.ROUNDS)
        self.assertGreaterEqual(rounds, self.ROUNDS, "the executor did not poll often enough")
        # Every orphan of every round is reaped by the supervisor.
        reaped = self.wait_for_reaped(rounds)
        self.assertGreaterEqual(reaped, rounds,
                                "the supervisor did not reap the orphans of the poll rounds")
        self.assertTrue(self.wait_for_no_zombie(),
                        f"the idle lane serve kept a zombie child: {self.zombies()}")
        self.assertEqual(self.zombies(), [], "the idle lane serve kept a zombie child")
        # The zombies never pile up: the supervisor holds at most the orphans of the
        # poll interval, never one per round (a leak would grow with every round).
        self.assertLess(self.peak_zombies, rounds,
                        f"the supervisor piled up {self.peak_zombies} zombies")


class LaneChildWakeupTests(unittest.TestCase):
    """The SIGCHLD wakeup of the supervisor (Refs #1817).

    The supervisor is a Linux subreaper, so it adopts the orphans of the executor.  The
    wakeup tells it that a child died right away, so the orphan is reaped in the loop
    iteration instead of at the next poll.
    """

    def test_a_dead_child_wakes_the_supervisor_up(self):
        wakeup = xs.lane_child_wakeup()
        if not wakeup:
            self.skipTest("this platform has no SIGCHLD wakeup")
        try:
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            data, deadline = b"", time.time() + 5
            while time.time() < deadline and not data:
                try:
                    data = wakeup["read"].recv(64, socket.MSG_DONTWAIT)
                except (BlockingIOError, OSError):
                    time.sleep(0.01)
            self.assertTrue(data, "the SIGCHLD did not wake the supervisor up")
            xs.lane_drain_wakeup(wakeup)
        finally:
            xs.lane_close_wakeup(wakeup)

    def test_the_wakeup_is_removed_again(self):
        wakeup = xs.lane_child_wakeup()
        if not wakeup:
            self.skipTest("this platform has no SIGCHLD wakeup")
        xs.lane_close_wakeup(wakeup)
        self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_DFL)


class LaneZombieWarningTests(unittest.TestCase):
    """The lane records the host's zombie count and warns above the threshold."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oslane-zombie-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_threshold_is_two_hundred(self):
        self.assertEqual(xs.lane_zombie_warn_threshold({}), 200)
        self.assertEqual(xs.lane_zombie_warn_threshold({"lane": {}}), 200)

    def test_policy_overrides_the_threshold(self):
        self.assertEqual(xs.lane_zombie_warn_threshold({"lane": {"zombie_warn_count": 5}}), 5)

    def test_unusable_threshold_falls_back_to_the_default(self):
        for value in ("", "abc", -1, 0, None, True, 1.5):
            with self.subTest(value=value):
                self.assertEqual(xs.lane_zombie_warn_threshold({"lane": {"zombie_warn_count": value}}), 200)

    def test_the_lane_state_records_the_count_and_the_warning(self):
        with mock.patch.object(xs, "host_zombie_count", return_value=4600), \
                mock.patch.object(xs, "lane_state_path", return_value=str(self.root / "state.json")), \
                mock.patch.object(xs, "NOW", return_value=1700.0):
            count, warning = xs.lane_record_zombie_state("M9", {"lane": {"zombie_warn_count": 200}})
            self.assertEqual(count, 4600)
            self.assertIn("4600 zombie processes", warning)
            state = xs.jload(str(self.root / "state.json"), {})
            self.assertEqual(state["zombie_count"], 4600)
            self.assertIn("zombie processes", state["zombie_warning"])

    def test_a_healthy_host_records_no_warning(self):
        with mock.patch.object(xs, "host_zombie_count", return_value=0), \
                mock.patch.object(xs, "lane_state_path", return_value=str(self.root / "state.json")):
            count, warning = xs.lane_record_zombie_state("M9", {})
            self.assertEqual(count, 0)
            self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
