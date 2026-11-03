"""The dispatch engine reads the PAUSE switches: provenance and freshness (Refs #1669).

A PAUSE switch that carries no creator and no basis, or that is older than
the freshness window, cannot be told apart from a snapshot restored from an
older run (PAUSE-DISPATCH / PAUSE-MINT were restored on 2026-11-02 from a
2026-11-01 record and froze the dispatch for ten minutes).  The engine refuses
such a switch, warns about it and asks for a review instead.
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_pause_provenance_test",
    os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)

NOW = 1_800_000_000.0


class EnginePauseProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-pause-provenance-")
        self.addCleanup(self.tmp.cleanup)
        self.now = [NOW]
        self.state_dir = os.path.join(self.tmp.name, "state")
        cfg = {"repo": "acme/project", "state_dir": self.state_dir,
               "local_repo": self.tmp.name, "ssh": {"control_dir": os.path.join(self.tmp.name, "cm")},
               "local_hosts": [], "replenish": {"cmd": ["mint"], "skip_files": ["PAUSE-MINT", "NO_SEED_MINT"]}}
        config = os.path.join(self.tmp.name, "config.json")
        with open(config, "w", encoding="utf-8") as stream:
            json.dump(cfg, stream)
        self.calls = []
        self.dispatcher = E.Dispatcher(E.load_config(config), runner=self.record,
                                       clock=lambda: self.now[0], out=open(os.devnull, "w"))
        self.addCleanup(self.dispatcher.out.close)
        self.logged = []
        self.dispatcher.log = lambda message, brain=True: self.logged.append(message)
        self.lc = {"host": "host1", "backend": "pi"}

    def record(self, cmd, timeout=120, inp=None, env=None):
        self.calls.append(cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd))
        return 0, "", ""

    def marker(self, name, body="", age_s=0):
        os.makedirs(self.dispatcher.D, exist_ok=True)
        path = self.dispatcher.path(name)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(body)
        stamp = self.now[0] - age_s
        os.utime(path, (stamp, stamp))
        return path

    def sound_marker(self, name, body="operator hold", created_at=None):
        """A marker that records its creator, its creation time and its basis."""
        created_at = self.now[0] - 60 if created_at is None else created_at
        text = E.dispatch_module("pause").format_marker(
            body=body, source="operator:main", basis="issue #1669", created_at=created_at)
        return self.marker(name, text)

    def test_marker_with_a_basis_pauses_the_lane(self):
        self.sound_marker("PAUSE-CX1")
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))

    def test_unattributed_marker_is_refused_and_reported(self):
        self.marker("PAUSE-CX1")
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))
        row = self.dispatcher.pause_review["PAUSE-CX1"]
        self.assertTrue(row["needs_review"])
        self.assertIn("no-basis", row["problems"])
        self.assertIn("no-source", row["problems"])
        self.assertTrue(any("needs review" in line for line in self.logged))

    def test_marker_restored_from_an_old_snapshot_is_refused(self):
        # the incident: an old switch (2026-11-01) restored on 2026-11-02 --
        # the copy refreshes the mtime, the body names no creator
        self.marker("PAUSE-DISPATCH", "was missing on gocc ctl\n")
        self.assertFalse(self.dispatcher.paused_lane("DISPATCH", self.lc))
        problems = self.dispatcher.pause_review["PAUSE-DISPATCH"]["problems"]
        self.assertIn("no-source", problems)
        self.assertIn("no-basis", problems)

    def test_copied_marker_of_an_old_switch_is_still_stale(self):
        # the copy refreshes the mtime, the recorded creation time does not move
        created_at = self.now[0] - 48 * 3600
        self.sound_marker("PAUSE-DISPATCH", created_at=created_at)
        os.utime(self.dispatcher.path("PAUSE-DISPATCH"), (self.now[0], self.now[0]))
        self.assertFalse(self.dispatcher.paused_lane("DISPATCH", self.lc))
        self.assertIn("stale", self.dispatcher.pause_review["PAUSE-DISPATCH"]["problems"])

    def test_marker_with_a_passed_deadline_is_refused(self):
        self.marker("PAUSE-BACKEND-pi", "auto:cooldown:until=1000\n")
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))
        self.assertIn("expired", self.dispatcher.pause_review["PAUSE-BACKEND-pi"]["problems"])

    def test_pause_record_without_provenance_is_refused(self):
        store = E.dispatch_module("pause").PauseStore(self.dispatcher.D)
        os.makedirs(os.path.join(self.dispatcher.D, "pauses"), exist_ok=True)
        with open(os.path.join(self.dispatcher.D, "pauses", "lane_CX1.json"), "w", encoding="utf-8") as stream:
            json.dump({"scope": "lane:CX1", "created_at": NOW - 10, "expires_at": None,
                       "reason": "was missing on gocc ctl"}, stream)
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))
        self.assertTrue(store.review(NOW)[0]["needs_review"])

    def test_pause_record_with_provenance_pauses_the_lane(self):
        store = E.dispatch_module("pause").PauseStore(self.dispatcher.D)
        store.pause("lane:CX1", ttl_s=600, reason="disk maintenance", now=NOW,
                    source="operator:main", basis="issue #1669")
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))

    def test_review_pauses_reports_every_unsound_switch(self):
        self.marker("PAUSE-CX1")
        self.sound_marker("PAUSE-HOST-host1")
        self.dispatcher.review_pauses()
        self.assertIn("PAUSE-CX1", self.dispatcher.pause_review)
        self.assertNotIn("PAUSE-HOST-host1", self.dispatcher.pause_review)

    def test_unattributed_global_pause_does_not_stop_the_tick(self):
        self.marker("PAUSE")
        self.tick()
        self.assertTrue(os.path.exists(self.dispatcher.path("lanes-status.json")))
        self.assertIn("PAUSE", self.dispatcher.pause_review)

    def test_global_pause_with_a_basis_stops_the_tick(self):
        self.sound_marker("PAUSE")
        self.assertIsNone(self.tick())
        self.assertFalse(os.path.exists(self.dispatcher.path("lanes-status.json")))

    def test_unattributed_mint_pause_does_not_stop_the_minting(self):
        self.marker("PAUSE-MINT")
        self.dispatcher.replenish(3, {}, [], 0)
        self.assertTrue(any("mint" in call for call in self.calls))
        self.assertIn("PAUSE-MINT", self.dispatcher.pause_review)

    def test_mint_pause_with_a_basis_stops_the_minting(self):
        self.sound_marker("PAUSE-MINT")
        self.dispatcher.replenish(3, {}, [], 0)
        self.assertFalse(any("mint" in call for call in self.calls))

    def test_lane_status_publishes_the_review_list(self):
        self.marker("PAUSE-CX1")
        status = self.tick()
        self.assertEqual(status["_pause_review"][0]["marker"], "PAUSE-CX1")
        self.assertTrue(status["_pause_review"][0]["needs_review"])

    def test_quota_guard_takes_over_an_unsound_marker(self):
        # an unsound switch (a snapshot restored from an older run) must not
        # block the automatic quota protection
        self.marker("PAUSE-BACKEND-pi", "was missing on gocc ctl\n")
        hit = {"kind": "402", "cooldown_s": 60, "scope": "backend"}
        self.assertTrue(self.dispatcher.quota_pause("PAUSE-BACKEND-pi", "CX1", hit))
        with open(self.dispatcher.path("PAUSE-BACKEND-pi"), encoding="utf-8") as stream:
            body = stream.read()
        self.assertTrue(body.startswith("auto:quota:"))
        self.assertTrue(self.dispatcher.pause_switch_honored("PAUSE-BACKEND-pi"))

    def test_quota_guard_keeps_a_sound_manual_hold(self):
        # a manual hold somebody confirmed is never overwritten
        self.sound_marker("PAUSE-BACKEND-pi")
        hit = {"kind": "402", "cooldown_s": 60, "scope": "backend"}
        self.assertFalse(self.dispatcher.quota_pause("PAUSE-BACKEND-pi", "CX1", hit))
        with open(self.dispatcher.path("PAUSE-BACKEND-pi"), encoding="utf-8") as stream:
            body = stream.read()
        self.assertIn("operator:main", body)

    def test_quota_marker_records_its_provenance(self):
        hit = {"kind": "402", "cooldown_s": 60, "scope": "backend"}
        self.dispatcher.quota_pause("PAUSE-BACKEND-pi", "CX1", hit)
        switch = E.dispatch_module("pause").marker_switch(
            self.dispatcher.path("PAUSE-BACKEND-pi"), name="PAUSE-BACKEND-pi")
        self.assertEqual(switch["source"], "auto:quota")
        self.assertEqual(switch["created_at"], self.now[0])
        self.assertIn("quota signal", switch["basis"])
        self.assertEqual(E.dispatch_module("pause").switch_problems(switch, self.now[0]), [])

    def tick(self):
        """One tick with everything but the pause switches stubbed out."""
        self.dispatcher.probe = lambda _lanes, _state: {}
        self.dispatcher.harvest = lambda *_args: None
        self.dispatcher.sweep_leaked_inprogress = lambda *_args: None
        self.dispatcher.queue = lambda: []
        self.dispatcher.dispatch_reviews = lambda *_args: None
        self.dispatcher.assign_issues = lambda *_args: None
        self.dispatcher.replenish = lambda *_args: None
        self.dispatcher.tick({}, {})
        path = self.dispatcher.path("lanes-status.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)


if __name__ == "__main__":
    unittest.main()
