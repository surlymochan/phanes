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


class HostBackoffTiers(unittest.TestCase):
    """Refs #1689: the host backoff is tiered -- short retries, then long ones."""

    def setUp(self):
        autodispatch.BACKOFF.clear()
        self.addCleanup(autodispatch.BACKOFF.clear)
        self.logs = []
        original = autodispatch.log
        autodispatch.log = lambda *args, **kwargs: self.logs.append(args[0] if args else "")
        self.addCleanup(setattr, autodispatch, "log", original)

    def clock(self, now):
        """Patch the shim clock for the duration of the block."""
        return mock.patch.object(autodispatch.time, "time", lambda: now[0])

    def test_the_soft_tier_waits_a_short_interval(self):
        now = [1000.0]
        with self.clock(now):
            autodispatch.host_fail("box", "Connection closed")
        state = autodispatch.BACKOFF["box"]
        self.assertEqual(state["n"], 1)
        self.assertEqual(state["tier"], "soft")
        self.assertEqual(state["wait"], 30)
        self.assertEqual(state["until"], 1030.0)
        self.assertEqual(state["probe_at"], 1030.0)

    def test_the_streak_switches_to_the_hard_tier(self):
        now = [1000.0]
        with self.clock(now):
            for _ in range(3):
                autodispatch.host_fail("box", "Connection closed")
                now[0] += 1
        state = autodispatch.BACKOFF["box"]
        self.assertEqual(state["n"], 3)
        self.assertEqual(state["tier"], "hard")
        self.assertEqual(state["wait"], 240)
        self.assertEqual(state["until"], 1002.0 + 240)

    def test_one_success_clears_the_streak(self):
        now = [1000.0]
        with self.clock(now):
            for _ in range(3):
                autodispatch.host_fail("box", "Connection closed")
                now[0] += 1
            autodispatch.host_good("box")
            self.assertNotIn("box", autodispatch.BACKOFF)
            autodispatch.host_fail("box", "Connection closed")
        state = autodispatch.BACKOFF["box"]
        self.assertEqual(state["n"], 1)
        self.assertEqual(state["tier"], "soft")
        self.assertEqual(state["wait"], 30)

    def test_the_hard_tier_keeps_a_probe_but_no_dispatch(self):
        now = [1000.0]
        with self.clock(now):
            for _ in range(3):
                autodispatch.host_fail("box", "Connection closed")
                now[0] += 1
            window = autodispatch.BACKOFF["box"]["until"]
            now[0] = window - 1
            self.assertFalse(autodispatch.host_ok("box"))
            now[0] = window
            # the low-frequency probe of the hard tier is due, the dispatch stays closed
            self.assertTrue(autodispatch.host_ok("box"))
            self.assertFalse(autodispatch.host_dispatchable("box"))
            # the probe succeeds: the streak is cleared and the dispatch resumes at once
            autodispatch.host_good("box")
            self.assertTrue(autodispatch.host_dispatchable("box"))
            self.assertTrue(autodispatch.host_ok("box"))

    def test_the_status_snapshot_publishes_the_backoff_state(self):
        now = [1000.0]
        with self.clock(now):
            autodispatch.host_fail("box", "Connection closed")
            status = autodispatch.status_snapshot({"A1": {"host": "box"}}, {},
                                                   {"A1": {"error": "host in backoff"}}, 0, 0)
        row = status["_host_backoff"]["box"]
        self.assertEqual(row["streak"], 1)
        self.assertEqual(row["tier"], "soft")
        self.assertEqual(row["wait_s"], 30)
        self.assertEqual(row["next_probe_at"], 1030.0)
        self.assertEqual(row["probe_due_in_s"], 30)
        self.assertFalse(row["dispatchable"])

    def test_a_lane_that_answered_clears_the_streak_of_the_host(self):
        """A lane of the host that died while another lane answered must not extend the backoff.

        The host is judged by its ssh results, not by its lanes: one answer is enough
        to clear the streak and to open the dispatch window again (Refs #1689).
        """
        now = [1000.0]
        with self.clock(now):
            for _ in range(3):
                autodispatch.host_fail("box", "Connection closed")
                now[0] += 1
            self.assertEqual(autodispatch.BACKOFF["box"]["tier"], "hard")
            now[0] = autodispatch.BACKOFF["box"]["until"]      # the low-frequency probe is due
            # the probe of the host: A1 answers, A2 dies -- the host answered
            results = {"A1": {"lane": "idle"}, "A2": {"error": "Connection closed"}}
            self.assertTrue(autodispatch.host_reachable(results))
            autodispatch.host_good("box")
        self.assertNotIn("box", autodispatch.BACKOFF)
        self.assertTrue(autodispatch.host_dispatchable("box"))

    def test_a_host_whose_lanes_all_died_keeps_its_streak(self):
        now = [1000.0]
        with self.clock(now):
            results = {"A1": {"error": "Connection closed"}, "A2": {"error": "ssh status timeout"}}
            self.assertFalse(autodispatch.host_reachable(results))
            autodispatch.host_fail("box", "Connection closed")
        self.assertEqual(autodispatch.BACKOFF["box"]["n"], 1)
        self.assertEqual(autodispatch.BACKOFF["box"]["tier"], "soft")

    def test_the_shim_agrees_with_the_shared_backoff_module(self):
        """The shim mirrors openswarm/lib/dispatch/ssh_backoff.py -- keep both in sync."""
        module = _load_ssh_backoff()

        for streak in range(1, 13):
            self.assertEqual(autodispatch.backoff_tier(streak), module.tier(streak), streak)
            self.assertEqual(autodispatch.backoff_seconds(streak), module.backoff_seconds(streak), streak)

    def test_the_shim_agrees_with_the_shared_host_verdict(self):
        """The host verdict of the shim mirrors the shared module (Refs #1689)."""
        module = _load_ssh_backoff()
        cases = ({"A1": {"lane": "idle"}, "A2": {"error": "Connection closed"}},
                 {"A1": {"error": "Connection closed"}, "A2": {"error": "ssh status timeout"}},
                 {"A1": {"error": "remote status exc: boom"}},
                 {"A1": {"error": "host in backoff", "skip": True}},
                 {})
        for results in cases:
            self.assertEqual(autodispatch.host_reachable(results), module.host_reachable(results), results)


