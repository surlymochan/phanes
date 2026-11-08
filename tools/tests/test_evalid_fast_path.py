"""Contract tests for the #1976 deterministic simple-Issue fast-path gate.

The batch baseline has tools/evalid_roles_control.py but no evalid_fast_path
module. These tests make that missing product capability an explicit assertion,
not an ImportError or an environmental failure. The gate now lives in
tools/evalid_fast_path.py and the same tests exercise it against the
acceptance matrix below.

Public test seam (there is no pre-existing fast-path API to preserve):
    evaluate(intake: dict, *, enabled: bool = False) -> dict
The result has eligible: bool and approved_spec: dict | None. An approved receipt
contains intake_digest, spec (public requirements), and spec_digest = SHA256 of
canonical JSON for spec. This uses the existing controller's digest convention.
The receipt must bind the frozen intake including base/policy/issue, not just an
Issue's text length or a human 'simple' label. Build/Reviewer integration remains
implementation work, not something these unit fixtures claim to verify.

Mapping: default/config-off -> test_default_off/test_explicit_off;
complete narrow intake -> test_complete_intake_has_content_addressed_receipt;
ambiguous words, unknown fields, absent boundaries, multiple commands and broad
scope -> negative tests. Commands below are opaque fixture data, never executed.
Run: python3 -m unittest tools.tests.test_evalid_fast_path
Expected on bbcc2778: assertion failures stating the gate is not implemented.
"""

import copy
import importlib
import importlib.util
import json
import unittest
from unittest import mock

from tools.evalid_roles_control import digest


