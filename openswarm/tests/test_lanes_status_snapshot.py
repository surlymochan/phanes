"""The lanes-status snapshot the dispatcher publishes (Refs #1697).

The snapshot is the control plane's queue watermark: it must be written atomically,
it must be refreshed while the tick works (not only at its end) and a failed write
must be logged instead of silently dropped.
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

AUTODISPATCH = HERE.parents[1] / "tools" / "lane-autodispatch" / "autodispatch.py"
STALE_AFTER = 180


def load_autodispatch(**environ):
    """Load the dispatcher with the given environment.

    The state dir is read at import time, so the module is loaded again for every
    configuration under test (the reader and the writer must agree on the path).
    """
    names = list(environ)
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update({name: str(value) for name, value in environ.items()})
    try:
        spec = importlib.util.spec_from_file_location("autodispatch_snapshot", AUTODISPATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# The dispatcher in its default configuration: the deployed paths are the ones under test.
ad = load_autodispatch(OPENSWARM_AD_STATE_DIR="", OPENSWARM_LANES_STATUS_FILE="")


class FakeClock:
    """A clock the test moves by hand: ten minutes pass in milliseconds."""

    def __init__(self, start):
        self.now = start
        self.lock = threading.Lock()

    def time(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds
            return self.now

    def strftime(self, fmt, *args):
        with self.lock:
            moment = self.now
        return time.strftime(fmt, time.localtime(moment))

    def sleep(self, seconds):
        self.advance(seconds)

    def mktime(self, stamp):
        return time.mktime(stamp)

    def strptime(self, text, fmt):
        return time.strptime(text, fmt)


class SnapshotWriteTests(unittest.TestCase):
    """The write itself: atomic, complete and never silent about a failure."""

    def test_a_failed_write_is_logged_and_keeps_the_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "lanes-status.json"
            path.write_text(json.dumps({"_at": "2026-11-02 22:13:20", "_ready": 4}), encoding="utf-8")
            messages = []

            # the directory of the snapshot is gone: the write cannot succeed
            broken = pathlib.Path(directory) / "gone" / "lanes-status.json"
            written = ad.write_status_snapshot(str(broken), {"_at": "2026-11-02 22:20:00"}, messages.append)

            self.assertFalse(written)
            self.assertEqual(len(messages), 1)
            self.assertIn("ERROR lanes-status write failed", messages[0])
            self.assertIn(str(broken), messages[0])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["_ready"], 4)

    def test_a_failed_write_leaves_no_temporary_file_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "lanes-status.json"
            messages = []
            ad.write_status_snapshot(str(path / "x"), {"_at": "now"}, messages.append)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_a_concurrent_reader_never_sees_a_half_written_snapshot(self):
        """The rename is atomic: a reader sees the old or the new file, never a partial one."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "lanes-status.json"
            payload = {"_at": "2026-11-02 22:13:20", "_ready": 3,
                       "lanes": {f"C{index}": "x" * 200 for index in range(2000)}}
            stop = threading.Event()
            failures = []

            def reader():
                while not stop.is_set():
                    try:
                        with open(path, encoding="utf-8") as stream:
                            data = json.load(stream)
                    except FileNotFoundError:
                        continue
                    except Exception as error:  # a half-written file would land here
                        failures.append(repr(error))
                        continue
                    if sorted(data) != sorted(payload):
                        failures.append(f"partial snapshot: {sorted(data)[:6]}")

            watcher = threading.Thread(target=reader, daemon=True)
            watcher.start()
            try:
                for _ in range(20):
                    self.assertTrue(ad.write_status_snapshot(str(path), payload))
            finally:
                stop.set()
                watcher.join(10)
            self.assertEqual(failures, [])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertEqual([entry.name for entry in pathlib.Path(directory).iterdir()],
                             ["lanes-status.json"])

    def test_a_large_snapshot_is_written_in_one_go(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "lanes-status.json"
            payload = {"_at": "2026-11-02 22:13:20", "_ready": 1,
                       "lanes": {f"C{index}": "y" * 500 for index in range(10000)}}
            self.assertTrue(ad.write_status_snapshot(str(path), payload))
            self.assertGreater(path.stat().st_size, 4 * 1024 * 1024)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)


