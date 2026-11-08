import pathlib
import runpy
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SWARM = runpy.run_path(str(ROOT / "bin" / "swrm"))


class ExecutorArtifactFilterTests(unittest.TestCase):
    def test_leaked_issue_card_is_excluded_from_executor_commits(self):
        leaked_card = "openswarm/tests/openswarm-card-412ygy9g.md"

        self.assertTrue(
            SWARM["executor_artifact"](leaked_card, SWARM["NEVER_COMMIT"]),
            "executor artifact filter must exclude the leaked issue-card sample",
        )
        self.assertFalse(
            SWARM["executor_artifact"]("swrm/tests/test_executor_artifact_filter.py", SWARM["NEVER_COMMIT"]),
            "executor artifact filter must keep real tests",
        )

    def test_finalize_workspace_drops_leaked_card_and_keeps_test(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = pathlib.Path(temp) / "repo"
            run_dir = pathlib.Path(temp) / "run"
            repo.mkdir()
            run_dir.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=repo, check=True, capture_output=True, text=True
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.invalid")
            (repo / "README.md").write_text("base\n")
            git("add", "README.md")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")

            leaked_card = repo / "openswarm/tests/openswarm-card-412ygy9g.md"
            leaked_card.parent.mkdir(parents=True)
            leaked_card.write_text("card two\n")
            real_test = repo / "swrm/tests/test_executor_artifact_filter.py"
            real_test.parent.mkdir(parents=True)
            real_test.write_text("assert True\n")

            result = SWARM["finalize_workspace"](
                {"mode": "worktree", "root": str(repo), "base": base,
                 "repo": str(repo), "branch": "executor", "linked": []},
                "filter artifacts", "codex", str(run_dir), keep=True,
            )

            committed = git("show", "--format=", "--name-only", result["commit"]).splitlines()
            self.assertEqual(result["excluded_files"], ["openswarm/tests/openswarm-card-412ygy9g.md"])
            self.assertNotIn("openswarm/tests/openswarm-card-412ygy9g.md", committed)
            self.assertIn("swrm/tests/test_executor_artifact_filter.py", committed)


if __name__ == "__main__":
    unittest.main()
