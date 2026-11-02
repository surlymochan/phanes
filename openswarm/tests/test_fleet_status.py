"""Tests for the fleet status page (Refs #1718).

Two halves are checked here:

* the pure module ``openswarm/lib/dispatch/fleet_status.py``: the readings of
  every section, the per-section stamp and the unverified handling;
* the collector ``tools/fleet-status-page.py``: the replay of the 2026-11-02
  night data and the simulation of a section that cannot be collected.
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MODULE_PATH = ROOT / "openswarm" / "lib" / "dispatch" / "fleet_status.py"
TOOL_PATH = ROOT / "tools" / "fleet-status-page.py"
FIXTURE = HERE / "fixtures" / "fleet-status-2026-11-02"

spec = importlib.util.spec_from_file_location("dispatch_fleet_status", MODULE_PATH)
fleet_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fleet_status)

tool_spec = importlib.util.spec_from_file_location("fleet_status_page", TOOL_PATH)
tool = importlib.util.module_from_spec(tool_spec)
tool_spec.loader.exec_module(tool)

# 2026-11-02 21:05:00 UTC+8, the moment of the night shift.
NOW = 1793624700.0

# The collection time of every source of that night (epoch).
STAMPS = {
    "lanes-status.json": 1793624652.0,       # 21:04:12
    "gh-issues.json": 1793624695.0,          # 21:04:55
    "gh-prs.json": 1793624698.0,             # 21:04:58
    "capacity.json": 1793624698.0,           # 21:04:58
    "fleet-health.json": 1793624580.0,       # 21:03:00
    "broker-requests.jsonl": 1793624699.0,   # 21:04:59
}


def replay(root):
    """Copy the night fixture into ``root`` and stamp every source with its own time."""
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    for name, stamp in STAMPS.items():
        path = pathlib.Path(root) / name
        os.utime(path, (stamp, stamp))
    pauses = pathlib.Path(root) / "pauses"
    os.utime(pauses, (1793624600.0, 1793624600.0))
    markers = pathlib.Path(root) / "markers"
    os.utime(markers, (1793624600.0, 1793624600.0))
    for path in pauses.glob("*.json"):
        os.utime(path, (1793624600.0, 1793624600.0))
    for path in markers.iterdir():
        os.utime(path, (1793624600.0, 1793624600.0))


def set_lanes_at(root, at_text, mtime=None):
    """Rewrite the lane status snapshot with another ``_at`` stamp.

    The file keeps (or gets) its own mtime: the mtime is not the clock of the
    snapshot, so the two have to be able to disagree.
    """
    path = pathlib.Path(root) / "lanes-status.json"
    status = json.loads(path.read_text(encoding="utf-8"))
    if at_text is None:
        status.pop("_at", None)
    else:
        status["_at"] = at_text
    path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    stamp = STAMPS["lanes-status.json"] if mtime is None else mtime
    os.utime(path, (stamp, stamp))
    return path


def section_of(report, key):
    """Return one section of a page."""
    for row in report["sections"]:
        if row["key"] == key:
            return row
    raise AssertionError("no section " + key)


def args_for(root, **overrides):
    """The collector arguments of the replay."""
    base = pathlib.Path(root)
    values = {
        "lanes_status": str(base / "lanes-status.json"),
        "gh_issues": str(base / "gh-issues.json"),
        "gh_prs": str(base / "gh-prs.json"),
        "capacity": str(base / "capacity.json"),
        "capacity_db": None,
        "capacity_dir": None,
        "hosts": str(base / "fleet-health.json"),
        "broker_log": str(base / "broker-requests.jsonl"),
        "pauses": str(base / "pauses"),
        "markers": str(base / "markers"),
        "gh_live": False,
        "max_age": 300.0,
        "tz": 28800,
    }
    values.update(overrides)
    return type("Args", (), values)()


class LaneReadingTests(unittest.TestCase):
    """The busy/idle reading of the two lanes-status shapes."""

    def test_flat_shape_counts_busy_idle_and_unknown(self):
        status = {"M1": "busy #1711", "M2": "idle", "M3": "ssh: timed out",
                  "_busy": 1, "_ready": 9, "_at": "2026-11-02 21:04:12"}
        data = fleet_status.lane_data(status)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["busy"], 1)
        self.assertEqual(data["idle"], 1)
        self.assertEqual(data["unknown"], 1)
        self.assertEqual(data["in_flight"], [1711])
        self.assertEqual(data["reported_ready"], 9)
        self.assertEqual(data["at"], "2026-11-02 21:04:12")

    def test_schema_two_shape_is_read(self):
        status = {"schema": 2, "lanes": {"CX1": {"state": "busy #42", "issue": 42,
                                                 "backend": "codex", "host": "gocc"},
                                         "CX2": {"state": "idle"}},
                  "_busy": 1, "_at": "2026-11-02 21:04:12"}
        data = fleet_status.lane_data(status)
        self.assertEqual(data["busy"], 1)
        self.assertEqual(data["idle"], 1)
        self.assertEqual(data["in_flight"], [42])
        self.assertEqual(data["lanes"][0]["backend"], "codex")


class SnapshotStampTests(unittest.TestCase):
    """The clock of a snapshot is the ``_at`` it carries."""

    def test_the_snapshot_stamp_is_read_in_the_fleet_timezone(self):
        status = {"_at": "2026-11-02 21:04:12"}
        self.assertEqual(fleet_status.snapshot_at(status, 28800), 1793624652.0)

    def test_a_snapshot_without_a_usable_stamp_has_no_clock(self):
        self.assertIsNone(fleet_status.snapshot_at({}, 28800))
        self.assertIsNone(fleet_status.snapshot_at({"_at": "not a time"}, 28800))
        self.assertIsNone(fleet_status.snapshot_at({"_at": None}, 28800))
        self.assertIsNone(fleet_status.snapshot_at(None, 28800))

    def test_a_non_finite_stamp_has_no_clock(self):
        """``nan`` and ``inf`` are numbers, but no moment in time."""
        for text in ("nan", "NaN", "inf", "-inf", "Infinity", "1e400"):
            self.assertIsNone(fleet_status.epoch(text), text)
            self.assertIsNone(fleet_status.snapshot_at({"_at": text}, 28800), text)

    def test_a_stamp_that_is_no_date_has_no_stamp(self):
        """A stamp that cannot be printed must not take the page down."""
        self.assertIsNone(fleet_status.iso("nan"))
        self.assertIsNone(fleet_status.iso("inf"))
        self.assertIsNone(fleet_status.iso(float("nan")))
        self.assertIsNone(fleet_status.iso(1e300))


class PoolReadingTests(unittest.TestCase):
    """The pool depth in the two readings."""

    def test_two_readings_are_printed_side_by_side(self):
        issues = [{"number": 1, "state": "OPEN", "labels": [{"name": "lane-ready"}]},
                  {"number": 2, "state": "OPEN", "labels": ["lane-ready", "in-progress"]},
                  {"number": 3, "state": "OPEN", "labels": ["lane-ready", "backend:pi"]},
                  {"number": 4, "state": "CLOSED", "labels": ["lane-ready"]}]
        data = fleet_status.pool_data(issues, ad_ready=3)
        self.assertEqual(data["gh"]["ready"], 2)
        self.assertEqual(data["gh"]["labelled"], 4)
        self.assertEqual(data["gh"]["excluded"], 2)
        self.assertEqual(data["gh"]["by_backend"], {"pi": 1})
        self.assertEqual(data["ad"]["ready"], 3)
        self.assertEqual(data["delta"], -1)
        self.assertFalse(data["agree"])

    def test_without_a_dispatcher_reading_there_is_no_delta(self):
        data = fleet_status.pool_data([], ad_ready=None)
        self.assertIsNone(data["ad"]["ready"])
        self.assertIsNone(data["delta"])
        self.assertEqual(data["level"], "empty")


class CapacityReadingTests(unittest.TestCase):
    """The reservations and the age of the oldest one."""

    def test_oldest_reservation_decides_the_age(self):
        reservations = [{"host": "gocc", "run_id": "a", "backend": "codex", "created": 1000.0},
                        {"host": "gores1", "run_id": "b", "backend": "pi", "created": 1900.0}]
        data = fleet_status.capacity_data(reservations, 2000.0, {"total": 8, "codex": 1})
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["oldest_age_s"], 1000.0)
        self.assertEqual(data["oldest"]["run_id"], "a")
        self.assertEqual(data["by_host"], {"gocc": 1, "gores1": 1})
        self.assertEqual(data["limits"]["total"], {"used": 2, "limit": 8})


class HostReadingTests(unittest.TestCase):
    """One line per host."""

    def test_unreachable_host_is_down(self):
        state = {"hosts": {"gocc": {"status": "ok", "updated_at": "2026-11-02 21:03:00"},
                           "govpn": {"status": "unreachable", "updated_at": "2026-11-02 21:03:00"}}}
        data = fleet_status.hosts_data(state, NOW, 28800)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["alive"], 1)
        self.assertEqual(data["down"], 1)
        self.assertEqual(data["worst"], "unreachable")
        self.assertEqual(data["hosts"][1]["age_s"], 120.0)


class BurnReadingTests(unittest.TestCase):
    """The MiniMax consumption of the last 15 minutes."""

    def test_only_the_window_counts(self):
        rows = [{"ts": 1793624640.0, "lane": "M1", "status": 200, "in_tok": 10, "out_tok": 5},
                {"ts": 1793624670.0, "lane": "M2", "status": 429},
                {"ts": 1793623000.0, "lane": "M3", "status": 200}]
        data = fleet_status.burn_data(rows, NOW)
        self.assertEqual(data["requests"], 2)
        self.assertEqual(data["ok"], 1)
        self.assertEqual(data["rate_limited"], 1)
        self.assertEqual(data["in_tok"], 10)
        self.assertEqual(data["out_tok"], 5)
        self.assertEqual(data["by_lane"], {"M1": 1, "M2": 1})


class PauseReadingTests(unittest.TestCase):
    """The PAUSE switches."""

    def test_switches_keep_their_scope_and_age(self):
        records = [{"scope": "lane:CX5", "reason": "auto:health", "created_at": 1793624000.0,
                    "expires_at": 1793625800.0}]
        data = fleet_status.pause_data(records, NOW)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["switches"][0]["scope"], "lane:CX5")
        self.assertEqual(data["switches"][0]["age_s"], 700.0)
        self.assertEqual(data["switches"][0]["expires_in_s"], 1100.0)


class PageAssemblyTests(unittest.TestCase):
    """The page keeps every stamp and survives a broken section."""

    def test_a_broken_section_does_not_take_the_page_down(self):
        good = fleet_status.make_section("lanes", "车道", "lanes-status.json", NOW, {"busy": 1})
        broken = fleet_status.make_section("hosts", "主机", "fleet-health.json", None, {},
                                           "FileNotFoundError: no such file")
        report = fleet_status.aggregate([good, broken], now=NOW)
        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(report["summary"]["unverified"], 1)
        self.assertEqual(report["summary"]["unverified_sections"], ["hosts"])
        self.assertFalse(report["ok"])
        self.assertFalse(report["sections"][1]["verified"])
        self.assertIn("未核实", report["text"])
        self.assertIn("FileNotFoundError", report["text"])

    def test_an_old_section_is_marked_expired(self):
        old = fleet_status.make_section("lanes", "车道", "lanes-status.json", NOW - 600, {})
        report = fleet_status.aggregate([old], now=NOW, max_age_s=300.0)
        self.assertTrue(report["sections"][0]["stale"])
        self.assertEqual(report["sections"][0]["age_s"], 600.0)
        self.assertFalse(report["ok"])
        self.assertIn("过期", report["text"])

    def test_every_section_keeps_its_own_stamp_and_source(self):
        section = fleet_status.make_section("pool", "池", "gh issue list", NOW, {"ready": 1})
        report = fleet_status.aggregate([section], now=NOW)
        self.assertEqual(report["sections"][0]["collected_at"], "2026-11-02T13:05:00+00:00")
        self.assertEqual(report["sections"][0]["source"], "gh issue list")


class NightReplayTests(unittest.TestCase):
    """The replay of the 2026-11-02 night data."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-status-replay-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        replay(self.tmp)
        self.report = tool.build_page(args_for(self.tmp), NOW)

    def section(self, key):
        return section_of(self.report, key)

    def test_the_page_holds_every_section(self):
        self.assertEqual(self.report["summary"]["total"], 7)
        self.assertEqual(self.report["summary"]["unverified"], 0)
        self.assertEqual([row["key"] for row in self.report["sections"]],
                         ["lanes", "pool", "capacity", "hosts", "burn", "needs_human", "pauses"])

    def test_pool_shows_both_readings(self):
        pool = self.section("pool")["data"]
        self.assertEqual(pool["gh"]["ready"], 7)
        self.assertEqual(pool["ad"]["ready"], 9)
        self.assertEqual(pool["delta"], -2)
        self.assertFalse(pool["agree"])
        self.assertEqual(pool["gh"]["by_backend"], {"pi": 3, "agy": 2, "codex": 2})
        self.assertIsNotNone(pool["gh"]["collected_at"])
        self.assertIsNotNone(pool["ad"]["collected_at"])

    def test_the_dispatcher_reading_is_dated_by_the_snapshot(self):
        """The AD reading is dated by the ``_at`` of the snapshot, not by its mtime."""
        pool = self.section("pool")["data"]
        self.assertEqual(pool["ad"]["collected_at"], "2026-11-02T13:04:12+00:00")
        self.assertEqual(pool["ad"]["status"], "ok")
        self.assertEqual(pool["ad"]["age_s"], 48.0)
        # The section is as fresh as its oldest reading: the gh reading is 5s
        # old, the dispatcher reading 48s.
        self.assertEqual(self.section("pool")["collected_at"], "2026-11-02T13:04:12+00:00")
        self.assertEqual(self.section("pool")["age_s"], 48.0)

    def test_lanes_and_the_cards_in_flight(self):
        lanes = self.section("lanes")["data"]
        self.assertEqual(lanes["total"], 11)
        self.assertEqual(lanes["busy"], 5)
        self.assertEqual(lanes["idle"], 5)
        self.assertEqual(lanes["unknown"], 1)
        self.assertEqual(lanes["in_flight"], [1711, 1712, 1713, 1715, 1716])

    def test_the_oldest_reservation_is_visible(self):
        capacity = self.section("capacity")["data"]
        self.assertEqual(capacity["total"], 3)
        self.assertEqual(capacity["oldest_age_s"], 2040.0)
        self.assertEqual(capacity["oldest"]["host"], "gocc")

    def test_the_burn_of_the_last_fifteen_minutes(self):
        burn = self.section("burn")["data"]
        self.assertEqual(burn["requests"], 6)
        self.assertEqual(burn["rate_limited"], 1)
        self.assertEqual(burn["in_tok"], 19190)
        self.assertEqual(burn["out_tok"], 5130)

    def test_the_hosts_and_the_needs_human_count(self):
        self.assertEqual(self.section("hosts")["data"]["alive"], 3)
        self.assertEqual(self.section("hosts")["data"]["down"], 1)
        self.assertEqual(self.section("needs_human")["data"]["count"], 2)

    def test_the_switch_list(self):
        pauses = self.section("pauses")["data"]
        self.assertEqual(pauses["count"], 2)
        self.assertEqual([row["scope"] for row in pauses["switches"]], ["lane:CX5", "mint"])

    def test_every_section_carries_its_own_time_and_source(self):
        for row in self.report["sections"]:
            self.assertIsNotNone(row["collected_at"], row["key"])
            self.assertTrue(row["source"], row["key"])
            self.assertIsNotNone(row["age_s"], row["key"])
        self.assertEqual(self.section("hosts")["collected_at"],
                         "2026-11-02T13:03:00+00:00")

    def test_the_summary_is_one_screen(self):
        text = self.report["text"]
        self.assertEqual(len(text.splitlines()), 8)
        for label in ("[车道]", "[池]", "[容量]", "[主机]", "[烧量]", "[待人工]", "[开关]"):
            self.assertIn(label, text)
        self.assertIn("gh=7", text)
        self.assertIn("ad=9", text)
        self.assertIn("oldest=34m00s", text)
        self.assertIn("count=2", text)

    def test_a_stalled_snapshot_is_marked_expired(self):
        """The incident: the lane status sat still for five minutes."""
        set_lanes_at(self.tmp, "2026-11-02 20:59:00")
        report = tool.build_page(args_for(self.tmp), NOW)
        lanes = section_of(report, "lanes")
        self.assertTrue(lanes["stale"])
        self.assertFalse(report["ok"])
        self.assertIn("过期", report["text"])


