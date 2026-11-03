"""Pause switch provenance: creator, time and basis (Refs #1669).

A PAUSE switch that carries no provenance, or that is older than the
freshness window, cannot be told apart from a snapshot restored from an older
run: on 2026-11-02 a PAUSE-DISPATCH / PAUSE-MINT pair was restored from a
2026-11-01 record and froze the dispatch for ten minutes.  Such a switch is
never honoured: it is reported for a review instead.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "dispatch" / "pause.py"
spec = importlib.util.spec_from_file_location("dispatch_pause_provenance", MODULE)
pause = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pause)

NOW = 1_800_000_000.0


class SwitchProblems(unittest.TestCase):
    def test_a_sound_switch_has_no_problem(self):
        switch = {"scope": "lane:M1", "source": "manual", "basis": "disk maintenance",
                  "created_at": NOW - 10, "expires_at": NOW + 600}
        self.assertEqual(pause.switch_problems(switch, NOW), [])

    def test_expired_switch_is_reported(self):
        switch = {"scope": "mint", "source": "manual", "basis": "operator request",
                  "created_at": NOW - 600, "expires_at": NOW - 1}
        self.assertIn("expired", pause.switch_problems(switch, NOW))

    def test_missing_creator_and_basis_are_reported(self):
        switch = {"scope": "mint", "created_at": NOW, "expires_at": None}
        problems = pause.switch_problems(switch, NOW)
        self.assertIn("no-source", problems)
        self.assertIn("no-basis", problems)

    def test_switch_without_a_timestamp_is_reported(self):
        switch = {"scope": "mint", "source": "manual", "basis": "operator request",
                  "created_at": 0, "expires_at": None}
        self.assertIn("no-timestamp", pause.switch_problems(switch, NOW))

    def test_open_ended_switch_goes_stale_after_the_window(self):
        switch = {"scope": "lane:M1", "source": "manual", "basis": "operator request",
                  "created_at": NOW - pause.DEFAULT_MAX_AGE_S - 1, "expires_at": None}
        self.assertIn("stale", pause.switch_problems(switch, NOW))
        # a switch with a deadline is governed by that deadline, not by its age
        switch["expires_at"] = NOW + 60
        self.assertNotIn("stale", pause.switch_problems(switch, NOW))

    def test_review_row_carries_the_provenance_and_the_problems(self):
        switch = {"scope": "mint", "source": "", "basis": "", "created_at": 0,
                  "expires_at": None, "reason": "was missing on gocc ctl"}
        row = pause.review_switch(switch, NOW)
        self.assertTrue(row["needs_review"])
        self.assertEqual(row["scope"], "mint")
        self.assertEqual(row["reason"], "was missing on gocc ctl")
        self.assertIn("no-source", row["problems"])


class MarkerSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-marker-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)

    def marker(self, name, body="", age_s=0):
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def sound_marker(self, name, body="operator hold", created_at=None, age_s=0):
        """A marker that records its creator, its creation time and its basis."""
        created_at = time.time() - 60 if created_at is None else created_at
        text = pause.format_marker(body=body, source="operator:main", basis="issue #1669",
                                   created_at=created_at)
        path = self.marker(name, text, age_s=age_s)
        return path

    def test_marker_names_are_mapped_to_scopes(self):
        self.assertEqual(pause.marker_scope("PAUSE"), "all")
        self.assertEqual(pause.marker_scope("PAUSE-MINT"), "mint")
        self.assertEqual(pause.marker_scope("PAUSE-M1"), "lane:M1")
        self.assertEqual(pause.marker_scope("PAUSE-HOST-gocc"), "host:gocc")
        self.assertEqual(pause.marker_scope("PAUSE-BACKEND-pi"), "backend:pi")
        self.assertEqual(pause.marker_scope("OTHER"), "")

    def test_scopes_map_back_to_marker_names(self):
        self.assertEqual(pause.marker_name("all"), "PAUSE")
        self.assertEqual(pause.marker_name("mint"), "PAUSE-MINT")
        self.assertEqual(pause.marker_name("lane:M1"), "PAUSE-M1")
        self.assertEqual(pause.marker_name("host:gocc"), "PAUSE-HOST-gocc")
        self.assertEqual(pause.marker_name("backend:pi"), "PAUSE-BACKEND-pi")

    def test_empty_marker_has_no_creator_and_no_basis(self):
        path = self.marker("PAUSE-DISPATCH")
        switch = pause.marker_switch(path)
        self.assertEqual(switch["source"], "")
        self.assertEqual(switch["basis"], "")
        problems = pause.switch_problems(switch, time.time())
        self.assertIn("no-source", problems)
        self.assertIn("no-basis", problems)
        self.assertIn("no-timestamp", problems)

    def test_auto_marker_names_its_creator(self):
        path = self.marker("PAUSE-BACKEND-pi", "auto:quota:M5:402 until=4600\n")
        switch = pause.marker_switch(path)
        self.assertEqual(switch["source"], "auto:quota")
        self.assertIn("auto:quota", switch["basis"])
        self.assertEqual(switch["expires_at"], 4600.0)

    def test_marker_with_provenance_is_honored(self):
        path = self.sound_marker("PAUSE-M1")
        switch = pause.marker_switch(path)
        self.assertEqual(switch["source"], "operator:main")
        self.assertEqual(switch["basis"], "issue #1669")
        self.assertEqual(pause.switch_problems(switch, time.time()), [])

    def test_plain_marker_body_names_no_creator(self):
        # a body nobody signed is not a provenance: the switch is refused
        path = self.marker("PAUSE-M1", "operator hold\n")
        switch = pause.marker_switch(path)
        self.assertEqual(switch["source"], "")
        self.assertEqual(switch["basis"], "")
        problems = pause.switch_problems(switch, time.time())
        self.assertIn("no-source", problems)
        self.assertIn("no-basis", problems)

    def test_marker_restored_from_a_snapshot_is_refused(self):
        # the incident: the body of an old switch, copied back into place
        path = self.marker("PAUSE-DISPATCH", "was missing on gocc ctl\n")
        switch = pause.marker_switch(path)
        problems = pause.switch_problems(switch, time.time())
        self.assertIn("no-source", problems)
        self.assertIn("no-basis", problems)

    def test_copied_marker_keeps_its_creation_time(self):
        # a copy of an old marker refreshes its mtime: the declared creation
        # time is what keeps it from looking fresh
        created_at = time.time() - pause.DEFAULT_MAX_AGE_S - 60
        path = self.sound_marker("PAUSE-DISPATCH", created_at=created_at)
        os.utime(path, (time.time(), time.time()))
        switch = pause.marker_switch(path)
        self.assertEqual(switch["created_at"], created_at)
        self.assertIn("stale", pause.switch_problems(switch, time.time()))

    def test_copied_legacy_auto_marker_requires_review(self):
        path = self.marker("PAUSE-BACKEND-pi", "auto:quota:M5:402\n")
        old = time.time() - pause.DEFAULT_MAX_AGE_S - 60
        os.utime(path, (old, old))
        snapshot = path.read_text()
        path.write_text(snapshot)
        os.utime(path, None)
        switch = pause.marker_switch(path)
        self.assertIn("no-timestamp", pause.switch_problems(switch, time.time()))

    def test_marker_deadline_in_the_past_expires_the_switch(self):
        path = self.marker("PAUSE-BACKEND-pi", "auto:cooldown:until=1000\n")
        switch = pause.marker_switch(path)
        self.assertEqual(switch["expires_at"], 1000.0)
        self.assertIn("expired", pause.switch_problems(switch, time.time()))

    def test_marker_older_than_the_window_is_stale(self):
        path = self.sound_marker("PAUSE-DISPATCH", created_at=time.time() - pause.DEFAULT_MAX_AGE_S - 60)
        switch = pause.marker_switch(path)
        self.assertIn("stale", pause.switch_problems(switch, time.time()))

    def test_legacy_markers_keep_their_body_and_their_provenance(self):
        self.sound_marker("PAUSE-CX5")
        records = pause.legacy_markers(self.dir)
        self.assertEqual([row["scope"] for row in records], ["lane:CX5"])
        self.assertEqual(records[0]["basis"], "issue #1669")
        self.assertEqual(records[0]["source"], "operator:main")
        self.assertGreater(records[0]["created_at"], 0)


class StoreProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-store-")
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.store = pause.PauseStore(self.home)

    def test_a_record_keeps_its_creator_and_basis(self):
        record = self.store.pause("lane:M1", ttl_s=600, reason="disk maintenance", now=NOW,
                                  source="operator:main", basis="issue #1669")
        self.assertEqual(record["source"], "operator:main")
        self.assertEqual(record["basis"], "issue #1669")
        stored = json.loads((self.home / "pauses" / "lane_M1.json").read_text())
        self.assertEqual(stored["source"], "operator:main")

    def test_the_reason_is_the_default_basis(self):
        record = self.store.pause("lane:M1", now=NOW, reason="disk maintenance")
        self.assertEqual(record["basis"], "disk maintenance")

    def test_a_record_without_a_creator_records_no_creator(self):
        # nobody stated a creator: no value is invented for it
        record = self.store.pause("lane:M1", now=NOW)
        self.assertEqual(record["source"], "")
        self.assertEqual(record["basis"], "")
        self.assertEqual(self.store.effective(NOW), [])
        problems = self.store.review(NOW)[0]["problems"]
        self.assertIn("no-source", problems)
        self.assertIn("no-basis", problems)

    def test_a_record_without_provenance_is_not_honored(self):
        # a record restored from an older snapshot: no creator, no basis
        path = self.home / "pauses" / "lane_M1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"scope": "lane:M1", "created_at": NOW - 10,
                                    "expires_at": None, "reason": "was missing on gocc ctl"}))
        self.assertEqual(self.store.active(NOW)[0]["scope"], "lane:M1")
        self.assertEqual(self.store.effective(NOW), [])
        self.assertFalse(self.store.is_paused(lane="M1", now=NOW))
        self.assertTrue(self.store.review(NOW)[0]["needs_review"])

    def test_a_stale_record_without_a_deadline_is_not_honored(self):
        self.store.pause("lane:M1", reason="operator request", now=NOW - pause.DEFAULT_MAX_AGE_S - 1)
        self.assertEqual(self.store.effective(NOW), [])
        self.assertIn("stale", self.store.review(NOW)[0]["problems"])

    def test_a_record_with_a_deadline_is_honored_until_it_passes(self):
        self.store.pause("lane:M1", ttl_s=60, reason="operator request", now=NOW,
                         source="operator:main", basis="issue #1669")
        self.assertTrue(self.store.is_paused(lane="M1", now=NOW))
        self.assertFalse(self.store.is_paused(lane="M1", now=NOW + 60))

    def test_a_record_without_a_creator_is_not_honored_even_with_a_deadline(self):
        self.store.pause("lane:M1", ttl_s=60, reason="operator request", now=NOW)
        self.assertFalse(self.store.is_paused(lane="M1", now=NOW))
        self.assertIn("no-source", self.store.review(NOW)[0]["problems"])


if __name__ == "__main__":
    unittest.main()
