import importlib.util
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "dispatch" / "pause.py"
spec = importlib.util.spec_from_file_location("dispatch_pause", MODULE)
pause = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pause)


class PauseStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-")
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.store = pause.PauseStore(self.home)

    def test_ttl_expires_at_deadline_and_resume_removes_scope(self):
        self.store.pause("lane:M1", ttl_s=30, reason="maintenance", now=100)

        self.assertEqual([row["scope"] for row in self.store.active(129)], ["lane:M1"])
        self.assertEqual(self.store.active(130), [])
        self.assertTrue(self.store.resume("lane:M1"))
        self.assertFalse(self.store.resume("lane:M1"))

    def test_scope_matching(self):
        self.store.pause("lane:M2", now=10)
        self.store.pause("host:worker-a", now=10)
        self.store.pause("backend:codex", now=10)

        self.assertTrue(self.store.is_paused(lane="M2", now=10))
        self.assertTrue(self.store.is_paused(host="worker-a", now=10))
        self.assertTrue(self.store.is_paused(backend="codex", now=10))
        self.assertFalse(self.store.is_paused(lane="M1", host="worker-b", backend="pi", now=10))

    def test_mint_does_not_pause_dispatch_but_all_does(self):
        self.store.pause("mint", now=5)
        self.assertFalse(self.store.is_paused(lane="M1", now=5))
        self.store.resume("mint")
        self.store.pause("all", now=6)
        self.assertTrue(self.store.is_paused(lane="M1", now=6))

    def test_legacy_markers_map_to_sorted_pause_records(self):
        markers = self.home / "legacy"
        markers.mkdir()
        for name in ("PAUSE-MINT", "PAUSE-M2", "PAUSE", "PAUSE-M1", "OTHER"):
            (markers / name).touch()

        records = pause.legacy_markers(markers)
        self.assertEqual([row["scope"] for row in records], ["all", "lane:M1", "lane:M2", "mint"])
        self.assertTrue(all(row["expires_at"] is None for row in records))
        self.assertEqual(records[1]["reason"], "legacy marker: PAUSE-M1")

    def test_active_output_is_sorted_by_scope(self):
        for scope in ("host:z", "all", "backend:b", "lane:A"):
            self.store.pause(scope, reason=scope, now=1)

        self.assertEqual(
            [row["scope"] for row in self.store.active(1)],
            ["all", "backend:b", "host:z", "lane:A"],
        )


class DerivedScopeTests(unittest.TestCase):
    """Refs #1367: a hold under a scope of its own pauses the target it derives from.

    The fleet health check keeps a hold of a host under ``host:<host>@health``
    when another program owns the scope of the host, so neither record
    overwrites the other.  The pause store still reports the host as paused
    for the callers that ask about the host.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-derived-")
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.store = pause.PauseStore(self.home)

    def test_a_scope_of_its_own_pauses_the_host_it_derives_from(self):
        self.store.pause("host:worker-a@health", reason="auto:health disk", now=100)

        self.assertTrue(self.store.is_paused(host="worker-a", now=100))
        self.assertFalse(self.store.is_paused(host="worker-b", now=100))
        self.assertFalse(self.store.is_paused(host="worker-ab", now=100))
        self.assertFalse(self.store.is_paused(lane="M1", now=100))

    def test_the_hold_of_another_program_stays_a_record_of_its_own(self):
        self.store.pause("host:worker-a", reason="operator: maintenance", now=100)
        self.store.pause("host:worker-a@health", reason="auto:health disk", now=100)

        scopes = {row["scope"]: row["reason"] for row in self.store.active(100)}
        self.assertEqual(scopes["host:worker-a"], "operator: maintenance")
        self.assertEqual(scopes["host:worker-a@health"], "auto:health disk")
        # The operator releases their own pause: the health hold stays and the
        # host is still paused.
        self.store.resume("host:worker-a")
        self.assertTrue(self.store.is_paused(host="worker-a", now=100))
        self.assertEqual([row["scope"] for row in self.store.active(100)],
                         ["host:worker-a@health"])
        # The health hold is released: the host is dispatchable again.
        self.store.resume("host:worker-a@health")
        self.assertFalse(self.store.is_paused(host="worker-a", now=100))


class PauseDirTests(unittest.TestCase):
    """Refs #1367: the host-level pause markers live in one shared directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-dir-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = pathlib.Path(self.tmp.name)
        self.previous = {name: os.environ.get(name)
                         for name in ("OPENSWARM_PAUSE_DIR", "OPENSWARM_HOME")}
        for name in self.previous:
            os.environ.pop(name, None)
        self.addCleanup(self.restore)

    def restore(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_the_pause_dir_env_names_the_marker_directory(self):
        os.environ["OPENSWARM_PAUSE_DIR"] = str(self.directory)
        self.assertEqual(pause.pause_dir(), str(self.directory))

    def test_the_openswarm_home_holds_the_markers_without_the_override(self):
        os.environ["OPENSWARM_HOME"] = str(self.directory)
        self.assertEqual(pause.pause_dir(), str(self.directory))

    def test_a_home_argument_overrides_the_openswarm_home(self):
        os.environ["OPENSWARM_HOME"] = "/nowhere"
        self.assertEqual(pause.pause_dir(str(self.directory)), str(self.directory))

    def test_the_host_marker_path_and_the_marker_lookup(self):
        path = pause.host_marker_path(self.directory, "worker-a")
        self.assertEqual(path, os.path.join(str(self.directory), "PAUSE-HOST-worker-a"))
        self.assertFalse(pause.host_paused("worker-a", [self.directory]))
        pathlib.Path(path).write_text("auto:health disk 97% used\n", encoding="utf-8")
        self.assertTrue(pause.host_paused("worker-a", [self.directory]))
        self.assertFalse(pause.host_paused("worker-b", [self.directory]))

    def test_the_health_marker_dir_lives_below_the_pause_dir(self):
        os.environ["OPENSWARM_PAUSE_DIR"] = str(self.directory)
        self.assertEqual(pause.health_pause_dir(), os.path.join(str(self.directory), "health"))
        self.assertEqual(pause.pause_dirs(), [str(self.directory),
                                              os.path.join(str(self.directory), "health")])

    def test_the_health_marker_of_a_host_is_found_in_the_pause_dirs(self):
        os.environ["OPENSWARM_PAUSE_DIR"] = str(self.directory)
        path = pause.host_marker_path(pause.health_pause_dir(), "worker-a")
        pathlib.Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text("auto:health disk 97% used\n", encoding="utf-8")
        self.assertTrue(pause.host_paused("worker-a", pause.pause_dirs()))
        self.assertFalse(pause.host_paused("worker-b", pause.pause_dirs()))


if __name__ == "__main__":
    unittest.main()
