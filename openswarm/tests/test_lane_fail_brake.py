"""The lane instant-failure brake of the autodispatcher (Refs #1711).

A lane that fails again and again within minutes burns the card pool and pollutes the
statistics with ``lane-failed`` labels.  The dispatcher brakes such a lane itself: after
N consecutive instant failures it writes the ``PAUSE-<lane>`` switch, records an audit row
and stops handing cards to that lane.  A run with a commit clears the streak again, and a
brake goes away when the operator removes the switch or when the health probe of the lane's
host reports the host healthy.

The tests drive the shipped code: the remote projection is executed for real (the snippet
the AD pipes over ssh), the tick is the real one and only the ssh probe, the GitHub calls
and the git calls are stubbed.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


AUTODISPATCH = pathlib.Path(__file__).parents[2] / "tools" / "lane-autodispatch" / "autodispatch.py"
spec = importlib.util.spec_from_file_location("autodispatch", AUTODISPATCH)
autodispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autodispatch)

CLI = pathlib.Path(__file__).parents[1] / "bin" / "openswarm"


class FakeGitHub:
    """The GitHub side of one dispatch tick: the queue, the labels and the comments."""

    def __init__(self, numbers=()):
        self.issues = {number: {"number": number, "state": "OPEN", "title": f"card {number}",
                                 "body": "", "labels": ["lane-ready"]} for number in numbers}
        self.comments = []
        self.prs = []

    def __call__(self, args, timeout=60):
        text = " ".join(str(part) for part in args)
        if "issue list" in text:
            rows = [dict(row, labels=[{"name": name} for name in row["labels"]])
                    for row in self.issues.values()
                    if row["state"] == "OPEN" and "lane-ready" in row["labels"]]
            return 0, json.dumps(rows), ""
        if "issue view" in text:
            number = int(args[args.index("view") + 1])
            row = self.issues.get(number)
            if row is None:
                return 1, "", f"no issue {number}"
            return 0, json.dumps({"state": row["state"],
                                  "labels": [{"name": name} for name in row["labels"]]}), ""
        if "issue edit" in text:
            number = int(args[args.index("edit") + 1])
            row = self.issues.setdefault(number, {"number": number, "state": "OPEN",
                                                  "title": f"card {number}", "body": "",
                                                  "labels": []})
            for part in args:
                if part == "--add-label":
                    index = args.index(part)
                    for name in args[index + 1].split(","):
                        if name not in row["labels"]:
                            row["labels"].append(name)
                if part == "--remove-label":
                    index = args.index(part)
                    for name in args[index + 1].split(","):
                        if name in row["labels"]:
                            row["labels"].remove(name)
            return 0, "", ""
        if "issue comment" in text:
            self.comments.append(text)
            return 0, "", ""
        if "pr create" in text:
            self.prs.append(text)
            return 0, f"https://github.com/acme/project/pull/{len(self.prs)}", ""
        return 0, "", ""


class LaneBrakeTestCase(unittest.TestCase):
    """A temporary autodispatch state dir: no real pause file is ever touched."""

    LANE = "CX9"
    CFG = {"host": "cx9-host", "backend": "codex"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lane-brake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.state_dir = self.root / "autodispatch"
        self.state_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.brain = self.root / "brain.md"
        self.brain.write_text("", encoding="utf-8")
        for name, value in (("D", str(self.state_dir)), ("BRAIN_LOG", str(self.brain))):
            patcher = mock.patch.object(autodispatch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    # ------------------------------------------------------------------ helpers

    def lanes(self):
        return {self.LANE: dict(self.CFG)}

    def marker(self, lane=None):
        return self.state_dir / f"PAUSE-{lane or self.LANE}"

    def audit_rows(self):
        path = self.state_dir / "lane-brakes.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def brain_log(self):
        return self.brain.read_text(encoding="utf-8")

    def failed_run(self, seconds=42.0, status="failed", error_class="exec_error"):
        """The run info of a run that ended without a commit."""
        return {"status": status, "error_class": error_class, "branch": None, "commit": None,
                "summary": "the executor died before it wrote anything", "seconds": seconds}

    def project(self, record, status=None):
        """Run the real remote projection snippet against a fake run directory.

        The snippet is the one the AD pipes over ssh; it reads the run record and the run
        status of the executor candidate home and prints the projected run info."""
        home = self.root / "home"
        runs = home / ".claude" / "openswarm-executor-candidate" / "runs" / "run-1"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "result.json").write_text(json.dumps(record), encoding="utf-8")
        if status is not None:
            (runs / "status.json").write_text(json.dumps(status), encoding="utf-8")
        environment = dict(os.environ, HOME=str(home))
        result = subprocess.run([sys.executable, "-", self.LANE, "run-1"],
                                input=autodispatch.REMOTE_STATUS, text=True,
                                capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])["run"]


class LaneFailureCountingTests(LaneBrakeTestCase):
    """The streak: what counts, what does not, and what clears it."""

    def test_the_duration_is_read_from_the_projection(self):
        info = self.project({"status": "failed", "seconds": 42.5})

        self.assertEqual(info["seconds"], 42.5)
        self.assertEqual(autodispatch.run_seconds(info), 42.5)

    def test_the_duration_falls_back_to_the_start_and_end_stamps(self):
        info = self.project({"status": "failed"}, {"started": 100.0, "finished": 130.0})

        self.assertIsNone(info["seconds"])
        self.assertEqual(autodispatch.run_seconds(info), 30.0)

    def test_a_run_without_any_duration_stays_unknown(self):
        info = self.project({"status": "failed"})

        self.assertIsNone(autodispatch.run_seconds(info))
        self.assertFalse(autodispatch.instant_failure(info))

    def test_only_a_short_run_counts_as_an_instant_failure(self):
        short = self.failed_run(seconds=90.0)
        long = self.failed_run(seconds=1800.0)

        self.assertTrue(autodispatch.instant_failure(short))
        self.assertFalse(autodispatch.instant_failure(long))

    def test_an_unknown_duration_counts_only_when_the_operator_asked_for_it(self):
        unknown = {"status": "failed"}

        self.assertFalse(autodispatch.instant_failure(unknown))
        self.assertTrue(autodispatch.instant_failure(unknown, count_unknown=True))

    def test_the_streak_counts_the_instant_failures_of_one_lane(self):
        state = {}

        for expected in (1, 2):
            record = autodispatch.note_lane_failure(self.LANE, state, self.failed_run(), issue=1700)
            self.assertTrue(record["counted"])
            self.assertEqual(record["streak"], expected)

        self.assertEqual(autodispatch.lane_failures(state)[self.LANE]["streak"], 2)

    def test_a_long_failure_does_not_count_and_does_not_clear_the_streak(self):
        state = {}
        autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0), issue=1700)
        record = autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=1800.0), issue=1701)

        self.assertFalse(record["counted"])
        self.assertIn("threshold", record["skipped"])
        self.assertEqual(autodispatch.lane_failures(state)[self.LANE]["streak"], 1)

    def test_a_run_with_a_commit_clears_the_streak(self):
        state = {}
        autodispatch.note_lane_failure(self.LANE, state, self.failed_run(), issue=1700)
        autodispatch.note_lane_failure(self.LANE, state, self.failed_run(), issue=1701)

        autodispatch.clear_lane_failures(self.LANE, state)

        self.assertNotIn(self.LANE, autodispatch.lane_failures(state))

    def test_the_streak_of_one_lane_does_not_touch_another_lane(self):
        state = {}
        autodispatch.note_lane_failure(self.LANE, state, self.failed_run(), issue=1700)

        self.assertNotIn("CX1", autodispatch.lane_failures(state))


class LaneBrakeSwitchTests(LaneBrakeTestCase):
    """The brake itself: the switch, the audit row and the brain log."""

    def test_the_threshold_writes_the_pause_switch_and_the_audit_row(self):
        state = {}
        for number in range(autodispatch.LANE_FAIL_STREAK):
            autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0),
                                           issue=1700 + number, run=f"run-{number}",
                                           host=self.CFG["host"])

        self.assertTrue(self.marker().exists())
        self.assertTrue(autodispatch.lane_braked(self.LANE, state))
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "brake")
        self.assertEqual(rows[0]["lane"], self.LANE)
        self.assertEqual(rows[0]["streak"], autodispatch.LANE_FAIL_STREAK)
        self.assertEqual(rows[0]["last_issue"], 1700 + autodispatch.LANE_FAIL_STREAK - 1)
        self.assertIn("exec_error", rows[0]["last_reason"])

    def test_the_brake_is_written_to_the_brain_log(self):
        state = {}
        for number in range(autodispatch.LANE_FAIL_STREAK):
            autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0), issue=1700)

        self.assertIn(f"{self.LANE} braked", self.brain_log())
        self.assertIn(f"PAUSE-{self.LANE}", self.brain_log())

    def test_the_switch_pauses_the_lane_for_the_dispatch_loop(self):
        state = {}
        for number in range(autodispatch.LANE_FAIL_STREAK):
            autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0), issue=1700)

        self.assertIn(f"lane:{self.LANE}", autodispatch.paused_scopes())
        self.assertTrue(autodispatch.lane_paused(self.LANE, self.CFG))

    def test_below_the_threshold_no_switch_is_written(self):
        state = {}
        for number in range(autodispatch.LANE_FAIL_STREAK - 1):
            autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0), issue=1700)

        self.assertFalse(self.marker().exists())
        self.assertFalse(autodispatch.lane_braked(self.LANE, state))
        self.assertEqual(self.audit_rows(), [])

    def test_the_brakes_are_published_for_the_health_output(self):
        state = {}
        for number in range(autodispatch.LANE_FAIL_STREAK):
            autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0), issue=1700)

        autodispatch.publish_lane_brakes(state)
        payload = json.loads((self.state_dir / "lane-brakes.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["lanes"][self.LANE]["lane"], self.LANE)
        self.assertEqual(payload["lanes"][self.LANE]["streak"], autodispatch.LANE_FAIL_STREAK)
        self.assertEqual(payload["lanes"][self.LANE]["last_issue"], 1700)


class LaneBrakeRecoveryTests(LaneBrakeTestCase):
    """The two ways back: the operator removes the switch, or the health probe says ok."""

    def brake(self):
        state = {}
        for number in range(autodispatch.LANE_FAIL_STREAK):
            autodispatch.note_lane_failure(self.LANE, state, self.failed_run(seconds=42.0), issue=1700)
        return state

    def test_the_operator_removing_the_switch_releases_the_brake(self):
        state = self.brake()
        self.marker().unlink()

        autodispatch.release_lane_brakes(state, self.lanes())

        self.assertFalse(autodispatch.lane_braked(self.LANE, state))
        self.assertNotIn(self.LANE, autodispatch.lane_failures(state))
        self.assertEqual([row["event"] for row in self.audit_rows()], ["brake", "release"])
        self.assertIn("operator", self.audit_rows()[-1]["reason"])

    def test_a_healthy_probe_after_the_brake_releases_the_brake(self):
        state = self.brake()
        (self.state_dir / "health.json").write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 60)),
            "hosts": {self.CFG["host"]: "ok"}, "hosts_ok": [self.CFG["host"]],
        }), encoding="utf-8")

        autodispatch.release_lane_brakes(state, self.lanes())

        self.assertFalse(autodispatch.lane_braked(self.LANE, state))
        self.assertFalse(self.marker().exists())
        self.assertIn("health probe", self.audit_rows()[-1]["reason"])

    def test_a_probe_from_before_the_brake_releases_nothing(self):
        state = self.brake()
        record = autodispatch.lane_brakes(state)[self.LANE]
        (self.state_dir / "health.json").write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 600)),
            "hosts": {self.CFG["host"]: "ok"}, "hosts_ok": [self.CFG["host"]],
        }), encoding="utf-8")

        autodispatch.release_lane_brakes(state, self.lanes())

        self.assertTrue(autodispatch.lane_braked(self.LANE, state))
        self.assertTrue(self.marker().exists())
        self.assertEqual(record["lane"], self.LANE)

    def test_a_sick_host_keeps_the_brake(self):
        state = self.brake()
        (self.state_dir / "health.json").write_text(json.dumps({
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 60)),
            "hosts": {self.CFG["host"]: "degraded"}, "hosts_ok": [],
        }), encoding="utf-8")

        autodispatch.release_lane_brakes(state, self.lanes())

        self.assertTrue(autodispatch.lane_braked(self.LANE, state))

    def test_a_manual_switch_is_not_removed_by_the_release(self):
        state = self.brake()
        self.marker().write_text("manual: the operator holds this lane\n", encoding="utf-8")

        autodispatch.release_lane_brake(self.LANE, state, "test")

        self.assertTrue(self.marker().exists())


class LaneBrakeTickTests(LaneBrakeTestCase):
    """The real tick: the brake stops the dispatch and the release lets it through."""

    def setUp(self):
        super().setUp()
        self.github = FakeGitHub(numbers=(1701, 1702, 1703, 1704))
        self.dispatched = []
        self.labelled = []
        self.runs = 0

    def fake_remote(self, lane, cfg, run=""):
        """The ssh probe: an idle lane, and the failed run of the lane that has one."""
        if not run:
            return {"lane": "idle"}
        return {"lane": "idle", "run": self.failed_run(seconds=42.0)}

    def fake_dispatch(self, lane, cfg, item, base):
        self.runs += 1
        self.dispatched.append(item["n"])
        return f"run-{self.runs}", None

    def fake_sh(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
        if "ls-remote" in text: return 0, "b" * 40 + "\n", ""
        if "--format=%s" in text: return 0, "#1701 the subject", ""
        if "--format=%B" in text: return 0, "Closes #1701\n\n验收：`python3 -m unittest`\n", ""
        return 0, "", ""

    def tick(self, state, remote=None):
        with mock.patch.object(autodispatch, "remote", remote or self.fake_remote), \
                mock.patch.object(autodispatch, "gh", self.github), \
                mock.patch.object(autodispatch, "dispatch", self.fake_dispatch), \
                mock.patch.object(autodispatch, "label_lane_failed",
                                  lambda number, body: self.labelled.append(number)), \
                mock.patch.object(autodispatch, "replenish", lambda count, state: []), \
                mock.patch.object(autodispatch, "sh", self.fake_sh):
            autodispatch.tick(self.lanes(), state, 0)

    def test_four_instant_failures_brake_the_lane_after_the_third_one(self):
        """The 2026-11-02 night replay: CX9 fails four times in a row, the fourth card is never taken."""
        state = {}

        self.tick(state)   # the first card is dispatched
        self.tick(state)   # the first run fails, the second card is dispatched
        self.tick(state)   # the second run fails, the third card is dispatched
        self.assertEqual(self.dispatched, [1701, 1702, 1703])

        self.tick(state)   # the third run fails: the lane is braked

        self.assertTrue(self.marker().exists())
        self.assertEqual(autodispatch.lane_brakes(state)[self.LANE]["streak"],
                         autodispatch.LANE_FAIL_STREAK)
        self.assertEqual(self.dispatched, [1701, 1702, 1703])   # the fourth card is not taken
        self.assertNotIn(1704, self.dispatched)

    def test_a_braked_lane_takes_no_card_at_all(self):
        state = {}
        for _ in range(autodispatch.LANE_FAIL_STREAK + 1):
            self.tick(state)

        self.assertEqual(self.dispatched, [1701, 1702, 1703])
        self.assertEqual(len(self.labelled), 3)

    def test_the_status_snapshot_carries_the_brake(self):
        state = {}
        for _ in range(autodispatch.LANE_FAIL_STREAK + 1):
            self.tick(state)

        snapshot = json.loads((self.state_dir / "lanes-status.json").read_text(encoding="utf-8"))

        self.assertEqual(len(snapshot["_brakes"]), 1)
        self.assertEqual(snapshot["_brakes"][0]["lane"], self.LANE)
        self.assertEqual(snapshot["_brakes"][0]["streak"], autodispatch.LANE_FAIL_STREAK)
        self.assertEqual(snapshot["_brakes"][0]["last_issue"], 1703)

    def test_a_successful_run_clears_the_streak(self):
        state = {}
        self.tick(state)   # the first card is dispatched
        self.tick(state)   # the run failed, the streak is 1
        self.assertEqual(autodispatch.lane_failures(state)[self.LANE]["streak"], 1)

        # the next run of the lane ends with a commit: the streak is over
        def successful_remote(lane, cfg, run=""):
            if not run:
                return {"lane": "idle"}
            return {"lane": "idle", "run": {"status": "done", "branch": "fleet/CX9",
                                            "commit": "abc123", "repo": "repo",
                                            "seconds": 120.0, "summary": "done"}}

        self.tick(state, remote=successful_remote)

        self.assertNotIn(self.LANE, autodispatch.lane_failures(state))


class FleetHealthLaneBrakeTests(unittest.TestCase):
    """The health output shows the brake, so a supervisor can quote it (Refs #1711)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-health-brake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def test_the_health_report_carries_the_lane_the_streak_and_the_last_failure(self):
        loader = importlib.machinery.SourceFileLoader("openswarm_cli_under_test", str(CLI))
        spec = importlib.util.spec_from_loader("openswarm_cli_under_test", loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        brakes = self.root / "lane-brakes.json"
        brakes.write_text(json.dumps({"updated_at": "2026-11-02 22:31:02", "lanes": {
            "CX9": {"lane": "CX9", "streak": 3, "threshold": 3, "last_issue": 1711,
                    "last_run": "run-3", "last_reason": "ended `failed`; error_class `exec_error`",
                    "last_seconds": 42.0, "host": "cx9-host", "marker": "PAUSE-CX9",
                    "braked_at": "2026-11-02 22:31:02"}}}), encoding="utf-8")
        lanes = self.home / "lanes.json"
        lanes.write_text(json.dumps({"lanes": [{"name": "C1", "backend": "codex", "model": "m",
                                                "effort": "high", "repo": str(self.root),
                                                "host": "worker-a"}]}), encoding="utf-8")

        with mock.patch.dict(os.environ, {"OPENSWARM_LANES_FILE": str(lanes),
                                          "AD_LANE_BRAKES_FILE": str(brakes)}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                report = cli.cmd_fleet_health(
                    type("Args", (), {"hosts": None, "json": True, "apply": False,
                                      "lane_brakes": None})(),
                    policy={}, runner=lambda host, command: "50%\n", hosts={}, now=1700000000.0,
                    home=self.home)

        self.assertEqual(len(report["lane_brakes"]), 1)
        row = report["lane_brakes"][0]
        self.assertEqual(row["lane"], "CX9")
        self.assertEqual(row["streak"], 3)
        self.assertEqual(row["last_issue"], 1711)
        self.assertIn("exec_error", row["last_reason"])
        self.assertIn("CX9", output.getvalue())


if __name__ == "__main__":
    unittest.main()
