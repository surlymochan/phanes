"""Tests for lib/dispatch/lane_pick.py (Refs #1396, part of #1360).

``openswarm/lib`` and ``openswarm/lib/dispatch`` are namespace packages (no
``__init__.py``), so the module is loaded by file path instead of by import.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "lib" / "dispatch" / "lane_pick.py"
spec = importlib.util.spec_from_file_location("lane_pick", MODULE_PATH)
lane_pick = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane_pick)


class LruPickerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_path = os.path.join(self.directory.name, "lane-pick.json")
        self.picker = lane_pick.LruPicker(self.state_path)

    def write_state(self, state):
        with open(self.state_path, "w", encoding="utf-8") as stream:
            json.dump(state, stream)

    def read_state(self):
        with open(self.state_path, encoding="utf-8") as stream:
            return json.load(stream)


class PickTests(LruPickerTestCase):
    def test_picks_the_lane_dispatched_longest_ago(self):
        self.write_state({"version": 1, "counter": 3, "lanes": {"alpha": 3, "beta": 1}})

        self.assertEqual(self.picker.pick(["alpha", "beta"]), "beta")

    def test_touch_makes_the_lane_the_most_recently_dispatched(self):
        self.write_state({"version": 1, "counter": 3, "lanes": {"alpha": 3, "beta": 1}})
        self.assertEqual(self.picker.pick(["alpha", "beta"]), "beta")

        self.picker.touch("beta")

        self.assertEqual(self.picker.pick(["alpha", "beta"]), "alpha")

    def test_ties_are_broken_by_lane_name(self):
        self.write_state({"version": 1, "counter": 2, "lanes": {"beta": 1, "alpha": 1}})

        self.assertEqual(self.picker.pick(["beta", "alpha"]), "alpha")
        self.assertEqual(self.picker.pick(["alpha", "beta"]), "alpha")

    def test_new_lane_waits_longest(self):
        self.write_state({"version": 1, "counter": 5, "lanes": {"alpha": 5}})

        self.assertEqual(self.picker.pick(["alpha", "zeta"]), "zeta")

    def test_new_lanes_tie_on_the_lane_name(self):
        self.write_state({"version": 1, "counter": 5, "lanes": {"alpha": 5}})

        self.assertEqual(self.picker.pick(["gamma", "beta"]), "beta")

    def test_paused_lanes_are_skipped(self):
        self.write_state({"version": 1, "counter": 2, "lanes": {"alpha": 1, "beta": 2}})

        self.assertEqual(self.picker.pick(["alpha", "beta"], paused={"alpha"}), "beta")

    def test_pick_returns_none_when_every_candidate_is_paused(self):
        self.write_state({"version": 1, "counter": 2, "lanes": {"alpha": 1, "beta": 2}})

        self.assertIsNone(self.picker.pick(["alpha", "beta"], paused={"alpha", "beta"}))

    def test_pick_returns_none_without_candidates(self):
        self.assertIsNone(self.picker.pick([]))

    def test_pick_does_not_write_the_state_file(self):
        self.picker.pick(["alpha"])

        self.assertFalse(os.path.exists(self.state_path))

    def test_pick_reads_the_state_file_on_every_call(self):
        self.write_state({"version": 1, "counter": 1, "lanes": {"alpha": 1}})
        self.assertEqual(self.picker.pick(["alpha", "beta"]), "beta")

        self.write_state({"version": 1, "counter": 1, "lanes": {"beta": 1}})
        self.assertEqual(self.picker.pick(["alpha", "beta"]), "alpha")


class TouchTests(LruPickerTestCase):
    def test_touch_writes_the_state_as_json(self):
        self.picker.touch("alpha")
        self.picker.touch("beta")

        state = self.read_state()

        self.assertEqual(state["lanes"], {"alpha": 1, "beta": 2})
        self.assertEqual(state["counter"], 2)

    def test_touch_creates_the_state_directory(self):
        picker = lane_pick.LruPicker(os.path.join(self.directory.name, "nested", "lane-pick.json"))

        picker.touch("alpha")

        self.assertTrue(os.path.exists(picker.state_path))

    def test_touch_leaves_no_temporary_file_behind(self):
        self.picker.touch("alpha")

        self.assertEqual([name for name in os.listdir(self.directory.name) if name != "lane-pick.json"], [])

    def test_touch_replaces_the_state_file_atomically(self):
        self.write_state({"version": 1, "counter": 7, "lanes": {"alpha": 7}})

        self.picker.touch("beta")

        state = self.read_state()

        self.assertEqual(state["lanes"], {"alpha": 7, "beta": 8})
        self.assertEqual(state["counter"], 8)


class DamagedStateTests(LruPickerTestCase):
    def test_corrupted_state_file_falls_back_to_a_fresh_state(self):
        with open(self.state_path, "w", encoding="utf-8") as stream:
            stream.write("{not json")

        self.assertEqual(self.picker.pick(["alpha", "beta"]), "alpha")

    def test_missing_state_file_falls_back_to_a_fresh_state(self):
        self.assertEqual(self.picker.pick(["beta", "alpha"]), "alpha")

    def test_state_file_with_an_unexpected_shape_falls_back_to_a_fresh_state(self):
        self.write_state(["alpha", "beta"])

        self.assertEqual(self.picker.pick(["beta", "alpha"]), "alpha")

    def test_state_file_of_an_unknown_version_falls_back_to_a_fresh_state(self):
        self.write_state({"version": 99, "counter": 3, "lanes": {"beta": 1}})

        self.assertEqual(self.picker.pick(["beta", "alpha"]), "alpha")

    def test_a_damaged_lane_counter_resets_the_whole_state(self):
        # A half read history would keep the valid "alpha" entry and treat "zeta" as
        # never dispatched; the whole state is damaged, so both lanes look new and
        # the name breaks the tie.
        self.write_state({"version": 1, "counter": 5, "lanes": {"zeta": "bad", "alpha": 4}})

        self.assertEqual(self.picker.pick(["zeta", "alpha"]), "alpha")
        self.assertEqual(self.picker.state(), {"version": 1, "counter": 0, "lanes": {}})

    def test_a_mixed_valid_and_damaged_entry_resets_the_whole_state(self):
        self.write_state({"version": 1, "counter": 2, "lanes": {"alpha": "bad", "beta": 1}})

        self.assertEqual(self.picker.pick(["beta", "zeta"]), "beta")
        self.assertEqual(self.picker.state(), {"version": 1, "counter": 0, "lanes": {}})

    def test_a_damaged_lane_name_resets_the_whole_state(self):
        self.write_state({"version": 1, "counter": 5, "lanes": {"": 4, "alpha": 2}})

        self.assertEqual(self.picker.pick(["alpha", "omega"]), "alpha")
        self.assertEqual(self.picker.state(), {"version": 1, "counter": 0, "lanes": {}})

    def test_a_damaged_state_counter_resets_the_whole_state(self):
        self.write_state({"version": 1, "counter": "many", "lanes": {"beta": 1}})

        self.assertEqual(self.picker.pick(["beta", "alpha"]), "alpha")
        self.assertEqual(self.picker.state(), {"version": 1, "counter": 0, "lanes": {}})

    def test_a_boolean_counter_resets_the_whole_state(self):
        self.write_state({"version": 1, "counter": 1, "lanes": {"beta": True}})

        self.assertEqual(self.picker.pick(["beta", "alpha"]), "alpha")

    def test_touch_repairs_a_corrupted_state_file(self):
        with open(self.state_path, "w", encoding="utf-8") as stream:
            stream.write("{not json")

        self.picker.touch("alpha")

        self.assertEqual(self.read_state()["lanes"], {"alpha": 1})

    def test_touch_repairs_a_state_with_a_damaged_entry(self):
        self.write_state({"version": 1, "counter": 2, "lanes": {"alpha": "bad", "beta": 1}})

        self.picker.touch("gamma")

        self.assertEqual(self.read_state()["lanes"], {"gamma": 1})
        self.assertEqual(self.read_state()["counter"], 1)


class LayoutTests(unittest.TestCase):
    def test_dispatch_is_a_namespace_package(self):
        self.assertFalse((ROOT / "lib" / "__init__.py").exists())
        self.assertFalse((ROOT / "lib" / "dispatch" / "__init__.py").exists())


if __name__ == "__main__":
    unittest.main()