class BoxJitterReplay(unittest.TestCase):
    """The jitter of the box of 2026-11-02, replayed through the real tick of the shim.

    The timeline is the one the issue reports: the box dispatched at 20:09 and at 20:15,
    then its ssh flapped with "Connection closed" until the streak climbed into the hard
    tier -- the old ladder reached 58 at 21:27 -- while the box kept answering now and
    then.  The tick is 30 s and the runs of the box finish right away, so every tick in
    which the host is dispatchable is a dispatch window.
    """

    TICK_S = 30
    BASE = 1_762_000_000.0        # 20:00 of the day of the jitter
    #: (tick, answer) change points: "ok" the ssh session worked, "closed" it died with
    #: "Connection closed", "mixed" one lane of the host answered and the other one died.
    TIMELINE = ((0, "ok"),        # 20:00:00 the box is alive
                (19, "closed"),   # 20:09:30 the ssh starts to flap
                (30, "mixed"),    # 20:15:00 one lane answers, the other one dies
                (31, "closed"),   # 20:15:30 the ssh flaps again
                (158, "ok"),      # 21:19:00 the low-frequency probe finds the box alive
                (159, "closed"),  # 21:19:30 the ssh flaps again
                (186, "ok"))      # 21:33:00 the next low-frequency probe finds it back
    LAST_TICK = 192
    HARD_TIER_START = 34          # 20:17:00, the streak of the second flap reaches the hard tier
    HARD_TIER_END = 158           # 21:19:00, the probe that finds the box alive again
    HARD_TIER_TICK = 100          # 21:00:00, a tick in the middle of the hard tier

    def setUp(self):
        autodispatch.BACKOFF.clear()
        self.addCleanup(autodispatch.BACKOFF.clear)
        self.logs = []
        original = autodispatch.log
        autodispatch.log = lambda *args, **kwargs: self.logs.append(args[0] if args else "")
        self.addCleanup(setattr, autodispatch, "log", original)
        self.tmp = tempfile.mkdtemp(prefix="ad-jitter-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name, value in (("D", self.tmp), ("BRAIN_LOG", os.path.join(self.tmp, "brain.md"))):
            original_value = getattr(autodispatch, name)
            setattr(autodispatch, name, value)
            self.addCleanup(setattr, autodispatch, name, original_value)

    def tick_of(self, moment):
        """The tick index of a moment of the replay."""
        return int((moment - self.BASE) // self.TICK_S)

    def answer(self, tick):
        """The answer of the box at a tick of the replay."""
        state = "ok"
        for index, value in self.TIMELINE:
            if index > tick:
                break
            state = value
        return state

    def test_the_jitter_of_the_day_keeps_the_probes_and_the_dispatch_windows(self):
        """The real tick of the shim replays the jitter of the day (Refs #1689).

        The hard tier keeps a low-frequency probe, and the first answer clears the
        streak, so the dispatch window opens again in the very same tick instead of
        being lost.
        """
        clock = [self.BASE]
        probes, dispatched, streaks = [], [], []
        lanes = {"A1": {"host": "box", "backend": "pi"}, "A2": {"host": "box", "backend": "pi"}}

        def remote(lane, _cfg, _run=""):
            tick = self.tick_of(clock[0])
            if not probes or probes[-1] != tick:
                probes.append(tick)
            answer = self.answer(tick)
            if answer == "ok":
                return {"lane": "idle"}
            if answer == "mixed":                 # one lane of the host answered, the other died
                return {"lane": "idle"} if lane == "A1" else {"error": "Connection closed"}
            return {"error": "Connection closed"}

        def dispatch(lane, _cfg, item, _base):
            dispatched.append((self.tick_of(clock[0]), lane, item["n"]))
            return f"run-{item['n']}", "verify"

        for name, value in (("remote", remote), ("dispatch", dispatch),
                            ("queue", lambda: [{"n": n, "title": f"card {n}", "body": "", "backend": None,
                                                "strict": False, "prio": 1} for n in range(1, 60)]),
                            ("read_issue", lambda _n: ("fresh", "")),
                            ("replenish", lambda _k, _state: []),
                            ("gh", lambda _args, timeout=60: (0, "[]", "")),
                            ("sh", lambda _cmd, timeout=120, inp=None: (0, "0" * 40 + "\trefs/heads/main", ""))):
            original = getattr(autodispatch, name)
            setattr(autodispatch, name, value)
            self.addCleanup(setattr, autodispatch, name, original)

        state, status = {}, {}
        with mock.patch.object(autodispatch.time, "time", lambda: clock[0]):
            for tick in range(self.LAST_TICK):
                clock[0] = self.BASE + tick * self.TICK_S
                autodispatch.tick(lanes, state, 0)
                for lane in [L for L in state if not L.startswith("_")]:
                    state.pop(lane)               # the run of the box finishes right away
                streaks.append((autodispatch.BACKOFF.get("box") or {}).get("n", 0))
                if tick == self.HARD_TIER_TICK:
                    with open(os.path.join(self.tmp, "lanes-status.json"), encoding="utf-8") as stream:
                        status = json.load(stream)
        ticks = [tick for tick, _lane, _n in dispatched]
        # the dispatch windows of the alive box are not lost: 20:09 and 20:15 dispatched
        self.assertIn(18, ticks)                  # 20:09:00
        self.assertIn(30, ticks)                  # 20:15:00 (one lane answered, the other died)
        # the hard tier keeps a low-frequency probe: the ticks in between are skipped
        hard_probes = [p for p in probes if self.HARD_TIER_START <= p <= self.HARD_TIER_END]
        self.assertTrue(hard_probes, "the hard tier must keep a probe")
        self.assertLess(len(hard_probes), 12)
        # the gap between two probes is at least the first wait of the hard tier (240s)
        self.assertGreaterEqual(min(b - a for a, b in zip(hard_probes, hard_probes[1:])), 8)
        # the probe that finds the box alive again opens the dispatch window in the same tick
        self.assertIn(158, ticks)                 # 21:19:00, the low-frequency probe of the hard tier
        self.assertIn(186, ticks)                 # 21:33:00, the box is back (still down at 21:27)
        # the streak never climbed into the dozens: one answer clears it
        self.assertLessEqual(max(streaks), 10)
        # at 21:27 the box was still flapping (the streak of the old ladder reached 58 there)
        self.assertNotIn(174, ticks)
        self.assertGreaterEqual(streaks[174], 3)
        # the backoff state is visible in the lane status
        row = status["_host_backoff"]["box"]
        self.assertEqual(row["tier"], "hard")
        self.assertGreaterEqual(row["streak"], 3)
        self.assertGreater(row["next_probe_at"], self.BASE + self.HARD_TIER_TICK * self.TICK_S)
        self.assertGreater(row["probe_due_in_s"], 0)
        self.assertFalse(row["dispatchable"])


def _load_ssh_backoff():
    """The shared SSH backoff policy module (lib/dispatch/ssh_backoff.py)."""
    path = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "ssh_backoff.py"
    spec = importlib.util.spec_from_file_location("openswarm_dispatch_ssh_backoff_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
