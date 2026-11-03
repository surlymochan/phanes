"""Unavailable models pause their lane instead of repeatedly failing cards (#1596)."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
# Keep _load's isolated OPENSWARM_HOME inside this worktree too.
os.environ["TMPDIR"] = str(HERE)
sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()


class LaneFailureClassification(unittest.TestCase):
    def test_provider_samples_are_model_unavailable_with_configured_model(self):
        samples = (
            "model 'claude-haiku-4.5' is unavailable",
            "Model metadata not found for gpt-test",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                error_class, detail = xs.classify_lane_failure(sample, "claude-haiku-4.5")
                self.assertEqual(error_class, "model_unavailable")
                self.assertIn("Configured model: claude-haiku-4.5", detail)

    def test_ordinary_failure_remains_exec_error(self):
        self.assertEqual(xs.classify_lane_failure("executor exited 1", "model-x"),
                         ("exec_error", "executor exited 1"))


class ModelUnavailableFinish(unittest.TestCase):
    def test_issue_returns_ready_and_writes_timed_pause(self):
        with tempfile.TemporaryDirectory(prefix="lane-model-unavailable-", dir=HERE) as home:
            engine_path = HERE.parent / "lib" / "dispatch" / "engine.py"
            import importlib.util
            spec = importlib.util.spec_from_file_location("model_unavailable_engine", engine_path)
            engine = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(engine)
            cfg_path = pathlib.Path(home) / "config.json"
            cfg_path.write_text(json.dumps({"repo": "acme/project", "local_repo": home,
                                           "state_dir": home, "pause_home": home,
                                           "ssh": {"control_dir": str(pathlib.Path(home) / "cm")},
                                           "local_hosts": [], "hot_files": {"limits": {}, "union": []}}))
            cfg = engine.load_config(str(cfg_path))
            d = engine.Dispatcher(cfg, runner=lambda *a, **k: (0, "", ""), clock=lambda: 1000)
            d.quota_lane_guard = lambda *args: None
            d.relabel = mock.Mock()
            d.gh = mock.Mock(return_value=(0, "", ""))
            d.mark_done = mock.Mock()
            info = {"status": "stopped", "error_class": "model_unavailable",
                    "model": "claude-haiku-4.5", "summary": "model unavailable"}
            self.assertTrue(d.finish("CX5", {"host": "box", "backend": "copilot"},
                                     {"issue": 1596, "run": "r1"}, info, {}))
            d.relabel.assert_called_once_with(1596, d.L["ready"], [d.L["in_progress"], d.L["failed"]])
            pause = engine.dispatch_module("pause").PauseStore(home).active(1001)
            self.assertEqual(len(pause), 1)
            self.assertEqual(pause[0]["scope"], "lane:CX5")
            self.assertEqual(pause[0]["expires_at"], 4600)
            self.assertEqual(pause[0]["reason"], "auto:model_unavailable claude-haiku-4.5")

    def test_review_model_failure_does_not_increment_fails(self):
        engine_path = HERE.parent / "lib" / "dispatch" / "engine.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("model_unavailable_review_engine", engine_path)
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        with tempfile.TemporaryDirectory(prefix="lane-model-review-", dir=HERE) as home:
            cfg_path = pathlib.Path(home) / "config.json"
            cfg_path.write_text(json.dumps({"repo": "acme/project", "local_repo": home,
                                           "state_dir": home, "pause_home": home,
                                           "ssh": {"control_dir": str(pathlib.Path(home) / "cm")},
                                           "local_hosts": [], "hot_files": {"limits": {}, "union": []}}))
            d = engine.Dispatcher(engine.load_config(str(cfg_path)), runner=lambda *a, **k: (0, "", ""))
            d.put_review = mock.Mock()
            d.log = mock.Mock()
            d.finish_review("CX5", {"backend": "copilot"}, {"review_pr": 12, "review_sha": "a"},
                            {"error_class": "model_unavailable", "status": "stopped"}, {})
            self.assertNotIn("fails", d.put_review.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
