#!/usr/bin/env python3
"""The pause decision of the dispatcher (Refs #1592).

#1592 asks the dispatch engine to read **both** pause switches in one decision: the
pause store with its TTL and its scopes (`$OPENSWARM_HOME/pauses`) and the legacy
`PAUSE-*` marker files, plus the global `PAUSE` marker that stops the whole loop. A
legacy marker that carries its own deadline (`until=<epoch|ISO>`, written by the health
loop) must stop holding the lane once that deadline passed.

The upstream engine (`openswarm/lib/dispatch/engine.py`) was removed in #1894 ("vendored
openswarm replaced by swrm as the single source"): the pause decision of the control
plane now lives in `tools/lane-autodispatch/autodispatch.py`, and this file pins the
contract of #1592 on that dispatcher.

Everything runs on fixtures inside a temporary directory: the live state directory of the
dispatcher and the live pause store are never read or written.

验收：`python3 -m pytest -q openswarm/tests/test_dispatch_engine.py`
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AD = REPO_ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
SCRATCH = REPO_ROOT / ".swrm-tmp"  # gitignored scratch root: no temporary file leaves the workspace


def scratch_dir():
    """The scratch root of the tests: inside the workspace when it is writable.

    A read-only checkout falls back to the temporary directory of the system."""
    try:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        return str(SCRATCH)
    except OSError:
        return None

_spec = importlib.util.spec_from_file_location("ad_dispatch", AD)
DISPATCH = importlib.util.module_from_spec(_spec)
sys.modules["ad_dispatch"] = DISPATCH
_spec.loader.exec_module(DISPATCH)


class DispatchPauseHarness(unittest.TestCase):
    """The dispatcher under test, with the markers and the pause store in a scratch tree."""

    LANES = {"CX1": {"host": "h1", "backend": "pi"},
             "CX2": {"host": "h2", "backend": "codex"}}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dispatch-engine-pause-", dir=scratch_dir()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the legacy markers live in the state directory of the dispatcher
        self.state = self.tmp / "ad-state"
        self.state.mkdir(parents=True)
        # the pause store lives in the swrm home of the runtime
        self.home = self.tmp / "swrm-home"
        (self.home / "pauses").mkdir(parents=True)
        self.saved = {"D": DISPATCH.D, "home": os.environ.get("OPENSWARM_HOME")}
        DISPATCH.D = str(self.state)
        os.environ["OPENSWARM_HOME"] = str(self.home)
        self.addCleanup(self.restore)

    def restore(self):
        DISPATCH.D = self.saved["D"]
        if self.saved["home"] is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = self.saved["home"]

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

    def paused(self, lane, cfg=None):
        """Whether the dispatcher refuses the lane."""
        cfg = cfg or self.LANES.get(lane, {"host": "h1", "backend": "pi"})
        return bool(DISPATCH.lane_paused(lane, cfg))


class DispatchPauseTests(DispatchPauseHarness):
    """The decision itself: which switches are read and what they pause."""

    def test_the_global_pause_marker_stops_every_lane(self):
        """The legacy `PAUSE` marker stops the whole loop, not one lane."""
        self.marker("PAUSE")
        self.assertTrue(self.paused("CX1"))
        self.assertTrue(self.paused("CX2"))

    def test_the_global_scope_of_the_pause_store_stops_every_lane(self):
        """The `all` scope of the pause store is the same switch as the global marker."""
        self.store_pause("all")
        self.assertTrue(self.paused("CX1"))
        self.assertTrue(self.paused("CX2"))

    def test_the_store_scopes_stop_only_the_matching_lane(self):
        """`lane:X`, `host:H` and `backend:B` are three different scopes."""
        for scope, lane, other in (("lane:CX1", "CX1", "CX2"),
                                   ("host:h1", "CX1", "CX2"),
                                   ("backend:pi", "CX1", "CX2")):
            with self.subTest(scope=scope):
                self.store_pause(scope)
                self.assertTrue(self.paused(lane))
                self.assertFalse(self.paused(other))
                (self.home / "pauses" / (scope.replace(":", "_") + ".json")).unlink()

    def test_an_expired_store_record_is_not_a_pause(self):
        """The TTL of the pause store is honoured: an expired record pauses nobody."""
        self.store_pause("lane:CX1", ttl=-1)
        self.assertFalse(self.paused("CX1"))

    def test_the_legacy_markers_stop_the_matching_lane(self):
        """The old marker files keep working next to the pause store."""
        for name, lane in (("PAUSE-CX1", "CX1"), ("PAUSE-HOST-h1", "CX1"), ("PAUSE-BACKEND-pi", "CX1")):
            with self.subTest(marker=name):
                self.marker(name, "operator hold\n")
                self.assertTrue(self.paused(lane))
                self.assertFalse(self.paused("CX2"))
                (self.state / name).unlink()

    def test_a_legacy_marker_with_a_future_deadline_still_stops_the_lane(self):
        """A cooldown that has not expired yet still holds the lane."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) + 600}\n")
        self.assertTrue(self.paused("CX1"))

    def test_a_legacy_marker_whose_deadline_passed_releases_the_lane(self):
        """A cooldown that expired stops nobody, and the marker stays as evidence.

        The health loop writes `auto:cooldown:until=<deadline>` into the marker
        (``tools/check-health-pause.py``).  The deadline belongs to the marker: once it
        is past, the lane takes new work again.
        """
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) - 60}\n")
        self.assertFalse(self.paused("CX1"),
                         "an expired cooldown must not stop the lane any more")
        self.assertTrue((self.state / "PAUSE-BACKEND-pi").exists(),
                        "the expired marker stays on disk as evidence")

    def test_the_decision_reads_both_switches_at_once(self):
        """The store and the legacy markers are one decision, not two."""
        self.store_pause("lane:CX2")
        self.marker("PAUSE-HOST-h1")
        self.assertTrue(self.paused("CX1"), "the legacy host marker holds CX1")
        self.assertTrue(self.paused("CX2"), "the store record holds CX2")

    def test_the_pause_reason_names_the_scope_and_the_remaining_seconds(self):
        """`pause_reason` tells the operator which switch holds the lane, and for how long."""
        self.store_pause("lane:CX1", ttl=600, reason="operator hold")
        reason = DISPATCH.pause_reason("CX1", self.LANES["CX1"])
        self.assertIsNotNone(reason)
        self.assertEqual(reason["scope"], "lane:CX1")
        self.assertEqual(reason["source"], "store")
        self.assertEqual(reason["reason"], "operator hold")
        self.assertTrue(0 < reason["remaining_s"] <= 600, reason)
        self.assertIsNone(DISPATCH.pause_reason("CX2", self.LANES["CX2"]))


if __name__ == "__main__":
    unittest.main()