class FastPathContractTests(unittest.TestCase):
    def setUp(self):
        self.intake = {
            "issue_ref": "issue:1976-example",
            "base_commit": "a" * 40,
            "policy_digest": "sha256:" + "b" * 64,
            "public_scope": {
                "allowed_files": ["tools/example.py", "tools/tests/test_example.py"],
                "requirements": "Return the exact decimal sum of two integers.",
                "acceptance_commands": [["python3", "-m", "unittest", "tools.tests.test_example"]],
                "expected_exit_code": 0,
                "expected_output": {"stream": "stdout", "match": "exact", "value": "3\n"},
                "boundaries": [
                    {"input": [0, 0], "exit_code": 0, "output": "0\n"},
                    {"input": [-1, 1], "exit_code": 0, "output": "0\n"},
                    {"input": ["x", 1], "exit_code": 2, "output": "invalid integer\n"},
                ],
            },
        }

    def evaluate(self, intake=None, **config):
        name = "tools.evalid_fast_path"
        self.assertIsNotNone(importlib.util.find_spec(name),
                             "#1976 deterministic fast-path gate is not implemented")
        module = importlib.import_module(name)
        self.assertTrue(callable(getattr(module, "evaluate", None)),
                        "fast-path module must expose the documented evaluate seam")
        argument = copy.deepcopy(self.intake if intake is None else intake)
        snapshot = copy.deepcopy(argument)
        # Eligibility evaluates metadata; it must not execute intake commands.
        with mock.patch("subprocess.run", side_effect=AssertionError("gate executed a command")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("gate launched a process")):
            result = module.evaluate(argument, **config)
        self.assertEqual(argument, snapshot, "gate must not rewrite frozen intake")
        self.assertIsInstance(result, dict)
        self.assertIs(type(result.get("eligible")), bool)
        self.assertIn("approved_spec", result)
        return result

    def assert_closed(self, intake=None, **config):
        result = self.evaluate(intake, **config)
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["approved_spec"], "rejected inputs cannot mint approved receipts")

    def test_default_off(self):
        self.assert_closed()

    def test_explicit_off(self):
        self.assert_closed(enabled=False)

    def test_non_boolean_opt_in_fails_closed(self):
        for value in (None, "true", "false", 1, [], {}):
            with self.subTest(enabled=value):
                self.assert_closed(enabled=value)

    def test_complete_intake_has_content_addressed_receipt(self):
        result = self.evaluate(enabled=True)
        self.assertTrue(result["eligible"])
        receipt = result["approved_spec"]
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt["intake_digest"], digest(self.intake))
        self.assertEqual(receipt["spec_digest"], digest(receipt["spec"]))
        spec = receipt["spec"]
        for field in ("allowed_files", "requirements", "acceptance_commands",
                      "expected_exit_code", "expected_output", "boundaries"):
            self.assertEqual(spec[field], self.intake["public_scope"][field])

    def test_ambiguous_requirements_do_not_become_approved_specs(self):
        for text in ("Return a reasonable result.", "Improve performance as appropriate.",
                     "尽量返回合适的结果", "输出格式待定", "TBD", "unknown"):
            with self.subTest(requirement=text):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["requirements"] = text
                self.assert_closed(intake, enabled=True)

    def test_each_missing_or_unknown_required_field_fails_closed(self):
        for field in self.intake["public_scope"]:
            for value in (None, "unknown", "TBD", ""):
                with self.subTest(field=field, value=value):
                    intake = copy.deepcopy(self.intake)
                    intake["public_scope"][field] = value
                    self.assert_closed(intake, enabled=True)
            with self.subTest(missing=field):
                intake = copy.deepcopy(self.intake)
                del intake["public_scope"][field]
                self.assert_closed(intake, enabled=True)

    def test_requirements_must_describe_observable_behavior(self):
        for text in ("Fix the bug.", "Fix.", "Update the implementation.",
                     "Improve performance.", "Return a result.",
                     "Return the expected value.", "Print the output.",
                     "Accept any input.", "Return a result depending on input.",
                     "Return a result based on input.",
                     "Return the expected value for the input."):
            with self.subTest(requirements=text):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["requirements"] = text
                self.assert_closed(intake, enabled=True)

    def test_unspecified_branch_conditions_fail_closed(self):
        for text in ("Return 3 or 4 depending on input.",
                     "Return 3 or 4 based on input.",
                     "Return either 3 or 4.",
                     "Return 3 when appropriate and 4 otherwise.",
                     "Print success or failure according to the input.",
                     "Return the exact decimal sum of two integers or 4.",
                     "Return 3. Choose another value for other inputs."):
            with self.subTest(requirements=text):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["requirements"] = text
                self.assert_closed(intake, enabled=True)

    def test_supported_unconditional_behaviors_remain_eligible(self):
        for text, output in (("Return 3.", "3\n"),
                             ('Print "success".', "success\n")):
            with self.subTest(requirements=text):
                intake = copy.deepcopy(self.intake)
                scope = intake["public_scope"]
                scope["requirements"] = text
                scope["expected_output"]["value"] = output
                scope["boundaries"] = [
                    {"input": value, "exit_code": 0, "output": output}
                    for value in ([], [0, 0], ["x", 1])]
                self.assertTrue(self.evaluate(intake, enabled=True)["eligible"])

    def test_contradictory_unconditional_behaviors_fail_closed(self):
        for requirement, expected, boundary in (
                ("Return 3.", "4\n", "5\n"),
                ("Return 3.", "4\n", "3\n"),
                ("Return 3.", "3\n", "5\n"),
                ('Print "success".', "success\n", "failure\n")):
            with self.subTest(requirement=requirement, expected=expected, boundary=boundary):
                intake = copy.deepcopy(self.intake)
                scope = intake["public_scope"]
                scope["requirements"] = requirement
                scope["expected_output"]["value"] = expected
                scope["boundaries"] = [
                    {"input": [], "exit_code": 0, "output": boundary}]
                self.assert_closed(intake, enabled=True)

    def test_unconditional_output_matchers_are_checked_against_behavior(self):
        for match, matching, conflicting in (
                ("exact", "success\n", "failure\n"),
                ("contains", "success", "failure"),
                ("prefix", "suc", "fail"),
                ("suffix", "cess\n", "ure\n"),
                ("regex", r"^success\n$", r"^failure\n$")):
            for value, eligible in ((matching, True), (conflicting, False)):
                with self.subTest(match=match, value=value):
                    intake = copy.deepcopy(self.intake)
                    scope = intake["public_scope"]
                    scope["requirements"] = 'Print "success".'
                    scope["expected_output"].update(match=match, value=value)
                    scope["boundaries"] = [
                        {"input": [], "exit_code": 0, "output": "success\n"}]
                    result = self.evaluate(intake, enabled=True)
                    self.assertEqual(result["eligible"], eligible)
                    if not eligible:
                        self.assertIsNone(result["approved_spec"])

    def test_unconditional_boundaries_must_have_a_single_outcome(self):
        for field, conflicting in (("output", "3"), ("exit_code", 2)):
            with self.subTest(field=field):
                intake = copy.deepcopy(self.intake)
                scope = intake["public_scope"]
                scope["requirements"] = "Return 3."
                scope["boundaries"] = [
                    {"input": [], "exit_code": 0, "output": "3\n"},
                    {"input": [1], "exit_code": 0, "output": "3\n"}]
                scope["boundaries"][1][field] = conflicting
                self.assert_closed(intake, enabled=True)

    def test_arithmetic_boundaries_agree_with_the_requirement(self):
        for operation, output in (("sum", "5\n"), ("difference", "1\n"),
                                  ("product", "6\n")):
            for boundary_output, eligible in ((output, True), ("99\n", False)):
                with self.subTest(operation=operation, output=boundary_output):
                    intake = copy.deepcopy(self.intake)
                    scope = intake["public_scope"]
                    scope["requirements"] = (
                        "Return the exact decimal " + operation + " of two integers.")
                    scope["boundaries"] = [
                        {"input": [3, 2], "exit_code": 0, "output": boundary_output}]
                    result = self.evaluate(intake, enabled=True)
                    self.assertEqual(result["eligible"], eligible)
                    if not eligible:
                        self.assertIsNone(result["approved_spec"])

    def test_identical_boundary_inputs_cannot_have_conflicting_outcomes(self):
        for field, conflicting in (("output", "another error\n"), ("exit_code", 3)):
            with self.subTest(field=field):
                intake = copy.deepcopy(self.intake)
                boundaries = intake["public_scope"]["boundaries"]
                duplicate = copy.deepcopy(boundaries[-1])
                duplicate[field] = conflicting
                boundaries.append(duplicate)
                self.assert_closed(intake, enabled=True)

    def test_unrecognized_public_contract_fields_fail_closed(self):
        # Unsupported requirements must go to Spec even when their values look
        # concrete; otherwise the receipt silently drops part of the contract.
        for location in ("intake", "scope", "output", "boundary"):
            for value in ("unknown", None, "TBD", 30):
                with self.subTest(location=location, value=value):
                    intake = copy.deepcopy(self.intake)
                    scope = intake["public_scope"]
                    target = {"intake": intake, "scope": scope,
                              "output": scope["expected_output"],
                              "boundary": scope["boundaries"][0]}[location]
                    target["timeout"] = value
                    self.assert_closed(intake, enabled=True)

    def test_invalid_output_regex_fails_closed(self):
        for pattern in ("[", "(", "(?P<name>a)(?P<name>b)", "a{99999999999999999999}"):
            with self.subTest(pattern=pattern):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["expected_output"] = {
                    "stream": "stdout", "match": "regex", "value": pattern}
                self.assert_closed(intake, enabled=True)

    def test_valid_output_regex_is_explicit_evidence(self):
        intake = copy.deepcopy(self.intake)
        intake["public_scope"]["expected_output"] = {
            "stream": "stdout", "match": "regex", "value": r"^-?\d+\n$"}
        result = self.evaluate(intake, enabled=True)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["approved_spec"]["spec"]["expected_output"],
                         intake["public_scope"]["expected_output"])

    def test_unknown_command_arguments_fail_closed(self):
        for command in (["TBD"], ["unknown"], ["python3", "TBD"],
                        ["python3", None]):
            with self.subTest(command=command):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["acceptance_commands"] = [command]
                self.assert_closed(intake, enabled=True)

    def test_unknown_boundary_values_fail_closed(self):
        for field, value in (("input", None), ("input", "TBD"),
                             ("input", [0, None]), ("input", {"x": "unknown"}),
                             ("output", "TBD"), ("output", "unknown")):
            with self.subTest(field=field, value=value):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["boundaries"][0][field] = value
                self.assert_closed(intake, enabled=True)

    def test_concrete_empty_boundary_values_and_command_flags_are_valid(self):
        intake = copy.deepcopy(self.intake)
        intake["public_scope"]["acceptance_commands"][0].append("--")
        for value in ("", [], {}, 0, False):
            with self.subTest(input=value):
                intake["public_scope"]["boundaries"][0] = {
                    "input": value, "exit_code": 0, "output": ""}
                self.assertTrue(self.evaluate(intake, enabled=True)["eligible"])

    def test_missing_or_unknown_intake_identity_fails_closed(self):
        for field in ("issue_ref", "base_commit", "policy_digest"):
            for value in (None, "", "unknown", "TBD"):
                with self.subTest(field=field, value=value):
                    intake = copy.deepcopy(self.intake)
                    intake[field] = value
                    self.assert_closed(intake, enabled=True)
            with self.subTest(missing=field):
                intake = copy.deepcopy(self.intake)
                del intake[field]
                self.assert_closed(intake, enabled=True)

    def test_missing_and_empty_boundaries_fail_closed(self):
        for value in ([], [{}], [{"input": [0, 0]}]):
            with self.subTest(boundaries=value):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["boundaries"] = value
                self.assert_closed(intake, enabled=True)

    def test_multiple_or_zero_acceptance_commands_fail_closed(self):
        command = self.intake["public_scope"]["acceptance_commands"][0]
        for commands in ([], [command, command], [command, ["python3", "other.py"]]):
            with self.subTest(commands=commands):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["acceptance_commands"] = commands
                self.assert_closed(intake, enabled=True)

    def test_wide_or_unknown_file_scope_fails_closed(self):
        for paths in ([], ["**"], ["tools/**"], ["../outside.py"], ["/etc/config"],
                      [None], ["."], ["./"], ["tools/.."], ["unknown"], ["TBD"]):
            with self.subTest(paths=paths):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["allowed_files"] = paths
                self.assert_closed(intake, enabled=True)

    def test_existing_directories_are_not_narrow_file_scopes(self):
        for path in ("tools", "./tools", "tools/tests", "tools/../tools"):
            with self.subTest(path=path):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["allowed_files"] = [path]
                self.assert_closed(intake, enabled=True)

    def test_explicit_existing_and_new_files_are_narrow(self):
        intake = copy.deepcopy(self.intake)
        intake["public_scope"]["allowed_files"] = [
            "tools/evalid_fast_path.py", "tools/new_fast_path_module.py"]
        self.assertTrue(self.evaluate(intake, enabled=True)["eligible"])

    def test_literal_file_scope_has_a_small_fixed_limit(self):
        # The conservative fast-path policy permits at most four exact files.
        # This limit depends only on frozen intake, not the checkout's size.
        for count, eligible in ((1, True), (4, True), (5, False), (1000, False)):
            with self.subTest(count=count):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["allowed_files"] = [
                    f"src/file{i}.py" for i in range(count)]
                result = self.evaluate(intake, enabled=True)
                self.assertEqual(result["eligible"], eligible)
                if not eligible:
                    self.assertIsNone(result["approved_spec"])

    def test_scope_verdict_and_receipt_ignore_workspace_path_types(self):
        # allowed_files is a set of exact file names, not directory prefixes.
        # The frozen intake must not acquire facts from a mutable checkout.
        for path, eligible in (("tools/new_fast_path_module.py", True),
                               ("tools/new_scope", False), ("tools", False)):
            with self.subTest(path=path):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["allowed_files"] = [path]
                verdicts = []
                for is_directory in (False, True):
                    with mock.patch("pathlib.Path.is_dir", return_value=is_directory) as probe:
                        result = self.evaluate(intake, enabled=True)
                    verdicts.append(result)
                    self.assertEqual(result["eligible"], eligible)
                    probe.assert_not_called()
                self.assertEqual(verdicts[0], verdicts[1])

    def test_ambiguous_or_noncanonical_file_names_fail_closed(self):
        for path in ("tools/new_scope", "tools/new_scope/", "./tools/example.py",
                     "tools//example.py", "tools/example.py ",
                     "tools/example.py\x00", "tools\\example.py"):
            with self.subTest(path=path):
                intake = copy.deepcopy(self.intake)
                intake["public_scope"]["allowed_files"] = [path]
                self.assert_closed(intake, enabled=True)

    def test_label_and_short_text_do_not_override_missing_evidence(self):
        intake = copy.deepcopy(self.intake)
        intake["labels"] = ["simple", "fast-path"]
        intake["public_scope"]["requirements"] = "Fix."
        del intake["public_scope"]["boundaries"]
        self.assert_closed(intake, enabled=True)

    def test_labels_are_not_evidence_and_not_a_veto(self):
        labelled = copy.deepcopy(self.intake)
        labelled["labels"] = ["simple", "fast-path"]
        result = self.evaluate(labelled, enabled=True)
        self.assertTrue(result["eligible"],
                        "a human label must neither replace nor block the evidence")
        self.assertEqual(result["approved_spec"]["intake_digest"], digest(labelled))

    def test_receipt_publishes_only_the_issue_reference_digest(self):
        receipt = self.evaluate(enabled=True)["approved_spec"]
        self.assertEqual(receipt["issue_ref_digest"], digest(self.intake["issue_ref"]))
        published = json.dumps(receipt, sort_keys=True)
        self.assertNotIn(self.intake["issue_ref"], published,
                         "the raw issue reference stays out of the receipt")

    def test_receipt_is_stable_for_equivalent_key_order(self):
        reordered = dict(reversed(list(self.intake.items())))
        reordered["public_scope"] = dict(reversed(list(self.intake["public_scope"].items())))
        first = self.evaluate(enabled=True)
        second = self.evaluate(reordered, enabled=True)
        self.assertTrue(first["eligible"])
        self.assertEqual(first, second)

    def test_receipt_binds_base_policy_issue_and_changed_public_contract(self):
        first = self.evaluate(enabled=True)
        self.assertTrue(first["eligible"])
        for key, replacement in (("base_commit", "c" * 40),
                                 ("policy_digest", "sha256:" + "d" * 64),
                                 ("issue_ref", "issue:1976-other")):
            with self.subTest(field=key):
                intake = copy.deepcopy(self.intake)
                intake[key] = replacement
                receipt = self.evaluate(intake, enabled=True)["approved_spec"]
                self.assertEqual(receipt["intake_digest"], digest(intake))
                self.assertNotEqual(receipt["intake_digest"], first["approved_spec"]["intake_digest"])
        intake = copy.deepcopy(self.intake)
        intake["public_scope"]["expected_output"]["value"] = "4\n"
        receipt = self.evaluate(intake, enabled=True)["approved_spec"]
        self.assertNotEqual(receipt["spec_digest"], first["approved_spec"]["spec_digest"])


if __name__ == "__main__":
    unittest.main()
