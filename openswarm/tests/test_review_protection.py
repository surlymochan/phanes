"""Offline protection checks and safe command generation, loaded by file path."""

import copy
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "review" / "protection.py"
spec = importlib.util.spec_from_file_location("review_protection", SCRIPT)
protection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protection)


def protected():
    return {"required_status_checks": {"strict": True, "contexts": ["ci", "lane-review"]},
            "enforce_admins": {"enabled": True}}


def command_payload(command):
    return json.loads(command.split("\n", 1)[1].rsplit("\n", 1)[0])


class ProtectionTests(unittest.TestCase):
    def codes(self, value, context="lane-review"):
        findings = protection.check_protection(value, context)
        for finding in findings:
            self.assertEqual(set(finding), {"level", "code", "message"})
            self.assertIn(finding["level"], ("ok", "warn"))
        return [finding["code"] for finding in findings]

    def test_unprotected(self):
        self.assertEqual(self.codes(None), ["unprotected"])

    def test_missing_context_in_legacy_and_modern_formats(self):
        for status in ({"contexts": ["ci"]}, {"checks": [{"context": "ci", "app_id": 4}]}, None):
            with self.subTest(status=status):
                self.assertEqual(self.codes({"required_status_checks": status,
                                             "enforce_admins": {"enabled": True}}), ["missing_context"])

    def test_context_found_in_either_format(self):
        for status in ({"contexts": ["lane-review"]},
                       {"contexts": ["ci"], "checks": [{"context": "lane-review"}]}):
            with self.subTest(status=status):
                self.assertEqual(self.codes({"required_status_checks": status,
                                             "enforce_admins": {"enabled": True}}), ["ok"])

    def test_admins_not_enforced(self):
        for admins in (None, False, {"enabled": False}):
            with self.subTest(admins=admins):
                value = protected()
                value["enforce_admins"] = admins
                self.assertEqual(self.codes(value), ["admins_not_enforced"])

    def test_reports_both_independent_warnings(self):
        self.assertEqual(self.codes({}), ["missing_context", "admins_not_enforced"])

    def test_all_requirements_and_custom_context(self):
        self.assertEqual(self.codes(protected()), ["ok"])
        value = {"required_status_checks": {"contexts": ["custom-review"]}, "enforce_admins": True}
        self.assertEqual(self.codes(value, "custom-review"), ["ok"])

    def test_fix_preserves_contexts_app_bindings_and_input(self):
        value = protected()
        value["required_status_checks"]["checks"] = [{"context": "ci", "app_id": 123},
                                                       {"context": "other", "app_id": None}]
        before = copy.deepcopy(value)
        payload = command_payload(protection.fix_command("owner/repo", "main", value))
        self.assertEqual(value, before)
        self.assertEqual(payload["required_status_checks"], {
            "strict": True, "contexts": ["ci", "lane-review"],
            "checks": [{"context": "ci", "app_id": 123}, {"context": "other"}],
        })
        self.assertIs(payload["enforce_admins"], True)

    def test_fix_for_unprotected_branch_and_custom_context(self):
        command = protection.fix_command("owner/repo", "main", None, "custom-review")
        self.assertTrue(command.startswith("gh api -X PUT repos/owner/repo/branches/main/protection --input -"))
        self.assertEqual(command_payload(command), {
            "required_status_checks": {"strict": False, "contexts": ["custom-review"]},
            "enforce_admins": True, "required_pull_request_reviews": None, "restrictions": None,
        })

    def test_fix_adds_missing_context_without_losing_existing_checks(self):
        value = protected()
        value["required_status_checks"]["contexts"] = ["ci", "lint"]
        payload = command_payload(protection.fix_command("owner/repo", "main", value))
        self.assertEqual(payload["required_status_checks"]["contexts"], ["ci", "lint", "lane-review"])
        self.assertEqual(self.codes(payload), ["ok"])

    def test_fix_preserves_review_restrictions_and_other_settings(self):
        actors = {"url": "read-only", "users": [{"login": "alice", "id": 2}],
                  "teams": [{"slug": "reviewers"}], "apps": [{"slug": "review-bot"}]}
        value = protected()
        value.update({"url": "read-only", "restrictions": actors,
                      "required_pull_request_reviews": {
                          "url": "read-only", "dismiss_stale_reviews": True,
                          "required_approving_review_count": 2, "require_code_owner_reviews": True,
                          "require_last_push_approval": True, "dismissal_restrictions": actors,
                          "bypass_pull_request_allowances": actors},
                      "required_linear_history": {"enabled": True},
                      "allow_force_pushes": {"enabled": False}, "allow_deletions": False,
                      "required_conversation_resolution": {"enabled": True}, "lock_branch": {"enabled": False}})
        before = copy.deepcopy(value)
        payload = command_payload(protection.fix_command("owner/repo", "main", value))
        expected = {"users": ["alice"], "teams": ["reviewers"], "apps": ["review-bot"]}
        self.assertEqual(payload["restrictions"], expected)
        self.assertEqual(payload["required_pull_request_reviews"]["dismissal_restrictions"], expected)
        self.assertEqual(payload["required_pull_request_reviews"]["bypass_pull_request_allowances"], expected)
        self.assertEqual(payload["required_pull_request_reviews"]["required_approving_review_count"], 2)
        self.assertNotIn("url", payload)
        self.assertNotIn("url", payload["required_pull_request_reviews"])
        self.assertIs(payload["required_linear_history"], True)
        self.assertIs(payload["required_conversation_resolution"], True)
        self.assertIs(payload["allow_force_pushes"], False)
        self.assertEqual(value, before)

    def test_command_quotes_branch_and_context_as_data(self):
        branch = "release/$(touch SENTINEL);'"
        context = "review\nOPENSWARM_PROTECTION_JSON\n$(touch SENTINEL)"
        command = protection.fix_command("owner/repo", branch, None, context)
        endpoint = shlex.split(command.splitlines()[0])[4]
        self.assertIn("release%2F%24%28touch%20SENTINEL%29%3B%27", endpoint)
        self.assertEqual(command_payload(command)["required_status_checks"]["contexts"], [context])
        self.assertEqual(command.splitlines().count("OPENSWARM_PROTECTION_JSON"), 1)
        # A shell function replaces gh: validate the here-document without any API call.
        result = subprocess.run(["sh", "-c", "gh() { cat; }\n" + command],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), command_payload(command))
        self.assertEqual(result.stderr, "")


