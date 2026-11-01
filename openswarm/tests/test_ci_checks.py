"""`openswarm/lib/ci/checks.py`: CI check classification and red-ignore policy (Refs #1461).

The module is loaded by path: `openswarm/lib/ci/` is a namespace package without an
`__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
CHECKS_MODULE = HERE.parent / "lib" / "ci" / "checks.py"

spec = importlib.util.spec_from_file_location("ci_checks", CHECKS_MODULE)
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def check_run(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def status_context(context, state="SUCCESS"):
    return {"__typename": "StatusContext", "context": context, "state": state, "targetUrl": ""}


def run_row(run_id, name="ci", conclusion="failure", head_sha="abc123"):
    """One `gh run list --json databaseId,name,conclusion,headSha` row (no `status`)."""
    return {
        "databaseId": run_id,
        "name": name,
        "conclusion": conclusion,
        "headSha": head_sha,
    }


class IgnorePolicyTests(unittest.TestCase):

    def test_upstream_is_always_ignored(self):
        policy = checks.IgnorePolicy.from_cfg({"ignore_red_re": "upstream"}, exists=lambda p: False)
        self.assertTrue(policy.ignores("Upstream drift check"))
        self.assertFalse(policy.ignores("unit tests"))

    def test_file_rule_follows_the_switch_file(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-ci-checks-") as tmp:
            switch = pathlib.Path(tmp) / "MACOS-UPSTREAM-RED"
            cfg = {"ignore_red_when_file": {"path": str(switch), "re": "macos"}}
            policy = checks.IgnorePolicy.from_cfg(cfg)
            self.assertFalse(policy.ignores("macOS build"))
            switch.write_text("")
            policy = checks.IgnorePolicy.from_cfg(cfg)
            self.assertTrue(policy.ignores("macOS build"))
            self.assertTrue(policy.ignores("MACOS lint"))
            self.assertFalse(policy.ignores("linux build"))

    def test_file_rule_accepts_a_list(self):
        cfg = {"ignore_red_when_file": [{"path": "/x/a", "re": "macos"}, {"path": "/x/b", "re": "windows"}]}
        policy = checks.IgnorePolicy.from_cfg(cfg, exists=lambda p: p == "/x/b")
        self.assertTrue(policy.ignores("windows smoke"))
        self.assertFalse(policy.ignores("macos smoke"))

    def test_empty_config_ignores_nothing(self):
        policy = checks.IgnorePolicy.from_cfg({})
        self.assertFalse(policy.ignores("upstream macos"))
        self.assertFalse(checks.IgnorePolicy.from_cfg(None).ignores("upstream"))


class ClassifyTests(unittest.TestCase):

    def setUp(self):
        self.policy = checks.IgnorePolicy.from_cfg({"ignore_red_re": "upstream"}, exists=lambda p: False)

    def test_check_run_and_status_context_shapes(self):
        result = checks.classify(
            [check_run("build"), status_context("ci/lint", "SUCCESS"), check_run("test", "FAILURE")],
            self.policy,
        )
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["red"], ["test"])

    def test_status_context_pending_and_error(self):
        pending = checks.classify([status_context("ci/x", "PENDING")], self.policy)
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["pending"], ["ci/x"])
        error = checks.classify([status_context("ci/y", "ERROR")], self.policy)
        self.assertEqual(error["state"], "red")
        self.assertEqual(error["red"], ["ci/y"])

    def test_upstream_red_is_ignored_and_reported(self):
        result = checks.classify([check_run("upstream drift", "FAILURE"), check_run("build")], self.policy)
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["red"], [])
        self.assertEqual(result["ignored"], ["upstream drift"])

    def test_only_ignored_red_is_green(self):
        result = checks.classify([status_context("upstream-sync", "FAILURE")], self.policy)
        self.assertEqual(result["state"], "green")

    def test_macos_red_follows_switch_file(self):
        cfg = {"ignore_red_when_file": {"path": "/switch/MACOS-UPSTREAM-RED", "re": "macos"}}
        on = checks.IgnorePolicy.from_cfg(cfg, exists=lambda p: True)
        off = checks.IgnorePolicy.from_cfg(cfg, exists=lambda p: False)
        rolled = [check_run("macos-14 tests", "FAILURE")]
        self.assertEqual(checks.classify(rolled, on)["state"], "green")
        self.assertEqual(checks.classify(rolled, off)["state"], "red")
        self.assertEqual(checks.classify(rolled, off)["red"], ["macos-14 tests"])

    def test_empty_checks_is_none(self):
        self.assertEqual(checks.classify([], self.policy)["state"], "none")
        self.assertEqual(checks.classify(None, self.policy)["state"], "none")

    def test_pending_wins_over_green(self):
        result = checks.classify(
            [check_run("build"), check_run("deploy", status="IN_PROGRESS", conclusion=None)],
            self.policy,
        )
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["pending"], ["deploy"])

    def test_skipped_and_neutral_count_as_passed(self):
        result = checks.classify(
            [check_run("optional", "SKIPPED"), check_run("info", "NEUTRAL")],
            self.policy,
        )
        self.assertEqual(result["state"], "green")


class RerunTargetsTests(unittest.TestCase):

    def test_failed_runs_minus_ignored(self):
        policy = checks.IgnorePolicy.from_cfg({"ignore_red_re": "upstream"}, exists=lambda p: False)
        runs = [
            {"databaseId": 1, "name": "ci", "status": "completed", "conclusion": "failure"},
            {"databaseId": 2, "name": "upstream-sync", "status": "completed", "conclusion": "failure"},
            {"databaseId": 3, "name": "ci", "status": "completed", "conclusion": "success"},
            {"databaseId": 4, "name": "lint", "status": "in_progress", "conclusion": ""},
        ]
        targets = checks.rerun_targets(runs, policy)
        self.assertEqual([r["databaseId"] for r in targets], [1])

    def test_failed_run_without_status_field_is_a_target(self):
        """`gh run list --json databaseId,name,conclusion,headSha` rows carry no `status`."""
        policy = checks.IgnorePolicy.from_cfg({"ignore_red_re": "upstream"}, exists=lambda p: False)
        runs = [
            run_row(1, name="ci", conclusion="failure"),
            run_row(2, name="upstream-sync", conclusion="failure"),
        ]
        targets = checks.rerun_targets(runs, policy)
        self.assertEqual([r["databaseId"] for r in targets], [1])
        self.assertEqual([r["name"] for r in targets], ["ci"])

    def test_no_runs_no_targets(self):
        policy = checks.IgnorePolicy.from_cfg({})
        self.assertEqual(checks.rerun_targets([], policy), [])


if __name__ == "__main__":
    unittest.main()
