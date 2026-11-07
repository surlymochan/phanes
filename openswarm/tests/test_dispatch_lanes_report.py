import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "lanes_report.py"
SPEC = importlib.util.spec_from_file_location("dispatch_lanes_report", MODULE_PATH)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


class LanesReportTests(unittest.TestCase):
    def test_string_lanes_preserved(self):
        got = report.summarize(["P1"], {"P1": "busy"}, {}, {}, [])
        self.assertEqual(got["busy"], 1)
        got = report.summarize({"lanes": ["P1"]}, {}, {}, {}, [])
        self.assertEqual(got["idle"][0]["lane"], "P1")

    def test_status_failures_and_drain(self):
        for status, reason in [("missing in batch", "host_fail"), ("down", "host_fail"),
                               ("error", "host_fail"), ("draining", "drain")]:
            with self.subTest(status=status):
                got = report.summarize({"P": {"backend": "pi"}}, {"P": status}, {}, {}, [])
                self.assertEqual(got["idle"][0]["reason"], reason)

    def test_external_backend_without_flag(self):
        got = report.summarize({"P": {"backend": "pi"}}, {}, {}, {}, [{"backend": "x"}])
        self.assertEqual(got["idle"][0]["reason"], "no_card")

    def test_hot_only_blocks_when_all_ready_candidates_conflict(self):
        lanes = {"L": {"backend": "pi"}, "B": {"backend": "pi"}}
        state = {"B": {"issue": 2, "hot": ["x.py"]}}
        queue = [{"backend": "pi", "deps_ok": False, "hot": ["x.py"]},
                 {"backend": "pi", "deps_ok": True, "hot": []}]
        got = report.summarize(lanes, {}, state, {}, queue)
        self.assertEqual(got["idle"][0]["reason"], "ready")
        queue[0]["deps_ok"] = True
        self.assertEqual(report.summarize(lanes, {}, state, {}, queue)["idle"][0]["reason"], "ready")

    def test_scheduler_dict_shape_and_reason_order(self):
        lanes = {"P1": {"host": "h1", "backend": "pi"}, "P2": {"backend": "pi"},
                 "C1": {"backend": "codex"}, "C2": {"backend": "codex", "drain": True},
                 "A1": {"backend": "agy"}, "X1": {"backend": "external"}}
        queue = [{"backend": "codex", "deps_ok": True}, {"backend": "pi", "deps_ok": False}]
        got = report.summarize(lanes, {"P1": "busy"}, {}, {"host:h1": {}}, queue)
        self.assertEqual((got["backends"]["pi"]["busy"], got["backends"]["pi"]["idle"]), (1, 1))
        reasons = {x["lane"]: x["reason"] for x in got["idle"]}
        self.assertNotIn("P1", reasons)
        self.assertEqual(reasons["P2"], "ready")
        self.assertEqual(reasons["C2"], "drain")
        self.assertEqual(reasons["A1"], "ready")  # unpinned cards remain takeable
        self.assertEqual(reasons["X1"], "ready")

    def test_idle_reasons_and_external_queue_exclusion(self):
        lanes = {"P": {"backend": "pi"}, "C": {"backend": "codex"}, "D": {"backend": "agy"},
                 "H": {"backend": "pi"}, "F": {"backend": "pi"}, "N": {"backend": "pi"}}
        queue = [{"backend": "other", "external": True}, {"backend": "codex", "deps_ok": False}]
        got = report.summarize(lanes, {"H": "ssh status timeout", "F": "unknown"}, {},
                               {"lane:N": {}, "host:h": {}}, queue)
        reasons = {x["lane"]: x["reason"] for x in got["idle"]}
        self.assertEqual(reasons["P"], "deps")
        self.assertEqual(reasons["C"], "deps")
        self.assertEqual(reasons["D"], "deps")
        self.assertEqual(reasons["H"], "host_fail")
        self.assertEqual(reasons["F"], "unknown")
        self.assertEqual(reasons["N"], "paused:PAUSE-N")
        external_only = report.summarize({"P": {"backend": "pi"}}, {}, {}, {}, [{"backend": "x", "external": True}])
        self.assertEqual(external_only["idle"][0]["reason"], "no_card")
        self.assertTrue(report.can_take("pi", {"backend": "agy"}))
        self.assertFalse(report.can_take("agy", {"backend": "codex", "strict": True}))

    def test_queue_unknown_hot_and_dependency_readiness(self):
        lanes = {"L": {"backend": "pi"}}
        self.assertEqual(report.summarize(lanes, {}, {}, {}, None)["idle"][0]["reason"], "unknown")
        queue = [{"backend": "pi", "deps_ok": True}]
        self.assertEqual(report.summarize(lanes, {}, {}, {}, queue)["idle"][0]["reason"], "ready")
        queue = [{"backend": "pi", "deps_ok": True, "hot": ["x.py"]}]
        state = {"B": {"issue": 2, "hot": ["x.py"]}}
        result = report.summarize({"L": {"backend": "pi"}, "B": {"backend": "pi"}},
                                  {"B": "busy #2"}, state, {}, queue)
        self.assertEqual(result["idle"][0]["reason"], "hot_blocked:x.py")

    def test_cli_config_repo_and_text_summary(self):
        with tempfile.TemporaryDirectory() as root:
            lanes = Path(root) / "lanes.json"; lanes.write_text('{"P1":{"backend":"pi"}}', encoding="utf-8")
            cfg = Path(root) / "dispatch.json"; cfg.write_text('{"repo":"org/repo"}', encoding="utf-8")
            result = report.summarize({"P1": {"backend": "pi"}}, {}, {}, {}, [])
            self.assertEqual(result["backends"]["pi"], {"busy": 0, "idle": 1})
            self.assertEqual("busy %d: %s" % (result["busy"], ", ".join("%s %d/%d" %
                (backend, values["busy"], values["busy"] + values["idle"]) for backend, values in result["backends"].items())),
                "busy 0: pi 0/1")
            completed = type("Completed", (), {"returncode": 0, "stdout": "[]"})()
            with mock.patch.object(report.subprocess, "run", return_value=completed) as run:
                self.assertEqual(report._queue(report._merge(report.DEFAULTS, {"repo": "org/repo"})), [])
            self.assertEqual(run.call_args.args[0][2:4], ["-R", "org/repo"])

    def test_pause_store_scope_is_applied(self):
        import time
        got = report.summarize({"X": {"backend": "copilot"}}, {}, {},
                               {"lane:X": {"scope": "lane:X", "expires_at": time.time() + 60}}, [])
        self.assertEqual(got["idle"][0]["reason"], "paused:PAUSE-X")


if __name__ == "__main__":
    unittest.main()