class CliTests(unittest.TestCase):
    def run_cli(self, value, stdin=False, *options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protection.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), "--json", "-" if stdin else str(path),
                                     *options], input=json.dumps(value) if stdin else None,
                                    text=True, capture_output=True, check=False)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), ["protection.json"])
            return result

    def test_stdin_null_exits_one_and_prints_fix(self):
        result = self.run_cli(None, True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("warn: unprotected", result.stdout)
        self.assertIn("gh api -X PUT repos/OWNER/REPO/branches/main/protection --input -", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_file_healthy_exits_zero_without_fix(self):
        result = self.run_cli(protected())
        self.assertEqual(result.returncode, 0)
        self.assertIn("ok: ok", result.stdout)
        self.assertNotIn("gh api", result.stdout)

    def test_cli_context_and_target_options(self):
        result = self.run_cli(protected(), False, "--context", "custom", "--repo", "org/project", "--branch", "release/v1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing_context", result.stdout)
        self.assertIn("repos/org/project/branches/release%2Fv1/protection", result.stdout)
        self.assertIn('"custom"', result.stdout)

    def test_invalid_shape_exits_two(self):
        result = self.run_cli([])
        self.assertEqual(result.returncode, 2)
        self.assertIn("JSON object or null", result.stderr)


if __name__ == "__main__":
    unittest.main()
