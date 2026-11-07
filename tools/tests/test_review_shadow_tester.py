"""Offline tests of the tester shadow verdict: real heads, real checks, bad input."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "review-shadow-tester.py"

DEMO_TEST = '''"""Tests for openswarm/lib/demo.py."""

import importlib.util
import pathlib
import unittest


def load():
    path = pathlib.Path(__file__).resolve().parents[2] / "openswarm" / "lib" / "demo.py"
    spec = importlib.util.spec_from_file_location("demo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DemoTests(unittest.TestCase):
    def test_value(self):
        self.assertEqual(load().VALUE, 1)
'''


def load_tool():
    """Import the tool as a module so its helpers can be exercised directly."""
    spec = importlib.util.spec_from_file_location("review_shadow_tester", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    """Run one git command inside a scratch repository."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class ShadowTesterTests(unittest.TestCase):
    """The shadow tester decides a real head with machine checks and abstains when it cannot."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        self.write("openswarm/lib/demo.py", "VALUE = 1\n")
        self.write("tools/tests/test_demo.py", DEMO_TEST)
        self.commit("base")
        self.base = self.rev("HEAD")

    def write(self, path, text):
        """Write one file into the scratch repository."""
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        """Commit everything in the scratch repository and return the new head."""
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return self.rev("HEAD")

    def rev(self, ref):
        """Return one revision of the scratch repository."""
        result = subprocess.run(["git", "-C", str(self.repo), "rev-parse", ref],
                                check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def run_tester(self, head, expect=0, *extra):
        """Run the shadow tester on a head and return its report."""
        report = Path(self.directory.name) / "report.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                                 "--pr", "7", "--head", head, "--json-out", str(report), *extra],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, expect, result.stderr or result.stdout)
        return json.loads(report.read_text(encoding="utf-8"))

    def test_a_clean_head_passes_and_the_report_names_every_check(self):
        self.write("openswarm/lib/demo.py", "VALUE = 1\nEXTRA = 2\n")
        head = self.commit("tweak")
        report = self.run_tester(head)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["schema"], "phanes.review-shadow-tester/1")
        self.assertEqual(report["base"], self.base)
        self.assertEqual(report["uncovered"], [])
        self.assertEqual({check["name"] for check in report["checks"]},
                         {"diff-hygiene", "python-syntax", "unit-tests", "cli-smoke"})
        self.assertFalse([check for check in report["checks"] if check["status"] == "fail"])

    def test_a_binary_artifact_in_the_diff_fails_the_head(self):
        self.write("openswarm/tests/.build-00000000.so", "not a source file\n")
        head = self.commit("artifact")
        report = self.run_tester(head, expect=1)
        self.assertEqual(report["verdict"], "FAIL")
        hygiene = [check for check in report["checks"] if check["name"] == "diff-hygiene"][0]
        self.assertEqual(hygiene["status"], "fail")
        self.assertIn(".build-00000000.so", hygiene["detail"])

    def test_a_broken_source_fails_the_head(self):
        self.write("openswarm/lib/demo.py", "def (\n")
        head = self.commit("broken")
        report = self.run_tester(head, expect=1)
        self.assertEqual(report["verdict"], "FAIL")
        syntax = [check for check in report["checks"] if check["name"] == "python-syntax"][0]
        self.assertEqual(syntax["status"], "fail")
        self.assertIn("demo.py", syntax["detail"])

    def test_a_failing_test_fails_the_head(self):
        self.write("openswarm/lib/demo.py", "VALUE = 2\n")
        self.write("tools/tests/test_demo.py", DEMO_TEST.replace("VALUE, 1", "VALUE, 1"))
        head = self.commit("wrong value")
        report = self.run_tester(head, expect=1)
        self.assertEqual(report["verdict"], "FAIL")
        tests = [check for check in report["checks"] if check["name"] == "unit-tests"][0]
        self.assertEqual(tests["status"], "fail")
        self.assertIn("test_demo.py", tests["detail"])

    def test_a_source_without_a_test_is_insufficient_and_not_a_failure(self):
        self.write("openswarm/lib/other.py", "VALUE = 3\n")
        head = self.commit("uncovered")
        report = self.run_tester(head, expect=3)
        self.assertEqual(report["verdict"], "INSUFFICIENT")
        self.assertEqual(report["uncovered"], ["openswarm/lib/other.py"])
        self.assertIn("no machine check covers", report["reason"])

    def test_a_head_that_is_not_in_the_object_store_is_insufficient(self):
        report = self.run_tester("0" * 40, expect=3)
        self.assertEqual(report["verdict"], "INSUFFICIENT")
        self.assertIn("not in the local object store", report["reason"])

    def test_a_diff_without_any_check_is_insufficient(self):
        self.write("docs/note.md", "a note\n")
        head = self.commit("docs only")
        report = self.run_tester(head, expect=3)
        self.assertEqual(report["verdict"], "INSUFFICIENT")
        self.assertEqual(report["uncovered"], [])

    def test_a_head_that_is_not_a_sha_is_rejected(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                                 "--head", "not-a-sha"], capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("hex characters", result.stderr)

    def test_the_helpers_of_the_tool_behave(self):
        module = load_tool()
        self.assertTrue(module.is_source("openswarm/lib/dispatch/fleet_status.py"))
        self.assertFalse(module.is_source("docs/fleet-status-page.md"))
        self.assertTrue(module.is_test("openswarm/tests/test_fleet_status.py"))
        self.assertTrue(module.mentions("import openswarm.lib.dispatch.fleet_status",
                                        "openswarm/lib/dispatch/fleet_status.py"))
        self.assertFalse(module.mentions("engineering notes", "openswarm/lib/dispatch/engine.py"))
        self.assertEqual(module.compile_source("x = 1\n", "x.py"), (True, ""))
        self.assertFalse(module.compile_source("def (\n", "x.py")[0])
        self.assertEqual(module.decide([{"name": "a", "role": "evidence", "subjects": 1,
                                        "status": "pass", "detail": ""}], [])[0], "PASS")
        self.assertEqual(module.decide([{"name": "a", "role": "evidence", "subjects": 1,
                                        "status": "fail", "detail": "boom"}], [])[0], "FAIL")
        self.assertEqual(module.decide([], ["x.py"])[0], "INSUFFICIENT")


if __name__ == "__main__":
    unittest.main()
