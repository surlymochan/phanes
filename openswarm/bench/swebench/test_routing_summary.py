import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "harness"))
fake_swe_env = types.ModuleType("swe_env")
fake_swe_env.ROOT = Path(__file__).parent
fake_swe_env.GIT = ["git"]
fake_swe_env.sh = lambda *args, **kwargs: None
fake_swe_env.visible_command = lambda inst, venv: "python -m unittest"
sys.modules.setdefault("swe_env", fake_swe_env)
import arms
from routing_summary import routing_summary


class RoutingSummaryTests(unittest.TestCase):
    def test_extracts_relevant_sections_under_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text(
                "# Overview\n" + "irrelevant " * 500 +
                "\n## Backend selection\nChoose pi for this run.\n"
                "## Acceptance commands\nRun python -m unittest.\n"
                "## Task file format\nInclude goal and acceptance criteria.\n"
            )
            summary = routing_summary(skill.read_text())
        self.assertLessEqual(len(summary), 4000)
        self.assertIn("Backend selection", summary)
        self.assertIn("Acceptance commands", summary)
        self.assertIn("Task file format", summary)
        self.assertNotIn("irrelevant", summary)

    def test_missing_relevant_headings_uses_noted_prefix(self):
        summary = routing_summary("# Intro\nfirst\nsecond\n# More\nthird", fallback_lines=3)
        self.assertIn("Fallback:", summary)
        self.assertIn("first", summary)
        self.assertIn("second", summary)
        self.assertNotIn("third", summary)

    def test_arm_uses_summary_and_120_second_planner_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            events = root / "events"
            events.mkdir()
            openswarm = root / "openswarm"
            openswarm.mkdir()
            full_skill = "# Overview\n" + "whole skill body " * 500
            full_skill += "\n## Routing rules\nUse the pi backend.\n## Task file format\nWrite one task.\n"
            (openswarm / "SKILL.md").write_text(full_skill)
            call = {}

            def fake_run_pi(task_id, workdir, prompt, event_dir, timeout, env_extra=None, label="agent"):
                call.update(prompt=prompt, timeout=timeout)
                return {"label": label}

            with patch.object(arms, "OPENSWARM_DIR", openswarm), \
                 patch.object(arms.E, "sh", return_value=type("Result", (), {"stdout": "abc\n"})()), \
                 patch.object(arms, "run_pi", side_effect=fake_run_pi), \
                 patch.object(arms, "GIT", ["git"]), \
                 patch.dict(os.environ, {"SWEBENCH_PLANNER_SKILL": "summary"}, clear=False):
                calls = arms.arm_openswarm(
                    {"instance_id": "x", "repo": "example", "problem_statement": "Fix issue"},
                    repo, root / "venv", events, "tid",
                )

            self.assertEqual(call["timeout"], 120)
            self.assertNotIn(full_skill, call["prompt"])
            self.assertIn("Routing rules", call["prompt"])
            self.assertEqual(calls[0]["skill_mode"], "summary")
            self.assertEqual(calls[0]["prompt_chars"], len(call["prompt"]))


if __name__ == "__main__":
    unittest.main()