class StatusPublisherTests(unittest.TestCase):
    """The publisher refreshes `_at` while the tick works, not only at its end."""

    def publish(self, directory, lanes=None, state=None):
        return ad.StatusPublisher(lanes or {"C1": {"host": "h1", "backend": "codex"}},
                                  state if state is not None else {},
                                  path=str(pathlib.Path(directory) / "lanes-status.json"),
                                  heartbeat=str(pathlib.Path(directory) / "heartbeat"))

    def test_every_publish_refreshes_the_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.publish(directory)
            clock = FakeClock(1700000000.0)
            with mock.patch.object(ad, "time", clock):
                first = publisher.publish()
                clock.now += 30
                second = publisher.publish()
            self.assertNotEqual(first["_at"], second["_at"])
            written = json.loads((pathlib.Path(directory) / "lanes-status.json").read_text(encoding="utf-8"))
            self.assertEqual(written["_at"], second["_at"])
            self.assertTrue((pathlib.Path(directory) / "heartbeat").exists())

    def test_an_early_publish_keeps_the_last_known_queue_counters(self):
        """A publish before the queue was measured must not report a false `_ready: 0`."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "lanes-status.json"
            path.write_text(json.dumps({"_at": "2026-11-02 22:13:20", "_ready": 7, "_blocked": 2}),
                            encoding="utf-8")
            publisher = self.publish(directory)
            snapshot = publisher.publish()
            self.assertEqual(snapshot["_ready"], 7)
            self.assertEqual(snapshot["_blocked"], 2)

    def test_the_publisher_reports_the_busy_lanes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"C1": {"issue": 42, "run": "r1"}}
            publisher = self.publish(directory, state=state)
            snapshot = publisher.publish(res={"C1": {"lane": "busy"}})
            self.assertEqual(snapshot["C1"], "busy #42")
            self.assertEqual(snapshot["_busy"], 1)

    def test_the_publisher_records_the_phase_of_the_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.publish(directory)
            snapshot = publisher.publish(phase="dispatch")
            self.assertEqual(snapshot["_phase"], "dispatch")
            self.assertEqual(snapshot["_tick_at"], snapshot["_at"])


class DispatcherFixture:
    """Drives the real ``tick()`` with the network replaced by a scripted clock.

    Every scripted step is slow (as the 2026-11-02 evening was): the harvest of a
    lane takes 90s, the queue fetch 20s, a dependency check 15s, a label read 5s, a
    dispatch 45s and the refill 60s.  The clock is moved by those steps, so the test
    sees ten minutes of dispatching in milliseconds.
    """

    LANES = {"C1": {"host": "h1", "backend": "codex"}, "C2": {"host": "h2", "backend": "codex"}}

    def __init__(self, ad, directory, clock):
        self.ad = ad
        self.directory = pathlib.Path(directory)
        self.clock = clock
        self.path = self.directory / "lanes-status.json"
        self.ages = []
        self.stamps = []
        self.statuses = []
        self.messages = []

    # ---------------------------------------------------------------- the scripted steps

    def note(self):
        """The age of the published snapshot right now: the gap the reader would see."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        stamp = data.get("_at")
        age = int(self.clock.time() - time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")))
        self.ages.append(age)
        self.stamps.append(stamp)
        self.statuses.append(xs.fleet_lanes_snapshot(path=str(self.path), moment=self.clock.time())["status"])
        return age

    def slow(self, seconds):
        """A slow step: the clock moves and the snapshot age is recorded."""
        self.clock.advance(seconds)
        self.note()

    def patches(self):
        ad = self.ad
        fixture = self

        def remote(lane, cfg, run=""):
            return {"lane": "idle", "run": {"status": "done"} if run else None}

        def finish(lane, cfg, st, info, state):
            fixture.slow(90)  # the harvest of one lane: a slow fetch plus a PR
            return True

        def queue():
            fixture.slow(20)  # the queue fetch
            return [{"n": 100 + index, "title": f"issue {index}", "body": "", "prio": 3,
                     "backend": None, "strict": False} for index in range(3)]

        def deps_met(item):
            fixture.slow(15)  # one `gh issue view` per dependency
            return True

        def label_state(n, cache=None):
            fixture.slow(5)  # the authoritative label read
            return ("fresh", {})

        def dispatch(lane, cfg, item, base):
            fixture.slow(45)  # the assignment over ssh
            return f"run-{lane}", None

        def replenish(k, state):
            fixture.slow(60)  # the refill (planning issue included)
            return []

        return [
            mock.patch.object(ad, "log", fixture.messages.append),
            mock.patch.object(ad, "remote", remote),
            mock.patch.object(ad, "finish", finish),
            mock.patch.object(ad, "queue", queue),
            mock.patch.object(ad, "deps_met", deps_met),
            mock.patch.object(ad, "prefetch_labels", lambda *a, **k: None),
            mock.patch.object(ad, "label_state", label_state),
            mock.patch.object(ad, "read_issue", lambda n: ("fresh", {})),
            mock.patch.object(ad, "dispatch", dispatch),
            mock.patch.object(ad, "replenish", replenish),
            mock.patch.object(ad, "save_state", lambda state: None),
            mock.patch.object(ad, "gh", lambda *a, **k: (0, "", "")),
            mock.patch.object(ad, "sh", lambda *a, **k: (0, "0" * 40, "")),
        ]

    def state(self):
        """Both lanes are busy: the tick has a harvest to do before it dispatches."""
        return {lane: {"issue": 1, "run": f"run-{lane}", "host": cfg["host"], "base": "0" * 40,
                       "at": "2026-11-02 22:13:20"} for lane, cfg in self.LANES.items()}

    def run(self, minutes=10):
        """Run the real tick until `minutes` of dispatching have passed."""
        deadline = self.clock.time() + minutes * 60
        patches = self.patches()
        with mock.patch.object(self.ad, "time", self.clock):
            for patch in patches:
                patch.start()
            try:
                while self.clock.time() < deadline:
                    self.ad.tick(dict(self.LANES), self.state(), 0)
            finally:
                for patch in reversed(patches):
                    patch.stop()
        return self


class TenMinuteConsistencyTests(unittest.TestCase):
    """The 22:13 stall replayed: `_at` must follow the dispatch rhythm (Refs #1697)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lanes-status-")
        self.addCleanup(self.tmp.cleanup)
        self.ad = load_autodispatch(OPENSWARM_AD_STATE_DIR=self.tmp.name, AD_STATUS_REFRESH="0.05")

    def test_ten_minutes_of_dispatching_keep_the_snapshot_fresh(self):
        """The real dispatch path: ten minutes of dispatching, never an expired snapshot."""
        clock = FakeClock(1700000000.0)
        fixture = DispatcherFixture(self.ad, self.tmp.name, clock)
        fixture.run(minutes=10)

        self.assertTrue(fixture.ages, "the tick never published a snapshot")
        self.assertLessEqual(max(fixture.ages), STALE_AFTER,
                             f"the snapshot went stale for {max(fixture.ages)}s")
        # the snapshot was republished all along, not written once at the end
        self.assertGreater(len(set(fixture.stamps)), 10)
        self.assertEqual(fixture.stamps, sorted(fixture.stamps))
        # and the reader of the control plane never sees it expired
        self.assertEqual(set(fixture.statuses), {"fresh"})
        self.assertGreaterEqual(clock.time() - 1700000000.0, 600)

    def test_the_legacy_end_of_tick_write_would_have_gone_stale(self):
        """The regression the fix removes: one write at the end of a slow tick froze `_at`."""
        clock = FakeClock(1700000000.0)
        path = pathlib.Path(self.tmp.name) / "lanes-status.json"
        ages, statuses = [], []
        busy = {lane: {"lane": "busy"} for lane in DispatcherFixture.LANES}
        with mock.patch.object(self.ad, "time", clock):
            snapshot = self.ad.status_snapshot(DispatcherFixture.LANES, {}, busy, 0, 0)
            self.ad.write_status_snapshot(str(path), snapshot)  # the previous tick's write
            for _ in range(10):
                clock.advance(30 + 120 + 30 + 30)  # the whole tick
                data = json.loads(path.read_text(encoding="utf-8"))
                ages.append(int(clock.time() - time.mktime(time.strptime(data["_at"], "%Y-%m-%d %H:%M:%S"))))
                statuses.append(xs.fleet_lanes_snapshot(path=str(path), moment=clock.time())["status"])
                snapshot = self.ad.status_snapshot(DispatcherFixture.LANES, {}, busy, 0, 0)
                self.ad.write_status_snapshot(str(path), snapshot)
        self.assertGreater(max(ages), STALE_AFTER)
        self.assertEqual(set(statuses), {"stale"})


class SlowStepTests(unittest.TestCase):
    """A step that blocks longer than the threshold must not freeze `_at` (Refs #1697)."""

    def test_a_blocked_step_does_not_freeze_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            # a short refresh interval so the test does not have to wait half a minute
            ad = load_autodispatch(OPENSWARM_AD_STATE_DIR=directory, AD_STATUS_REFRESH="0.2")
            path = pathlib.Path(directory) / "lanes-status.json"

            def blocked_queue():
                """A queue fetch that hangs until the snapshot was refreshed meanwhile."""
                first = json.loads(path.read_text(encoding="utf-8"))["_at"]
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        current = json.loads(path.read_text(encoding="utf-8"))["_at"]
                    except (OSError, ValueError):
                        current = None
                    if current and current != first:
                        return []
                    time.sleep(0.05)
                raise AssertionError("the snapshot was not refreshed while the step was blocked")

            lanes = {"C1": {"host": "h1", "backend": "codex"}}
            with mock.patch.object(ad, "log", lambda *a, **k: None), \
                    mock.patch.object(ad, "remote", lambda *a, **k: {"lane": "idle"}), \
                    mock.patch.object(ad, "queue", blocked_queue), \
                    mock.patch.object(ad, "prefetch_labels", lambda *a, **k: None), \
                    mock.patch.object(ad, "label_state", lambda *a, **k: ("fresh", {})), \
                    mock.patch.object(ad, "replenish", lambda *a, **k: []), \
                    mock.patch.object(ad, "save_state", lambda *a, **k: None), \
                    mock.patch.object(ad, "gh", lambda *a, **k: (0, "", "")), \
                    mock.patch.object(ad, "sh", lambda *a, **k: (0, "0" * 40, "")):
                ad.tick(lanes, {}, 0)

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("_at", data)
            self.assertEqual(data["_phase"], "idle")

    def test_the_refresher_does_not_pretend_the_dispatcher_loop_is_alive(self):
        """The heartbeat is the loop's signal: the refresher must not refresh it."""
        with tempfile.TemporaryDirectory() as directory:
            ad = load_autodispatch(OPENSWARM_AD_STATE_DIR=directory, AD_STATUS_REFRESH="0.2")
            publisher = ad.StatusPublisher({"C1": {"host": "h1"}}, {})
            publisher.start()
            try:
                time.sleep(0.6)
            finally:
                publisher.stop()
            heartbeat = pathlib.Path(directory) / "heartbeat"
            self.assertFalse(heartbeat.exists(), "the refresher must not write the heartbeat")
            self.assertTrue((pathlib.Path(directory) / "lanes-status.json").exists())


class SnapshotPathTests(unittest.TestCase):
    """The reader and the writer must name the same file (Refs #1697)."""

    def test_the_reader_reads_the_file_the_writer_writes(self):
        self.assertEqual(ad.D, os.path.join(os.path.expanduser("~"), "phanes-ctl", "autodispatch"))
        self.assertEqual(ad.lanes_status_path(), os.path.join(ad.D, "lanes-status.json"))
        self.assertEqual(xs.fleet_lanes_snapshot_path(), os.path.join(ad.D, "lanes-status.json"))

    def test_the_default_deployment_path_is_the_one_the_dispatcher_writes(self):
        """The deployed path: ~/phanes-ctl/autodispatch/lanes-status.json on both sides."""
        default = os.path.join(os.path.expanduser("~"), "phanes-ctl", "autodispatch", "lanes-status.json")
        self.assertEqual(xs.fleet_lanes_snapshot_path(), default)
        self.assertEqual(ad.lanes_status_path(), default)

    def test_the_state_dir_can_be_moved_for_both_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE_DIR": directory}):
                self.assertEqual(xs.fleet_lanes_snapshot_path(), os.path.join(directory, "lanes-status.json"))
                self.assertEqual(ad.lanes_status_path(), os.path.join(directory, "lanes-status.json"))

    def test_the_snapshot_file_can_be_moved_for_both_sides(self):
        """OPENSWARM_LANES_STATUS_FILE names the file for the writer AND the reader."""
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "elsewhere", "lanes-status.json")
            os.makedirs(os.path.dirname(target))
            with mock.patch.dict(os.environ, {"OPENSWARM_LANES_STATUS_FILE": target}):
                self.assertEqual(ad.lanes_status_path(), target)
                self.assertEqual(xs.fleet_lanes_snapshot_path(), target)
                # the writer publishes there and the reader reads the very same file
                self.assertTrue(ad.write_status_snapshot(ad.lanes_status_path(),
                                                         {"_at": time.strftime("%F %T"), "_ready": 2}))
                report = xs.fleet_lanes_snapshot()
                self.assertEqual(report["status"], "fresh")
                self.assertEqual(report["ready"], 2)
                self.assertEqual(report["path"], target)

    def test_the_configured_path_is_the_one_the_dispatcher_uses(self):
        """The dispatcher started with the variable writes where the health check reads."""
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "lanes-status.json")
            ad = load_autodispatch(OPENSWARM_AD_STATE_DIR=directory, OPENSWARM_LANES_STATUS_FILE=target)
            self.assertEqual(ad.lanes_status_path(), target)
            self.assertEqual(ad.lanes_status_heartbeat_path(), os.path.join(directory, "heartbeat"))
            ad.write_status_snapshot(ad.lanes_status_path(), {"_at": time.strftime("%F %T")})
            self.assertTrue(os.path.exists(target))
            with mock.patch.dict(os.environ, {"OPENSWARM_LANES_STATUS_FILE": target}):
                self.assertEqual(xs.fleet_lanes_snapshot_path(), target)
                self.assertEqual(xs.fleet_lanes_snapshot()["status"], "fresh")

    def test_the_heartbeat_is_the_state_dir_file_on_both_sides(self):
        """The heartbeat is the dispatcher loop's signal: writer and reader name the same file."""
        with tempfile.TemporaryDirectory() as directory:
            ad = load_autodispatch(OPENSWARM_AD_STATE_DIR=directory)
            self.assertEqual(ad.lanes_status_heartbeat_path(), os.path.join(directory, "heartbeat"))
            with mock.patch.dict(os.environ, {"OPENSWARM_AD_STATE_DIR": directory}):
                writer = xs.fleet_lanes_writer(os.path.join(directory, "lanes-status.json"), home=directory)
                self.assertEqual(writer["heartbeat_path"], os.path.join(directory, "heartbeat"))


if __name__ == "__main__":
    unittest.main()
