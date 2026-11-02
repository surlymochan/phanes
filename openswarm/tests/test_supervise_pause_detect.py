"""Regression coverage for VM pause detection and the resume grace data (Refs #1497).

The 05:12 incident is the sample: the platform froze the whole box for about
26 minutes, so the control loop and every lane process stopped together.  The
detector has to see the gap, write the resume marker the dispatch engine and
the control plane read, and leave a pause log that the liveness helpers can
use.
"""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest

BASE = pathlib.Path(__file__).resolve().parents[1] / "lib" / "supervise"
SPEC = importlib.util.spec_from_file_location("pause_detect_under_test", BASE / "pause_detect.py")
pause_detect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pause_detect)
_LIVENESS_SPEC = importlib.util.spec_from_file_location("liveness_under_test", BASE / "liveness.py")
liveness = importlib.util.module_from_spec(_LIVENESS_SPEC)
_LIVENESS_SPEC.loader.exec_module(liveness)
_CONTROLPLANE_SPEC = importlib.util.spec_from_file_location("controlplane_under_test", BASE / "controlplane.py")
controlplane = importlib.util.module_from_spec(_CONTROLPLANE_SPEC)
_CONTROLPLANE_SPEC.loader.exec_module(controlplane)

# 2026-11-02 00:00 UTC, the day of the incident.
MIDNIGHT = 1793577600.0
# The box was frozen from 04:46 to 05:12; the loop ticked 1585 seconds apart.
FROZEN_FROM = MIDNIGHT + 4 * 3600 + 46 * 60
FROZEN_TO = FROZEN_FROM + 1585


def at(hour, minute=0, second=0):
    """Return the timestamp of a wall clock time of the sample day."""
    return MIDNIGHT + hour * 3600 + minute * 60 + second


class FakeClock:
    """A clock that hands out the values it was given, one per call."""

    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class PauseDetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pause-detect-")
        self.addCleanup(self.temp.cleanup)
        self.state = pathlib.Path(self.temp.name)

    def detector(self, times, mono=None, threshold=60):
        clock = FakeClock(*times)
        mono_clock = FakeClock(*(mono if mono is not None else range(len(times))))
        return pause_detect.PauseDetector(self.state, threshold, clock, mono_clock)

    def records(self):
        path = self.state / "box-pauses.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_incident_gap_writes_resume_marker_and_pause_record(self):
        """The reported 1585 second gap creates one durable resume event."""
        detector = self.detector([FROZEN_FROM, FROZEN_TO], mono=[0.0, 1.0])
        self.assertIsNone(detector.tick())
        result = detector.tick()
        self.assertEqual(result, {"paused_s": 1585.0, "resumed_at": FROZEN_TO})
        record, = self.records()
        self.assertEqual(record["started"], FROZEN_FROM)
        self.assertEqual(record["resumed"], FROZEN_TO)
        self.assertEqual(record["gap_s"], 1585.0)
        self.assertEqual(record["source"], "pause_detect")
        # The monotonic clock does not run while the guest is suspended: the
        # difference is the time the box was frozen.
        self.assertEqual(record["mono_gap_s"], 1.0)
        self.assertEqual(record["frozen_s"], 1584.0)

    def test_resume_marker_holds_a_bare_epoch(self):
        """The dispatch engine reads the marker with float()."""
        detector = self.detector([FROZEN_FROM, FROZEN_TO])
        detector.tick()
        detector.tick()
        marker = (self.state / "box-resume").read_text(encoding="utf-8")
        self.assertEqual(marker, "%r\n" % FROZEN_TO)
        self.assertEqual(float(marker.strip()), FROZEN_TO)

    def test_normal_five_second_ticks_do_not_write_files(self):
        detector = self.detector([100.0, 105.0, 110.0])
        detector.tick()
        self.assertIsNone(detector.tick())
        self.assertIsNone(detector.tick())
        self.assertFalse((self.state / "box-resume").exists())
        self.assertFalse((self.state / "box-pauses.jsonl").exists())

    def test_gap_equal_to_threshold_is_not_a_pause(self):
        detector = self.detector([10.0, 70.0])
        detector.tick()
        self.assertIsNone(detector.tick())

    def test_wall_clock_rollback_is_logged_but_not_a_pause(self):
        """A backwards clock is a clock adjustment, it never freezes the box."""
        detector = self.detector([200.0, 190.0], mono=[0.0, 1.0])
        detector.tick()
        self.assertIsNone(detector.tick())
        record, = self.records()
        self.assertEqual(record["gap_s"], -10.0)
        self.assertTrue(record["clock_adjustment"])
        self.assertEqual(record["from"], 200.0)
        self.assertEqual(record["to"], 190.0)
        # The record does not look like a pause, so no reader counts it as one.
        self.assertNotIn("started", record)
        self.assertNotIn("resumed", record)
        self.assertFalse((self.state / "box-resume").exists())
        self.assertEqual(pause_detect.awake_seconds(195, 205, [record]), 10.0)
        self.assertEqual(liveness.load_pauses(str(self.state / "box-pauses.jsonl")), [])

    def test_a_clock_correction_shows_the_guest_kept_running(self):
        """A backwards clock with a large monotonic gap is a correction, not a freeze."""
        detector = self.detector([200.0, 190.0], mono=[0.0, 1585.0])
        detector.tick()
        detector.tick()
        record, = self.records()
        self.assertEqual(record["mono_gap_s"], 1585.0)
        self.assertEqual(record["frozen_s"], 0.0)
        self.assertTrue(record["clock_adjustment"])

    def test_write_failures_do_not_leave_the_loop(self):
        """A supervisor loop must not die because the state dir is unusable."""
        broken = self.state / "not-a-dir"
        broken.write_text("x", encoding="utf-8")
        detector = pause_detect.PauseDetector(broken, 60, FakeClock(100.0, 200.0), FakeClock(0.0, 1.0))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            detector.tick()
            result = detector.tick()
        self.assertEqual(result, {"paused_s": 100.0, "resumed_at": 200.0})
        self.assertIn("pause_detect", errors.getvalue())


