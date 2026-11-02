"""Check finding file paths at the JSON and command boundaries."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

if __package__:
    from . import test_cross_check as fixtures
else:
    import test_cross_check as fixtures

cross_check = fixtures.cross_check


class FindingPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = fixtures.temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.inputs = self.repo / "inputs"
        self.inputs.mkdir()
        brief = copy.deepcopy(fixtures.BRIEF)
        del brief["producer"]
        self.brief = fixtures.write_json(self.inputs / "brief.json", brief)
        self.data = fixtures.write_json(self.inputs / "diagnosis.json", fixtures.DIAGNOSIS)
        self.json_out = self.repo / "output" / "review.json"

    def result(self, path) -> cross_check.Result:
        return cross_check.Result(False, "file metadata", "Missing producer metadata", finding={"path": path, "field": "producer"})

    def run_cli(self, brief=None, repo_root=None, cwd=None, markdown_out=None) -> subprocess.CompletedProcess:
        command = [sys.executable, str(fixtures.SCRIPT), "--brief", str(brief or self.brief),
                   "--data", str(self.data), "--json-out", str(self.json_out)]
        if repo_root is not None:
            command.extend(["--repo-root", str(repo_root)])
        if markdown_out is not None:
            command.extend(["--out", str(markdown_out)])
        return subprocess.run(command, cwd=cwd or self.repo, capture_output=True, text=True)

    def test_cli_serializes_absolute_input_as_repo_relative_posix_path(self) -> None:
        result = self.run_cli(repo_root=self.repo)
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stderr)
        payload = json.loads(self.json_out.read_text(encoding="utf-8"))
        self.assertIn("inputs/brief.json", [item["path"] for item in payload["findings"]])
        self.assertFalse(any(item["path"].startswith(str(self.root)) for item in payload["findings"]))

    def test_default_root_is_the_nearest_repo_ancestor_of_nested_cwd(self) -> None:
        nested = self.repo / "nested" / "work"
        nested.mkdir(parents=True)
        result = self.run_cli(cwd=nested)
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stderr)
        payload = json.loads(self.json_out.read_text(encoding="utf-8"))
        self.assertIn("inputs/brief.json", [item["path"] for item in payload["findings"]])

    def test_relative_input_from_nested_cwd_is_resolved_before_serialization(self) -> None:
        result = self.run_cli(brief="brief.json", cwd=self.inputs)
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stderr)
        payload = json.loads(self.json_out.read_text(encoding="utf-8"))
        self.assertIn("inputs/brief.json", [item["path"] for item in payload["findings"]])

    def test_in_repo_symlink_and_dot_component_use_the_canonical_relative_path(self) -> None:
        link = self.repo / "brief-alias.json"
        link.symlink_to(self.brief)
        for path in (link, f"{self.inputs}/./brief.json"):
            with self.subTest(path=path):
                result = self.run_cli(brief=path, repo_root=self.repo)
                self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stderr)
                payload = json.loads(self.json_out.read_text(encoding="utf-8"))
                self.assertIn("inputs/brief.json", [item["path"] for item in payload["findings"]])

    def test_explicit_root_is_used_even_when_cwd_is_outside_that_root(self) -> None:
        result = self.run_cli(repo_root=self.repo, cwd=self.root)
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stderr)
        payload = json.loads(self.json_out.read_text(encoding="utf-8"))
        self.assertIn("inputs/brief.json", [item["path"] for item in payload["findings"]])

    def test_absolute_file_outside_root_is_rejected_without_a_json_report(self) -> None:
        outside = fixtures.write_json(self.root / "outside.json", json.loads(self.brief.read_text(encoding="utf-8")))
        markdown = self.json_out.with_suffix(".md")
        result = self.run_cli(brief=outside, repo_root=self.repo, markdown_out=markdown)
        self.assertEqual(result.returncode, cross_check.EXIT_USAGE, result.stdout + result.stderr)
        self.assertFalse(self.json_out.exists())
        self.assertFalse(markdown.exists())
        self.assertTrue(result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_symlink_to_file_outside_root_is_rejected(self) -> None:
        outside = fixtures.write_json(self.root / "outside.json", json.loads(self.brief.read_text(encoding="utf-8")))
        link = self.inputs / "external.json"
        link.symlink_to(outside)
        markdown = self.json_out.with_suffix(".md")
        result = self.run_cli(brief=link, repo_root=self.repo, markdown_out=markdown)
        self.assertEqual(result.returncode, cross_check.EXIT_USAGE, result.stdout + result.stderr)
        self.assertFalse(self.json_out.exists())
        self.assertFalse(markdown.exists())

    def test_path_traversal_is_rejected_at_serialization(self) -> None:
        for path in ("../outside.json", "inputs/../../outside.json", str(self.repo / ".." / "outside.json")):
            with self.subTest(path=path), self.assertRaises(cross_check.CheckError):
                cross_check.findings_report([self.result(path)], repo_root=self.repo)

    def test_empty_nul_and_foreign_windows_paths_are_rejected(self) -> None:
        paths = ("", "   ", ".", str(self.repo), "inputs/\x00brief.json", "inputs/bad\nname.json",
                 r"C:\repo\brief.json", "C:/repo/brief.json", "C:brief.json",
                 r"\\server\share\brief.json", r"\repo\brief.json")
        for path in paths:
            with self.subTest(path=path), self.assertRaises(cross_check.CheckError):
                cross_check.findings_report([self.result(path)], repo_root=self.repo)

    def test_relative_paths_are_stable_and_result_context_is_not_modified(self) -> None:
        result = self.result(str(self.brief))
        before = copy.deepcopy(result.finding)
        report = cross_check.findings_report([result], repo_root=self.repo)
        self.assertEqual(report[0]["path"], "inputs/brief.json")
        self.assertEqual(result.finding, before)
        self.assertEqual(report[0]["context"]["path"], str(self.brief))
        relative = cross_check.findings_report([self.result("inputs/brief.json")], repo_root=self.repo)
        self.assertEqual(relative[0]["path"], "inputs/brief.json")
        payload = json.loads(cross_check.json_report([result], repo_root=self.repo))
        self.assertEqual(payload["findings"][0]["path"], "inputs/brief.json")

    def test_generated_json_field_pointers_keep_their_released_meaning(self) -> None:
        results = [cross_check.Result(False, "版本一致", "Version mismatch"),
                   cross_check.Result(False, "期间一致", "Period mismatch",
                                      finding={"diagnosis_path": "/metrics/0/数值/2026-09实际"}),
                   cross_check.Result(False, "隐私边界", "Sensitive field: /diagnosis/岗位")]
        paths = [item["path"] for item in cross_check.findings_report(results, repo_root=self.repo)]
        self.assertEqual(paths, ["/schema版本", "/metrics/0/数值/2026-09实际", "/diagnosis/岗位"])


if __name__ == "__main__":
    unittest.main()
