"""Planner routing and the SWE-bench direct-executor path."""
import importlib.util
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import _load

xs = _load.load()


def test_planner_policy_limit():
    assert xs.planner_decision({"difficulty": 2}, {}) == "skipped"
    assert xs.planner_decision({"difficulty": 3}, {}) == "used"
    assert xs.planner_decision({"difficulty": 2}, {"routing": {"planner_skip_max_difficulty": 1}}) == "used"
    assert xs.planner_decision({"difficulty": "1-4 hours"}, {}) == "used"
    assert xs.planner_decision({"difficulty": True}, {}) == "used"


def test_dataset_difficulty_runs_planner_and_executor():
    stub = types.SimpleNamespace(ROOT=ROOT, GIT=["git"], visible_command=lambda *args: "pytest", sh=mock.Mock())
    spec = importlib.util.spec_from_file_location("bench_arms_dataset", Path(__file__).parent / "harness" / "arms.py")
    arms = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"swe_env": stub}):
        spec.loader.exec_module(arms)
    result = types.SimpleNamespace(stdout="", stderr="", returncode=0)
    stub.sh.return_value = result
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, events, swarm = root / "repo", root / "events", root / "swarm"
        for path in (repo, events, swarm):
            path.mkdir()
        (swarm / "SKILL.md").write_text("skill")

        def plan(*args, **kwargs):
            (root / "planner" / "executor-task.md").write_text("Fix issue")
            return {"label": "planner"}

        routing = {"planner_decision": xs.planner_decision, "load_policy": lambda: {}}
        with mock.patch.object(arms, "OPENSWARM_DIR", swarm), mock.patch("runpy.run_path", return_value=routing), \
             mock.patch.object(arms, "run_pi", side_effect=plan), mock.patch.object(arms.subprocess, "run", return_value=result):
            calls = arms.arm_swrm({"instance_id": "x", "repo": "example", "problem_statement": "Fix issue",
                                   "difficulty": "1-4 hours"}, repo, root / "venv", events, "tid")
        assert [call["label"] for call in calls] == ["planner", "executor"]


def test_easy_task_runs_executor_without_planner():
    stub = types.SimpleNamespace(ROOT=ROOT, GIT=["git"], visible_command=lambda *args: "pytest", sh=mock.Mock())
    spec = importlib.util.spec_from_file_location("bench_arms_skip", Path(__file__).parent / "harness" / "arms.py")
    arms = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"swe_env": stub}):
        spec.loader.exec_module(arms)
    result = types.SimpleNamespace(stdout="", stderr="", returncode=0)
    stub.sh.return_value = result
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, events, swarm = root / "repo", root / "events", root / "swarm"
        for path in (repo, events, swarm):
            path.mkdir()
        routing = {"planner_decision": lambda profile, policy: "skipped", "load_policy": lambda: {}}
        with mock.patch.object(arms, "OPENSWARM_DIR", swarm), mock.patch("runpy.run_path", return_value=routing), \
             mock.patch.object(arms, "run_pi") as planner, mock.patch.object(arms.subprocess, "run", return_value=result):
            calls = arms.arm_swrm({"instance_id": "x", "repo": "example", "problem_statement": "Fix issue", "difficulty": 2},
                                  repo, root / "venv", events, "tid")
        planner.assert_not_called()
        assert [call["label"] for call in calls] == ["executor"]
        assert "Fix issue" in (events / "executor-task.md").read_text()
