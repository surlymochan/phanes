"""Lane liveness: silence is judged by awake time, so a frozen box cannot kill a live run (Refs #1498).

The 05:12 incident is the regression sample: the box was frozen from 04:46 to 05:12
while the runs of M12 and M14 kept going; the watchdog counted the freeze as lane
silence and killed them.  The judgement therefore subtracts the frozen stretches
from the silence and gives the lane a grace window after a resume.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "lib" / "supervise" / "liveness.py"
_spec = importlib.util.spec_from_file_location("liveness_under_test", MODULE_PATH)
liveness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(liveness)

# 2026-11-02 00:00 UTC, the day of the incident.
MIDNIGHT = 1793577600.0


def at(hour, minute=0, second=0):
    """Return the timestamp of a wall clock time of the sample day."""
    return MIDNIGHT + hour * 3600 + minute * 60 + second


class SilenceTests(unittest.TestCase):
    """The silence of a lane is the awake part of the window only."""

    def test_incident_sample_is_not_stalled(self):
        """05:12: 28 minutes of wall clock, 26 of them frozen -- the run stays alive."""
        run = {"run_id": "r-1", "lane": "M12", "started": at(4, 40), "last_output_at": at(4, 45)}
        pauses = [{"started": at(4, 46), "resumed": at(5, 12)}]
        self.assertEqual(liveness.silence_s(at(4, 40), at(4, 45), at(5, 13), pauses), 120.0)
        self.assertNotEqual(liveness.verdict("M12", run, at(5, 13), pauses, 1200), liveness.STALLED)

    def test_same_run_without_pauses_is_stalled(self):
        """Without the freeze record the same run is silent for 28 minutes."""
        run = {"run_id": "r-1", "lane": "M12", "started": at(4, 40), "last_output_at": at(4, 45)}
        self.assertEqual(liveness.silence_s(at(4, 40), at(4, 45), at(5, 13), []), 1680.0)
        self.assertEqual(liveness.verdict("M12", run, at(5, 13), [], 1200), liveness.STALLED)

    def test_several_pauses_accumulate(self):
        """Two frozen stretches are added up, not only the last one."""
        run = {"run_id": "r-2", "lane": "M14", "started": at(4, 0), "last_output_at": at(4, 0)}
        pauses = [{"started": at(4, 0), "resumed": at(4, 40)}, {"started": at(4, 50), "resumed": at(5, 10)}]
        now = at(5, 13)
        # 73 minutes of wall clock, 60 of them frozen.
        self.assertEqual(liveness.silence_s(at(4, 0), at(4, 0), now, pauses), 13 * 60.0)
        self.assertNotEqual(liveness.verdict("M14", run, now, pauses, 1200), liveness.STALLED)
        # Without the freeze records the same run is long silent.
        self.assertEqual(liveness.verdict("M14", run, now, [], 1200), liveness.STALLED)

    def test_pause_before_the_run_start_is_not_deducted(self):
        """A freeze before the run started is outside the window and does not help the run."""
        pauses = [{"started": at(3, 0), "resumed": at(3, 30)}]
        self.assertEqual(liveness.silence_s(at(4, 0), at(4, 0), at(5, 0), pauses), 3600.0)
        run = {"run_id": "r-3", "lane": "M12", "started": at(4, 0), "last_output_at": at(4, 0)}
        self.assertEqual(liveness.verdict("M12", run, at(5, 0), pauses, 1200), liveness.STALLED)

    def test_open_pause_covers_the_rest_of_the_window(self):
        """A pause without a resume means the box is frozen right now: no silence at all."""
        pauses = [{"started": at(4, 30)}]
        self.assertEqual(liveness.silence_s(at(4, 40), at(4, 45), at(5, 13), pauses), 0.0)
        run = {"run_id": "r-4", "lane": "M12", "started": at(4, 40), "last_output_at": at(4, 45)}
        self.assertNotEqual(liveness.verdict("M12", run, at(5, 13), pauses, 1200), liveness.STALLED)

    def test_output_after_the_resume_ends_the_grace(self):
        """A lane that keeps talking after the resume is ok, not in grace."""
        run = {"run_id": "r-5", "lane": "M12", "started": at(4, 0), "last_output_at": at(5, 12)}
        pauses = [{"started": at(4, 0), "resumed": at(5, 12)}]
        self.assertEqual(liveness.verdict("M12", run, at(5, 13), pauses, 1200), liveness.OK)


class VerdictTests(unittest.TestCase):
    """After a resume the lane gets a grace window in which it is not killed."""

    def test_resume_grace_protects_the_run(self):
        run = {"run_id": "r-6", "lane": "M12", "started": at(4, 0), "last_output_at": at(4, 0)}
        pauses = [{"started": at(4, 5), "resumed": at(4, 10)}]
        # Five minutes after the resume the lane is in grace: not killed, not re-dispatched.
        self.assertEqual(liveness.verdict("M12", run, at(4, 15), pauses, 300), liveness.GRACE)
        # A minute later the grace is over and the awake silence counts.
        self.assertEqual(liveness.verdict("M12", run, at(4, 16), pauses, 300), liveness.STALLED)

    def test_a_live_run_is_ok(self):
        run = {"run_id": "r-7", "lane": "M12", "started": at(4, 40), "last_output_at": at(5, 12)}
        self.assertEqual(liveness.verdict("M12", run, at(5, 13), [], 1200), liveness.OK)

    def test_pause_of_another_lane_does_not_help(self):
        """A freeze of another lane says nothing about this one."""
        run = {"run_id": "r-8", "lane": "M12", "started": at(4, 40), "last_output_at": at(4, 45)}
        pauses = [{"lane": "M14", "started": at(4, 46), "resumed": at(5, 12)}]
        self.assertEqual(liveness.verdict("M12", run, at(5, 13), pauses, 1200), liveness.STALLED)


class LoadPausesTests(unittest.TestCase):
    """The pause log is read defensively: a broken line must not stop the watchdog."""

    def write(self, lines):
        tmp = tempfile.TemporaryDirectory(prefix="liveness-pauses-")
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "box-pauses.jsonl"
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return path

    def test_bad_jsonl_lines_are_skipped(self):
        path = self.write([
            json.dumps({"started": at(4, 46), "resumed": at(5, 12)}),
            "{not json",
            json.dumps([1, 2, 3]),
            json.dumps({"reason": "no timestamps"}),
            "",
            json.dumps({"started": at(5, 20), "resumed": at(5, 30)}),
        ])
        records = liveness.load_pauses(str(path), at(4, 0))
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["started"], at(4, 46))
        self.assertEqual(records[1]["resumed"], at(5, 30))

    def test_only_the_records_after_since_are_kept(self):
        path = self.write([
            json.dumps({"started": at(4, 0), "resumed": at(4, 30)}),
            json.dumps({"started": at(4, 45), "resumed": at(5, 10)}),
        ])
        records = liveness.load_pauses(str(path), at(5, 0))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["started"], at(4, 45))

    def test_iso_timestamps_are_accepted(self):
        path = self.write([json.dumps({"started": "2026-11-02T04:46:00+00:00",
                                        "resumed": "2026-11-02T05:12:00+00:00"})])
        records = liveness.load_pauses(str(path), None)
        self.assertEqual(len(records), 1)
        run = {"started": at(4, 40), "last_output_at": at(4, 45)}
        self.assertEqual(liveness.silence_s(run["started"], run["last_output_at"], at(5, 13), records), 120.0)


class CliTests(unittest.TestCase):
    """The CLI reports the verdict of every run of a box."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="liveness-cli-")
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.runs = self.root / "runs.json"
        self.runs.write_text(json.dumps([
            {"run_id": "r-1", "lane": "M12", "started": at(4, 40), "last_output_at": at(4, 45)},
            {"run_id": "r-2", "lane": "M14", "started": at(4, 40), "last_output_at": at(4, 45)},
        ]), encoding="utf-8")
        self.pauses = self.root / "box-pauses.jsonl"
        self.pauses.write_text(json.dumps({"started": at(4, 46), "resumed": at(5, 12)}) + "\n",
                               encoding="utf-8")

    def check(self, *extra):
        argv = ["check", "--runs", str(self.runs), "--pauses", str(self.pauses),
                "--limit", "1200", "--now", str(at(5, 13)), "--json"] + list(extra)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = liveness.main(argv)
        return code, json.loads(buffer.getvalue())

    def test_check_reports_the_verdict_of_every_run(self):
        code, report = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(len(report["runs"]), 2)
        verdicts = {run["run_id"]: run["verdict"] for run in report["runs"]}
        self.assertEqual(verdicts["r-1"], liveness.GRACE)
        self.assertEqual(verdicts["r-2"], liveness.GRACE)
        for run in report["runs"]:
            self.assertEqual(run["silence_s"], 120.0)

    def test_check_without_the_pause_log_reports_a_stalled_run(self):
        code, report = self.check()
        self.assertEqual(code, 0)
        # The same runs without the freeze record: 28 minutes of silence.
        self.assertEqual(liveness.verdict("M12", {"started": at(4, 40), "last_output_at": at(4, 45)},
                                          at(5, 13), [], 1200), liveness.STALLED)

    def test_a_missing_runs_file_is_an_error(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = liveness.main(["check", "--runs", str(self.root / "missing.json")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
