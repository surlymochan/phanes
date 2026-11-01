import os
import tempfile
import unittest
from unittest import mock

from lib.dispatch import lanes_report


class LanesReportTest(unittest.TestCase):
    def test_reasons_fallback_external_and_pause_priority(self):
        cfg = {"lanes": [
            {"name": "P1", "backend": "pi"}, {"name": "P2", "backend": "pi"},
            {"name": "C1", "backend": "codex"}, {"name": "A1", "backend": "agy"},
            {"name": "X1", "backend": "external"}, {"name": "D1", "backend": "pi", "drain": True},
            {"name": "H1", "backend": "pi"}, {"name": "U1", "backend": "pi"},
        ]}
        status = {"C1": "host_fail", "A1": {"hot_blocked": "PAUSE-hot.py"},
                 "H1": {"deps": True}}
        result = lanes_report.summarize(cfg, status, {"P1": {"issue": 10}},
             ["PAUSE-P2"], [{"backend": "codex"}, {"backend": "external"}])
        reasons = {x["lane"]: x["reason"] for x in result["idle"]}
        self.assertEqual(reasons["P2"], "paused:PAUSE-P2")
        self.assertEqual(reasons["C1"], "host_fail")
        self.assertEqual(reasons["A1"], "hot_blocked:PAUSE-hot.py")
        self.assertEqual(reasons["D1"], "drain")
        self.assertEqual(reasons["H1"], "deps")
        self.assertEqual(reasons["X1"], "no_card")
        self.assertEqual(reasons["U1"], "unknown")
        self.assertEqual(result["backends"]["pi"], {"busy": 1, "idle": 4})

    def test_cli_fetches_scheduler_ready_queue_by_default_and_formats_text(self):
        with tempfile.TemporaryDirectory() as root:
            lanes = os.path.join(root, "lanes.json")
            with open(lanes, "w") as f:
                f.write('{"lanes":[{"name":"P1","backend":"pi"}]}')
            for name, value in (("state.json", {}), ("lanes-status.json", {"P1": "idle"})):
                with open(os.path.join(root, name), "w") as f:
                    import json
                    json.dump(value, f)
            completed = mock.Mock(stdout='[{"number":1,"labels":[{"name":"lane-ready"},{"name":"backend:codex"}]}]')
            output = __import__("io").StringIO()
            with mock.patch.object(lanes_report.subprocess, "run", return_value=completed) as run, \
                 mock.patch("sys.stdout", output):
                self.assertEqual(lanes_report.main(["--state-dir", root, "--lanes", lanes]), 0)
            self.assertEqual(run.call_args.args[0][:5], ["gh", "issue", "list", "--state", "open"])
            self.assertIn("busy 0: pi 0/1", output.getvalue())
            self.assertIn("P1: unknown", output.getvalue())


if __name__ == "__main__":
    unittest.main()
