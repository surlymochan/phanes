import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evalid_roles", ROOT / "bin" / "evalid_roles.py")
roles = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = roles
SPEC.loader.exec_module(roles)


class EvalidRoleTests(unittest.TestCase):
    def test_twenty_first_round_tasks_split_evenly(self):
        profiles = [roles.resolve_profile("evalid-tdd", "red", assignment_index=i) for i in range(20)]
        counts = {backend: sum(profile.backend == backend for profile in profiles) for backend in ("pi", "codex")}
        self.assertEqual(counts, {"pi": 10, "codex": 10})
        # Red and green keep the same bucket when they retain the task's assignment index.
        green = roles.resolve_profile("evalid-tdd", "green", assignment_index=8)
        self.assertEqual((green.backend, green.model, green.effort),
                         (profiles[8].backend, profiles[8].model, profiles[8].effort))

    def test_minimax_fallback_changes_only_its_bucket_to_agy(self):
        profile = roles.resolve_profile("evalid-tdd", "green", assignment_index=0,
                                        minimax_available=False, agy_model="approved-flash")
        self.assertEqual((profile.backend, profile.model, profile.effort), ("agy", "approved-flash", None))
        self.assertEqual((profile.planned_backend, profile.planned_model), ("pi", "MiniMax-M3.1-Flash"))
        self.assertEqual(roles.resolve_profile("evalid-tdd", "red", assignment_index=1,
                                               minimax_available=False, agy_model="approved-flash").backend,
                         "codex")
        with self.assertRaises(roles.RoleError):
            roles.resolve_profile("evalid-tdd", "red", assignment_index=0, minimax_available=False)

    def test_spec_and_review_profiles_are_frozen(self):
        self.assertEqual(roles.resolve_profile("evalid-spec", "spec").model, "gpt-6.1-sol")
        self.assertEqual(roles.resolve_profile("evalid-review", "delivery").model, "gpt-6.1-terra")
        with self.assertRaises(roles.RoleError):
            roles.resolve_profile("evalid-review", "delivery", model="gpt-6.1-sol")
        with self.assertRaises(roles.RoleError):
            roles.resolve_profile("evalid-spec", "red")

    def test_quality_repair_is_medium_then_high_then_needs_boss(self):
        self.assertEqual(roles.repair_profile(0).effort, "medium")
        self.assertEqual(roles.repair_profile(1).effort, "high")
        with self.assertRaises(roles.NeedsBoss):
            roles.repair_profile(2)
        with self.assertRaises(roles.RoleError):
            roles.repair_profile(0, effort="high")

    def test_repair_preserves_controller_authorized_red_or_green_stage(self):
        for stage in ("red", "green"):
            for round_, effort in ((0, "medium"), (1, "high")):
                with self.subTest(stage=stage, round=round_):
                    profile = roles.repair_profile(round_, stage=stage)
                    self.assertEqual((profile.stage, profile.effort), (stage, effort))
            with self.assertRaises(roles.NeedsBoss):
                roles.repair_profile(2, stage=stage)
        with self.assertRaises(roles.RoleError):
            roles.repair_profile(0, stage="spec")

    def test_controlled_fields_without_role_never_fall_back_to_legacy(self):
        for field in ("evalid_version: 1", "evalid_version: 2", "change_id: example",
                      'input_evidence: []'):
            for lane in (None, "evalid-tdd"):
                with self.subTest(field=field, lane=lane), self.assertRaises(roles.RoleError):
                    roles.resolve_role(f"---\n{field}\n---\nBuild it", lane)
        self.assertIsNone(roles.resolve_role("---\ntitle: legacy\n---\nBuild it"))
        self.assertEqual(roles.resolve_role("legacy card", "evalid-tdd")["role"], "evalid-tdd")

    def test_card_and_lane_resolution_rejects_conflicts(self):
        digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        card = f"""---\nevalid_version: 1\nrole: evalid-tdd\nchange_id: example-1\ninput_evidence: [\"{digest}\"]\n---\nBuild it\n"""
        result = roles.resolve_role(card, "evalid-tdd")
        self.assertEqual(result["role"], "evalid-tdd")
        self.assertEqual(result["input_evidence"], [digest])
        self.assertIsNone(roles.resolve_role("legacy card"))
        with self.assertRaises(roles.RoleError):
            roles.resolve_role(card, "evalid-review")
        with self.assertRaises(roles.RoleError):
            roles.resolve_role(card.replace("evalid_version: 1", "evalid_version: 2"))
        with self.assertRaises(roles.RoleError):
            roles.resolve_role(card.replace("input_evidence: [\"" + digest + "\"]", "input_evidence: []"))

    def test_mock_cli_receives_profile_and_read_only_review_arguments(self):
        profile = roles.resolve_profile("evalid-review", "delivery")
        command = roles.build_swrm_command("/mock/swrm", task_file="card.md", cwd="repo", base="abc",
                                           verify="pytest -q", profile=profile, python="python3")
        self.assertEqual(command[:3], ["python3", "/mock/swrm", "run"])
        self.assertIn("--backend", command)
        self.assertEqual(command[command.index("--backend") + 1], "codex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-terra")
        self.assertIn("--effort", command)
        self.assertIn("--read-only", command)
        self.assertIn("--no-auto-review", command)

        completed = subprocess.CompletedProcess(command, 0, json.dumps({
            "id": "run-actual-1", "status": "done", "backend": "codex",
            "model": "gpt-6.1-terra", "effort": "high",
        }), "")
        runner = mock.Mock(return_value=completed)
        result = roles.run_swrm(command, profile, runner=runner)
        self.assertEqual(result["id"], "run-actual-1")
        runner.assert_called_once_with(command, capture_output=True, text=True, timeout=None)

    def test_role_evidence_accepts_yaml_flow_string_sequences(self):
        digest = "sha256:" + "a" * 64
        for value in (f"['{digest}']", f"[{digest}]", f"[ '{digest}', ] # parent",
                      f'["{digest}"]', f'["sha256:\\x61' + "a" * 63 + '"]'):
            card = ("---\nevalid_version: 1\nrole: evalid-tdd\nchange_id: example\n"
                    f"input_evidence: {value}\n---\nBuild it")
            with self.subTest(value=value):
                self.assertEqual(roles.resolve_role(card)["input_evidence"], [digest])

    def test_legacy_yaml_flow_lists_preserve_strings_and_dispatch(self):
        card = """---
title: legacy
tags: ['author''s, tag', plain, "line\\nbreak", 'literal\\n', 'hash # tag', 10abc, 'true',]
---
Build it
"""
        self.assertEqual(roles.parse_role_header(card)["tags"],
                         ["author's, tag", "plain", "line\nbreak", "literal\\n", "hash # tag", "10abc", "true"])
        self.assertIsNone(roles.resolve_role(card))
        self.assertEqual(roles.resolve_role(card, "evalid-tdd")["role"], "evalid-tdd")

    def test_legacy_numeric_tags_skip_role_header_parsing(self):
        card = "---\ntitle: legacy\ntags: [1, 2]\n---\nBuild it"
        with mock.patch.object(roles, "_yaml_flow_strings", wraps=roles._yaml_flow_strings) as parse_list:
            self.assertIsNone(roles.resolve_role(card))
            self.assertEqual(roles.resolve_role(card, "evalid-tdd")["role"], "evalid-tdd")
            parse_list.assert_not_called()

    def test_yaml_flow_lists_reject_malformed_or_nonstring_items(self):
        for value in ("['unterminated]", "['one' 'two']", "[one,,two]", "[one", "[one] junk",
                      '["bad\\q"]', "[42]", "[1e2]", "[true]", "[null]", "[{key: value}]", "[[nested]]",
                      "[*alias]", "[!!str tagged]"):
            with self.subTest(value=value), self.assertRaises(roles.RoleError):
                roles.parse_role_header(f"---\ntags: {value}\n---\nBuild it")

    def test_subprocess_result_must_be_terminal_and_match_profile(self):
        profile = roles.resolve_profile("evalid-spec", "spec")
        command = ["mock"]
        for status in ("queued", "accepted", "running", "verifying", "unknown", None):
            result = {"id": "run-1", "status": status, "backend": profile.backend,
                      "model": profile.model, "effort": profile.effort}
            with self.subTest(status=status), self.assertRaises(roles.RoleError):
                roles.run_swrm(command, profile, runner=mock.Mock(return_value=
                    subprocess.CompletedProcess(command, 0, json.dumps(result), "")))
        with self.assertRaises(roles.RoleError):
            roles.run_swrm(command, profile, runner=lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, '{"id":"run-1","status":"done","backend":"codex","model":"wrong","effort":"high"}', ""))

    def test_nonzero_swrm_terminal_results_preserve_run_and_failure_evidence(self):
        profile = roles.resolve_profile("evalid-spec", "spec")
        for status, rc in (("partial", 1), ("failed", 1), ("stopped", 130), ("budget_exceeded", 124)):
            result = {"id": "actual-run", "status": status, "backend": profile.backend,
                      "model": profile.model, "effort": profile.effort,
                      "verify": {"rc": 1}, "summary": "stage failed", "artifacts": ["proof.json"],
                      "error_class": status, "result": None,
                      "attempts": [{"rc": rc, "error_class": status, "error_tail": "diagnostic"}]}
            # Real subprocess exit semantics, without invoking swrm or a model service.
            for returncode in (0, rc):
                command = [sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[2]))",
                           json.dumps(result), str(returncode)]
                with self.subTest(status=status, rc=returncode):
                    self.assertEqual(roles.run_swrm(command, profile), result)
        for body in ('{}', 'not JSON', json.dumps({**result, "model": "wrong"}),
                     json.dumps({**result, "status": "done"})):
            with self.subTest(body=body), self.assertRaises(roles.RoleError):
                roles.run_swrm([sys.executable, "-c",
                                "import sys; print(sys.argv[1]); sys.exit(1)", body], profile)

    def test_unroutable_swrm_preserves_dispatch_failure_without_claiming_execution(self):
        profile = roles.resolve_profile("evalid-spec", "spec")
        result = {"id": "actual-run", "status": "unroutable",
                  "decision": {"ok": False, "reasons": ["no backend had headroom"]}}
        command = [sys.executable, "-c", "import sys; print(sys.argv[1]); sys.exit(2)", json.dumps(result)]
        self.assertEqual(roles.run_swrm(command, profile), result)

    def test_evalid_subprocess_passes_request_and_preserves_all_stage_return_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request = root / "controlled request.json"
            request.write_text(json.dumps({"schema_version": 1, "stage": "red"}))
            cli = root / "mock-xflow"
            cli.write_text(f"#!{sys.executable}\n" + """import json, pathlib, sys
assert sys.argv[1:4] == ['role', 'run', '--request']
assert sys.argv[5] == '--output'
request = json.loads(pathlib.Path(sys.argv[4]).read_text())
pathlib.Path(sys.argv[6]).write_text(json.dumps({'payload': request, 'kind': 'tdd'}))
sys.exit(request['rc'])
""")
            cli.chmod(0o755)
            for rc in range(4):
                with self.subTest(rc=rc):
                    controlled = {"schema_version": 1, "stage": "red", "rc": rc}
                    request.write_text(json.dumps(controlled))
                    output = root / f"result-{rc}.json"
                    command = roles.build_evalid_command(str(cli), request_file=str(request),
                                                         output_file=str(output))
                    result = roles.run_evalid(command)
                    self.assertEqual(result["rc"], rc)
                    self.assertEqual(result["artifact"], {"payload": controlled, "kind": "tdd"})
                    self.assertEqual(json.loads(request.read_text()), controlled)
                    with self.assertRaises(roles.RoleError):
                        roles.run_evalid(command)

    def test_evalid_rejects_missing_success_output_invalid_json_and_unknown_rc(self):
        with tempfile.TemporaryDirectory() as directory:
            request = str(pathlib.Path(directory) / "request.json")
            output = pathlib.Path(directory) / "result.json"
            command = roles.build_evalid_command("xflow", request_file=request, output_file=str(output))
            for rc in (1, 2, 3):
                result = roles.run_evalid(command, runner=mock.Mock(return_value=
                    subprocess.CompletedProcess(command, rc, "", "stage diagnostic")))
                self.assertEqual((result["rc"], result["artifact"], result["stderr"]),
                                 (rc, None, "stage diagnostic"))
            for rc in (0, 4, -9):
                with self.subTest(rc=rc), self.assertRaises(roles.RoleError):
                    roles.run_evalid(command, runner=mock.Mock(return_value=
                        subprocess.CompletedProcess(command, rc, "", "")))
            def invalid_output(*args, **kwargs):
                output.write_text("not JSON")
                return subprocess.CompletedProcess(command, 0, "", "")
            with self.assertRaises(roles.RoleError):
                roles.run_evalid(command, runner=invalid_output)
            with self.assertRaises(roles.RoleError):
                roles.build_evalid_command("xflow", request_file=request, output_file=request)


if __name__ == "__main__":
    unittest.main()
