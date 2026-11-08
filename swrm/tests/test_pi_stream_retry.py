"""Regression for #1594: a broken upstream stream must be retried, not failed as exec_error.

The pi provider (MiniMax behind the local broker) breaks the answer stream when the host
pauses: the broker answers `502 provider unavailable` and pi reports
`Anthropic stream ended before message_stop`.  The lane used to end the whole run as
`failed/exec_error`, which threw away the review/implementation work of that run (#1594).

The contract of the issue (#1535 wins over #1594):

  * a broken upstream stream is a transient error and the lane retries it,
  * an exhausted Token Plan is not transient and must never be retried,
  * the stream retries are counted on their own (default cap 3), the counter is written to
    status.json and the run ends as `exec_error` with `stream_retries=N` once the budget of
    the stream retries is used up.

The issue names `openswarm/bin/openswarm` and `openswarm/tests/test_pi_stream_retry.py`, but
the vendored openswarm tree was replaced by swrm as the single source (#1894): the lane
implementation now lives in `swrm/bin/swrm` and its tests in `swrm/tests/`.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()

CLI = pathlib.Path(_load.ROOT) / "bin" / "swrm"

# The two raw samples of the 07:31 / 07:55 incidents (#1594).
STREAM_SAMPLES = ("Anthropic stream ended before message_stop", "502 provider unavailable")
# The Token Plan is exhausted: not transient, so it must not be retried (#1535).
QUOTA_SAMPLE = "已达到 Token Plan 用量上限 (2056)"
# The default cap of the stream retries (#1594).
STREAM_RETRY_CAP = 3

# A fake pi RPC executor that can break the upstream stream on demand.
FAKE_PI = r'''#!/usr/bin/env python3
"""Fake pi JSON-lines RPC executor: it breaks the upstream stream on demand (#1594)."""
import json
import os
import sys

STREAM_ERROR = "Anthropic stream ended before message_stop"
QUOTA_ERROR = "已达到 Token Plan 用量上限 (2056)"


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def record(obj):
    path = os.environ.get("FAKE_PI_LOG")
    if path:
        with open(path, "a") as stream:
            stream.write(json.dumps(obj, ensure_ascii=False) + "\n")


def fail_turn(error):
    message = {"role": "assistant", "content": [], "usage": {"input": 1, "output": 0},
               "stopReason": "error", "errorMessage": error}
    send({"type": "message_end", "message": message})
    send({"type": "agent_end", "messages": [message]})


def succeed_turn():
    with open("lane_result.txt", "w") as stream:
        stream.write("completed")
    result = {"status": "done", "summary": "fake Pi completed", "files_changed": ["lane_result.txt"],
              "tests": "fake executor", "followups": []}
    message = {"role": "assistant", "content": [{"type": "text", "text": json.dumps(result)}],
               "usage": {"input": 3, "output": 2}, "stopReason": "stop"}
    send({"type": "message_end", "message": message})
    send({"type": "agent_end", "messages": [message]})


mode = os.environ.get("FAKE_PI_MODE", "ok")
prompts = 0
for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    record(request)
    kind = request.get("type")
    if kind == "new_session":
        send({"id": request.get("id"), "type": "response", "command": "new_session", "success": True})
    elif kind == "prompt":
        prompts += 1
        send({"id": request.get("id"), "type": "response", "command": "prompt", "success": True})
        if mode == "quota":
            fail_turn(QUOTA_ERROR)
        elif mode == "stream_always":
            fail_turn(STREAM_ERROR)
        elif mode == "stream" and prompts == 1:
            fail_turn(STREAM_ERROR)
        else:
            succeed_turn()
'''


class PiStreamRetryContractTests(unittest.TestCase):
    """The classification contract of #1594 (quota first, then the broken stream class)."""

    def retryable_pattern(self):
        """The regex that marks a provider error as retryable."""
        for name in ("PI_RETRYABLE_RE", "PI_STREAM_RETRY_RE"):
            pattern = getattr(xs, name, None)
            if pattern is not None:
                return pattern
        self.fail("bin/swrm has no retryable provider error pattern (PI_RETRYABLE_RE / PI_STREAM_RETRY_RE)")

    def retry_kind(self, text):
        """The retry class of a provider error ("quota", "stream" or None)."""
        try:
            return xs.pi_retry_kind(text)
        except AttributeError:
            self.fail("bin/swrm has no pi_retry_kind: the broken stream class is not implemented")

    def quota_exhausted(self, text):
        """Whether a provider error is an exhausted quota."""
        try:
            return xs.quota_exhausted_error(text)
        except AttributeError:
            self.fail("bin/swrm has no quota_exhausted_error: an exhausted quota is not classified")

    def decide(self, error_class, error_tail, retries):
        """Ask the lane what it does with a turn that failed like this."""
        try:
            return xs.pi_stream_retry_decision("pi", error_class, error_tail, retries)
        except AttributeError:
            self.fail("bin/swrm has no pi_stream_retry_decision: the broken stream class is not implemented")
        except TypeError as error:
            self.fail(f"pi_stream_retry_decision has an unexpected signature: {error}")

    def stream_retry_cap(self):
        """How many broken streams the lane retries before it gives up."""
        for retries in range(20):
            action, _, _ = self.decide("exec_error", STREAM_SAMPLES[0], retries)
            if action != "retry":
                return retries
        self.fail("the stream retry budget never ends")

    def test_broken_stream_samples_are_retryable(self):
        for sample in STREAM_SAMPLES:
            self.assertEqual(self.retry_kind(sample), "stream", sample)
            self.assertTrue(self.retryable_pattern().search(sample), sample)

    def test_token_plan_exhaustion_is_not_retryable(self):
        self.assertEqual(self.retry_kind(QUOTA_SAMPLE), "quota")
        self.assertTrue(self.quota_exhausted(QUOTA_SAMPLE))

    def test_stream_retry_budget_ends_as_exec_error_with_the_counter(self):
        action, error_class, _ = self.decide("exec_error", STREAM_SAMPLES[0], 0)
        self.assertEqual(action, "retry")
        self.assertEqual(error_class, "exec_error")
        cap = self.stream_retry_cap()
        self.assertEqual(cap, STREAM_RETRY_CAP)
        action, error_class, detail = self.decide("exec_error", STREAM_SAMPLES[0], cap)
        self.assertEqual(action, "stop")
        self.assertEqual(error_class, "exec_error")
        self.assertIn(f"stream_retries={cap}", detail)

    def test_quota_error_is_never_retried(self):
        action, _, _ = self.decide("exec_error", QUOTA_SAMPLE, 0)
        self.assertEqual(action, "stop")


class PiStreamRetryLaneTests(unittest.TestCase):
    """End to end: a pi lane whose upstream stream breaks retries the turn instead of failing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-pi-stream-")
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Test")
        self.git("config", "user.email", "lane-test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")
        self.fake_pi = self.root / "fake_pi_rpc.py"
        self.fake_pi.write_text(FAKE_PI)
        self.fake_pi.chmod(0o755)
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        self.env["FAKE_PI_LOG"] = str(self.root / "fake-pi.jsonl")
        self.lane_name = "L1"

    def tearDown(self):
        if self.data.exists():
            try:
                self.cli("lane", "down", check=False, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def cli(self, *args, check=True, timeout=30):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env, text=True,
                                capture_output=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result

    def set_config(self, mode):
        """A pi lane whose fake executor fails the way the upstream provider failed."""
        self.env["FAKE_PI_MODE"] = mode
        (self.data / "lanes").mkdir(parents=True, exist_ok=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": self.lane_name, "backend": "pi", "model": "fake-model", "provider": "fake-provider",
            "repo": str(self.repo), "binary": str(self.fake_pi)}]}))
        (self.data / "policy.local.json").write_text(json.dumps({"pi": {"require_isolation": False}}))

    def start_lane(self):
        result = self.cli("lane", "up", self.lane_name)
        self.assertTrue(json.loads(result.stdout)["lanes"][0]["state"] in ("idle", "starting"))

    def assign(self, task):
        result = self.cli("lane", "assign", self.lane_name, "--prompt", task)
        return json.loads(result.stdout)["id"]

    def run_state(self, run_id):
        path = self.data / "runs" / run_id / "status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def wait_run(self, run_id, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.run_state(run_id)
            if state.get("state") == "finished":
                return state
            time.sleep(0.05)
        self.fail(f"timed out waiting for run {run_id} to finish; last={self.run_state(run_id)!r}")

    def result(self, run_id):
        return json.loads((self.data / "runs" / run_id / "result.json").read_text())

    def prompts(self):
        """The prompts the fake pi received (one per turn of the lane)."""
        path = pathlib.Path(self.env["FAKE_PI_LOG"])
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [row["message"] for row in rows if row.get("type") == "prompt"]

    def error_classes(self, result):
        """A short description of the attempts of a run (the error tails are long)."""
        return [(attempt.get("error_class"), attempt.get("error_tail"))
                for attempt in result.get("attempts") or []]

    def test_broken_stream_is_retried_and_the_run_finishes(self):
        self.set_config("stream")
        self.start_lane()
        run_id = self.assign("the upstream stream breaks once")
        self.wait_run(run_id)
        result = self.result(run_id)
        self.assertEqual(result["status"], "done", self.error_classes(result))
        self.assertGreaterEqual(len(self.prompts()), 2, "the broken stream was not retried")

    def test_token_plan_exhaustion_is_not_retried(self):
        self.set_config("quota")
        self.start_lane()
        run_id = self.assign("the token plan is exhausted")
        self.wait_run(run_id)
        result = self.result(run_id)
        self.assertEqual(result["status"], "failed", self.error_classes(result))
        self.assertEqual(len(self.prompts()), 1, "an exhausted Token Plan must not be retried")
        self.assertIn("Token Plan", json.dumps(result, ensure_ascii=False))

    def test_stream_retry_budget_stops_the_run_with_the_counter(self):
        self.set_config("stream_always")
        # The cap is configurable; a small one keeps the test short.
        self.env["PI_STREAM_RETRY_MAX"] = "1"
        self.env["OPENSWARM_PI_STREAM_RETRY_MAX"] = "1"
        self.start_lane()
        run_id = self.assign("the upstream stream keeps breaking")
        self.wait_run(run_id)
        result = self.result(run_id)
        self.assertEqual(result["status"], "failed", self.error_classes(result))
        self.assertEqual(result["error_class"], "exec_error")
        prompts = self.prompts()
        self.assertGreaterEqual(len(prompts), 2, "the broken stream was not retried at all")
        counter = re.search(r"stream_retries=(\d+)", json.dumps(result, ensure_ascii=False))
        self.assertIsNotNone(counter, "the run does not report the stream retries")
        self.assertEqual(int(counter.group(1)), len(prompts) - 1)
        self.assertEqual(self.run_state(run_id).get("stream_retries"), len(prompts) - 1)


if __name__ == "__main__":
    unittest.main()
