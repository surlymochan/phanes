"""`openswarm/lib/ci/checks.py`: advisory checks never gate red/green/pending (Refs #1501).

`Check upstream drift` is not a required check, but AD/merge-green treated it as
blocking: a red drift check parked a PR, and a drift check that stayed PENDING
forever kept the PR from ever counting as green. Both lanes burned on a
non-gate. An advisory check now counts for nothing: it is only reported.

The module is loaded by path: `openswarm/lib/ci/` is a namespace package without an
`__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
CHECKS_MODULE = HERE.parent / "lib" / "ci" / "checks.py"

spec = importlib.util.spec_from_file_location("ci_checks_advisory", CHECKS_MODULE)
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)

DRIFT = "Check upstream drift"
LINUX = "OpenSwarm tests (ubuntu-latest, Python 3.11)"


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


def policy_with(advisory=(DRIFT,)):
    return checks.IgnorePolicy.from_cfg({"advisory_checks": list(advisory)})


class AdvisoryPolicyTests(unittest.TestCase):

    def test_names_match_exactly_and_case_insensitively(self):
        policy = policy_with()
        self.assertTrue(policy.is_advisory(DRIFT))
        self.assertTrue(policy.is_advisory("check upstream drift"))
        self.assertFalse(policy.is_advisory("upstream drift"))
        self.assertFalse(policy.is_advisory(LINUX))
        self.assertFalse(policy.is_advisory(""))

    def test_no_advisory_config_means_nothing_is_advisory(self):
        policy = checks.IgnorePolicy.from_cfg({})
        self.assertFalse(policy.is_advisory(DRIFT))
        self.assertFalse(checks.IgnorePolicy.from_cfg(None).is_advisory(DRIFT))

    def test_advisory_file_is_read_one_name_per_line(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-advisory-") as tmp:
            path = pathlib.Path(tmp) / "advisory-checks.txt"
            path.write_text("# advisory checks\n\nCheck upstream drift\n  \n", encoding="utf-8")
            policy = checks.IgnorePolicy.from_cfg({"advisory_file": str(path)})
            self.assertTrue(policy.is_advisory(DRIFT))
            self.assertFalse(policy.is_advisory("# advisory checks"))
            self.assertFalse(policy.is_advisory(""))

    def test_missing_advisory_file_is_empty(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-advisory-") as tmp:
            missing = pathlib.Path(tmp) / "no-such-file.txt"
            policy = checks.IgnorePolicy.from_cfg({"advisory_file": str(missing)})
            self.assertFalse(policy.is_advisory(DRIFT))

    def test_advisory_file_and_list_are_merged(self):
        with tempfile.TemporaryDirectory(prefix="openswarm-advisory-") as tmp:
            path = pathlib.Path(tmp) / "advisory-checks.txt"
            path.write_text("Check upstream drift\n", encoding="utf-8")
            policy = checks.IgnorePolicy.from_cfg(
                {"advisory_checks": [LINUX], "advisory_file": str(path)}
            )
            self.assertTrue(policy.is_advisory(DRIFT))
            self.assertTrue(policy.is_advisory(LINUX))


class AdvisoryClassifyTests(unittest.TestCase):

    def setUp(self):
        self.policy = policy_with()

    def test_red_advisory_with_green_linux_is_green(self):
        result = checks.classify([check_run(DRIFT, "FAILURE"), check_run(LINUX)], self.policy)
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["red"], [])
        self.assertEqual(result["pending"], [])
        self.assertEqual(
            result["advisory"],
            [{"name": DRIFT, "conclusion": "FAILURE"}],
        )

    def test_pending_advisory_with_green_linux_is_green(self):
        result = checks.classify(
            [check_run(DRIFT, conclusion=None, status="IN_PROGRESS"), check_run(LINUX)],
            self.policy,
        )
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["pending"], [])
        self.assertEqual(
            result["advisory"],
            [{"name": DRIFT, "conclusion": "PENDING"}],
        )

    def test_only_advisory_checks_is_none_not_green(self):
        result = checks.classify([check_run(DRIFT, "FAILURE")], self.policy)
        self.assertEqual(result["state"], "none")
        self.assertEqual(result["red"], [])
        self.assertEqual(len(result["advisory"]), 1)

    def test_advisory_pending_alone_is_none(self):
        result = checks.classify(
            [check_run(DRIFT, conclusion=None, status="IN_PROGRESS")], self.policy
        )
        self.assertEqual(result["state"], "none")

    def test_status_context_advisory_is_reported(self):
        result = checks.classify([status_context(DRIFT, "ERROR"), check_run(LINUX)], self.policy)
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["advisory"], [{"name": DRIFT, "conclusion": "ERROR"}])

    def test_non_advisory_red_is_still_red(self):
        result = checks.classify([check_run("unit tests", "FAILURE"), check_run(DRIFT)], self.policy)
        self.assertEqual(result["state"], "red")
        self.assertEqual(result["red"], ["unit tests"])
        self.assertEqual(result["advisory"], [{"name": DRIFT, "conclusion": "SUCCESS"}])

    def test_non_advisory_pending_is_still_pending(self):
        result = checks.classify(
            [check_run("unit tests", conclusion=None, status="IN_PROGRESS"), check_run(LINUX)],
            self.policy,
        )
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["pending"], ["unit tests"])

    def test_empty_checks_is_still_none(self):
        self.assertEqual(checks.classify([], self.policy)["state"], "none")
        self.assertEqual(checks.classify(None, self.policy)["state"], "none")
        self.assertEqual(checks.classify([], self.policy)["advisory"], [])


class AdvisoryRerunTargetsTests(unittest.TestCase):

    def test_advisory_failure_is_not_a_rerun_target(self):
        policy = policy_with()
        runs = [
            run_row(1, name="Phanes components", conclusion="failure"),
            run_row(2, name=DRIFT, conclusion="failure"),
        ]
        targets = checks.rerun_targets(runs, policy)
        self.assertEqual([r["databaseId"] for r in targets], [1])

    def test_advisory_run_with_status_field_is_not_a_rerun_target(self):
        policy = policy_with()
        runs = [
            {"databaseId": 1, "name": "ci", "status": "completed", "conclusion": "failure"},
            {"databaseId": 2, "name": DRIFT, "status": "completed", "conclusion": "failure"},
        ]
        targets = checks.rerun_targets(runs, policy)
        self.assertEqual([r["databaseId"] for r in targets], [1])


if __name__ == "__main__":
    unittest.main()
