"""Exercise the actual remote read/write snippets without SSH or a control plane."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "autodispatch_state_home", Path(__file__).resolve().parents[1] / "lane-autodispatch/autodispatch.py")
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


class RemoteStateHomeTest(unittest.TestCase):
    def check_home(self, relative, overrides=None, alternatives=()):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / relative
            run = root / "runs/run-1"
            run.mkdir(parents=True)
            lane = root / "lanes/L1"
            lane.mkdir(parents=True)
            (lane / "state.json").write_text(json.dumps({"state": "busy", "current_run": "run-1"}))
            (run / "result.json").write_text(json.dumps({"status": "done", "commit": "test-commit"}))
            for other in alternatives:
                (home / other).mkdir(parents=True)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("OPENSWARM_", "XSWARM_"))}
            env["HOME"] = tmp
            env.update(overrides or {})
            def snippet(source, *args):
                return subprocess.run([sys.executable, "-c", source, *args], env=env,
                                      check=True, capture_output=True, text=True).stdout
            status = json.loads(snippet(AD.REMOTE_STATUS, "L1", "run-1"))
            self.assertEqual(status["lane"], "busy")
            self.assertEqual(status["run"]["commit"], "test-commit")
            record = {"at": "test", "error_class": "validation", "class": "blocked", "missing": ["test"]}
            payload = base64.b64encode(json.dumps(record).encode()).decode()
            snippet(AD.RUN_RECORD_SNIPPET, "run-1", payload)
            self.assertEqual(json.loads((run / "pr-validation.json").read_text()), record)
            self.assertIn("validation", (run / "pr-validation.log").read_text())
            self.assertEqual(list(home.rglob("pr-validation.json")), [run / "pr-validation.json"])

    def test_explicit_home_wins_over_existing_defaults_and_alias(self):
        self.check_home("custom state", {"OPENSWARM_HOME": "~/custom state", "XSWARM_HOME": "~/unused"},
                        (".claude/swrm-executor-candidate", ".claude/openswarm-executor-candidate"))

    def test_legacy_environment_alias(self):
        self.check_home("alias", {"XSWARM_HOME": "~/alias"})

    def test_existing_default_directories(self):
        for name in ("swrm-executor-candidate", "openswarm-executor-candidate", "swrm", "openswarm"):
            with self.subTest(name=name):
                self.check_home(".claude/" + name)

    def test_new_candidate_wins_when_both_exist(self):
        self.check_home(".claude/swrm-executor-candidate", alternatives=(".claude/openswarm-executor-candidate",))

    def test_explicit_missing_home_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("OPENSWARM_", "XSWARM_"))}
            env.update(HOME=tmp, OPENSWARM_HOME=str(Path(tmp) / "missing"))
            result = subprocess.run([sys.executable, "-c", AD.REMOTE_STATUS, "L1", "run-1"],
                                    env=env, check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)["lane"], "unknown")
            self.assertFalse((Path(tmp) / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
