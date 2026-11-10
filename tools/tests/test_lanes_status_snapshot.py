"""Reproduction of #1697: the lanes-status snapshot must stay fresh, complete and observable.

The control plane reads ``~/phanes-ctl/autodispatch/lanes-status.json`` as the queue
watermark: the pool level, the number of parallel lanes and every supervisor decision
are taken from it.  On 2026-11-02 the ``_at`` stamp froze at 22:13:20 while the
dispatcher kept dispatching, so every reader judged a frozen snapshot as a live one.

The tests below drive the real ``tick()`` of ``tools/lane-autodispatch/autodispatch.py``
with every side effect faked and look at what a reader of the snapshot would see:

* a publish is atomic -- a concurrent reader never sees a half-written file;
* a failed publish is logged and never leaves a half-written file behind;
* the snapshot is refreshed while a slow step of the tick runs, not only at its end;
* the writer publishes the file the control plane is configured to read
  (``OPENSWARM_LANES_STATUS_FILE``).

They fail on the base commit: the write is a plain ``json.dump`` into the live file, a
failed write is neither logged nor contained, the snapshot is written once at the very
end of the tick and the configured snapshot path is ignored.

The reader side of the issue (the health output that marks an expired snapshot) is
covered by ``test_lanes_status_health_probe.py``.
"""

import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
AD_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

# The stamp the issue reports: the dispatcher froze there on 2026-11-02.
STALLED_AT = "2026-11-02 22:13:20"

# The refresh cadence of the snapshot publisher.  The dispatcher may name this knob
# differently; the test offers the common spellings so the fix only has to honour one
# of them (and the test names the ones it sets).
REFRESH_VARS = ("AD_STATUS_REFRESH", "AD_STATUS_REFRESH_SECONDS", "AD_SNAPSHOT_REFRESH",
                "AD_REFRESH_SECONDS", "LANES_STATUS_REFRESH")


