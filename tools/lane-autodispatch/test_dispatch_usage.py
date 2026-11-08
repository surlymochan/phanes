#!/usr/bin/env python3
"""The burn of the lanes reaches the dispatcher's snapshot (Refs #1690).

The chain of the feature is exercised end to end here: the executor records
the token usage of a finished run and the staged counts of a running run, the
remote collector reads those samples on the host that owns the runs, and the
dispatcher tick turns them into the per lane burn of ``lanes-status.json``.

Delivery mapping (Refs #1690, PR #1802) -- blocker -> change -> test -> result
B1 the windows are recomputed against the clock of the tick
    -> tools/lane-autodispatch/autodispatch.py:933 lane_burn() and :955 status_snapshot()
    -> BurnTickTest.test_run_end_feeds_the_dispatcher_tick -- PASS
B2 the newest run without a reading stays missing
    -> tools/lane-autodispatch/autodispatch.py:921 usage_samples() keeps the final record of a run
    -> BurnTickTest.test_the_newest_run_without_a_reading_is_not_collected -- PASS
B3 the fleet burn table with the host subtotal and the hour total
    -> swrm/bin/dispatch_usage.py:316 fleet_table(), :408 render_table(); swrm/bin/swrm:9823 fleet_burn_report()
    -> BurnTickTest.test_the_health_check_reads_the_snapshot_of_the_tick -- PASS
B5 integration coverage: run end -> remote collection -> dispatcher tick
    -> the executor writes the samples (swrm/bin/swrm:7073 lane_append_usage(), :7106 lane_record_staged_usage()),
       the collector reads them (tools/lane-autodispatch/autodispatch.py:620 REMOTE_STATUS), the tick writes the snapshot
    -> BurnTickTest.test_run_end_feeds_the_dispatcher_tick -- PASS
B6 the health check reads the dispatcher's own status directory (the default one at least)
    -> swrm/bin/swrm:9809 dispatch_lanes_status_path() (default ~/phanes-ctl/autodispatch/lanes-status.json)
    -> BurnTickTest.test_the_health_check_reads_the_snapshot_of_the_tick -- PASS
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location("autodispatch", HERE / "autodispatch.py")
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

SCRATCH = ROOT / ".swrm-tmp"  # gitignored scratch root


def load_swrm(home):
    """The executor, loaded with its own state home (the lane home of the test)."""
    os.environ["OPENSWARM_HOME"] = str(home)
    path = ROOT / "swrm" / "bin" / "swrm"
    loader = importlib.machinery.SourceFileLoader("swrm_usage_test", str(path))
    spec = importlib.util.spec_from_loader("swrm_usage_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BurnTickTest(unittest.TestCase):
    """run end -> remote collection -> dispatcher tick -> lanes-status.json."""

    LANES = {"L1": {"host": "h2", "backend": "pi"}}

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="ad-usage-", dir=SCRATCH)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.ctl = self.root / "ctl"
        self.home.mkdir()
        self.ctl.mkdir()
        self.saved_home = os.environ.get("OPENSWARM_HOME")
        self.swrm = load_swrm(self.home)

    def tearDown(self):
        if self.saved_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = self.saved_home
        self.tmp.cleanup()

    def lane_state(self, lane, state="busy", run=""):
        directory = self.home / "lanes" / lane
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text(json.dumps({"state": state, "current_run": run}))

    def collect(self, lane, run=""):
        """The remote collector of the dispatcher, run against the lane home."""
        env = dict(os.environ, OPENSWARM_HOME=str(self.home))
        proc = subprocess.run([sys.executable, "-", lane, run], input=AUTODISPATCH.REMOTE_STATUS,
                              text=True, capture_output=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def tick(self, remote):
        """One dispatcher tick with the side effects faked; returns the snapshot."""
        logs = []
        patched = ("D", "gh", "sh", "log", "host_ok", "host_fail", "host_good", "remote", "deps_met",
                   "dispatch", "replenish", "save_state")
        saved = {name: getattr(AUTODISPATCH, name) for name in patched}
        try:
            AUTODISPATCH.D = str(self.ctl)
            AUTODISPATCH.gh = lambda args, timeout=60: (0, json.dumps([]), "")
            AUTODISPATCH.sh = lambda *a, **k: (0, "", "")
            AUTODISPATCH.log = lambda msg, brain=True: logs.append(msg)
            AUTODISPATCH.host_ok = lambda host: True
            AUTODISPATCH.host_fail = lambda host, why: None
            AUTODISPATCH.host_good = lambda host: None
            AUTODISPATCH.remote = remote
            AUTODISPATCH.deps_met = lambda item: True
            AUTODISPATCH.dispatch = lambda lane, cfg, item, base: ("run-1", None)
            AUTODISPATCH.replenish = lambda count, state: []
            AUTODISPATCH.save_state = lambda state: None
            # tick() writes lanes-status.json without closing it: the warning is pre-existing
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                AUTODISPATCH.tick(dict(self.LANES), {}, 0)
        finally:
            for name, value in saved.items():
                setattr(AUTODISPATCH, name, value)
        return json.loads((self.ctl / "lanes-status.json").read_text())

    def test_run_end_feeds_the_dispatcher_tick(self):
        now = time.time()
        # the run end of a finished run with a reading
        self.swrm.lane_append_usage("L1", "r1", {"input": 1200, "output": 300,
                                                  "cacheRead": 400, "cacheWrite": 0}, now - 300)
        # the staged counts of the run that is still going
        running = self.home / "runs" / "r2"
        running.mkdir(parents=True)
        self.swrm.lane_record_staged_usage(str(running), {"input": 10, "output": 5})
        self.lane_state("L1", "busy", "r2")

        collected = self.collect("L1", "r2")
        samples = collected["usage_samples"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["usage"]["input"], 1200)
        self.assertEqual(samples[0]["usage"]["cache_read"], 400)
        self.assertTrue(samples[1]["staged"])

        status = self.tick(lambda lane, cfg, run="": collected)
        burn = status["_burn"]["L1"]
        self.assertEqual(burn["host"], "h2")
        self.assertEqual(burn["last"]["run"], "r2")           # the running run is the newest
        self.assertEqual(burn["last"]["usage"]["input"], 10)
        # the 15 minute window holds the finished run and the staged sample
        self.assertEqual(burn["window_15m"]["total"], 1200 + 300 + 400 + 10 + 5)
        self.assertEqual(burn["window_15m"]["measured"], 2)
        self.assertEqual(status["_summary"]["burn_15m"]["total"], burn["window_15m"]["total"])
        self.assertEqual(status["_summary"]["burn_1h"]["total"], burn["window_1h"]["total"])

    def test_the_newest_run_without_a_reading_is_not_collected(self):
        now = time.time()
        self.swrm.lane_append_usage("L1", "r1", {"input": 1200, "output": 300}, now - 300)
        self.swrm.lane_append_usage("L1", "r2", {}, now - 60)
        self.lane_state("L1", "idle")

        collected = self.collect("L1")
        self.assertEqual(len(collected["usage_samples"]), 2)

        status = self.tick(lambda lane, cfg, run="": collected)
        burn = status["_burn"]["L1"]
        # the newest run is the one without a reading: it is missing, not the older numbers
        self.assertEqual(burn["last"]["run"], "r2")
        self.assertFalse(burn["last"]["collected"])
        self.assertIsNone(burn["last"]["usage"])
        # the measured run still counts in the window
        self.assertEqual(burn["window_15m"]["total"], 1500)
        self.assertEqual(burn["window_15m"]["measured"], 1)

    def test_an_unreachable_host_is_not_a_zero_burn(self):
        self.lane_state("L1", "unknown")
        status = self.tick(lambda lane, cfg, run="": {"error": "ssh status timeout"})
        burn = status["_burn"]["L1"]
        self.assertFalse(burn["last"]["collected"])
        self.assertEqual(burn["window_15m"]["measured"], 0)
        # the health check prints the missing label, not a zero
        report = self.swrm.fleet_burn_report(str(self.ctl))
        self.assertTrue(report["collected"])
        text = "\n".join(self.swrm.dispatch_usage_module().render_table(report["table"]))
        self.assertIn(self.swrm.dispatch_usage_module().MISSING_LABEL, text)

    def test_the_health_check_reads_the_snapshot_of_the_tick(self):
        now = time.time()
        self.swrm.lane_append_usage("L1", "r1", {"input": 1000, "output": 200}, now - 60)
        self.lane_state("L1", "idle")
        collected = self.collect("L1")
        status = self.tick(lambda lane, cfg, run="": collected)
        self.assertIn("_burn", status)

        report = self.swrm.fleet_burn_report(str(self.ctl))
        self.assertTrue(report["collected"])
        self.assertEqual(report["window_1h"]["total"], 1200)
        self.assertEqual(report["hosts"][0]["host"], "h2")
        text = "\n".join(self.swrm.dispatch_usage_module().render_table(report["table"]))
        self.assertIn("fleet 1h total: 1,200", text)


if __name__ == "__main__":
    unittest.main()
