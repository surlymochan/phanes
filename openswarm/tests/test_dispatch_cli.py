import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "openswarm"
ENGINE = ROOT / "lib" / "dispatch" / "engine.py"


class DispatchCliTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=ROOT, env=env,
                              text=True, capture_output=True)

    def test_help_lists_tick_and_serve(self):
        result = self.run_cli("dispatch", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tick", result.stdout)
        self.assertIn("serve", result.stdout)

    def test_dry_run_json_with_empty_gh_queue(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = pathlib.Path(directory)
            gh = root / "gh"
            gh.write_text("#!/bin/sh\nprintf '[]\\n'\n")
            gh.chmod(0o755)
            state = root / "state"
            control = root / "control"
            control.mkdir()
            config = root / "dispatch.json"
            config.write_text(json.dumps({"repo": "example/project", "state_dir": str(state),
                                          "lanes_file": str(root / "lanes.json"),
                                          "ssh": {"control_dir": str(control)} }))
            env = os.environ.copy()
            env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
            result = self.run_cli("dispatch", "tick", "--config", str(config), "--dry-run", "--json", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), list)
            self.assertFalse(state.exists())
            self.assertEqual({item.name for item in root.iterdir()}, {"gh", "dispatch.json", "control"})

    def test_missing_engine_reports_path_and_exit_two(self):
        if not ENGINE.exists():
            self.skipTest("engine is present in this checkout")
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            moved = ENGINE.with_name("engine.py.test-hidden")
            ENGINE.rename(moved)
            try:
                result = self.run_cli("dispatch", "tick", "--config", "unused")
            finally:
                moved.rename(ENGINE)
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(ENGINE), result.stderr)


if __name__ == "__main__":
    unittest.main()
