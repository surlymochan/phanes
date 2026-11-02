"""Offline contracts for ctl-heartbeat-probe.py; the fake runner never calls ssh (Refs #1550)."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ctl-heartbeat-probe.py"
SPEC = importlib.util.spec_from_file_location("ctl_heartbeat_probe", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

FILES = ["heartbeat", "merge-heartbeat", "health.heartbeat"]
NOW = 1793580000.0  # arbitrary fixed epoch; every fixture is relative to it


def fake_runner(heartbeats, pause_lines=(), rc=0):
    """Return a runner that answers like the remote command would."""
    def runner(host, command):
        assert host == "box"
        assert "box-pauses.jsonl" in command
        lines = [f"HB {name} {heartbeats[name]}" if heartbeats.get(name) is not None
                 else f"HB {name} " for name in FILES]
        lines.append("PAUSES")
        lines.extend(pause_lines)
        return rc, "\n".join(lines) + "\n"
    return runner


def pause(started, resumed=None):
    return json.dumps({"started": started, "resumed": resumed})


class ProbeStatusTests(unittest.TestCase):
    def run_probe(self, runner, now=NOW, stale_s=180):
        return MODULE.probe("box", FILES, stale_s=stale_s, now=now, runner=runner)

    def test_ok_when_all_heartbeats_fresh(self):
        fresh = {name: NOW - 10 for name in FILES}
        result = self.run_probe(fake_runner(fresh))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit"], 0)

    def test_unreachable_on_nonzero_ssh_exit(self):
        result = self.run_probe(fake_runner({}, rc=255))
        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(result["exit"], 4)

    def test_unreachable_when_ssh_cannot_start(self):
        def broken(host, command):
            raise FileNotFoundError("ssh")
        result = self.run_probe(broken)
        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(result["exit"], 4)

    def test_vm_paused_when_stale_heartbeat_is_covered_by_pause(self):
        stale = {name: NOW - 900 for name in FILES}
        # Resumed 100s ago, under the 180s stale threshold: the box still has
        # time to beat again, so the pause still explains the silence.
        pauses = [pause(NOW - 1000, NOW - 100)]
        result = self.run_probe(fake_runner(stale, pauses))
        self.assertEqual(result["status"], "vm_paused")
        self.assertEqual(result["exit"], 2)

    def test_vm_paused_for_open_pause_still_running(self):
        stale = {name: NOW - 900 for name in FILES}
        result = self.run_probe(fake_runner(stale, [pause(NOW - 950)]))
        self.assertEqual(result["status"], "vm_paused")

    def test_hung_when_pause_ended_long_before_and_no_new_heartbeat(self):
        # Heartbeat stopped 900s ago; the pause ended 300s ago, past the
        # stale threshold, and nothing beat since: the loop is hung.
        stale = {name: NOW - 900 for name in FILES}
        result = self.run_probe(fake_runner(stale, [pause(NOW - 1000, NOW - 300)]))
        self.assertEqual(result["status"], "hung")
        self.assertEqual(result["reason"], "hung:heartbeat")
        self.assertEqual(result["exit"], 3)

    def test_hung_when_only_old_pause_and_no_current_gap_cover(self):
        heartbeats = {"heartbeat": NOW - 10, "merge-heartbeat": NOW - 900,
                      "health.heartbeat": NOW - 5}
        old_pause = [pause(NOW - 7200, NOW - 7000)]
        result = self.run_probe(fake_runner(heartbeats, old_pause))
        self.assertEqual(result["status"], "hung")
        self.assertEqual(result["reason"], "hung:merge-heartbeat")
        self.assertEqual(result["exit"], 3)

    def test_short_recent_pause_does_not_hide_earlier_hang(self):
        # Silent since 1000s ago while awake; a 20s pause came and went 100s ago.
        # The 880s of awake silence before the pause is not explained.
        stale = {name: NOW - 1000 for name in FILES}
        result = self.run_probe(fake_runner(stale, [pause(NOW - 120, NOW - 100)]))
        self.assertEqual(result["status"], "hung")
        self.assertEqual(result["reason"], "hung:heartbeat")
        self.assertEqual(result["exit"], 3)

    def test_second_file_hang_not_hidden_by_recent_pause(self):
        # heartbeat is fresh; merge-heartbeat stopped 900s ago, 400s before a
        # pause that ended 100s ago, so merge hung while the box was awake.
        heartbeats = {"heartbeat": NOW - 10, "merge-heartbeat": NOW - 900,
                      "health.heartbeat": NOW - 5}
        result = self.run_probe(fake_runner(heartbeats, [pause(NOW - 500, NOW - 100)]))
        self.assertEqual(result["status"], "hung")
        self.assertEqual(result["reason"], "hung:merge-heartbeat")

    def test_vm_paused_when_pause_covers_whole_silence(self):
        stale = {name: NOW - 900 for name in FILES}
        result = self.run_probe(fake_runner(stale, [pause(NOW - 950, NOW - 50)]))
        self.assertEqual(result["status"], "vm_paused")
        self.assertEqual(result["exit"], 2)

    def test_missing_heartbeat_counts_as_stale(self):
        heartbeats = {"heartbeat": NOW - 10, "merge-heartbeat": NOW - 10}
        result = self.run_probe(fake_runner(heartbeats))
        self.assertEqual(result["status"], "hung")
        self.assertEqual(result["reason"], "hung:health.heartbeat")

    def test_stale_threshold_is_respected(self):
        beats = {name: NOW - 200 for name in FILES}
        self.assertEqual(self.run_probe(fake_runner(beats), stale_s=180)["status"], "hung")
        self.assertEqual(self.run_probe(fake_runner(beats), stale_s=300)["status"], "ok")


class PauseParsingTests(unittest.TestCase):
    def test_bad_json_lines_are_skipped(self):
        stale = {name: NOW - 900 for name in FILES}
        lines = ["{not json", "[1, 2]", json.dumps({"reason": "no start"}),
                 "", pause(NOW - 1000, NOW - 100)]
        result = MODULE.probe("box", FILES, now=NOW, runner=fake_runner(stale, lines))
        self.assertEqual(result["status"], "vm_paused")
        self.assertEqual(result["pauses"], 1)

    def test_timezone_offsets_compare_by_epoch(self):
        # 04:46 at +08:00 is 20:46 UTC the previous day; the heartbeat at
        # 20:50 UTC falls inside that pause only if offsets are honoured.
        heartbeat_epoch = MODULE._epoch("2026-11-01T20:50:00+00:00")
        now = heartbeat_epoch + 900
        stale = {name: heartbeat_epoch for name in FILES}
        in_pause = pause("2026-11-02T04:46:00+08:00", "2026-11-02T05:12:00+08:00")
        result = MODULE.probe("box", FILES, now=now, runner=fake_runner(stale, [in_pause]))
        self.assertEqual(result["status"], "vm_paused")

        same_wall_clock_utc = pause("2026-11-01T04:46:00Z", "2026-11-01T05:12:00Z")
        result = MODULE.probe("box", FILES, now=now,
                              runner=fake_runner(stale, [same_wall_clock_utc]))
        self.assertEqual(result["status"], "hung")

    def test_naive_iso_is_read_as_utc(self):
        self.assertEqual(MODULE._epoch("2026-11-01T20:50:00"),
                         MODULE._epoch("2026-11-01T20:50:00+00:00"))

    def test_parse_heartbeats_reads_mtime_epoch(self):
        out = "HB heartbeat 1793579000\nHB merge-heartbeat \nHB health.heartbeat 1793579500\n"
        beats = MODULE.parse_heartbeats(out, FILES)
        self.assertEqual(beats["heartbeat"], 1793579000.0)
        self.assertIsNone(beats["merge-heartbeat"])
        self.assertEqual(beats["health.heartbeat"], 1793579500.0)


class CliTests(unittest.TestCase):
    def run_cli(self, argv, runner):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = MODULE.main(argv, runner=runner, now=NOW)
        return code, buf.getvalue()

    def test_json_output_and_exit_code(self):
        stale = {name: NOW - 900 for name in FILES}
        code, out = self.run_cli(["--host", "box", "--json"], fake_runner(stale, [pause(NOW - 1000, NOW - 100)]))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "vm_paused")

    def test_text_output_for_hung(self):
        stale = {name: NOW - 900 for name in FILES}
        code, out = self.run_cli(["--host", "box", "--files", "heartbeat"], fake_runner(stale))
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), "box hung:heartbeat")

    def test_unreachable_exit_code(self):
        code, _out = self.run_cli(["--host", "box"], fake_runner({}, rc=255))
        self.assertEqual(code, 4)

    def test_rejects_unsafe_file_names(self):
        for bad in ("x;rm", "..", "."):
            with self.subTest(bad=bad):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        self.run_cli(["--host", "box", "--files", bad], fake_runner({}))
                self.assertEqual(ctx.exception.code, 64)
                self.assertNotEqual(ctx.exception.code, 2)

    def test_usage_errors_do_not_exit_2(self):
        for argv in (["--host", "box", "--files", ","],
                     ["--host", "box", "--stale-s", "soon"],
                     ["--host", "box", "--bogus"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        self.run_cli(argv, fake_runner({}))
                self.assertEqual(ctx.exception.code, 64)


if __name__ == "__main__":
    unittest.main()
