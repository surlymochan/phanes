import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "lane-exec-error-triage.py"
spec = importlib.util.spec_from_file_location("lane_exec_error_triage", SCRIPT)
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


# 2026-11-03 00:17-00:25 (UTC+8): four GH2 runs, each ending after ~2 minutes
# with failed/exec_error and no commit.
GH2_RUNS = (
    ("20261103-001700-aaaa", 118),
    ("20261103-002100-bbbb", 121),
    ("20261103-002300-cccc", 115),
    ("20261103-002500-dddd", 120),
)
BROKER_TAIL = ("pi isolated launcher\n"
               "socat: connect(/tmp/openswarm-pi-abc123/broker.sock): "
               "No such file or directory\n")


def write_run(root, run_id, lane, error_class, rc, seconds, backend="pi", with_result=True,
              tail="", started=1793665020.0):
    run_dir = Path(root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    result = {"id": run_id, "status": "failed", "error_class": error_class, "backend": backend,
              "model": "MiniMax-M3.1-Flash", "lane": lane, "seconds": seconds, "commit": None,
              "attempts": [{"backend": backend, "rc": rc, "seconds": seconds,
                            "error_class": error_class, "error_tail": tail}]}
    if with_result:
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (run_dir / "status.json").write_text(json.dumps(
        {"lane": lane, "backend": backend, "started": started, "finished": started + seconds,
         "state": "finished", "status": "failed"}), encoding="utf-8")
    return run_dir


class LaneExecErrorTriageTests(unittest.TestCase):
    def test_exec_error_burst_is_classified_from_the_evidence_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            for run_id, seconds in GH2_RUNS:
                run_dir = write_run(tmp, run_id, "GH2", "exec_error", 1, seconds,
                                    tail="socat: connect(/tmp/openswarm-pi-abc123/broker.sock): "
                                         "No such file or directory")
                (run_dir / "events.1.pi.jsonl.err").write_text(BROKER_TAIL, encoding="utf-8")
            report = triage.build_report([tmp])
        lane = report["lanes"][0]
        self.assertEqual(lane["lane"], "GH2")
        self.assertEqual(lane["failures"], 4)
        self.assertEqual(lane["dominant_signature"], "exec_error:broker_unreachable")
        self.assertEqual(lane["causes"], {"broker_unreachable": 4})
        evidence = lane["evidence"][0]
        self.assertEqual(evidence["file"], "events.1.pi.jsonl.err")
        self.assertEqual(evidence["line"], 2)
        self.assertIn("broker.sock", evidence["text"])

    def test_large_log_evidence_uses_absolute_line_numbers(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parents[1]) as tmp:
            run_dir = write_run(tmp, "20261103-001700-aaaa", "GH2", "exec_error", 1, 118)
            path = run_dir / "events.1.pi.jsonl.err"
            for prefix in ("x" * 99 + "\n", "x" * 100 + "\n"):
                with self.subTest(line_bytes=len(prefix)):
                    path.write_text(prefix * 6000 + "socat: connection refused\n", encoding="utf-8")
                    evidence = triage.scan_evidence(run_dir, {})
                    self.assertEqual(len(evidence), 1)
                    self.assertEqual(evidence[0]["line"], 6001)
                    self.assertEqual(path.read_text().splitlines()[6000], evidence[0]["text"])

    def test_a_run_without_an_error_class_has_unknown_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "20261102-231000-1111", "CX9", None, None, 95, backend="codex")
            write_run(tmp, "20261102-233000-2222", "CX9", None, None, 95, backend="codex")
            write_run(tmp, "20261102-235000-3333", "CX9", None, None, 40, backend="codex",
                      with_result=False)
            report = triage.build_report([tmp])
        lane = report["lanes"][0]
        self.assertEqual(lane["dominant_signature"], "none:missing_error_class")
        self.assertEqual(lane["causes"]["no_result"], 1)

    def test_different_signatures_do_not_establish_root_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            gh2 = Path(tmp) / "gh2"
            cx9 = Path(tmp) / "cx9"
            for run_id, seconds in GH2_RUNS:
                run_dir = write_run(gh2, run_id, "GH2", "exec_error", 1, seconds,
                                    tail="socat: connect(/tmp/openswarm-pi-abc123/broker.sock): "
                                         "No such file or directory")
                (run_dir / "events.1.pi.jsonl.err").write_text(BROKER_TAIL, encoding="utf-8")
            for run_id in ("20261102-231000-1111", "20261102-233000-2222"):
                write_run(cx9, run_id, "CX9", None, None, 95, backend="codex")
            report = triage.build_report([tmp], compare_lane="CX9")
        compare = report["compare"]
        self.assertIsNone(compare["same_root_cause"])
        self.assertIn("insufficient evidence", compare["reason"])

    def test_missing_lane_and_equal_signatures_remain_unknown(self):
        lane = {"lane": "GH2", "dominant_signature": "exec_error:unknown"}
        self.assertIsNone(triage.compare_lanes(lane, lane)["same_root_cause"])
        report = {"runs": 0, "lanes": [], "compare": triage.compare_lanes(None, lane)}
        self.assertIn("insufficient evidence", triage.render_text(report))

    def test_a_quota_failure_keeps_its_own_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "20261103-003000-eeee", "M8", "rate_limit", 75, 30,
                      tail="429 Too Many Requests")
            report = triage.build_report([tmp])
        self.assertEqual(report["lanes"][0]["dominant_signature"], "rate_limit:quota_or_rate_limit")

    def test_a_broker_log_without_requests_proves_the_failure_was_before_the_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_run(tmp, "20261103-001700-aaaa", "GH2", "exec_error", 1, 118,
                                tail="socat: connect(/tmp/openswarm-pi-abc123/broker.sock): "
                                     "No such file or directory")
            (run_dir / "events.1.pi.jsonl.err").write_text(BROKER_TAIL, encoding="utf-8")
            log = Path(tmp) / "broker.requests.jsonl"
            log.write_text(json.dumps({"ts": "2026-11-03T00:17:30Z", "id": "x", "lane": "M8",
                                       "status": 200, "err": "", "ms": 900}) + "\n", encoding="utf-8")
            report = triage.build_report([tmp], broker_path=str(log))
            record = triage.read_run(Path(tmp) / "20261103-001700-aaaa")
            cross = triage.broker_cross_check(triage.broker_records(str(log)), record)
        # M8 got a request in the same window, GH2 did not: the broker was
        # alive and the GH2 failure stayed inside the lane.
        self.assertEqual(cross["requests"], 0)
        self.assertEqual(cross["other_requests"], 1)
        self.assertEqual(cross["verdict"], "lane_silent_broker_alive")
        self.assertEqual(report["lanes"][0]["runs_detail"][0]["status"], "failed")

    def test_a_signal_killed_executor_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "20261103-001700-aaaa", "GH2", "exec_error", -9, 118,
                      tail="")
            report = triage.build_report([tmp])
        lane = report["lanes"][0]
        self.assertEqual(lane["dominant_signature"], "exec_error:killed_by_signal_9")
        self.assertEqual(lane["evidence"][0]["cause"], "killed_by_signal_9")

    def test_the_harvest_report_has_no_error_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            harvest = Path(tmp) / "harvest.json"
            harvest.write_text(json.dumps({"ok": False, "status": "failed", "completed": [],
                                           "fetch_fails": 2,
                                           "failed": [{"host": "gores2", "lane": "CX9",
                                                       "run_id": "r1", "status": "failed",
                                                       "exit_code": 255,
                                                       "error": "remote run info failed (rc=255)",
                                                       "fetch_fails": 2}]}), encoding="utf-8")
            rows = triage.harvest_failures(harvest)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["error_class"])
        self.assertEqual(rows[0]["exit_code"], 255)


if __name__ == "__main__":
    unittest.main()
