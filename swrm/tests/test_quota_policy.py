"""Per-backend quota policy: the pattern, the cooldown and the scoped pause (Refs #1368).

The quota policy of one backend (``policy["backends"][<backend>]``) holds the
pattern that marks a quota error, the cooldown of the pause and the scope that
is paused ("lane" pauses the lane, "backend" pauses the backend).  A quota
error pauses its scope through the scoped pause store ($OPENSWARM_HOME/pauses):
the records the autodispatch reads, so a paused lane or a paused backend is not
dispatched any more until the record expires.

BLOCKER mapping of PR #1654 (Refs #1368):
  B1  the scoped quota pause of the lane and of the backend, the expiry clean
      up and the probe recovery
      -> test_copilot_402_pauses_the_lane_scope
      -> test_agy_429_pauses_the_backend_scope
      -> test_plain_429_uses_the_policy_cooldown
      -> test_quota_pause_expires_and_is_reaped
      -> test_probe_recovery_resumes_the_automatic_pause
      -> test_quota_error_pauses_the_backend_scope (end to end: a lane run that
         ends with a quota error pauses the scope)
  B2  the automatic pause is removed, a manual pause of the same scope stays
      -> test_quota_pause_does_not_replace_a_manual_pause
      -> test_probe_recovery_keeps_a_manual_pause
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _load

xs = _load.load()


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "swrm"
FIXTURES = ROOT / "tests" / "fixtures"


COPILOT_402 = "HTTP 402 Payment Required: the Copilot plan quota is exhausted"
AGY_429 = "RESOURCE_EXHAUSTED: 429 Too Many Requests (quota exceeded)"


class QuotaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("DATA", str(self.root)), ("RUNS", str(self.root / "runs")),
                            ("LEDGER", str(self.root / "ledger.jsonl"))):
            patch = mock.patch.object(xs, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.now = 1_700_000_000.0
        patch = mock.patch.object(xs, "NOW", return_value=self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.policy = {"backends": {
            "copilot": {"quota_error_re": "402|429", "quota_cooldown_s": 3600, "scope": "lane"},
            "agy": {"quota_error_re": "429|RESOURCE_EXHAUSTED", "quota_cooldown_s": 1800, "scope": "backend"},
        }}

    def lane(self, name="M5", backend="copilot"):
        return {"name": name, "backend": backend, "model": "claude-haiku-5.5"}

    def attempts(self, backend, text):
        return [{"backend": backend, "model": "m", "rc": 1, "seconds": 1.0,
                 "error_class": "exec_error", "error_tail": text}]

    def record(self, scope):
        return xs.pause_record(scope)

    # --- the policy of one backend ------------------------------------------------

    def test_policy_quota_fields_are_read_per_backend(self):
        table = xs.quota_policy_table(self.policy)
        self.assertEqual(table["copilot"], {"quota_error_re": "402|429", "cooldown_s": 3600, "scope": "lane"})
        self.assertEqual(table["agy"], {"quota_error_re": "429|RESOURCE_EXHAUSTED", "cooldown_s": 1800,
                                        "scope": "backend"})
        self.assertNotIn("codex", table)

    def test_local_policy_overrides_the_cooldown_of_one_backend_only(self):
        Path(self.root, "policy.local.json").write_text(
            json.dumps({"backends": {"copilot": {"quota_cooldown_s": 77}}}), encoding="utf-8")
        table = xs.quota_policy_table(xs.load_policy())
        self.assertEqual(table["copilot"]["cooldown_s"], 77)
        self.assertEqual(table["copilot"]["scope"], "lane")
        self.assertTrue(table["copilot"]["quota_error_re"])
        self.assertEqual(table["agy"]["cooldown_s"], 1800)

    def test_default_policy_pauses_the_copilot_lane_on_402(self):
        hit = xs.quota_policy_hit("copilot", COPILOT_402, xs.load_policy())
        self.assertEqual(hit["scope"], "lane")
        self.assertEqual(hit["cooldown_s"], 3600)

    # --- the scoped pause ---------------------------------------------------------

    def test_copilot_402_pauses_the_lane_scope(self):
        paused = xs.apply_backend_quota_policy(self.lane(), self.attempts("copilot", COPILOT_402),
                                                 policy=self.policy)
        self.assertEqual([row["scope"] for row in paused], ["lane:M5"])
        record = self.record("lane:M5")
        self.assertEqual(record["scope"], "lane:M5")
        self.assertEqual(record["expires_at"], self.now + 3600)
        self.assertTrue(record["reason"].startswith("auto:quota:M5:"))

    def test_agy_429_pauses_the_backend_scope(self):
        paused = xs.apply_backend_quota_policy(self.lane(backend="agy"),
                                                 self.attempts("agy", AGY_429), policy=self.policy)
        self.assertEqual([row["scope"] for row in paused], ["backend:agy"])
        record = self.record("backend:agy")
        self.assertEqual(record["expires_at"], self.now + 1800)
        self.assertIsNone(self.record("lane:M5"))

    def test_plain_429_uses_the_policy_cooldown(self):
        # A quota error without a reset time still gets the configured cooldown,
        # so the pause is not endless (Refs #1368).
        xs.apply_backend_quota_policy(self.lane(backend="agy"),
                                      self.attempts("agy", "HTTP 429 Too Many Requests"), policy=self.policy)
        self.assertEqual(self.record("backend:agy")["expires_at"], self.now + 1800)

    def test_reset_time_in_the_error_wins_over_the_policy_cooldown(self):
        xs.apply_backend_quota_policy(self.lane(backend="agy"),
                                      self.attempts("agy", "429 quota exceeded, resets in 90s"), policy=self.policy)
        self.assertEqual(self.record("backend:agy")["expires_at"], self.now + 90)

    def test_zero_cooldown_pauses_nothing(self):
        policy = {"backends": {"copilot": {"quota_error_re": "402", "quota_cooldown_s": 0, "scope": "lane"}}}
        paused = xs.apply_backend_quota_policy(self.lane(), self.attempts("copilot", COPILOT_402), policy=policy)
        self.assertEqual(paused, [])
        self.assertEqual(xs.pause_records(self.now), [])

    def test_quota_pause_expires_and_is_reaped(self):
        xs.apply_backend_quota_policy(self.lane(backend="agy"), self.attempts("agy", AGY_429), policy=self.policy)
        self.assertEqual([row["scope"] for row in xs.pause_records(self.now)], ["backend:agy"])
        self.assertEqual(xs.pause_records(self.now + 1800), [])
        self.assertEqual(xs.reap_quota_pauses(self.now + 1799), [])
        self.assertEqual(xs.reap_quota_pauses(self.now + 1800), ["backend:agy"])
        self.assertIsNone(self.record("backend:agy"))

    def test_dry_run_writes_no_pause(self):
        paused = xs.apply_backend_quota_policy(self.lane(), self.attempts("copilot", COPILOT_402),
                                                 policy=self.policy, dry_run=True)
        self.assertEqual([row["scope"] for row in paused], ["lane:M5"])
        self.assertTrue(paused[0]["planned"])
        self.assertFalse(os.path.exists(xs.pause_scope_file("lane:M5")))
        self.assertEqual(xs.pause_records(self.now), [])

    # --- a manual pause wins ------------------------------------------------------

    def test_quota_pause_does_not_replace_a_manual_pause(self):
        xs.write_scope_pause("backend:agy", None, "operator hold", now=self.now)
        paused = xs.apply_backend_quota_policy(self.lane(backend="agy"),
                                                 self.attempts("agy", AGY_429), policy=self.policy)
        self.assertEqual(paused, [])
        record = self.record("backend:agy")
        self.assertEqual(record["reason"], "operator hold")
        self.assertIsNone(record["expires_at"])

    def test_probe_recovery_keeps_a_manual_pause(self):
        xs.write_scope_pause("backend:agy", None, "operator hold", now=self.now)
        xs.write_scope_pause("backend:codex", 60, "auto:quota:M5:429", now=self.now)
        self.assertFalse(xs.resume_quota_scope("backend:agy"))
        self.assertIsNotNone(self.record("backend:agy"))
        self.assertTrue(xs.resume_quota_scope("backend:codex"))
        self.assertIsNone(self.record("backend:codex"))

    def test_probe_recovery_resumes_the_automatic_pause(self):
        xs.apply_backend_quota_policy(self.lane(backend="agy"), self.attempts("agy", AGY_429), policy=self.policy)
        self.assertIsNotNone(self.record("backend:agy"))
        self.assertTrue(xs.resume_quota_scope("backend:agy"))
        self.assertIsNone(self.record("backend:agy"))
        self.assertEqual(xs.pause_records(self.now), [])


class QuotaPauseLaneTests(unittest.TestCase):
    """A lane run that ends with a quota error pauses the scope (Refs #1368).

    The end to end path of the policy: the run fails with a quota error, the
    lane supervisor writes the scoped pause record of the pause store, and the
    record expires with the cooldown of the policy.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-quota-lane-", dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["config", "user.name", "Quota Test"],
                     ["config", "user.email", "quota@example.invalid"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "README.md").write_text("base\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.repo, check=True, capture_output=True)
        (self.data / "lanes").mkdir(parents=True, exist_ok=True)
        (self.data / "lanes.json").write_text(json.dumps({"lanes": [{
            "name": "L1", "backend": "codex", "model": "fake-model", "effort": "xhigh",
            "repo": str(self.repo), "binary": str(FIXTURES / "fake_app_server.py")}]}))
        # The quota policy of the backend under test; the retry backoff is off
        # so the failing run ends at once.
        (self.data / "policy.local.json").write_text(json.dumps({
            "rate_limit": {"backoff_seconds": [0], "max_retries": 1},
            "backends": {"codex": {"quota_error_re": "402", "quota_cooldown_s": 90,
                                   "scope": "backend"}}}), encoding="utf-8")
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.data))
        self.addCleanup(self.lane_down)

    def cli(self, *args, timeout=45):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def lane_down(self):
        try:
            self.cli("lane", "down", timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def wait_run(self, run_id, timeout=30):
        deadline = time.monotonic() + timeout
        path = self.data / "runs" / run_id / "status.json"
        while time.monotonic() < deadline:
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = {}
            if state.get("state") == "finished":
                return state
            time.sleep(0.05)
        self.fail(f"the run {run_id} did not finish")

    def test_quota_error_pauses_the_backend_scope(self):
        self.assertEqual(self.cli("lane", "up", "L1").returncode, 0)
        assigned = self.cli("lane", "assign", "L1", "--prompt", "LANE_TEST_QUOTA the plan is exhausted")
        self.assertEqual(assigned.returncode, 0, assigned.stderr)
        run_id = json.loads(assigned.stdout)["id"]
        self.wait_run(run_id)
        record = json.loads((self.data / "pauses" / "backend_codex.json").read_text())
        self.assertEqual(record["scope"], "backend:codex")
        self.assertTrue(record["reason"].startswith("auto:quota:"))
        self.assertGreater(record["expires_at"], time.time())


if __name__ == "__main__":
    unittest.main()
