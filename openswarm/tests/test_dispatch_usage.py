"""The burn attribution of a run: the usage of a lane and the fleet table (Refs #1690).

The delivery mapping of the review rounds of PR #1802 (the mapping lives in the
diff, in the module docstring of ``openswarm/lib/dispatch/usage.py`` too):

    B1  the 15 minute and one hour windows are recomputed against the clock of
        the moment they are written, so a lane that went idle ages out of the
        window instead of keeping the numbers of its last run
        -> ``usage.window``/``usage.lane_view``
        -> ``WindowTests.test_window_is_recomputed_against_the_current_clock``,
           ``WindowTests.test_an_idle_lane_drops_out_of_the_window``
    B2  the newest run of a lane wins even when it reported no usage: the lane
        then reads "not collected" instead of the numbers of an older run
        -> ``usage.normalize``/``usage.last_run``
        -> ``LastRunTests.test_the_newest_run_without_usage_stays_missing``
    B3  the health check prints the fleet burn table (per lane, per host
        subtotal and the hour total, with the input and the cache share)
        -> ``usage.fleet_table``/``usage.render_table`` and the fleet health
           command of ``openswarm/bin/openswarm``
        -> ``HealthCheckTests.test_health_check_prints_the_fleet_burn_table``
    B4  no unrelated ELF build artifact in the change
        -> the tracked ``openswarm/tests/.*.so`` files are removed
    B5  the integration coverage of the run end, the remote collection and the
        dispatcher tick
        -> ``RunEndTests.test_the_run_end_writes_the_canonical_usage``,
           ``RemoteCollectionTests.test_the_remote_scan_reads_the_runs``,
           ``DispatcherTickTests.test_the_tick_writes_the_burn_of_every_lane``
    B6  the health check reads the state directory the dispatcher really uses
        -> ``dispatch_state_dir``/``dispatch_lanes_status_path``
        -> ``HealthCheckTests.test_the_default_dispatch_state_dir_is_read``,
           ``HealthCheckTests.test_health_check_prints_the_fleet_burn_table``
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

_spec = importlib.util.spec_from_file_location("dispatch_usage_under_test", ROOT / "lib" / "dispatch" / "usage.py")
usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usage)

_engine_spec = importlib.util.spec_from_file_location(
    "dispatch_engine_usage_under_test", ROOT / "lib" / "dispatch" / "engine.py")
E = importlib.util.module_from_spec(_engine_spec)
_engine_spec.loader.exec_module(E)


def remote_usage_scan(home):
    """Run the remote usage scan of the engine against a home, in process."""
    buffer = io.StringIO()
    argv = sys.argv
    sys.argv = ["usage", home]
    try:
        with contextlib.redirect_stdout(buffer):
            exec(E.REMOTE_USAGE, {"__name__": "__main__"})
    finally:
        sys.argv = argv
    return json.loads(buffer.getvalue().strip().splitlines()[-1])


class NormalizeTests(unittest.TestCase):
    """The backend shapes fold into one canonical record."""

    def test_the_backend_shapes_fold_into_the_canonical_record(self):
        pi = usage.normalize({"input": 120, "output": 30, "cacheRead": 7, "cacheWrite": 2})
        cursor = usage.normalize({"inputTokens": 120, "outputTokens": 30,
                                  "cacheReadTokens": 7, "cacheWriteTokens": 2})
        self.assertEqual(pi, {"input": 120, "output": 30, "cache_read": 7, "cache_write": 2})
        self.assertEqual(cursor, pi)

    def test_a_run_without_a_reading_is_not_collected(self):
        self.assertIsNone(usage.normalize(None))
        self.assertIsNone(usage.normalize({}))
        self.assertFalse(usage.collected({"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}))

    def test_the_input_and_the_output_are_always_kept(self):
        counts = usage.normalize({"input": 900, "output": 100})
        self.assertEqual(counts["input"], 900)
        self.assertEqual(counts["output"], 100)
        self.assertEqual(usage.total(counts), 1000)
        self.assertEqual(usage.cache_total(counts), 0)

class WindowTests(unittest.TestCase):
    """B1: the windows are recomputed against the clock of the caller."""

    def test_window_is_recomputed_against_the_current_clock(self):
        samples = [usage.sample("r1", "GH1", 1000.0, {"input": 100, "output": 10})]
        fresh = usage.window(samples, 1100.0, 900)
        self.assertEqual(fresh["total"], 110)
        self.assertEqual(fresh["measured"], 1)
        # the same sample, read later: it left the window
        stale = usage.window(samples, 1100.0 + 901, 900)
        self.assertEqual(stale["total"], 0)
        self.assertEqual(stale["measured"], 0)

    def test_an_idle_lane_drops_out_of_the_window(self):
        samples = [usage.sample("r1", "GH1", 4000.0, {"input": 100, "output": 10}),
                   usage.sample("r2", "GH1", 5000.0, {"input": 50, "output": 5})]
        view = usage.lane_view(samples, 5000.0)
        self.assertEqual(view["window_15m"]["total"], 55)
        self.assertEqual(view["window_1h"]["total"], 165)
        # 16 minutes later the first run left the 15 minute window, the hour keeps it
        later = usage.lane_view(samples, 5000.0 + 960)
        self.assertEqual(later["window_15m"]["total"], 0)
        self.assertEqual(later["window_1h"]["total"], 165)

    def test_a_window_without_a_measurement_reads_as_missing(self):
        view = usage.lane_view([usage.sample("r1", "GH1", 1000.0, {})], 1000.0)
        self.assertEqual(view["window_15m"]["measured"], 0)
        row = usage.render_table(usage.fleet_view(
            {"GH1": {"host": "h1", "samples": [usage.sample("r1", "GH1", 1000.0, {})]}}, 1000.0))[2]
        self.assertIn("未采集", row)


class LastRunTests(unittest.TestCase):
    """B2: the newest run wins, even when it reported nothing."""

    def test_the_newest_run_without_usage_stays_missing(self):
        samples = [usage.sample("r1", "GH1", 1500.0, {"input": 100, "output": 10}),
                   usage.sample("r2", "GH1", 2000.0, {})]
        view = usage.lane_view(samples, 2000.0)
        self.assertEqual(view["last"]["run"], "r2")
        self.assertFalse(view["last"]["collected"])
        self.assertIsNone(view["last"]["usage"])
        # the numbers of the older run are not reused
        self.assertEqual(view["window_15m"]["total"], 110)

    def test_the_newest_run_with_usage_is_reported(self):
        samples = [usage.sample("r1", "GH1", 1000.0, {"input": 100, "output": 10}),
                   usage.sample("r2", "GH1", 2000.0, {"input": 7, "output": 3})]
        view = usage.lane_view(samples, 2000.0)
        self.assertTrue(view["last"]["collected"])
        self.assertEqual(view["last"]["usage"]["input"], 7)

    def test_a_lane_without_any_run_is_missing(self):
        view = usage.lane_view([], 1000.0)
        self.assertFalse(view["last"]["collected"])
        self.assertEqual(view["window_15m"]["measured"], 0)


class FleetTests(unittest.TestCase):
    """The fleet table: per lane, per host subtotal and the fleet total."""

    def lanes(self):
        return {"GH1": {"host": "h1", "samples": [usage.sample("r1", "GH1", 1000.0,
                                                               {"input": 800, "output": 100,
                                                                "cacheRead": 100})]},
                "GH2": {"host": "h1", "samples": [usage.sample("r2", "GH2", 1000.0,
                                                               {"input": 200, "output": 20})]},
                "GH3": {"host": "h2", "samples": [usage.sample("r3", "GH3", 1000.0, {})]}}

    def test_the_host_subtotals_and_the_fleet_total(self):
        view = usage.fleet_view(self.lanes(), 1000.0)
        hosts = {row["host"]: row for row in view["hosts"]}
        self.assertEqual(hosts["h1"]["window_1h"]["total"], 1220)
        self.assertEqual(hosts["h2"]["window_1h"]["total"], 0)
        self.assertEqual(view["window_1h"]["total"], 1220)

    def test_the_input_and_the_cache_shares_are_reported(self):
        view = usage.fleet_view(self.lanes(), 1000.0)
        shares = usage.shares(view["window_1h"])
        self.assertEqual(shares["input"], 82.0)
        self.assertEqual(shares["cache"], 8.2)

    def test_the_table_of_a_lane_status_snapshot(self):
        status = {"GH1": {"host": "h1", "usage": usage.lane_view(
            [usage.sample("r1", "GH1", 1000.0, {"input": 800, "output": 100})], 1000.0)}}
        table = usage.fleet_table(status)
        self.assertEqual(table["window_1h"]["total"], 900)
        self.assertEqual(table["hosts"][0]["host"], "h1")
        self.assertIsNone(usage.fleet_table({"GH1": {"host": "h1"}}))

    def test_the_table_names_the_missing_readings(self):
        table = usage.fleet_table({"GH1": {"host": "h1", "usage": usage.lane_view(
            [usage.sample("r1", "GH1", 1000.0, {})], 1000.0)}})
        lines = usage.render_table(table)
        self.assertIn("未采集", "\n".join(lines))
        self.assertTrue(any(line.startswith("fleet 1h total") for line in lines))


class RunEndTests(unittest.TestCase):
    """B5: the run end writes the canonical usage into the run status."""

    def test_the_run_end_writes_the_canonical_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = pathlib.Path(temp) / "runs"
            run_dir = runs / "run"
            run_dir.mkdir(parents=True)
            old = {key: getattr(xs, key) for key in ("RUNS", "LEDGER")}
            xs.RUNS = str(runs)
            xs.LEDGER = str(runs / "ledger.jsonl")
            spec = {"run_id": "run", "task": "test", "started": 1, "budget_minutes": 1,
                    "timings": {"prepare_s": 0, "first_event_s": 0, "model_s": 0, "tools_s": 0,
                                "verify_s": 0, "push_s": 0, "turns": 1, "tools_top": []}}
            ws = {"root": temp, "path": temp, "repo": temp, "mode": "in-place"}
            usage_reading = {"input": 900, "output": 120, "cacheRead": 30, "cacheWrite": 4}
            with mock.patch.object(xs, "NOW", return_value=30), \
                 mock.patch.object(xs, "git", return_value=(0, "", "")), \
                 mock.patch.object(xs, "finalize_workspace", return_value={
                     "branch": None, "commit": None, "diffstat": "", "excluded_files": [],
                     "patch": None, "worktree": temp, "merge_hint": None}), \
                 mock.patch.object(xs, "append_ledger"), mock.patch.object(xs, "cleanup_executor_tmp"):
                xs.lane_finalize_run({"name": "L", "backend": "pi", "model": "m"}, spec, ws,
                                     None, usage_reading, "budget_exceeded", 124,
                                     str(run_dir / "events.jsonl"),
                                     attempts=[{"seconds": 20, "error_tail": "deadline"}])
            status = json.loads((run_dir / "status.json").read_text())
            self.assertEqual(status["usage"], {"input": 900, "output": 120,
                                               "cache_read": 30, "cache_write": 4})
            for key, value in old.items():
                setattr(xs, key, value)

    def test_a_run_without_a_reading_writes_no_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = pathlib.Path(temp) / "run"
            run_dir.mkdir(parents=True)
            with mock.patch.object(xs, "NOW", return_value=30):
                xs.record_executor_descendants(run_dir, 0, {})
            self.assertNotIn("usage", json.loads((run_dir / "status.json").read_text()))

    def test_the_running_turn_samples_its_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = pathlib.Path(temp) / "run"
            run_dir.mkdir(parents=True)
            with mock.patch.object(xs, "NOW", return_value=30), \
                 mock.patch.object(xs, "executor_descendants_fields", return_value=[]):
                xs.record_executor_descendants(run_dir, 0, {"input": 12, "output": 3})
            self.assertEqual(json.loads((run_dir / "status.json").read_text())["usage"],
                             {"input": 12, "output": 3, "cache_read": 0, "cache_write": 0})


class RemoteCollectionTests(unittest.TestCase):
    """B5: the remote collection reads the runs of a host."""

    def home(self, temp):
        home = pathlib.Path(temp) / "executor"
        runs = home / "runs"
        (runs / "20261103-010000-aaaaaa").mkdir(parents=True)
        (runs / "20261103-010000-aaaaaa" / "result.json").write_text(json.dumps(
            {"lane": "GH1", "status": "done", "usage": {"input": 800, "output": 90,
                                                        "cacheRead": 20, "cacheWrite": 5}}))
        (runs / "20261103-010000-aaaaaa" / "status.json").write_text(json.dumps(
            {"lane": "GH1", "state": "finished", "started": 900.0, "finished": 1000.0}))
        (runs / "20261103-020000-bbbbbb").mkdir(parents=True)
        (runs / "20261103-020000-bbbbbb" / "result.json").write_text(json.dumps(
            {"lane": "GH1", "status": "done"}))
        (runs / "20261103-020000-bbbbbb" / "status.json").write_text(json.dumps(
            {"lane": "GH1", "state": "finished", "started": 1900.0, "finished": 2000.0}))
        (runs / "20261103-030000-cccccc").mkdir(parents=True)
        (runs / "20261103-030000-cccccc" / "status.json").write_text(json.dumps(
            {"lane": "GH2", "state": "started", "started": 2500.0,
             "usage": {"input": 10, "output": 2, "cache_read": 0, "cache_write": 0}}))
        return home

    def test_the_remote_scan_reads_the_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            home = self.home(temp)
            samples = remote_usage_scan(str(home))["samples"]
            self.assertEqual(len(samples), 3)
            newest = samples[-1]
            self.assertEqual(newest["lane"], "GH2")
            self.assertTrue(newest["running"])
            self.assertEqual(newest["usage"]["input"], 10)

    def test_a_run_without_a_reading_is_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = self.home(temp)
            samples = remote_usage_scan(str(home))["samples"]
            without = [row for row in samples if row["run"] == "20261103-020000-bbbbbb"][0]
            self.assertIsNone(without["usage"])


class DispatcherTickTests(unittest.TestCase):
    """B5: the tick writes the burn of every lane into the lane status."""

    def dispatcher(self, temp, home, clock):
        cfg_path = pathlib.Path(temp) / "dispatch.json"
        policy_path = pathlib.Path(temp) / "policy.json"
        policy_path.write_text(json.dumps({"claude": {"default_model": "claude-haiku-4.5"}}))
        state_dir = pathlib.Path(temp) / "state"
        state_dir.mkdir()
        cfg_path.write_text(json.dumps({
            "repo": "a/b", "local_repo": temp, "state_dir": str(state_dir),
            "policy_file": str(policy_path), "remote_home": str(home),
            "local_hosts": ["h1"],
            "ssh": {"control_dir": str(pathlib.Path(temp) / "cm")}}))
        cfg = E.load_config(str(cfg_path))

        def runner(cmd, timeout=120, inp=None, env=None):
            if inp == E.REMOTE_USAGE:
                return 0, json.dumps(remote_usage_scan(cmd[-1])), ""
            return 0, "", ""

        return E.Dispatcher(cfg, runner=runner, clock=lambda: clock, out=io.StringIO())

    def status(self, dispatcher, lanes):
        dispatcher.probe = lambda _lanes, _state: {name: {"lane": "idle"} for name in lanes}
        dispatcher.harvest = lambda *_args: None
        dispatcher.sweep_leaked_inprogress = lambda *_args: None
        dispatcher.queue = lambda: []
        dispatcher.dispatch_reviews = lambda *_args: None
        dispatcher.assign_issues = lambda *_args: None
        dispatcher.replenish = lambda *_args: None
        dispatcher.tick(lanes, {})
        with open(dispatcher.path("lanes-status.json"), encoding="utf-8") as stream:
            return json.load(stream)

    def test_the_tick_writes_the_burn_of_every_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            home = pathlib.Path(temp) / "executor"
            runs = home / "runs"
            (runs / "20261103-010000-aaaaaa").mkdir(parents=True)
            (runs / "20261103-010000-aaaaaa" / "result.json").write_text(json.dumps(
                {"lane": "GH1", "status": "done", "usage": {"input": 800, "output": 90,
                                                            "cacheRead": 20, "cacheWrite": 5}}))
            (runs / "20261103-010000-aaaaaa" / "status.json").write_text(json.dumps(
                {"lane": "GH1", "state": "finished", "started": 900.0, "finished": 1000.0}))
            (runs / "20261103-020000-bbbbbb").mkdir(parents=True)
            (runs / "20261103-020000-bbbbbb" / "result.json").write_text(json.dumps(
                {"lane": "GH2", "status": "done"}))
            (runs / "20261103-020000-bbbbbb" / "status.json").write_text(json.dumps(
                {"lane": "GH2", "state": "finished", "started": 500.0, "finished": 600.0}))
            dispatcher = self.dispatcher(temp, home, 1000.0)
            lanes = {"GH1": {"backend": "pi", "host": "h1", "model": "m"},
                     "GH2": {"backend": "pi", "host": "h1", "model": "m"}}
            status = self.status(dispatcher, lanes)
            first = status["GH1"]["usage"]
            self.assertTrue(first["last"]["collected"])
            self.assertEqual(first["last"]["usage"]["input"], 800)
            self.assertEqual(first["last"]["usage"]["cache_read"], 20)
            self.assertEqual(first["window_15m"]["total"], 915)
            self.assertEqual(first["window_1h"]["total"], 915)
            second = status["GH2"]["usage"]
            self.assertFalse(second["last"]["collected"])
            self.assertIsNone(second["last"]["usage"])
            self.assertEqual(second["window_15m"]["measured"], 0)
            self.assertEqual(status["_summary"]["burn_1h"]["total"], 915)


class HealthCheckTests(unittest.TestCase):
    """B3 and B6: the health check prints the fleet burn table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-burn-health-")
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.root / "home"
        self.home.mkdir()
        self.previous = {name: os.environ.get(name) for name in
                         ("OPENSWARM_DISPATCH_STATE_DIR", "OPENSWARM_DISPATCH_CONFIG")}
        self.addCleanup(self.restore)
        os.environ.pop("OPENSWARM_DISPATCH_CONFIG", None)
        os.environ["OPENSWARM_DISPATCH_STATE_DIR"] = str(self.root / "dispatch")

    def restore(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def snapshot(self, lanes):
        state_dir = self.root / "dispatch"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "lanes-status.json").write_text(json.dumps(lanes))

    def health(self, json_out=False):
        import argparse

        args = argparse.Namespace(hosts=None, json=json_out, apply=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = xs.cmd_fleet_health(args, policy={}, runner=lambda host, command: "",
                                         hosts={"h1": {"name": "h1", "kind": "local"}},
                                         now=1000.0, home=self.home)
        return report, output.getvalue()

    def test_the_default_dispatch_state_dir_is_read(self):
        os.environ.pop("OPENSWARM_DISPATCH_STATE_DIR", None)
        os.environ.pop("OPENSWARM_DISPATCH_CONFIG", None)
        self.assertEqual(xs.dispatch_lanes_status_path(),
                         os.path.join(os.path.expanduser("~"), ".claude", "openswarm-dispatch",
                                      "lanes-status.json"))

    def test_health_check_prints_the_fleet_burn_table(self):
        self.snapshot({"GH1": {"host": "h1", "usage": usage.lane_view(
            [usage.sample("r1", "GH1", 1000.0, {"input": 800, "output": 90, "cacheRead": 20})], 1000.0)},
            "GH2": {"host": "h2", "usage": usage.lane_view(
                [usage.sample("r2", "GH2", 1000.0, {})], 1000.0)}})
        report, text = self.health()
        self.assertTrue(report["burn"]["collected"])
        self.assertEqual(report["burn"]["window_1h"]["total"], 910)
        self.assertIn("fleet burn", text)
        self.assertIn("host h1: 1h 910", text)
        self.assertIn("fleet 1h total: 910", text)
        self.assertIn("未采集", text)

    def test_health_check_without_a_snapshot_says_so(self):
        report, text = self.health()
        self.assertFalse(report["burn"]["collected"])
        self.assertIn("no lane status snapshot", text)


if __name__ == "__main__":
    unittest.main()
