import importlib.util
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


if __name__ == "__main__":
    unittest.main()
