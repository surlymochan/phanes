"""`openswarm/lib/merge/post_hook.py`: the post-merge hook of `main` (Refs #1446).

The hook runs a command once per new sha of `main`: an unchanged sha is skipped,
a failed sha is retried, and repeated failures back off exponentially.  The
wiring into the merge flow belongs to the parent card (#1432).

The module is loaded by path: `openswarm/lib/merge/` is a namespace package
without an `__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
POST_HOOK_MODULE = HERE.parent / "lib" / "merge" / "post_hook.py"

spec = importlib.util.spec_from_file_location("merge_post_hook", POST_HOOK_MODULE)
post_hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post_hook)

CMD = ["/usr/local/bin/refresh-workspace", "--quiet"]


class FakeRunner:
    """A stand-in for `subprocess.run` that records the calls and replays results.

    Every result is either a `(returncode, stderr)` pair or an exception that
    the fake raises instead of running the command.
    """

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({
            "cmd": list(cmd),
            "env": kwargs.get("env"),
            "timeout": kwargs.get("timeout"),
        })
        outcome = self.results.pop(0) if self.results else (0, "")
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, stderr = outcome
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)


class PostMergeHookTests(unittest.TestCase):
    """The hook runs once per new sha, retries a failure and backs off."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-post-hook-")
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "post-hook.json")

    def hook(self, runner, cmd=None, timeout_s=600):
        return post_hook.PostMergeHook(cmd or CMD, self.state_path, timeout_s=timeout_s, runner=runner)

    # ------------------------------------------------------------------ skip

    def test_an_unchanged_sha_does_not_run_the_hook(self):
        runner = FakeRunner([(0, "")])
        hook = self.hook(runner)

        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_OK)
        self.assertEqual(hook.maybe_run("sha-a", now=1001.0), post_hook.STATUS_SKIPPED)

        # the hook ran exactly once, the second call was skipped
        self.assertEqual(len(runner.calls), 1)

    def test_a_new_sha_runs_the_hook_again(self):
        runner = FakeRunner([(0, ""), (0, "")])
        hook = self.hook(runner)

        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_OK)
        self.assertEqual(hook.maybe_run("sha-b", now=1001.0), post_hook.STATUS_OK)

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual([call["cmd"] for call in runner.calls], [CMD, CMD])

    # ----------------------------------------------------------------- retry

    def test_a_failed_sha_is_retried_on_the_next_round(self):
        runner = FakeRunner([(1, "boom"), (0, "")])
        hook = self.hook(runner)

        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_FAILED)
        # the same sha is not skipped after a failure: it is tried again
        self.assertEqual(hook.maybe_run("sha-a", now=1001.0), post_hook.STATUS_OK)

        self.assertEqual(len(runner.calls), 2)

    def test_a_success_clears_the_failure_counter(self):
        runner = FakeRunner([(1, "boom"), (0, "")])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)
        hook.maybe_run("sha-a", now=1001.0)

        state = hook.state()
        self.assertEqual(state["fail_count"], 0)
        self.assertEqual(state["last_ok_sha"], "sha-a")
        self.assertEqual(state["last_fail_sha"], "sha-a")
        self.assertIsNone(state["last_error"])

    def test_a_failure_keeps_the_last_successful_sha(self):
        runner = FakeRunner([(0, ""), (1, "boom")])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)
        self.assertEqual(hook.maybe_run("sha-b", now=1001.0), post_hook.STATUS_FAILED)

        state = hook.state()
        # the version that worked is kept: the hook never rolls anything back
        self.assertEqual(state["last_ok_sha"], "sha-a")
        self.assertEqual(state["last_fail_sha"], "sha-b")
        self.assertEqual(state["fail_count"], 1)
        self.assertEqual(state["last_error"], "boom")

    def test_a_timeout_counts_as_a_failure(self):
        runner = FakeRunner([subprocess.TimeoutExpired(cmd=CMD, timeout=600)])
        hook = self.hook(runner)

        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_FAILED)

        state = hook.state()
        self.assertEqual(state["last_fail_sha"], "sha-a")
        self.assertEqual(state["fail_count"], 1)
        self.assertIsNone(state["last_ok_sha"])
        self.assertIn("timeout", state["last_error"])
        # the run was bounded by the timeout
        self.assertEqual(runner.calls[0]["timeout"], 600)

    def test_a_timeout_with_bytes_stderr_keeps_the_error_tail(self):
        # a timeout carries the raw output of the process: that is bytes
        stderr = b"x" * 600 + b"END"
        runner = FakeRunner([subprocess.TimeoutExpired(cmd=CMD, timeout=600, output=b"", stderr=stderr)])
        hook = self.hook(runner)

        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_FAILED)

        error = hook.state()["last_error"]
        self.assertIn("timeout", error)
        # the bytes are decoded, not dropped: the tail of the output is kept
        detail = error.split(": ", 1)[1]
        self.assertEqual(len(detail), 500)
        self.assertEqual(detail, "x" * 497 + "END")

    def test_the_stderr_of_a_failure_is_truncated_to_its_tail(self):
        stderr = "x" * 600 + "END"
        runner = FakeRunner([(1, stderr)])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)

        error = hook.state()["last_error"]
        self.assertEqual(len(error), 500)
        self.assertTrue(error.endswith("END"))

    def test_the_bytes_stderr_of_a_failure_is_decoded(self):
        stderr = b"x" * 600 + b"END"
        runner = FakeRunner([(1, stderr)])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)

        error = hook.state()["last_error"]
        self.assertEqual(len(error), 500)
        self.assertTrue(error.endswith("END"))

    # --------------------------------------------------------------- backoff

    def test_the_backoff_holds_after_three_failures_on_the_same_sha(self):
        runner = FakeRunner([(1, "boom")] * 6)
        hook = self.hook(runner)

        for moment in (1000.0, 1001.0, 1002.0):
            self.assertEqual(hook.maybe_run("sha-a", now=moment), post_hook.STATUS_FAILED)
        self.assertEqual(hook.state()["fail_count"], 3)

        # the window is 2**3 * 60 = 480 seconds after the last failure
        self.assertEqual(hook.maybe_run("sha-a", now=1003.0), post_hook.STATUS_BACKOFF)
        self.assertEqual(hook.maybe_run("sha-a", now=1482.0), post_hook.STATUS_BACKOFF)
        self.assertEqual(len(runner.calls), 3)

        # after the window the hook tries again
        self.assertEqual(hook.maybe_run("sha-a", now=1483.0), post_hook.STATUS_FAILED)
        self.assertEqual(len(runner.calls), 4)

    def test_the_backoff_window_grows_with_the_failure_count(self):
        runner = FakeRunner([(1, "boom")] * 6)
        hook = self.hook(runner)

        for moment in (1000.0, 1001.0, 1002.0):
            hook.maybe_run("sha-a", now=moment)
        hook.maybe_run("sha-a", now=1483.0)  # the fourth failure, at 1483.0

        # the window is now 2**4 * 60 = 960 seconds after the last failure
        self.assertEqual(hook.maybe_run("sha-a", now=1484.0), post_hook.STATUS_BACKOFF)
        self.assertEqual(hook.maybe_run("sha-a", now=2443.0), post_hook.STATUS_BACKOFF)
        self.assertEqual(len(runner.calls), 4)

        self.assertEqual(hook.maybe_run("sha-a", now=2444.0), post_hook.STATUS_FAILED)
        self.assertEqual(len(runner.calls), 5)

    def test_a_new_sha_is_not_blocked_by_the_backoff_of_another_sha(self):
        runner = FakeRunner([(1, "boom")] * 4)
        hook = self.hook(runner)

        for moment in (1000.0, 1001.0, 1002.0):
            hook.maybe_run("sha-a", now=moment)

        # sha-b never failed: it runs right away
        self.assertEqual(hook.maybe_run("sha-b", now=1003.0), post_hook.STATUS_FAILED)
        self.assertEqual(len(runner.calls), 4)

    # ------------------------------------------------------------ environment

    def test_the_environment_carries_the_sha_of_main(self):
        runner = FakeRunner([(0, "")])
        hook = self.hook(runner)
        before = dict(os.environ)

        hook.maybe_run("sha-a", now=1000.0)

        self.assertEqual(runner.calls[0]["env"]["OPENSWARM_MAIN_SHA"], "sha-a")
        # the environment of the caller is left alone
        self.assertEqual(dict(os.environ), before)

    def test_the_command_is_an_argv_list_and_no_shell_is_used(self):
        runner = FakeRunner([(0, "")])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)

        call = runner.calls[0]
        self.assertEqual(call["cmd"], CMD)
        self.assertIsInstance(call["cmd"], list)
        with self.assertRaises(ValueError):
            post_hook.PostMergeHook("refresh-workspace", self.state_path, runner=runner)

    # ------------------------------------------------------------ state file

    def test_a_damaged_state_file_falls_back_to_an_empty_state(self):
        with open(self.state_path, "w", encoding="utf-8") as stream:
            stream.write("{not json")

        runner = FakeRunner([(0, "")])
        hook = self.hook(runner)

        # nothing is known, so the hook runs instead of skipping the sha
        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_OK)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(hook.state()["last_ok_sha"], "sha-a")

    def test_a_state_file_with_a_broken_field_falls_back_to_an_empty_state(self):
        with open(self.state_path, "w", encoding="utf-8") as stream:
            json.dump({"last_ok_sha": "sha-a", "fail_count": "many"}, stream)

        runner = FakeRunner([(0, "")])
        hook = self.hook(runner)

        self.assertEqual(hook.maybe_run("sha-a", now=1000.0), post_hook.STATUS_OK)
        self.assertEqual(len(runner.calls), 1)

    def test_the_state_file_is_written_atomically(self):
        runner = FakeRunner([(0, "")])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)

        # no half written temporary file is left behind
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["post-hook.json"])
        payload = json.loads(pathlib.Path(self.state_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["last_ok_sha"], "sha-a")
        self.assertEqual(payload["fail_count"], 0)

    def test_the_state_file_survives_a_reload(self):
        runner = FakeRunner([(1, "boom")])
        hook = self.hook(runner)

        hook.maybe_run("sha-a", now=1000.0)

        # a second hook on the same state file sees the failure
        other = self.hook(FakeRunner([(0, "")]))
        self.assertEqual(other.state()["fail_count"], 1)
        self.assertEqual(other.state()["last_fail_sha"], "sha-a")

    def test_an_empty_sha_is_refused(self):
        hook = self.hook(FakeRunner([(0, "")]))

        with self.assertRaises(ValueError):
            hook.maybe_run("", now=1000.0)
        with self.assertRaises(ValueError):
            hook.maybe_run(None, now=1000.0)


if __name__ == "__main__":
    unittest.main()
