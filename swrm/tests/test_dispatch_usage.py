"""Per-run token usage: the helper, the executor recording and the health check (Refs #1690).

The control plane could see which lane was busy, but not what a run burned.
These tests cover the three places the burn is written and read: the executor
records the breakdown of a session, the helper folds the backend shapes into
one canonical record, and the health check turns the dispatcher's snapshot
into the fleet wide table.

Delivery mapping (Refs #1690, PR #1802) -- blocker -> change -> test -> result
B1 the windows are recomputed against the clock of the reader
    -> swrm/bin/dispatch_usage.py:223 window(), :268 lane_view()
    -> UsageWindowTests.test_window_drops_a_lane_that_went_idle -- PASS
B2 the newest run without a reading stays missing
    -> swrm/bin/dispatch_usage.py:126 normalize(), :253 last_run()
    -> UsageWindowTests.test_last_run_without_usage_is_not_collected,
       ExecutorUsageTests.test_a_run_without_a_reading_is_logged_as_missing -- PASS
B3 the fleet burn table with the host subtotal and the hour total
    -> swrm/bin/dispatch_usage.py:316 fleet_table(), :408 render_table(); swrm/bin/swrm:9916 prints the table
    -> UsageWindowTests.test_fleet_table_has_the_host_subtotal_and_the_hour_total,
       HealthCheckBurnTests.test_fleet_status_prints_the_burn_table -- PASS
B5 integration coverage: run end -> remote collection -> dispatcher tick
    -> swrm/tests/test_lane.py LaneTests.test_pi_run_records_the_token_usage_of_the_session (the real lane run),
       tools/lane-autodispatch/test_dispatch_usage.py BurnTickTest.test_run_end_feeds_the_dispatcher_tick (the tick)
    -> ExecutorUsageTests.test_run_end_records_the_usage_in_the_status_and_the_lane_log -- PASS
B6 the health check reads the dispatcher's own status directory (the default one at least)
    -> swrm/bin/swrm:9809 dispatch_lanes_status_path() (default ~/phanes-ctl/autodispatch/lanes-status.json)
    -> HealthCheckBurnTests.test_health_check_reads_the_default_dispatch_status_path,
       HealthCheckBurnTests.test_health_check_without_a_snapshot_reports_missing -- PASS
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import _load

xs = _load.load()
usage = xs.dispatch_usage_module()
CLI = pathlib.Path(_load.ROOT) / "bin" / "swrm"
NOW = 1_800_000_000.0


def snapshot_of(lanes, now=NOW):
    """A lanes-status snapshot of the given lanes, as the dispatcher writes it."""
    burn = {}
    for name, (host, samples) in lanes.items():
        view = usage.lane_view(samples, now)
        view["host"] = host
        burn[name] = view
    fleet = usage.fleet_view({name: {"host": host, "samples": samples}
                              for name, (host, samples) in lanes.items()}, now)
    return {"_at": "2026-11-08 12:00:00", "_burn": burn,
            "_summary": {"burn_15m": fleet["window_15m"], "burn_1h": fleet["window_1h"]}}


class UsageNormalizeTests(unittest.TestCase):
    """The canonical reading of a backend usage object."""

    def test_normalize_folds_the_backend_shapes(self):
        self.assertEqual(usage.normalize({"input": 3, "output": 2, "cacheRead": 1, "cacheWrite": 4}),
                         {"input": 3, "output": 2, "cache_read": 1, "cache_write": 4})
        self.assertEqual(usage.normalize({"inputTokens": 5, "outputTokens": 4,
                                           "cacheReadTokens": 2, "cacheWriteTokens": 1}),
                         {"input": 5, "output": 4, "cache_read": 2, "cache_write": 1})
        self.assertEqual(usage.normalize({"input_tokens": 7, "output_tokens": 6, "cache_read_tokens": 3}),
                         {"input": 7, "output": 6, "cache_read": 3, "cache_write": 0})
        # the codex shape of a finished turn
        self.assertEqual(usage.normalize({"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5}),
                         {"input": 100, "output": 5, "cache_read": 80, "cache_write": 0})

    def test_normalize_reports_a_missing_reading(self):
        # A run without a reading is missing, never a zero burn.
        self.assertIsNone(usage.normalize({}))
        self.assertIsNone(usage.normalize({"input": 0, "output": 0}))
        self.assertIsNone(usage.normalize(None))
        self.assertFalse(usage.collected({"inputTokens": 0, "outputTokens": 0}))
        self.assertTrue(usage.collected({"inputTokens": 1, "outputTokens": 0}))

    def test_shares_are_the_reading_of_the_console(self):
        shares = usage.shares({"input": 60, "output": 20, "cache_read": 20, "cache_write": 0})
        self.assertEqual(shares["input"], 60.0)
        self.assertEqual(shares["cache"], 20.0)


class UsageWindowTests(unittest.TestCase):
    """The windows of a lane are recomputed, never cached."""

    def test_window_drops_a_lane_that_went_idle(self):
        samples = [{"run": "r1", "at": NOW - 1800, "usage": {"input": 100, "output": 10}}]
        view = usage.lane_view(samples, NOW)
        self.assertEqual(view["window_15m"]["total"], 0)     # older than 15 minutes
        self.assertEqual(view["window_1h"]["total"], 110)
        # an hour later the same run has aged out of the hour as well
        later = usage.lane_view(samples, NOW + 3600)
        self.assertEqual(later["window_1h"]["total"], 0)
        self.assertEqual(later["window_15m"]["runs"], 0)
        # the last run itself is still the last run
        self.assertEqual(later["last"]["run"], "r1")

    def test_last_run_without_usage_is_not_collected(self):
        samples = [{"run": "r1", "at": NOW - 600, "usage": {"input": 100, "output": 10}},
                   {"run": "r2", "at": NOW - 60, "usage": None}]
        view = usage.lane_view(samples, NOW)
        self.assertEqual(view["last"]["run"], "r2")
        self.assertFalse(view["last"]["collected"])
        self.assertIsNone(view["last"]["usage"])
        # the older run still counts in the window, but the lane does not show its numbers
        self.assertEqual(view["window_15m"]["total"], 110)
        table = usage.fleet_table(snapshot_of({"L1": ("h1", samples)}))
        text = "\n".join(usage.render_table(table))
        self.assertIn(usage.MISSING_LABEL, text)
        self.assertNotIn("100", text.splitlines()[2])

    def test_fleet_table_has_the_host_subtotal_and_the_hour_total(self):
        lanes = {"L1": ("h1", [{"run": "r1", "at": NOW - 60, "usage": {"input": 100, "output": 10}}]),
                 "L2": ("h1", [{"run": "r2", "at": NOW - 60, "usage": {"input": 200, "output": 20}}]),
                 "L3": ("h2", [{"run": "r3", "at": NOW - 60, "usage": {"input": 300, "output": 30}}])}
        table = usage.fleet_table(snapshot_of(lanes))
        self.assertEqual([row["lane"] for row in table["lanes"]], ["L1", "L2", "L3"])
        self.assertEqual(table["window_15m"]["total"], 660)
        self.assertEqual(table["window_1h"]["total"], 660)
        hosts = {row["host"]: row for row in table["hosts"]}
        self.assertEqual(hosts["h1"]["window_1h"]["total"], 330)
        self.assertEqual(hosts["h2"]["window_1h"]["total"], 330)
        self.assertEqual(hosts["h1"]["lanes"], ["L1", "L2"])
        text = "\n".join(usage.render_table(table))
        self.assertIn("host h1: 1h 330", text)
        self.assertIn("fleet 1h total: 660", text)


class ExecutorUsageTests(unittest.TestCase):
    """The executor side: the run end and the staged sample of a running run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-usage-")
        self.root = pathlib.Path(self.tmp.name)
        self.saved = {key: getattr(xs, key) for key in ("DATA", "RUNS", "HOME", "NOW")}
        xs.DATA = str(self.root / "data")
        xs.RUNS = str(self.root / "data" / "runs")
        xs.HOME = str(self.root / "home")
        xs.NOW = lambda: NOW

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def test_run_end_records_the_usage_in_the_status_and_the_lane_log(self):
        rdir = self.root / "data" / "runs" / "r1"
        rdir.mkdir(parents=True)
        # a run that is still going publishes the counts of its last sample
        xs.lane_record_staged_usage(str(rdir), {"input": 3, "output": 2})
        status = json.loads((rdir / "status.json").read_text())
        self.assertEqual(status["usage"], {"input": 3, "output": 2, "cache_read": 0, "cache_write": 0})
        self.assertTrue(status["usage_staged"])
        # the run end writes the same counts into the lane's usage log
        xs.lane_append_usage("L1", "r1", {"input": 3, "output": 2}, NOW)
        lines = (self.root / "data" / "lanes" / "L1" / "usage.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[-1]),
                         {"run": "r1", "at": NOW,
                          "usage": {"input": 3, "output": 2, "cache_read": 0, "cache_write": 0}})

    def test_a_run_without_a_reading_is_logged_as_missing(self):
        xs.lane_append_usage("L1", "r2", {}, NOW)
        lines = (self.root / "data" / "lanes" / "L1" / "usage.jsonl").read_text().splitlines()
        self.assertIsNone(json.loads(lines[-1])["usage"])
        view = usage.lane_view([{"run": "r2", "at": NOW, "usage": None}], NOW)
        self.assertFalse(view["last"]["collected"])


