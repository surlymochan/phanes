"""Tests for external dispatch task source hooks (Refs #1449)."""
import importlib.util
import json
import pathlib
import subprocess
import types
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "dispatch" / "source_hooks.py"
spec = importlib.util.spec_from_file_location("dispatch_source_hooks", MODULE)
source_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_hooks)


def output(*tasks):
    return "\n".join(json.dumps(task) for task in tasks)


class FakeRunner:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


def completed(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def hook(name="source", argv=None, pause_scope=None):
    return {"name": name, "argv": argv or ["source", "--json"], "pause_scope": pause_scope}


class SourceHookTests(unittest.TestCase):
    def test_parses_and_sorts_by_priority_then_issue(self):
        runner = FakeRunner([completed(output(
            {"issue": 12, "priority": "p2", "backend": None, "reason": "later"},
            {"issue": 9, "priority": "p0", "backend": "pi", "reason": "urgent"},
            {"issue": 4, "priority": "p1", "backend": None, "reason": "middle"},
        ))])
        result = source_hooks.run_hooks([hook()], runner=runner)
        self.assertEqual([task["issue"] for task in result["tasks"]], [9, 4, 12])
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(runner.calls[0][0], ["source", "--json"])
        self.assertFalse(runner.calls[0][1].get("shell", False))

    def test_rejects_non_json_and_invalid_field_types(self):
        runner = FakeRunner([completed("not json\n" + output(
            {"issue": True, "priority": "p0", "backend": None, "reason": "bad"},
            {"issue": 3, "priority": "p3", "backend": None, "reason": "bad"},
            {"issue": 4, "priority": None, "backend": None, "reason": 8},
        ))])
        result = source_hooks.run_hooks([hook()], runner=runner)
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["rejected"], 4)

    def test_timeout_is_an_error_and_next_hook_runs(self):
        runner = FakeRunner([subprocess.TimeoutExpired(["slow"], 1, stderr="x" * 350), completed()])
        result = source_hooks.run_hooks([hook("slow"), hook("next")], runner=runner)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["error"], "timeout")
        self.assertEqual(len(result["errors"][0]["stderr"]), 300)
        self.assertEqual(len(runner.calls), 2)

    def test_nonzero_exit_is_an_error_and_next_hook_runs(self):
        runner = FakeRunner([completed("", "failed", 7), completed()])
        result = source_hooks.run_hooks([hook("bad"), hook("good")], runner=runner)
        self.assertEqual(result["errors"][0]["error"], "exit status 7")
        self.assertEqual(result["errors"][0]["stderr"], "failed")
        self.assertEqual(len(runner.calls), 2)

    def test_paused_scope_skips_hook(self):
        runner = FakeRunner([completed()])
        result = source_hooks.run_hooks(
            [hook("mint", pause_scope="mint"), hook("other")], runner=runner,
            is_paused=lambda scope: scope == "mint")
        self.assertEqual(result["skipped"], ["mint"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0], ["source", "--json"])

    def test_duplicate_issue_uses_best_priority_and_combines_reasons(self):
        runner = FakeRunner([
            completed(output({"issue": 21, "priority": "p2", "backend": None, "reason": "first"})),
            completed(output({"issue": 21, "priority": "p0", "backend": "codex", "reason": "second"})),
        ])
        result = source_hooks.run_hooks([hook("a"), hook("b")], runner=runner)
        self.assertEqual(result["tasks"], [{"issue": 21, "priority": "p0", "backend": "codex", "reason": "first; second"}])

    def test_duplicate_reason_is_not_repeated(self):
        runner = FakeRunner([
            completed(output({"issue": 21, "priority": "p1", "backend": None, "reason": "same"})),
            completed(output({"issue": 21, "priority": "p1", "backend": None, "reason": "same"})),
        ])
        result = source_hooks.run_hooks([hook("a"), hook("b")], runner=runner)
        self.assertEqual(result["tasks"][0]["reason"], "same")

    def test_empty_output_returns_empty_result(self):
        result = source_hooks.run_hooks([hook()], runner=FakeRunner([completed()]))
        self.assertEqual(result, {"tasks": [], "errors": [], "rejected": 0, "skipped": []})

    def test_missing_fields_are_rejected_and_next_hook_still_runs(self):
        """A line without all four protocol fields is dropped, not fatal."""
        runner = FakeRunner([
            completed('{"issue": 5, "reason": "no priority"}\n'
                      '{"issue": 6, "priority": "p1", "reason": "no backend"}'),
            completed(output({"issue": 5, "priority": "p0", "backend": "pi", "reason": "ok"})),
        ])
        result = source_hooks.run_hooks([hook("broken"), hook("next")], runner=runner)
        self.assertEqual(result["rejected"], 2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result["tasks"], [{"issue": 5, "priority": "p0", "backend": "pi", "reason": "ok"}])

    def test_missing_backend_on_the_same_issue_does_not_break_the_merge(self):
        runner = FakeRunner([
            completed('{"issue": 5, "priority": "p0", "reason": "no backend"}'),
            completed(output({"issue": 5, "priority": "p2", "backend": None, "reason": "ok"})),
            completed(output({"issue": 6, "priority": "p1", "backend": None, "reason": "other"})),
        ])
        result = source_hooks.run_hooks([hook("a"), hook("b"), hook("c")], runner=runner)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual([task["issue"] for task in result["tasks"]], [6, 5])
        self.assertEqual(result["tasks"][1], {"issue": 5, "priority": "p2", "backend": None, "reason": "ok"})

    def test_runner_without_stdout_is_not_a_rejected_line(self):
        runner = FakeRunner([completed(None)])
        result = source_hooks.run_hooks([hook()], runner=runner)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(result["tasks"], [])

    def test_broken_hook_configuration_does_not_stop_the_other_hooks(self):
        runner = FakeRunner([completed(output({"issue": 3, "priority": "p1", "backend": None, "reason": "ok"}))])
        hooks = [{"name": "bad", "argv": [], "pause_scope": None}, "not-a-dict", hook("good")]
        result = source_hooks.run_hooks(hooks, runner=runner)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(result["errors"][0]["name"], "bad")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([task["issue"] for task in result["tasks"]], [3])

    def test_hook_that_breaks_does_not_stop_the_other_hooks(self):
        class Exploding:
            returncode = 0
            stderr = ""

            @property
            def stdout(self):
                raise ValueError("boom")

        runner = FakeRunner([
            Exploding(),
            completed(output({"issue": 3, "priority": "p1", "backend": None, "reason": "ok"})),
        ])
        result = source_hooks.run_hooks([hook("boom"), hook("good")], runner=runner)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["name"], "boom")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual([task["issue"] for task in result["tasks"]], [3])


if __name__ == "__main__":
    unittest.main()
