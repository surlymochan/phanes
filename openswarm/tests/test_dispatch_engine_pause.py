"""Dispatch engine pauses: global PAUSE, PauseStore scopes/TTL and legacy PAUSE-* markers (Refs #1592)."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_pause_under_test",
    os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"),
)
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)

NOW = 1_800_000_000.0          # 2027-01-15T08:26:40Z


class DispatchEnginePause(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-pause-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = [NOW]
        cfg = {"repo": "acme/project", "state_dir": os.path.join(self.tmp, "state"),
               "pause_home": os.path.join(self.tmp, "pause-home"), "local_repo": self.tmp,
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}}
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w", encoding="utf-8") as stream:
            json.dump(cfg, stream)
        self.dispatcher = E.Dispatcher(E.load_config(config), clock=lambda: self.now[0],
                                       dry_run=True, out=open(os.devnull, "w"))
        self.addCleanup(self.dispatcher.out.close)
        self.lc = {"host": "host1", "backend": "pi"}

    def marker(self, name, content=""):
        os.makedirs(self.dispatcher.D, exist_ok=True)
        with open(self.dispatcher.path(name), "w", encoding="utf-8") as stream:
            stream.write(content)

    def store(self, home=None):
        return E.dispatch_module("pause").PauseStore(home or self.dispatcher.pause_home)

    def test_global_marker_pauses_every_lane(self):
        self.marker("PAUSE")
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))
        self.assertTrue(self.dispatcher.paused_lane("CX2", {"host": "host2", "backend": "codex"}))
        self.assertEqual(self.dispatcher.pause_reason("CX1", self.lc)["scope"], "all")

    def test_pause_store_ttl_expires(self):
        self.store().pause("backend:pi", ttl_s=10, reason="cooldown", now=self.now[0])
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))
        self.now[0] += 10
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))

    def test_pause_store_scopes_are_read_from_the_pause_home(self):
        for scope, lane, lc in (("lane:CX1", "CX1", self.lc),
                                ("host:host1", "CX1", self.lc),
                                ("backend:pi", "CX1", self.lc)):
            with self.subTest(scope=scope):
                self.store().pause(scope, reason="operator", now=self.now[0])
                self.assertTrue(self.dispatcher.paused_lane(lane, lc))
                self.assertEqual(self.dispatcher.pause_reason(lane, lc)["scope"], scope)
                self.store().resume(scope)
                self.assertFalse(self.dispatcher.paused_lane(lane, lc))

    def test_pause_store_in_the_state_dir_is_still_honored(self):
        # the engine writes its own scoped pauses (model_unavailable) into <state_dir>/pauses
        E.dispatch_module("pause").PauseStore(self.dispatcher.D).pause(
            "lane:CX1", ttl_s=600, reason="auto:model_unavailable", now=self.now[0])
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))

    def test_health_marker_body_expires_once_without_removal(self):
        name = "PAUSE-BACKEND-pi"
        self.marker(name, "mm_remains:until=2027-01-15T08:00:00+00:00\n")
        logged = []
        self.dispatcher.log = lambda message, brain=True: logged.append(message)
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))
        self.assertTrue(os.path.exists(self.dispatcher.path(name)))
        self.dispatcher.paused_lane("CX1", self.lc)
        self.dispatcher.paused_lane("CX1", self.lc)
        self.assertEqual(sum("expired pause marker" in line for line in logged), 1)
        self.assertTrue(os.path.exists(self.dispatcher.path(name)))

    def test_health_epoch_marker_until_future_still_pauses(self):
        name = "PAUSE-BACKEND-pi"
        self.marker(name, "auto:cooldown:until=1800000100\n")
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))
        reason = self.dispatcher.pause_reason("CX1", self.lc)
        self.assertEqual(reason["scope"], "backend:pi")
        self.assertEqual(reason["remaining_s"], 100)
        self.assertIn("auto:cooldown", reason["reason"])

    def test_engine_quota_marker_body_expires(self):
        name = "PAUSE-BACKEND-pi"
        self.marker(name, "auto:quota:M6:rate_limit until=1800000100\n")
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))
        self.now[0] = 1_800_000_100.0
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))

    def test_tick_preserves_expired_quota_marker(self):
        name = "PAUSE-BACKEND-pi"
        self.marker(name, "auto:quota:M6:rate_limit until=1799999999\n")
        logged = []
        self.dispatcher.log = lambda message, brain=True: logged.append(message)
        self.dispatcher.dry_run = False
        self.dispatcher.queue = lambda: []
        self.dispatcher.replenish = lambda *args: None
        self.dispatcher.tick({}, {})
        self.assertTrue(os.path.exists(self.dispatcher.path(name)))
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))
        self.assertEqual(sum("expired pause marker" in line for line in logged), 1)
        self.assertTrue(os.path.exists(self.dispatcher.path(name)))

    def test_legacy_lane_host_backend_scopes(self):
        for name, scope, lane, lc in (
            ("PAUSE-CX1", "lane:CX1", "CX1", self.lc),
            ("PAUSE-HOST-host1", "host:host1", "CX1", self.lc),
            ("PAUSE-BACKEND-pi", "backend:pi", "CX1", self.lc),
        ):
            with self.subTest(name=name):
                self.marker(name, "operator hold\n")
                self.assertTrue(self.dispatcher.paused_lane(lane, lc))
                reason = self.dispatcher.pause_reason(lane, lc)
                self.assertEqual(reason["scope"], scope)
                self.assertEqual(reason["reason"], "operator hold")
                os.unlink(self.dispatcher.path(name))
                self.assertFalse(self.dispatcher.paused_lane(lane, lc))

    def test_naive_iso_deadline_is_utc(self):
        self.marker("PAUSE-BACKEND-pi", "mm_remains:until=2027-01-15T09:00:00\n")
        reason = self.dispatcher.pause_reason("CX1", self.lc)
        self.assertTrue(self.dispatcher.paused_lane("CX1", self.lc))
        # 2027-01-15T09:00:00Z is 3600s away; a local-time reading would be in the past
        self.assertEqual(reason["remaining_s"], 3600)

    def test_naive_iso_deadline_in_the_past_releases_the_lane(self):
        self.marker("PAUSE-BACKEND-pi", "mm_remains:until=2027-01-15T07:00:00\n")
        self.assertFalse(self.dispatcher.paused_lane("CX1", self.lc))


if __name__ == "__main__":
    unittest.main()
