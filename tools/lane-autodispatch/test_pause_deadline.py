import sys
from pathlib import Path as _FixturePath
sys.path.insert(0, str(_FixturePath(__file__).resolve().parent))
from pr_guard_fixtures import no_open_prs
#!/usr/bin/env python3
"""The deadline of a legacy pause marker (Refs #1592).

A `PAUSE-*` marker is a switch without a clock: a manual `touch PAUSE-BACKEND-pi` holds
the backend until an operator removes it. The health loop writes a deadline into the body
instead (`auto:cooldown:until=<epoch|ISO>`, `tools/check-health-pause.py`), and the quota
loop writes `mm_remains:until=<ISO>` (`tools/check-health-quota-fresh.py`). The dispatcher
has to read that deadline: a cooldown that expired must not hold the lane for good, and
the marker file stays on disk as the evidence of the hold.

This file pins that contract on the dispatcher of the control plane
(`tools/lane-autodispatch/autodispatch.py`): the deadline is read from the real body
shapes, a naive ISO timestamp is UTC (like in the health loop), an expired marker is
logged once and never removed, and `pause_reason` tells the operator which switch holds a
lane and for how long.

Everything runs on fixtures inside a temporary directory: the live state directory of the
dispatcher and the live pause store are never read or written.

验收：`python3 -m unittest discover -s tools/lane-autodispatch -p 'test_pause_deadline.py' -v`
"""
import importlib.util
import json
from card_fixtures import fixture_json
import os
import shutil
import tempfile
import time
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH = REPO_ROOT / ".swrm-tmp"  # gitignored scratch root


def scratch_dir():
    """The scratch root of the tests: inside the workspace when it is writable.

    A read-only checkout falls back to the temporary directory of the system."""
    try:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        return str(SCRATCH)
    except OSError:
        return None
AD = REPO_ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

