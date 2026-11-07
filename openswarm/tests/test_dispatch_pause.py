import importlib.util
import json
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


class ScopeKeyTests(unittest.TestCase):
    """The scope -> file name mapping must be injective (Refs #1829)."""

    def test_a_safe_scope_keeps_its_file_name(self):
        for scope in ("all", "mint", "lane_X", "host_worker-a", "backend.pi"):
            self.assertEqual(pause._scope_key(scope), scope)

    def test_the_colliding_scopes_get_different_file_names(self):
        self.assertNotEqual(pause._scope_key("lane:X"), pause._scope_key("lane_X"))
        self.assertNotEqual(pause._scope_key("host:a:b"), pause._scope_key("host_a_b"))

    def test_the_mapping_is_injective(self):
        scopes = ("all", "mint", "lane:X", "lane_X", "lane:X:Y", "lane:X_Y",
                  "host:a:b", "host_a_b", "host:a b", "host:a~b", "lane:é")
        keys = [pause._scope_key(scope) for scope in scopes]
        self.assertEqual(len(keys), len(set(keys)))

    def test_scopes_that_differ_in_case_only_get_different_file_names(self):
        # a case-insensitive file system maps `lane~3aX` and `lane~3ax` onto
        # one file, so the names must differ in more than case (Refs #1829)
        self.assertNotEqual(pause._scope_key("lane:X"), pause._scope_key("lane:x"))
        self.assertNotEqual(pause._scope_key("lane:X").lower(),
                            pause._scope_key("lane:x").lower())

    def test_a_long_scope_still_names_a_file(self):
        key = pause._scope_key("lane:" + "é" * 50)
        self.assertLessEqual(len(key.encode("utf-8")) + len(".json"), 255)


class ScopeCollisionTests(unittest.TestCase):
    """Two scopes that shared a file must not affect each other (Refs #1829)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-")
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.store = pause.PauseStore(self.home)
        self.dir = self.home / "pauses"

    def write_record(self, name, scope):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        path.write_text(json.dumps({"scope": scope, "created_at": 0.0,
                                    "expires_at": None, "reason": "written"}),
                        encoding="utf-8")
        return path

    def test_two_lane_scopes_are_set_and_cleared_apart(self):
        # the old mapping sent both scopes to the same file (lane_X)
        self.store.pause("lane:X", now=1)
        self.store.pause("lane_X", now=1)

        self.assertEqual([row["scope"] for row in self.store.active(1)],
                         ["lane:X", "lane_X"])
        self.assertTrue(self.store.resume("lane_X"))
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane:X"])
        self.assertTrue(self.store.resume("lane:X"))
        self.assertEqual(self.store.active(1), [])

    def test_two_scopes_that_differ_in_case_only_are_set_and_cleared_apart(self):
        # a case-insensitive file system maps the two names onto one file
        self.store.pause("lane:X", now=1)
        self.store.pause("lane:x", now=1)

        self.assertEqual([row["scope"] for row in self.store.active(1)],
                         ["lane:X", "lane:x"])
        self.assertTrue(self.store.resume("lane:x"))
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane:X"])
        self.assertTrue(self.store.resume("lane:X"))
        self.assertEqual(self.store.active(1), [])

    def test_a_long_scope_is_set_and_cleared(self):
        scope = "lane:" + "é" * 50

        self.store.pause(scope, now=1)

        self.assertEqual([row["scope"] for row in self.store.active(1)], [scope])
        self.assertTrue(self.store.resume(scope))
        self.assertEqual(self.store.active(1), [])

    def test_a_safe_scope_keeps_its_file_name_on_disk(self):
        # the records written before #1829 stay where they are
        self.store.pause("lane_X", now=1)

        self.assertTrue((self.dir / "lane_X.json").exists())
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane_X"])

    def test_two_host_scopes_are_set_and_cleared_apart(self):
        self.store.pause("host:a:b", now=1)
        self.store.pause("host:a_b", now=1)

        self.assertEqual([row["scope"] for row in self.store.active(1)],
                         ["host:a:b", "host:a_b"])
        self.assertTrue(self.store.is_paused(host="a:b", now=1))
        self.assertTrue(self.store.is_paused(host="a_b", now=1))
        self.assertTrue(self.store.resume("host:a:b"))
        self.assertFalse(self.store.is_paused(host="a:b", now=1))
        self.assertTrue(self.store.is_paused(host="a_b", now=1))

    def test_every_scope_lives_in_its_own_file(self):
        self.store.pause("lane:X", now=1)
        self.store.pause("lane_X", now=1)

        names = sorted(path.name for path in self.dir.glob("*.json"))
        self.assertEqual(names, sorted([pause._scope_key("lane:X") + ".json",
                                         pause._scope_key("lane_X") + ".json"]))

    def test_a_record_of_another_scope_is_not_the_requested_scope(self):
        path = self.write_record(pause._scope_key("lane:X") + ".json", "lane:Y")

        self.assertFalse(self.store.is_paused(lane="X", now=1))
        self.assertFalse(self.store.resume("lane:X"))
        self.assertTrue(path.exists())
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane:Y"])

    def test_a_record_of_the_old_file_name_still_pauses_and_is_cleared(self):
        path = self.write_record(pause._legacy_scope_key("lane:M1") + ".json", "lane:M1")

        self.assertTrue(self.store.is_paused(lane="M1", now=1))
        self.assertTrue(self.store.resume("lane:M1"))
        self.assertFalse(path.exists())
        self.assertEqual(self.store.active(1), [])

    def test_writing_a_scope_moves_the_old_record_to_the_current_name(self):
        legacy = self.write_record(pause._legacy_scope_key("lane:M1") + ".json", "lane:M1")

        self.store.pause("lane:M1", now=1, reason="again")

        self.assertFalse(legacy.exists())
        self.assertTrue((self.dir / (pause._scope_key("lane:M1") + ".json")).exists())
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane:M1"])

    def test_a_legacy_collision_file_is_honoured_and_moved_on_the_next_write(self):
        # before #1829 the scopes `lane:X` and `lane_X` shared `lane_X.json`
        legacy = self.write_record(pause._legacy_scope_key("lane:X") + ".json", "lane:X")

        self.assertTrue(self.store.is_paused(lane="X", now=1))
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane:X"])

        self.store.pause("lane:X", now=1, reason="again")

        self.assertFalse(legacy.exists())
        self.assertTrue((self.dir / (pause._scope_key("lane:X") + ".json")).exists())
        self.assertEqual([row["scope"] for row in self.store.active(1)], ["lane:X"])

    def test_the_underscore_scope_does_not_take_the_legacy_file_of_the_colon_scope(self):
        legacy = self.write_record(pause._legacy_scope_key("lane:X") + ".json", "lane:X")

        self.store.pause("lane_X", now=1)

        self.assertTrue(legacy.exists())
        self.assertEqual([row["scope"] for row in self.store.active(1)],
                         ["lane:X", "lane_X"])


if __name__ == "__main__":
    unittest.main()