def load_autodispatch(**environ):
    """Load a fresh copy of the dispatcher with the given environment.

    The state dir and the refresh cadence are read at import time, so the module is
    loaded again for every configuration under test (the reader and the writer must
    agree on the path).
    """
    previous = {name: os.environ.get(name) for name in environ}
    os.environ.update({name: str(value) for name, value in environ.items()})
    try:
        spec = importlib.util.spec_from_file_location("autodispatch_lanes_status", AD_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class SnapshotWatcher(threading.Thread):
    """Reads the snapshot file while the dispatcher publishes it.

    A reader that catches a half-written file would judge a broken snapshot; the
    watcher records every sample that is not a complete snapshot.
    """

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path = pathlib.Path(path)
        self.partial = []
        self.samples = 0
        self._finished = threading.Event()

    def stop(self):
        self._finished.set()

    def run(self):
        while not self._finished.is_set():
            try:
                with open(self.path, "rb") as stream:
                    data = stream.read()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            self.samples += 1
            if not data:
                self.partial.append("the snapshot file was empty")
                continue
            try:
                json.loads(data)
            except ValueError as error:
                self.partial.append(f"the snapshot file was half written: {error}")


class TickRunner:
    """Runs one tick of the dispatcher with the network and the state faked."""

    def __init__(self, module, state_dir, lanes=None, state=None):
        self.module = module
        self.state_dir = pathlib.Path(state_dir)
        self.lanes = lanes or {"C1": {"host": "h1", "backend": "codex"}}
        self.state = state if state is not None else {}
        self.messages = []

    @property
    def snapshot_path(self):
        return self.state_dir / "lanes-status.json"

    def _collect(self, msg, brain=True):
        """The dispatcher's log: the messages are kept and written to the log file."""
        self.messages.append(msg)
        try:
            with open(self.state_dir / "autodispatch.log", "a", encoding="utf-8") as stream:
                stream.write(f"{msg}\n")
        except OSError:
            pass

    def run(self, **overrides):
        """Run ``tick()``; ``overrides`` replaces the faked side effects."""
        module = self.module
        defaults = {
            "D": str(self.state_dir),
            "audit_switches": lambda *a, **k: None,
            "paused_scopes": lambda *a, **k: set(),
            "host_ok": lambda host: True,
            "host_fail": lambda host, why: None,
            "host_good": lambda host: None,
            "remote": lambda lane, cfg, run="": {"lane": "idle"},
            "deps_met": lambda item: True,
            "dispatch": lambda lane, cfg, item, base: ("run-1", None),
            "replenish": lambda count, state: [],
            "save_state": lambda state: None,
            "queue": lambda *args, **kwargs: [],
            "gh": lambda *a, **k: (0, "", ""),
            "sh": lambda *a, **k: (0, "0" * 40, ""),
            "log": self._collect,
        }
        defaults.update(overrides)
        saved = {}
        try:
            for name, value in defaults.items():
                if not hasattr(module, name):
                    continue  # the side effect does not exist in this revision
                saved[name] = getattr(module, name)
                setattr(module, name, value)
            with warnings.catch_warnings():
                # the dispatcher writes the snapshot without closing the file: the warning
                # is pre-existing and says nothing about the contract under test
                warnings.simplefilter("ignore", ResourceWarning)
                return module.tick(dict(self.lanes), dict(self.state), 0)
        finally:
            for name, value in saved.items():
                setattr(module, name, value)


class SnapshotPublishTests(unittest.TestCase):
    """The write itself: atomic, complete and never silent about a failure."""

    def test_a_concurrent_reader_never_sees_a_half_written_snapshot(self):
        """A big snapshot: the reader polls the file while the dispatcher writes it."""
        with tempfile.TemporaryDirectory() as directory:
            module = load_autodispatch()
            lanes = {f"C{index}": {"host": "h1", "backend": "codex"} for index in range(40)}
            runner = TickRunner(module, directory, lanes=lanes)
            watcher = SnapshotWatcher(runner.snapshot_path)
            watcher.start()
            try:
                runner.run(remote=lambda lane, cfg, run="": {"lane": "x" * 50000})
            finally:
                watcher.stop()
                watcher.join(60)
            self.assertEqual(watcher.partial, [],
                             "a reader of the snapshot caught a half-written file")
            data = json.loads(runner.snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(len([key for key in data if not key.startswith("_")]), 40)
            self.assertIn("_at", data)

    def test_a_failed_publish_is_logged_and_leaves_a_complete_snapshot(self):
        """The write fails: it must be logged and the file must stay a complete snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            previous = {"_at": STALLED_AT, "_ready": 4, "_busy": 1}
            runner = TickRunner(load_autodispatch(), directory)
            runner.snapshot_path.write_text(json.dumps(previous), encoding="utf-8")
            # a value json cannot serialize: the publish fails
            broken = {"lane": {"not serializable": object()}}
            try:
                runner.run(remote=lambda lane, cfg, run="": broken)
            except Exception as error:  # a failed write must not kill the dispatcher
                self.fail(f"a failed snapshot publish must not kill the tick: {error!r}")
            errors = [line for line in runner.messages if "lanes-status" in line.lower()]
            self.assertTrue(errors, f"the failed publish was not logged: {runner.messages}")
            self.assertTrue(any(word in line.lower() for line in errors
                                for word in ("error", "fail", "exception")),
                            f"the log does not report the failure: {errors}")
            # the file on disk is still a complete snapshot: a failed publish never
            # leaves a half-written file behind
            data = json.loads(runner.snapshot_path.read_text(encoding="utf-8"))
            self.assertIn("_at", data)


class SnapshotFreshnessTests(unittest.TestCase):
    """`_at` must follow the dispatch rhythm, not only the end of the tick."""

    def test_the_snapshot_is_refreshed_while_a_slow_step_runs(self):
        """The 22:13 stall: a slow step must not freeze the snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            module = load_autodispatch(OPENSWARM_AD_STATE_DIR=directory,
                                        **{name: "0.2" for name in REFRESH_VARS})
            runner = TickRunner(module, directory)
            runner.snapshot_path.write_text(json.dumps({"_at": STALLED_AT, "_ready": 4}),
                                            encoding="utf-8")
            refreshed = []

            def blocked_queue(*args, **kwargs):
                """A queue fetch that blocks until the snapshot was refreshed meanwhile."""
                first = json.loads(runner.snapshot_path.read_text(encoding="utf-8"))["_at"]
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        current = json.loads(runner.snapshot_path.read_text(encoding="utf-8"))["_at"]
                    except (OSError, ValueError):
                        current = None
                    if current and current != first:
                        refreshed.append(current)
                        return []
                    time.sleep(0.05)
                return []

            runner.run(queue=blocked_queue)
            self.assertTrue(refreshed,
                            "the snapshot was not refreshed while the slow step ran: "
                            f"_at stayed at {STALLED_AT}")


class SnapshotPathTests(unittest.TestCase):
    """The reader and the writer must name the same file (Refs #1697)."""

    def test_the_writer_publishes_the_configured_snapshot_file(self):
        for variable in ("OPENSWARM_LANES_STATUS_FILE", "XSWARM_LANES_STATUS_FILE"):
            with self.subTest(variable=variable):
                with tempfile.TemporaryDirectory() as directory:
                    target = pathlib.Path(directory) / "elsewhere" / "lanes-status.json"
                    target.parent.mkdir(parents=True)
                    module = load_autodispatch(**{variable: str(target)})
                    runner = TickRunner(module, directory)
                    with mock.patch.dict(os.environ, {variable: str(target)}):
                        runner.run()
                    self.assertTrue(target.exists(),
                                    f"the writer ignored {variable}: {list(pathlib.Path(directory).rglob('*'))}")
                    data = json.loads(target.read_text(encoding="utf-8"))
                    self.assertIn("_at", data)


if __name__ == "__main__":
    unittest.main()