class SnapshotClockTests(unittest.TestCase):
    """The clock of the lane reading is the snapshot's own ``_at``.

    The mtime of the file only says when the file was written: a copied,
    touched or rewritten file would make a snapshot that stopped at 21:00 look
    live, which is exactly the failure the page exists to make visible.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-status-clock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        replay(self.tmp)

    def test_an_old_snapshot_with_a_fresh_file_is_expired(self):
        set_lanes_at(self.tmp, "2026-11-02 20:59:00", mtime=NOW)
        report = tool.build_page(args_for(self.tmp), NOW)
        lanes = section_of(report, "lanes")
        self.assertEqual(lanes["collected_at"], "2026-11-02T12:59:00+00:00")
        self.assertEqual(lanes["age_s"], 360.0)
        self.assertTrue(lanes["stale"])
        self.assertFalse(report["ok"])
        self.assertIn("过期", report["text"])

    def test_the_stalled_snapshot_expires_the_dispatcher_reading_too(self):
        set_lanes_at(self.tmp, "2026-11-02 20:59:00", mtime=NOW)
        report = tool.build_page(args_for(self.tmp), NOW)
        pool = section_of(report, "pool")
        self.assertEqual(pool["data"]["ad"]["collected_at"], "2026-11-02T12:59:00+00:00")
        self.assertTrue(pool["data"]["ad"]["stale"])
        self.assertTrue(pool["stale"])
        self.assertFalse(report["ok"])

    def test_a_fresh_snapshot_with_an_old_file_is_not_expired(self):
        set_lanes_at(self.tmp, "2026-11-02 21:04:12", mtime=NOW - 3600)
        report = tool.build_page(args_for(self.tmp), NOW)
        lanes = section_of(report, "lanes")
        self.assertEqual(lanes["collected_at"], "2026-11-02T13:04:12+00:00")
        self.assertEqual(lanes["age_s"], 48.0)
        self.assertFalse(lanes["stale"])
        self.assertTrue(lanes["verified"])
        self.assertTrue(report["ok"])

    def test_a_snapshot_without_a_stamp_is_unverified(self):
        set_lanes_at(self.tmp, None)
        report = tool.build_page(args_for(self.tmp), NOW)
        lanes = section_of(report, "lanes")
        self.assertFalse(lanes["verified"])
        self.assertEqual(lanes["status"], "unverified")
        self.assertIn("_at", lanes["error"])
        # The numbers are still there, the mark says they cannot be dated.
        self.assertEqual(lanes["data"]["busy"], 5)
        self.assertEqual(report["summary"]["total"], 7)
        self.assertIn("未核实", report["text"])

    def test_an_invalid_stamp_is_unverified(self):
        set_lanes_at(self.tmp, "yesterday")
        report = tool.build_page(args_for(self.tmp), NOW)
        lanes = section_of(report, "lanes")
        self.assertFalse(lanes["verified"])
        self.assertIn("_at", lanes["error"])

    def test_a_non_finite_stamp_is_unverified(self):
        """``nan`` and ``inf`` are no clocks: the lane reading is 未核实."""
        for text in ("nan", "inf", "-inf"):
            set_lanes_at(self.tmp, text)
            report = tool.build_page(args_for(self.tmp), NOW)
            lanes = section_of(report, "lanes")
            self.assertFalse(lanes["verified"], text)
            self.assertEqual(lanes["status"], "unverified", text)
            self.assertIn("_at", lanes["error"], text)
            self.assertIsNone(lanes["collected_at"], text)
            # The numbers are still there, the mark says they cannot be dated.
            self.assertEqual(lanes["data"]["busy"], 5, text)

    def test_a_non_finite_stamp_keeps_the_whole_page(self):
        """The page is still produced and every other section is untouched."""
        set_lanes_at(self.tmp, "nan")
        report = tool.build_page(args_for(self.tmp), NOW)
        self.assertEqual(report["summary"]["total"], 7)
        self.assertEqual(report["summary"]["unverified_sections"], ["lanes", "pool"])
        self.assertFalse(report["ok"])
        self.assertIn("未核实", report["text"])
        for key in ("capacity", "hosts", "burn", "needs_human", "pauses"):
            row = section_of(report, key)
            self.assertTrue(row["verified"], key)
            self.assertIsNotNone(row["collected_at"], key)

    def test_the_pool_section_is_unverified_without_a_finite_clock(self):
        set_lanes_at(self.tmp, "nan")
        report = tool.build_page(args_for(self.tmp), NOW)
        pool = section_of(report, "pool")
        self.assertFalse(pool["verified"])
        self.assertEqual(pool["data"]["ad"]["status"], "unverified")
        self.assertIn("_at", pool["data"]["ad"]["error"])
        # The gh reading is untouched.
        self.assertEqual(pool["data"]["gh"]["ready"], 7)
        self.assertEqual(pool["data"]["gh"]["status"], "ok")

    def test_the_pool_section_is_unverified_without_a_dispatcher_clock(self):
        set_lanes_at(self.tmp, None)
        report = tool.build_page(args_for(self.tmp), NOW)
        pool = section_of(report, "pool")
        self.assertFalse(pool["verified"])
        self.assertEqual(pool["data"]["ad"]["status"], "unverified")
        self.assertIn("_at", pool["data"]["ad"]["error"])
        # The gh reading is untouched.
        self.assertEqual(pool["data"]["gh"]["ready"], 7)
        self.assertEqual(pool["data"]["gh"]["status"], "ok")
        self.assertIn("未核实", report["text"])


class BrokenSectionTests(unittest.TestCase):
    """A section that cannot be collected does not take the page down."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-status-broken-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        replay(self.tmp)

    def test_a_missing_host_probe_marks_only_that_section_unverified(self):
        args = args_for(self.tmp, hosts=str(pathlib.Path(self.tmp) / "no-health.json"))
        report = tool.build_page(args, NOW)
        self.assertEqual(report["summary"]["total"], 7)
        self.assertEqual(report["summary"]["unverified"], 1)
        self.assertEqual(report["summary"]["unverified_sections"], ["hosts"])
        hosts = [row for row in report["sections"] if row["key"] == "hosts"][0]
        self.assertFalse(hosts["verified"])
        self.assertEqual(hosts["status"], "unverified")
        self.assertIn("no-health.json", hosts["error"])
        self.assertIn("未核实", report["text"])
        # every other section is still there
        for key in ("lanes", "pool", "capacity", "burn", "needs_human", "pauses"):
            row = [item for item in report["sections"] if item["key"] == key][0]
            self.assertTrue(row["verified"], key)
            self.assertIsNotNone(row["collected_at"], key)

    def test_a_broken_json_file_only_breaks_its_own_section(self):
        path = pathlib.Path(self.tmp) / "capacity.json"
        path.write_text("{not json", encoding="utf-8")
        report = tool.build_page(args_for(self.tmp), NOW)
        self.assertEqual(report["summary"]["unverified"], 1)
        capacity = [row for row in report["sections"] if row["key"] == "capacity"][0]
        self.assertFalse(capacity["verified"])
        self.assertIn("JSONDecodeError", capacity["error"])
        self.assertTrue([row for row in report["sections"] if row["key"] == "pool"][0]["verified"])


