"""Best-of-two runs use stub executors and reviewers with local fixtures only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()


class CompareScoreUnitTests(unittest.TestCase):
    """Exercise compare scoring and ties with in-memory candidate records."""

    @staticmethod
    def candidate(verify_rc=0, findings=None, changed_lines=0, seconds=1.0):
        candidate = {"verify_rc": verify_rc, "seconds": seconds,
                     "diff": {"available": True, "changed_lines": changed_lines},
                     "review": {"status": "reviewed", "findings": findings or []}}
        candidate["score"] = xs.compare_candidate_score(candidate)
        return candidate

    def test_candidate_score_applies_verification_and_p0_p1_counts(self):
        findings = ([{"severity": "P0"}] * 2 + [{"severity": "P1"}] * 3
                     + [{"severity": "P2"}] * 9)
        self.assertEqual(
            xs.compare_candidate_score(self.candidate(verify_rc=1, findings=findings,
                                                       changed_lines=17, seconds=4.5)),
            {"verify_pass": False, "p0p1": 5, "diff_size": 17, "seconds": 4.5})
        self.assertTrue(xs.compare_candidate_score(self.candidate(verify_rc=0))["verify_pass"])

    def test_diff_size_and_seconds_are_ascending_penalties(self):
        clean = self.candidate()
        larger_diff = self.candidate(changed_lines=20)
        slower = self.candidate(seconds=2.5)
        self.assertEqual(xs.compare_order(clean, larger_diff), 1)
        self.assertEqual(xs.compare_order(clean, slower), 1)
        self.assertEqual(xs.compare_order(self.candidate(seconds=1.25),
                                          self.candidate(seconds=1.5)), 1)

    def test_same_score_for_different_candidates_is_a_tie(self):
        left = self.candidate(changed_lines=8, seconds=3.25)
        right = self.candidate(changed_lines=8, seconds=3.25)
        left["label"], right["label"] = "A", "B"
        left["score"], right["score"] = (xs.compare_candidate_score(left),
                                           xs.compare_candidate_score(right))
        self.assertIsNone(xs.choose_compare_winner(left, right))

    def test_missing_optional_score_field_uses_remaining_criteria(self):
        missing = self.candidate(changed_lines=8, seconds=1.0)
        complete = self.candidate(changed_lines=12, seconds=2.0)
        del missing["review"]["findings"]
        del missing["score"]["diff_size"]
        self.assertEqual(xs.choose_compare_winner(missing, complete), missing)

        del complete["score"]["diff_size"]
        missing["score"]["seconds"] = complete["score"]["seconds"]
        self.assertIsNone(xs.choose_compare_winner(missing, complete))

    def test_empty_review_findings_are_missing_review_score(self):
        candidate = self.candidate()
        candidate["review"] = {}
        self.assertEqual(xs.compare_candidate_score(candidate),
                         {"verify_pass": True, "p0p1": None, "diff_size": 0, "seconds": 1.0})

    def test_all_candidates_without_review_can_win_or_tie(self):
        left = self.candidate(changed_lines=4, seconds=2.0)
        right = self.candidate(changed_lines=8, seconds=1.0)
        left["review"] = right["review"] = {}
        left["score"] = xs.compare_candidate_score(left)
        right["score"] = xs.compare_candidate_score(right)
        self.assertIs(xs.choose_compare_winner(left, right), left)

        right["diff"]["changed_lines"] = 4
        right["seconds"] = 2.0
        right["score"] = xs.compare_candidate_score(right)
        self.assertIsNone(xs.choose_compare_winner(left, right))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-compare-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.runs = self.data / "runs"
        self.knowledge = self.root / "knowledge"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "RUNS")}
        xs.DATA, xs.LEDGER, xs.RUNS = str(self.data), str(self.data / "ledger.jsonl"), str(self.runs)
        self.git("init", "-q")
        self.git("config", "user.name", "Compare Test")
        self.git("config", "user.email", "compare@example.invalid")
        (self.repo / "module.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "module.py")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.task = self.root / "task.md"
        self.task.write_text("Implement the same task.\n", encoding="utf-8")
        self.policy = {"run": {"timeout_seconds": 120}, "budget": {"timezone": "UTC"},
                       "knowledge": {"repo": str(self.knowledge)}}

    def tearDown(self):
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout

    def candidate(self, label, verify, p0p1, diff_size, seconds):
        findings = [{"severity": "P1", "path": "x.py", "line": 1, "title": f"finding {index}", "body": "issue"}
                    for index in range(p0p1)]
        candidate = {"label": label, "verify_rc": 0 if verify else 1, "seconds": seconds,
                     "diff": {"available": True, "changed_lines": diff_size},
                     "review": {"status": "reviewed", "findings": findings}}
        candidate["score"] = xs.compare_candidate_score(candidate)
        return candidate

    def test_winner_selection_follows_verify_review_diff_and_time_order(self):
        # Verification wins over findings, diff size, and elapsed time.
        self.assertEqual(xs.choose_compare_winner(self.candidate("A", True, 4, 100, 50),
                                                  self.candidate("B", False, 0, 1, 1))["label"], "A")
        # Review findings win over diff size and elapsed time.
        self.assertEqual(xs.choose_compare_winner(self.candidate("A", True, 0, 100, 50),
                                                  self.candidate("B", True, 1, 1, 1))["label"], "A")
        # Smaller diff wins before elapsed time.
        self.assertEqual(xs.choose_compare_winner(self.candidate("A", True, 0, 5, 50),
                                                  self.candidate("B", True, 0, 10, 1))["label"], "A")
        self.assertEqual(xs.choose_compare_winner(self.candidate("A", True, 0, 5, 1),
                                                  self.candidate("B", True, 0, 5, 2))["label"], "A")

    def test_exact_tie_has_no_winner(self):
        self.assertIsNone(xs.choose_compare_winner(self.candidate("A", True, 1, 5, 2),
                                                   self.candidate("B", True, 1, 5, 2)))

    def test_missing_review_field_uses_remaining_score_criteria(self):
        missing = self.candidate("A", True, 0, 5, 2)
        del missing["review"]
        reviewed = self.candidate("B", True, 1, 5, 2)
        for candidate, backend in ((missing, "codex"), (reviewed, "agy")):
            candidate["backend"] = backend
            candidate["diff"].update(files=1, insertions=3, deletions=2, paths=["x.py"])
            candidate["score"] = xs.compare_candidate_score(candidate)

        # A missing review is not the same as a clean review: with all other
        # evidence tied, compare reports an explainable tie instead of raising.
        self.assertIsNone(xs.choose_compare_winner(missing, reviewed))
        self.assertIn("no winner", " ".join(xs.compare_diff_notes(missing, reviewed, None)))

        # The remaining criteria still apply when the review criterion is unavailable.
        missing["diff"]["changed_lines"] = 4
        missing["score"] = xs.compare_candidate_score(missing)
        winner = xs.choose_compare_winner(missing, reviewed)
        self.assertEqual(winner["label"], "A")
        self.assertIn("Winner: A", " ".join(xs.compare_diff_notes(missing, reviewed, winner)))

    def test_tied_run_ledger_entries_are_marked_tie(self):
        candidates = [{"run_id": "run-a", "backend": "codex", "result": {"status": "done"}},
                      {"run_id": "run-b", "backend": "agy", "result": {"status": "done"}}]
        xs.update_compare_ledger(candidates, "compare-1", None)
        entries = [json.loads(line) for line in Path(xs.LEDGER).read_text(encoding="utf-8").splitlines()]
        self.assertEqual({entry["compare_result"] for entry in entries}, {"tie"})
        self.assertEqual({entry["compare_id"] for entry in entries}, {"compare-1"})

    def test_empty_runs_have_stable_json_and_markdown_summary(self):
        report = {"id": "compare-empty", "title": "No candidates", "winner": None,
                  "winner_label": None, "runs": {}, "diff_notes": []}
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(json.loads(encoded), report)
        markdown = xs.compare_markdown(report)
        self.assertIn("Winner: **none**", markdown)
        self.assertNotIn("## A —", markdown)
        self.assertNotIn("## B —", markdown)

    def test_missing_review_fields_are_preserved_in_json_and_markdown_tie(self):
        left = self.candidate("A", True, 0, 5, 2)
        right = self.candidate("B", True, 0, 5, 2)
        del left["review"]
        del right["review"]
        for candidate, backend in ((left, "codex"), (right, "agy")):
            candidate.update(run_id=f"run-{candidate['label'].lower()}", backend=backend,
                             result={"status": "done"})
            candidate["diff"].update(files=1, insertions=3, deletions=2, paths=["x.py"])
        left["score"] = right["score"] = xs.compare_candidate_score(left)
        winner = xs.choose_compare_winner(left, right)
        report = {"id": "compare-missing-review", "title": "Missing review", "winner": None,
                  "winner_label": None, "runs": {"a": left, "b": right},
                  "diff_notes": xs.compare_diff_notes(left, right, winner)}
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertEqual(json.loads(encoded)["winner_label"], None)
        markdown = xs.compare_markdown(report)
        self.assertIn("Winner: **none**", markdown)
        self.assertIn("- Review: None; unknown error", markdown)

    def test_default_selection_uses_available_different_families(self):
        state = {"ocgo": {"models": [{"id": "openai-model", "family": "OpenAI"}]}}

        def route(profile, _state, _policy):
            backend = profile["backend"]
            if backend not in ("codex", "ocgo", "deepseek"):
                return {"ok": False}
            return {"ok": True, "backend": backend,
                    "model": {"codex": "codex-model", "ocgo": "openai-model",
                              "deepseek": "deepseek-chat"}[backend]}

        args = type("Args", (), {"a": None, "b": None})()
        with mock.patch.object(xs, "get_state", return_value=state), mock.patch.object(xs, "route", side_effect=route):
            self.assertEqual(xs.select_compare_backends(args, {"routing": {"default_order": [
                "codex", "ocgo", "deepseek"]}}),
                {"a": {"backend": "codex", "model": "codex-model"},
                 "b": {"backend": "deepseek", "model": "deepseek-chat"}})
            args.b = "codex"
            self.assertEqual(xs.select_compare_backends(args, {"routing": {"default_order": [
                "codex", "ocgo", "deepseek"]}}),
                {"a": {"backend": "deepseek", "model": "deepseek-chat"},
                 "b": {"backend": "codex", "model": None}})

    def test_review_failure_is_recorded_and_ledgers_and_proposal_are_written(self):
        args = type("Args", (), {"task_file": str(self.task), "title": "Same task", "a": "codex",
                                  "b": "agy", "verify": "true", "card_type": None,
                                  "budget_minutes": 2})()
        launched = []

        class StubProcess:
            returncode = 0

            def communicate(self):
                self.assert_both_started()
                return ("{}", None)

            def assert_both_started(self):
                if len(launched) != 2:
                    raise AssertionError("both executor processes must start before either is joined")

        def stub_popen(command, **_options):
            if "--no-auto-review" not in command:
                raise AssertionError("compare child runs must not schedule duplicate automatic reviews")
            run_id = command[command.index("--run-id") + 1]
            backend = command[command.index("--backend") + 1]
            model = command[command.index("--model") + 1] if "--model" in command else None
            result = {"id": run_id, "title": "Same task", "status": "done", "backend": backend,
                      "model": model, "seconds": 3 if backend == "codex" else 4, "verify": {"rc": 0},
                      "workspace": {"repo": str(self.repo), "base": self.base}, "branch": f"openswarm/{run_id}",
                      "result": {"summary": f"{backend} completed"}}
            run_dir = self.runs / run_id
            (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
            xs.append_ledger({"id": run_id, "run_id": run_id, "backend": backend, "status": "done"})
            launched.append(run_id)
            return StubProcess()

        def reviewer(run_id, _by, _policy):
            if (json.loads((self.runs / run_id / "result.json").read_text(encoding="utf-8"))["backend"] == "codex"):
                raise RuntimeError("stub reviewer unavailable")
            return {"findings": [{"severity": "P2", "path": "module.py", "line": 1,
                                  "title": "Minor concern", "body": "Keep an eye on this."}],
                    "verdict": "approve"}

        def diff_stats(result):
            is_codex = result["backend"] == "codex"
            return {"available": True, "files": 1, "insertions": 2 if is_codex else 4,
                    "deletions": 1 if is_codex else 4, "changed_lines": 3 if is_codex else 8,
                    "paths": ["module.py" if is_codex else "other.py"]}

        stdout = io.StringIO()
        with mock.patch.object(xs, "select_compare_backends", return_value={
                    "a": {"backend": "codex"}, "b": {"backend": "agy"}}), \
                mock.patch.object(xs, "check_budget"), \
                mock.patch.object(xs.subprocess, "Popen", side_effect=stub_popen), \
                mock.patch.object(xs, "perform_peer_review", side_effect=reviewer) as review, \
                mock.patch.object(xs, "compare_diff_stats", side_effect=diff_stats), \
                contextlib.redirect_stdout(stdout):
            xs.cmd_compare(args, self.policy)

        cli = json.loads(stdout.getvalue())
        report = json.loads(Path(cli["compare_json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(launched), 2)
        self.assertEqual(review.call_count, 2)
        self.assertEqual(report["winner_label"], "A")
        self.assertEqual(report["runs"]["a"]["review"]["status"], "failed")
        self.assertEqual(report["runs"]["b"]["review"]["status"], "reviewed")
        self.assertTrue(Path(cli["compare_md"]).is_file())
        self.assertIn("Review of A failed", Path(cli["compare_md"]).read_text(encoding="utf-8"))
        ledger = [json.loads(line) for line in Path(xs.LEDGER).read_text(encoding="utf-8").splitlines()]
        by_id = {entry["id"]: entry for entry in ledger}
        self.assertEqual(by_id[report["runs"]["a"]["run_id"]]["compare_id"], report["id"])
        self.assertEqual(by_id[report["runs"]["a"]["run_id"]]["compare_result"], "win")
        self.assertEqual(by_id[report["runs"]["b"]["run_id"]]["compare_result"], "lose")
        self.assertTrue(Path(cli["proposal"]).is_file())
        self.assertIn("Proposed pattern", Path(cli["proposal"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