SPEC = importlib.util.spec_from_file_location("autodispatch", AD)
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class PauseDeadlineHarness(unittest.TestCase):
    """The dispatcher under test, with the markers and the pause store in a scratch tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pause-deadline-", dir=scratch_dir()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the legacy markers live in the state directory of the dispatcher
        self.state = self.tmp / "ad-state"
        self.state.mkdir(parents=True)
        # the pause store lives in the swrm home of the runtime
        self.home = self.tmp / "swrm-home"
        (self.home / "pauses").mkdir(parents=True)
        self.saved = {"D": AUTODISPATCH.D, "home": os.environ.get("OPENSWARM_HOME")}
        AUTODISPATCH.D = str(self.state)
        os.environ["OPENSWARM_HOME"] = str(self.home)
        self.addCleanup(self.restore)
        AUTODISPATCH.EXPIRED_PAUSE_LOGGED.clear()  # the expiry log is once per marker
        self.logs = []
        self.saved_log = AUTODISPATCH.log
        AUTODISPATCH.log = lambda msg, brain=True: self.logs.append(msg)
        self.addCleanup(self.restore_log)

    def restore(self):
        AUTODISPATCH.D = self.saved["D"]
        if self.saved["home"] is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = self.saved["home"]

    def restore_log(self):
        AUTODISPATCH.log = self.saved_log

    def marker(self, name, body=""):
        """One legacy PAUSE marker file in the state directory of the dispatcher."""
        (self.state / name).write_text(body, encoding="utf-8")

    def store_pause(self, scope, ttl=None, reason="operator"):
        """One pause record in the pause store (the shape the pause CLI writes)."""
        record = {"scope": scope, "created_at": time.time(),
                  "expires_at": None if ttl is None else time.time() + ttl,
                  "reason": reason}
        (self.home / "pauses" / (scope.replace(":", "_") + ".json")).write_text(
            json.dumps(record), encoding="utf-8")

    def paused(self, lane="CX1", cfg=None):
        """Whether the dispatcher refuses the lane."""
        cfg = cfg or {"host": "h1", "backend": "pi"}
        return bool(AUTODISPATCH.lane_paused(lane, cfg))


class PauseDeadlineTests(PauseDeadlineHarness):
    """The deadline of the legacy markers: the shapes the health loop really writes."""

    def test_an_expired_cooldown_releases_the_lane(self):
        """`auto:cooldown:until=<epoch>` in the past holds no lane any more."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) - 60}\n")
        self.assertFalse(self.paused())

    def test_a_cooldown_that_has_not_expired_still_holds_the_lane(self):
        """The same body with a deadline in the future keeps the hold."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) + 600}\n")
        self.assertTrue(self.paused())

    def test_the_quota_body_of_the_health_loop_expires_the_marker(self):
        """`mm_remains:until=<ISO>` is the other body the health loop writes."""
        self.marker("PAUSE-BACKEND-pi", "mm_remains:until=2020-01-01T00:00:00+00:00\n")
        self.assertFalse(self.paused())
        self.marker("PAUSE-BACKEND-pi", "mm_remains:until=2099-01-01T00:00:00+00:00\n")
        self.assertTrue(self.paused())

    def test_a_naive_iso_deadline_is_utc(self):
        """A deadline without a timezone is UTC, like in the health loop.

        The control plane runs on UTC+8: a deadline read in the local timezone would be
        eight hours off, and a cooldown would end eight hours too late (or too early).
        """
        now = time.time()
        naive = datetime.fromtimestamp(now + 3600, tz=timezone.utc).replace(tzinfo=None).isoformat()
        expected = datetime.fromisoformat(naive).replace(tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(AUTODISPATCH.pause_until(f"auto:cooldown:until={naive}"), expected,
                               places=3)
        self.assertGreater(expected - now, 3000, "the deadline moved with the local timezone")
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={naive}\n")
        self.assertTrue(self.paused(), "a deadline an hour ahead (UTC) still holds the lane")

    def test_a_marker_without_a_deadline_never_expires(self):
        """A manual hold carries no deadline: it holds until an operator removes it."""
        self.marker("PAUSE-BACKEND-pi", "operator hold\n")
        self.assertTrue(self.paused())

    def test_an_unreadable_deadline_keeps_the_hold(self):
        """A deadline that cannot be read never releases the lane."""
        self.marker("PAUSE-BACKEND-pi", "auto:cooldown:until=whenever\n")
        self.assertTrue(self.paused())

    def test_an_expired_marker_is_logged_once_and_stays_on_disk(self):
        """The expiry is reported once, and the marker file is never removed."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) - 60}\n")
        AUTODISPATCH.pause_records()
        AUTODISPATCH.pause_records()
        expired = [line for line in self.logs if "expired" in line]
        self.assertEqual(len(expired), 1, f"the expiry must be logged once, got {expired}")
        self.assertTrue((self.state / "PAUSE-BACKEND-pi").exists(),
                        "the expired marker stays on disk as evidence")

    def test_the_pause_reason_names_the_scope_and_the_remaining_seconds(self):
        """`pause_reason` tells the operator which switch holds the lane, and for how long."""
        self.store_pause("lane:CX1", ttl=600, reason="operator hold")
        reason = AUTODISPATCH.pause_reason("CX1", {"host": "h1", "backend": "pi"})
        self.assertIsNotNone(reason)
        self.assertEqual(reason["scope"], "lane:CX1")
        self.assertEqual(reason["source"], "store")
        self.assertEqual(reason["reason"], "operator hold")
        self.assertTrue(0 < reason["remaining_s"] <= 600, reason)

    def test_the_pause_reason_of_a_legacy_marker_carries_its_body(self):
        """The body of a legacy marker is part of the reason (the operator needs it)."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) + 600}\n")
        reason = AUTODISPATCH.pause_reason("CX1", {"host": "h1", "backend": "pi"})
        self.assertIsNotNone(reason)
        self.assertEqual(reason["scope"], "backend:pi")
        self.assertEqual(reason["source"], "marker")
        self.assertIn("auto:cooldown:until=", reason["reason"])
        self.assertTrue(0 < reason["remaining_s"] <= 600, reason)

    def test_a_lane_that_is_not_paused_has_no_reason(self):
        """No switch holds the lane: there is nothing to report."""
        self.assertIsNone(AUTODISPATCH.pause_reason("CX1", {"host": "h1", "backend": "pi"}))

    def test_the_global_pause_marker_wins_over_the_other_switches(self):
        """The global PAUSE is read first: it holds every lane."""
        self.store_pause("lane:CX1")
        self.marker("PAUSE")
        reason = AUTODISPATCH.pause_reason("CX2", {"host": "h2", "backend": "codex"})
        self.assertIsNotNone(reason)
        self.assertEqual(reason["scope"], "all")
        self.assertEqual(reason["source"], "marker")

    def test_the_pause_home_can_be_injected(self):
        """The home of the pause store is a parameter: a dry run reads its own store."""
        self.assertEqual(AUTODISPATCH.pause_store_dir("/tmp/pause-home"), "/tmp/pause-home/pauses")

    def test_an_empty_openswarm_home_keeps_the_store_path_absolute(self):
        """An empty $OPENSWARM_HOME is no home at all: the fallback stays absolute."""
        os.environ["OPENSWARM_HOME"] = ""
        store = AUTODISPATCH.pause_store_dir()
        self.assertTrue(os.path.isabs(store), store)
        self.assertTrue(store.endswith(os.path.join(".claude", "swrm", "pauses")), store)


class PauseTickTests(PauseDeadlineHarness):
    """The tick path: an expired cooldown must not keep the lane idle for good.

    The decision is not enough: the tick has to dispatch the card of a lane whose
    cooldown expired, and the marker file has to stay on disk (the health loop owns the
    removal of a marker, the dispatcher only stops honouring it).
    """

    LANES = {"CX1": {"host": "h1", "backend": "pi"}}
    ISSUES = [{"number": 12, "title": "card", "body": "", "labels": ["lane-ready"]}]

    def run_tick(self):
        """One tick with GitHub, ssh and the dispatch faked; returns the dispatched pairs."""
        dispatched = []

        @no_open_prs

        def fake_gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                return 0, fixture_json(self.ISSUES), ""
            if args[:2] == ["issue", "view"]:
                number = int(args[2])
                for row in self.ISSUES:
                    if row["number"] == number:
                        return 0, json.dumps({"number": number, "state": "OPEN",
                                               "labels": row["labels"]}), ""
                return 0, json.dumps({"number": number, "state": "CLOSED", "labels": []}), ""
            return 0, "", ""

        def fake_dispatch(lane, cfg, item, base):
            dispatched.append((lane, item["n"]))
            return "run-1", None

        patched = ("gh", "sh", "host_ok", "host_fail", "host_good", "remote", "deps_met",
                   "dispatch", "replenish", "save_state", "status_snapshot")
        saved = {name: getattr(AUTODISPATCH, name) for name in patched}
        try:
            AUTODISPATCH.gh = fake_gh
            AUTODISPATCH.sh = lambda *a, **k: (0, "0" * 40 + "\n", "")
            AUTODISPATCH.host_ok = lambda host: True
            AUTODISPATCH.host_fail = lambda host, why: None
            AUTODISPATCH.host_good = lambda host: None
            AUTODISPATCH.remote = lambda lane, cfg, run="": {"lane": "idle"}
            AUTODISPATCH.deps_met = lambda item: True
            AUTODISPATCH.dispatch = fake_dispatch
            AUTODISPATCH.replenish = lambda k, state: []
            AUTODISPATCH.save_state = lambda state: None
            AUTODISPATCH.status_snapshot = lambda *a, **k: {}
            AUTODISPATCH.VERIFY_REJECT_LOGGED.clear()
            # tick() writes lanes-status.json without closing it: the warning is pre-existing
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                AUTODISPATCH.tick(dict(self.LANES), {}, 0)
        finally:
            for name, value in saved.items():
                setattr(AUTODISPATCH, name, value)
        return dispatched

    def test_an_expired_cooldown_lets_the_lane_take_a_card_again(self):
        """The tick dispatches again once the deadline of the marker passed."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) - 60}\n")
        self.assertEqual(self.run_tick(), [("CX1", 12)],
                         "the expired cooldown still holds the lane")
        self.assertTrue((self.state / "PAUSE-BACKEND-pi").exists(),
                        "the expired marker stays on disk as evidence")

    def test_a_cooldown_that_has_not_expired_still_holds_the_lane(self):
        """A cooldown in the future keeps the lane idle."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) + 600}\n")
        self.assertEqual(self.run_tick(), [], "the lane took a card although it is on cooldown")


if __name__ == "__main__":
    unittest.main()