class PageFileTests(unittest.TestCase):
    """The machine-readable page and its text version."""

    def test_the_page_is_written_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay(tmp)
            out = pathlib.Path(tmp) / "status.json"
            brain = pathlib.Path(tmp) / "brain.log"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = tool.main(["--lanes-status", str(pathlib.Path(tmp) / "lanes-status.json"),
                                  "--gh-issues", str(pathlib.Path(tmp) / "gh-issues.json"),
                                  "--gh-prs", str(pathlib.Path(tmp) / "gh-prs.json"),
                                  "--capacity", str(pathlib.Path(tmp) / "capacity.json"),
                                  "--hosts", str(pathlib.Path(tmp) / "fleet-health.json"),
                                  "--broker-log", str(pathlib.Path(tmp) / "broker-requests.jsonl"),
                                  "--pauses", str(pathlib.Path(tmp) / "pauses"),
                                  "--markers", str(pathlib.Path(tmp) / "markers"),
                                  "--now", "2026-11-02T21:05:00+08:00",
                                  "--tz", "+08:00",
                                  "--out", str(out),
                                  "--brain-log", str(brain)])
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 1)
            self.assertEqual(len(payload["sections"]), 7)
            self.assertIn("fleet status @", payload["text"])
            self.assertEqual(payload["generated_at"], "2026-11-02T13:05:00+00:00")
            self.assertIn("fleet status @", brain.read_text(encoding="utf-8"))
            self.assertIn("[车道]", buffer.getvalue())

    def test_strict_exits_non_zero_when_a_section_is_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            replay(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = tool.main(["--lanes-status", str(pathlib.Path(tmp) / "lanes-status.json"),
                                  "--hosts", str(pathlib.Path(tmp) / "missing.json"),
                                  "--now", "2026-11-02T21:05:00+08:00",
                                  "--no-write", "--strict"])
            self.assertEqual(code, 1)
            self.assertIn("未核实", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