class AwakeSecondsTests(unittest.TestCase):
    def test_no_pauses(self):
        self.assertEqual(pause_detect.awake_seconds(0, 100, []), 100.0)

    def test_one_pause_is_clipped_to_query_window(self):
        pauses = [{"started": 20, "resumed": 80}]
        self.assertEqual(pause_detect.awake_seconds(0, 100, pauses), 40.0)
        self.assertEqual(pause_detect.awake_seconds(30, 50, pauses), 0.0)

    def test_two_pauses_are_both_subtracted(self):
        pauses = [{"started": 10, "resumed": 20}, {"started": 40, "resumed": 60}]
        self.assertEqual(pause_detect.awake_seconds(0, 100, pauses), 70.0)

    def test_partial_overlaps_and_duplicate_overlap_are_counted_once(self):
        pauses = [{"started": -10, "resumed": 15}, {"started": 10, "resumed": 30},
                  {"started": 70, "resumed": 120}]
        self.assertEqual(pause_detect.awake_seconds(0, 100, pauses), 40.0)

    def test_an_open_pause_covers_up_to_the_end_of_the_window(self):
        """A box that is frozen right now is not awake, the freeze is not silence."""
        pauses = [{"started": 50}]
        self.assertEqual(pause_detect.awake_seconds(0, 100, pauses), 50.0)
        self.assertEqual(pause_detect.awake_seconds(0, 40, pauses), 40.0)
        self.assertEqual(liveness.silence_s(0, 0, 100, pauses), 50.0)

    def test_broken_records_are_ignored(self):
        pauses = [{"started": 20, "resumed": 10}, "bad", {"n": 1}]
        self.assertEqual(pause_detect.awake_seconds(0, 100, pauses), 100.0)

    def test_iso_timestamps_of_other_producers_are_read(self):
        pauses = [{"started": "2026-11-02T04:46:00+00:00", "resumed": "2026-11-02T05:12:00+00:00"}]
        self.assertEqual(pause_detect.awake_seconds(at(4, 30), at(5, 30), pauses), 34 * 60.0)


class RecentResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recent-resume-")
        self.addCleanup(self.temp.cleanup)
        self.state = pathlib.Path(self.temp.name)
        (self.state / "box-pauses.jsonl").write_text(
            '{broken line\n'
            + json.dumps({"started": 100, "resumed": 200}) + "\n"
            + json.dumps({"started": 250, "resumed": 300}) + "\n",
            encoding="utf-8")

    def test_latest_resume_within_window_including_boundary(self):
        self.assertTrue(pause_detect.recent_resume(self.state, 10, now=310))

    def test_resume_outside_window(self):
        self.assertFalse(pause_detect.recent_resume(self.state, 9, now=310))

    def test_future_resume_is_not_recent(self):
        self.assertFalse(pause_detect.recent_resume(self.state, 100, now=299))

    def test_missing_log_returns_false(self):
        other = pathlib.Path(self.temp.name) / "missing"
        self.assertFalse(pause_detect.recent_resume(other, 60, now=310))

    def test_a_clock_adjustment_in_the_log_is_skipped(self):
        path = self.state / "box-pauses.jsonl"
        path.write_text(json.dumps({"clock_adjustment": True, "gap_s": -10.0,
                                     "from": 320, "to": 310}) + "\n", encoding="utf-8")
        self.assertFalse(pause_detect.recent_resume(self.state, 60, now=310))

    def test_an_open_pause_is_not_a_resume(self):
        path = self.state / "box-pauses.jsonl"
        path.write_text(json.dumps({"started": 300}) + "\n", encoding="utf-8")
        self.assertFalse(pause_detect.recent_resume(self.state, 60, now=310))


class ResumeMarkerInteropTests(unittest.TestCase):
    """The marker has to be readable by the consumers that already exist."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pause-interop-")
        self.addCleanup(self.temp.cleanup)
        self.state = pathlib.Path(self.temp.name)

    def test_engine_reads_the_marker_as_a_float(self):
        detector = pause_detect.PauseDetector(self.state, 60,
                                              FakeClock(FROZEN_FROM, FROZEN_TO), FakeClock(0.0, 1.0))
        detector.tick()
        detector.tick()
        with open(self.state / "box-resume", encoding="utf-8") as stream:
            resumed_at = float(stream.read().strip())
        self.assertEqual(resumed_at, FROZEN_TO)

    def test_controlplane_sees_the_resume(self):
        zone = datetime.timezone(datetime.timedelta(hours=8))
        beat_at = datetime.datetime(2026, 11, 2, 4, 45, 0, tzinfo=zone).timestamp()
        resume_at = datetime.datetime(2026, 11, 2, 5, 12, 30, tzinfo=zone).timestamp()
        detector = pause_detect.PauseDetector(self.state, 60,
                                              FakeClock(beat_at, resume_at), FakeClock(0.0, 1.0))
        detector.tick()
        detector.tick()
        (self.state / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
        (self.state / "beat").write_text(
            datetime.datetime.fromtimestamp(beat_at, zone).isoformat(), encoding="utf-8")
        service = {"name": "worker", "heartbeat": "beat", "heartbeat_max_s": 180}
        record = controlplane.status([service], self.state, now=resume_at + 30,
                                      resume_file=self.state / "box-resume")["worker"]
        self.assertEqual(record["state"], "resuming")


class TailCliTests(unittest.TestCase):
    def test_tail_skips_corrupt_lines_and_limits_output(self):
        with tempfile.TemporaryDirectory(prefix="pause-tail-") as directory:
            path = pathlib.Path(directory) / "box-pauses.jsonl"
            path.write_text('{bad}\n{"n": 1}\n{"n": 2}\n', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = pause_detect.main(["tail", "--state-dir", directory, "-n", "1"])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), '{"n": 2}\n')


if __name__ == "__main__":
    unittest.main()
