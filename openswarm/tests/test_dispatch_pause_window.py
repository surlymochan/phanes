"""Offline VM-pause classification and real dispatch-harvest integration."""

import contextlib
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


DISPATCH = Path(__file__).resolve().parents[1] / "lib" / "dispatch"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = load_module("pause_window_under_test", DISPATCH / "pause_window.py")
E = load_module("pause_window_engine_under_test", DISPATCH / "engine.py")
SHA = "a" * 40
SAMPLES = [
    {"from": "2026-11-02 07:31:44", "to": "2026-11-02 07:49:46", "gap_s": 1082},
    {"from": "2026-11-02 07:55:24", "to": "2026-11-02 08:11:35", "gap_s": 971},
]


def epoch(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


def failed_run(**changes):
    info = {"status": "failed", "error_class": "exec_error", "backend": "pi",
            "started": epoch("2026-11-02 07:30:00"),
            "finished": epoch("2026-11-02 07:50:00"), "summary": "stream interrupted"}
    info.update(changes)
    return info


class PauseWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pause-window-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "box-pauses.jsonl"
        self.path.write_text("\n".join(json.dumps(item) for item in SAMPLES) + "\n")
        self.pauses = P.load_pauses(self.path)

    def test_today_samples_and_malformed_records_are_tolerated(self):
        lines = ["{bad", "null", "[]", "{}", json.dumps({"from": "bad", "to": "bad", "gap_s": 120}),
                 json.dumps({"from": SAMPLES[0]["to"], "to": SAMPLES[0]["from"], "gap_s": 120})]
        self.path.write_text("\n".join(lines + [json.dumps(item) for item in SAMPLES]) + "\n")
        pauses = P.load_pauses(self.path)
        self.assertEqual(len(pauses), 2)
        self.assertEqual([item["gap_s"] for item in pauses], [1082, 971])
        self.assertEqual([item["from"] for item in pauses], [item["from"] for item in SAMPLES])

    def test_missing_or_nonfile_pause_logs_are_empty(self):
        self.assertEqual(P.load_pauses(self.root / "missing.jsonl"), [])
        self.assertEqual(P.load_pauses(self.root), [])

    def test_each_today_pause_overlaps_and_the_first_matching_window_is_returned(self):
        for sample in SAMPLES:
            with self.subTest(sample=sample):
                matched = P.overlapping(epoch(sample["from"]) - 1, epoch(sample["to"]) + 1, self.pauses)
                self.assertEqual(matched["from"], sample["from"])
        matched = P.overlapping(epoch("2026-11-02 07:00:00"), epoch("2026-11-02 08:20:00"), self.pauses)
        self.assertEqual(matched["from"], SAMPLES[0]["from"])

    def test_minimum_gap_boundary_and_override(self):
        short = dict(SAMPLES[0], gap_s=119,
                     to=datetime.fromtimestamp(epoch(SAMPLES[0]["from"]) + 119).strftime("%Y-%m-%d %H:%M:%S"))
        self.path.write_text(json.dumps(short) + "\n")
        small = P.load_pauses(self.path)
        start, end = failed_run()["started"], failed_run()["finished"]
        self.assertIsNone(P.overlapping(start, end, small))
        self.assertIsNotNone(P.overlapping(start, end, small, min_gap_s=119))
        short.update(gap_s=120, to=datetime.fromtimestamp(epoch(short["from"]) + 120).strftime("%Y-%m-%d %H:%M:%S"))
        self.path.write_text(json.dumps(short) + "\n")
        self.assertIsNotNone(P.overlapping(start, end, P.load_pauses(self.path)))
        self.assertIsNone(P.overlapping(start, end, self.pauses, min_gap_s=2000))

    def test_only_local_exec_error_or_stalled_runs_are_reclassified(self):
        for error in ("exec_error", "stalled"):
            with self.subTest(error=error):
                info = failed_run(error_class=error)
                self.assertEqual(P.classify(info, {"host": "box"}, self.pauses, {"box"}), "vm_paused")
                self.assertEqual(P.classify(info, {"host": "remote"}, self.pauses, {"box"}), error)
        for error in ("quota", "rate_limit", "model_unavailable", None):
            with self.subTest(error=error):
                self.assertEqual(P.classify(failed_run(error_class=error), {"host": "box"},
                                            self.pauses, {"box"}), error)

    def test_runs_after_pauses_and_runs_finished_before_pauses_are_not_changed(self):
        for started, finished in (("2026-11-02 08:12:00", "2026-11-02 08:20:00"),
                                  ("2026-11-02 07:00:00", "2026-11-02 07:20:00")):
            with self.subTest(started=started):
                info = failed_run(started=epoch(started), finished=epoch(finished))
                self.assertEqual(P.classify(info, {"host": "box"}, self.pauses, {"box"}), "exec_error")

    def test_missing_invalid_or_reversed_run_windows_are_conservative(self):
        for changes in ({"started": None}, {"finished": None}, {"started": "bad"},
                        {"started": epoch("2026-11-02 08:20:00")}):
            with self.subTest(changes=changes):
                self.assertEqual(P.classify(failed_run(**changes), {"host": "box"},
                                            self.pauses, {"box"}), "exec_error")

    def test_local_string_run_timestamps_and_explicit_minimum(self):
        info = failed_run(started="2026-11-02 07:30:00", finished="2026-11-02 07:50:00")
        self.assertEqual(P.classify(info, {"host": "box"}, self.pauses, {"box"}), "vm_paused")
        self.assertEqual(P.classify(info, {"host": "box"}, self.pauses, {"box"}, min_gap_s=2000), "exec_error")


class HarvestPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pause-harvest-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = {"repo": "acme/project", "local_repo": str(self.root),
               "state_dir": str(self.root / "state"), "local_hosts": ["box"],
               "ssh": {"control_dir": str(self.root / "cm")}}
        cfg_path = self.root / "config.json"
        cfg_path.write_text(json.dumps(raw))
        self.cfg = E.load_config(str(cfg_path))
        self.calls = []
        self.output = io.StringIO()

        def fake_run(command, **kwargs):
            self.calls.append(list(command) if not isinstance(command, str) else command)
            return 0, "", ""

        self.dispatcher = E.Dispatcher(self.cfg, runner=fake_run, out=self.output,
                                       clock=lambda: epoch("2026-11-02 09:00:00"))
        self.pause_file = Path(self.dispatcher.path("box-pauses.jsonl"))
        self.pause_file.write_text("\n".join(json.dumps(item) for item in SAMPLES) + "\n")
        self.lane = {"host": "box", "backend": "pi"}

    def command_text(self):
        return [command if isinstance(command, str) else " ".join(map(str, command)) for command in self.calls]

    def review(self, info=None, lane=None):
        self.dispatcher.put_review(1572, {"fails": 2})
        state = {"review_pr": 1572, "review_sha": SHA, "issue": "PR1572", "run": "review-run"}
        result = self.dispatcher.finish_review("M7", lane or self.lane, state, info or failed_run(), {})
        self.assertTrue(result)
        return self.dispatcher.load_reviews()["1572"]

    def test_review_overlap_does_not_increment_existing_failures_and_logs_interval(self):
        self.assertEqual(self.review()["fails"], 2)
        log = self.output.getvalue()
        self.assertIn("vm_paused", log)
        self.assertIn(SAMPLES[0]["from"], log)
        self.assertIn(SAMPLES[0]["to"], log)

    def test_configured_gap_threshold_controls_review_reclassification(self):
        self.cfg["min_gap_s"] = 2000
        self.assertEqual(self.review()["fails"], 3)

    def test_configured_pause_file_is_used_instead_of_default(self):
        configured = self.root / "custom-pauses.jsonl"
        configured.write_text(self.pause_file.read_text())
        self.pause_file.write_text("")
        self.cfg["pauses_file"] = str(configured)
        self.assertEqual(self.review()["fails"], 2)

    def test_remote_review_and_old_run_harvest_are_not_excused(self):
        self.assertEqual(self.review(lane={"host": "far", "backend": "pi"})["fails"], 3)
        old = failed_run(started=epoch("2026-11-02 07:00:00"), finished=epoch("2026-11-02 07:20:00"))
        self.assertEqual(self.review(info=old)["fails"], 3)

    def test_review_harvest_keeps_quota_guard_except_for_vm_pause(self):
        old = failed_run(started=epoch("2026-11-02 07:00:00"), finished=epoch("2026-11-02 07:20:00"))
        for info, expected_calls in ((old, 1), (failed_run(), 0)):
            with self.subTest(expected_calls=expected_calls):
                st = {"review_pr": 1572, "review_sha": SHA, "issue": "PR1572", "run": "review-run"}
                with mock.patch.object(self.dispatcher, "quota_lane_guard",
                                       wraps=self.dispatcher.quota_lane_guard) as guard:
                    self.assertTrue(self.dispatcher.finish("M7", self.lane, st, info, {}))
                self.assertEqual(guard.call_count, expected_calls)

    def test_implementation_card_returns_to_ready_without_failed_label(self):
        st = {"issue": 1503, "run": "failed-run", "base": SHA}
        self.assertTrue(self.dispatcher.finish("M14", self.lane, st, failed_run(), {}))
        commands = self.command_text()
        label_commands = [command for command in commands if "issue edit 1503" in command]
        self.assertTrue(any("--add-label lane-ready" in command for command in label_commands))
        self.assertTrue(any("--remove-label in-progress" in command for command in label_commands))
        self.assertFalse(any("--add-label lane-failed" in command for command in commands))
        comments = [command for command in commands if "issue comment 1503" in command]
        self.assertTrue(any("VM" in command and "非卡片失败" in command for command in comments))

    def test_paused_task_failed_relabel_stays_unfinished_without_success_comment(self):
        def rejected_labels(command, **kwargs):
            self.calls.append(list(command))
            if command[:4] == ["gh", "issue", "edit", "1503"]:
                return 1, "", "fixture: labels rejected"
            return 0, "", ""

        self.dispatcher.runner = rejected_labels
        self.dispatcher.sleep = lambda _: None
        st = {"issue": 1503, "run": "paused-label-failure", "base": SHA}
        state = {"M14": st, "_done": [999]}
        self.assertFalse(self.dispatcher.finish("M14", self.lane, st, failed_run(), state))
        self.assertEqual(state["_done"], [999])
        self.assertIs(state["M14"], st)
        self.assertEqual(state["_label_debt"]["1503"]["add"], "lane-ready")
        commands = self.command_text()
        self.assertEqual(sum("issue edit 1503" in command for command in commands), 3)
        self.assertFalse(any("issue comment 1503" in command or "pr create" in command for command in commands))
        self.assertNotIn("→ requeued lane-ready", self.output.getvalue())

    def test_partial_commit_from_paused_run_is_requeued_without_fetch_or_pr(self):
        st = {"issue": 1503, "run": "partial-run", "base": SHA}
        info = failed_run(branch="openswarm/partial-run", commit="b" * 40)
        self.assertTrue(self.dispatcher.finish("M14", self.lane, st, info, {}))
        commands = self.command_text()
        self.assertTrue(any("issue edit 1503" in command and "--add-label lane-ready" in command for command in commands))
        self.assertFalse(any(" fetch " in command or "pr create" in command or " push " in command for command in commands))

    def test_remote_status_preserves_run_window_from_status_json(self):
        home = self.root / "executor"
        lane_dir, run_dir = home / "lanes" / "M7", home / "runs" / "review-run"
        lane_dir.mkdir(parents=True)
        run_dir.mkdir(parents=True)
        (lane_dir / "state.json").write_text(json.dumps({"state": "idle"}))
        info = failed_run()
        resets_at = "2026-11-02T12:00:00Z"
        (run_dir / "result.json").write_text(json.dumps({"status": "failed", "error_class": "exec_error",
                                                         "seconds": 1200, "finished_at": info["finished"],
                                                         "resets_at": resets_at,
                                                         "result": {"summary": "interrupted"}}))
        (run_dir / "status.json").write_text(json.dumps({"started": info["started"], "finished": info["finished"]}))
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["remote", "M7", "review-run", str(home)]), \
                contextlib.redirect_stdout(output):
            exec(E.REMOTE_STATUS, {})
        result = json.loads(output.getvalue())["run"]
        self.assertEqual(result["started"], info["started"])
        self.assertEqual(result["finished"], info["finished"])
        self.assertEqual(result["error_class"], "exec_error")
        self.assertEqual(result["seconds"], 1200)
        self.assertEqual(result["timestamp"], info["finished"])
        self.assertEqual(result["resets_at"], resets_at)

    def test_remote_status_missing_status_uses_result_mtime_not_harvest_clock(self):
        home = self.root / "old-executor"
        run_dir = home / "runs" / "old-run"
        run_dir.mkdir(parents=True)
        result_path = run_dir / "result.json"
        result_path.write_text(json.dumps({"status": "failed", "error_class": "exec_error",
                                          "seconds": 1200, "result": {"summary": "old failure"}}))
        finished = epoch("2026-11-02 07:20:00")
        os.utime(result_path, (finished, finished))
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["remote", "M7", "old-run", str(home)]), \
                contextlib.redirect_stdout(output):
            exec(E.REMOTE_STATUS, {})
        result = json.loads(output.getvalue())["run"]
        self.assertEqual(result["finished"], finished)
        self.assertEqual(result["timestamp"], finished)
        self.assertEqual(P.classify(result, self.lane, P.load_pauses(self.pause_file), {"box"}), "exec_error")


if __name__ == "__main__":
    unittest.main()