class HealthCheckBurnTests(unittest.TestCase):
    """The health check reads the dispatcher's snapshot and prints the fleet table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-burn-")
        self.root = pathlib.Path(self.tmp.name)
        self.saved = {key: getattr(xs, key) for key in ("HOME",)}
        xs.HOME = str(self.root / "home")
        os.environ.pop("OPENSWARM_LANES_STATUS", None)
        os.environ.pop("OPENSWARM_AD_STATE_DIR", None)

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def write_snapshot(self, snapshot):
        path = pathlib.Path(xs.dispatch_lanes_status_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot))
        return path

    def test_health_check_reads_the_default_dispatch_status_path(self):
        # the default path is the one the dispatcher writes: ~/phanes-ctl/autodispatch
        self.assertEqual(xs.dispatch_lanes_status_path(),
                         os.path.join(str(self.root / "home"), "phanes-ctl", "autodispatch", "lanes-status.json"))
        self.write_snapshot(snapshot_of({"L1": ("h1", [{"run": "r1", "at": NOW, "usage": {"input": 100, "output": 10}}])}))
        report = xs.fleet_burn_report()
        self.assertTrue(report["collected"])
        self.assertEqual(report["window_1h"]["total"], 110)
        self.assertEqual(report["hosts"][0]["host"], "h1")
        self.assertEqual(report["lanes"][0]["lane"], "L1")

    def test_health_check_without_a_snapshot_reports_missing(self):
        report = xs.fleet_burn_report()
        self.assertFalse(report["collected"])
        self.assertIn(usage.MISSING_LABEL, "\n".join(usage.render_table(report["table"])))

    def test_fleet_status_prints_the_burn_table(self):
        self.write_snapshot(snapshot_of({"L1": ("h1", [{"run": "r1", "at": NOW, "usage": {"input": 100, "output": 10}}])}))
        env = dict(os.environ, HOME=str(self.root / "home"),
                   OPENSWARM_HOME=str(self.root / "state"))
        proc = subprocess.run([sys.executable, str(CLI), "fleet", "status"], env=env, text=True,
                              capture_output=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fleet burn", proc.stdout)
        self.assertIn("L1", proc.stdout)
        self.assertIn("110", proc.stdout)
        self.assertIn("fleet 1h total: 110", proc.stdout)


if __name__ == "__main__":
    unittest.main()
