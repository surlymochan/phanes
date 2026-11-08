#!/usr/bin/env python3
"""Regression tests for dispatch-time fetch resilience (Refs #1719)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class DispatchFetchResilienceTest(unittest.TestCase):
    def test_tls_fetch_retries_then_revalidates_base_before_assign(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".swrm-tmp") as scratch:
            calls = []
            fetch_attempts = 0
            base_checks = 0

            def fake_sh(command, timeout=None, inp=None):
                nonlocal fetch_attempts, base_checks
                calls.append(command)
                text = " ".join(command) if isinstance(command, (list, tuple)) else command
                if "cat > /tmp/task-CX10.md" in text:
                    return 0, "", ""
                if command[0] == "ssh" and "cat-file -e" in text:
                    base_checks += 1
                    if base_checks == 1:
                        return 1, "", "fatal: Not a valid object name abc123^{commit}"
                    return 0, '{"id":"run-1719"}', ""
                if command[0] == "ssh" and " fetch -q https://github.com/" in text:
                    fetch_attempts += 1
                    if fetch_attempts < 3:
                        return 1, "", "gnutls_handshake() failed: Error in the pull function"
                    return 0, "", ""
                raise AssertionError(f"unexpected dispatch command: {text}")

            with mock.patch.object(AUTODISPATCH, "D", scratch), \
                 mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
                 mock.patch.object(AUTODISPATCH.time, "sleep"):
                run, verify = AUTODISPATCH.dispatch(
                    "CX10", {"host": "govpn"}, {"n": 1719, "title": "fetch resilience", "body": ""}, "abc123")

            self.assertEqual((run, verify), ("run-1719", None))
            self.assertEqual(fetch_attempts, 3, "TLS failure should retry before dispatch continues")
            self.assertEqual(base_checks, 2, "base must be checked again after the successful fetch")
            self.assertIn("lane assign", " ".join(calls[-1]))

    def setUp(self):
        AUTODISPATCH.DISPATCH_FETCH_FAILURES.clear()
        self.scratch = Path(__file__).resolve().parents[2] / ".swrm-tmp"
        self.scratch.mkdir(exist_ok=True)

    def test_controller_seed_push_retries_then_starts_run(self):
        self.run_seed_scenario()

    def test_seed_success_with_missing_base_cannot_start_run(self):
        self.run_seed_scenario(missing=True)

    def test_seed_push_exhaustion_cannot_start_run(self):
        self.run_seed_scenario(push_fails=True)

    def test_pr_head_uses_same_policy_and_seeds_both_head_and_base(self):
        self.run_seed_scenario(pr=1749)

    def run_seed_scenario(self, missing=False, push_fails=False, pr=None):
        with tempfile.TemporaryDirectory(dir=self.scratch) as scratch:
            commands = []
            checks = 0
            pushes = 0
            seeded = False

            def fake_sh(command, timeout=None, inp=None):
                nonlocal checks, pushes, seeded
                text = " ".join(command)
                commands.append(text if command[0] == "ssh" else "local: " + text)
                if "cat > /tmp/task-" in text:
                    return 0, "", ""
                if command[0] == "ssh" and "cat-file -e" in text:
                    checks += 1
                    if missing or not seeded:
                        return 1, "", "fatal: Not a valid object name abc123^{commit}"
                    return 0, '{"id":"run-seeded"}', ""
                if command[0] == "ssh" and " fetch -q https://github.com/" in text:
                    return 1, "", "gnutls_handshake failed"
                if command[:2] == ["git", "init"]:
                    return 0, "", ""
                if "core.sshCommand=" in text and "push" in command:
                    pushes += 1
                    if pushes == 1 or push_fails:
                        return 1, "", "connection reset"
                    seeded = True
                    return 0, "", ""
                if command[0] == "git" and "fetch" in command:
                    return 0, "", ""
                if command[0] == "git" and "cat-file" in command:
                    return 0, "", ""
                raise AssertionError(text)

            cfg = {"host": "govpn", "fetch_policy": {"retries": 3, "delay": 0.25, "fallback_after": 2}}
            item = {"n": 1719, "title": "TLS regression", "body": ""}
            if pr:
                item["body"] = f"重基PR：#{pr}"
            with mock.patch.object(AUTODISPATCH, "D", scratch), \
                 mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
                 mock.patch.object(AUTODISPATCH, "log") as log, \
                 mock.patch.dict("os.environ", {"AD_BASE_FETCH_MIRROR": ""}), \
                 mock.patch.object(AUTODISPATCH.time, "sleep") as sleep:
                if missing or push_fails:
                    with self.assertRaises(RuntimeError):
                        AUTODISPATCH.dispatch("CX10", cfg, item, "abc123")
                else:
                    self.assertEqual(AUTODISPATCH.dispatch("CX10", cfg, item, "abc123"), ("run-seeded", None))
                self.assertIn(mock.call(0.25), sleep.call_args_list)
                self.assertTrue(any("controller seed push" in call.args[0] for call in log.call_args_list))
            direct = [text for text in commands if not text.startswith("local:") and " fetch -q https://github.com/" in text]
            if not missing:
                self.assertEqual(len(direct), 2)
            self.assertGreaterEqual(pushes, 2)
            self.assertTrue(any("abc123:refs/swrm-seed/" in text for text in commands))
            if pr:
                self.assertTrue(any("refs/pull/1749/head:refs/seed/head" in text for text in commands))
                self.assertTrue(any("refs/seed/head:refs/swrm-seed/" in text for text in commands))
            if not (missing or push_fails):
                self.assertEqual(checks, 1 if pr else 2)
                self.assertIn("lane assign", commands[-1])
            health = AUTODISPATCH.dispatch_fetch_health()
            self.assertGreaterEqual(health["govpn"]["recent_failures"], 3)
            self.assertIn(health["govpn"]["primary_error"], {"tls", "network"})
            status = AUTODISPATCH.status_snapshot({"CX10": cfg}, {}, {"CX10": {}}, 0, 0)
            self.assertEqual(status["_dispatch_fetch_failures"], health)

    def test_fetch_failure_health_is_recent_and_classified(self):
        with mock.patch.object(AUTODISPATCH.time, "time", return_value=1000):
            AUTODISPATCH.record_dispatch_fetch_failure("govpn", "gnutls_handshake failed")
        self.assertEqual(AUTODISPATCH.dispatch_fetch_health(now=1000)["govpn"]["primary_error"], "tls")
        self.assertEqual(AUTODISPATCH.dispatch_fetch_health(now=4601), {})

    def test_host_policy_override_and_exponential_backoff(self):
        with mock.patch.dict("os.environ", {"AD_FETCH_HOST_POLICIES": json.dumps({
                "govpn": {"retries": 4, "delay": 2, "fallback_after": 3}})}), \
             mock.patch.object(AUTODISPATCH, "sh", return_value=(1, "", "gnutls_handshake failed")), \
             mock.patch.object(AUTODISPATCH, "log"), \
             mock.patch.object(AUTODISPATCH.time, "sleep") as sleep:
            policy = AUTODISPATCH.dispatch_fetch_policy("govpn")
            self.assertEqual(policy, {"retries": 4, "delay": 2, "fallback_after": 3})
            self.assertEqual(AUTODISPATCH.dispatch_fetch_policy("govpn", {"fetch_policy": {"delay": 1}})["delay"], 1)
            fallback = mock.Mock()
            AUTODISPATCH.dispatch_git_network("govpn", ["fetch"], "PR head fetch", policy, fallback)
            fallback.assert_called_once()
            self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])
            with self.assertRaisesRegex(RuntimeError, "seed push"):
                AUTODISPATCH.dispatch_git_network("govpn", ["push"], "seed push", policy)

    def test_assign_failure_does_not_count_as_fetch_failure(self):
        with tempfile.TemporaryDirectory(dir=self.scratch) as scratch, \
             mock.patch.object(AUTODISPATCH, "D", scratch), \
             mock.patch.object(AUTODISPATCH, "sh", side_effect=[(0, "", ""), (1, "", "assign failed")]):
            with self.assertRaisesRegex(RuntimeError, "assign failed"):
                AUTODISPATCH.dispatch("CX10", {"host": "govpn"}, {"n": 1719, "title": "x", "body": ""}, "abc123")
        self.assertEqual(AUTODISPATCH.dispatch_fetch_health(), {})

    def test_tls_harvest_failure_retries_and_uses_seed_fallback(self):
        state = {}
        attempts = []

        def fake_sh(command, timeout=None, inp=None):
            attempts.append(command)
            return (0, "", "") if command[-1] == "seed" else (1, "", "gnutls_handshake failed")

        AUTODISPATCH.DISPATCH_FETCH_FAILURES.clear()
        with mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
             mock.patch.object(AUTODISPATCH, "HARVEST_FETCH_MAX_ATTEMPTS", 3), \
             mock.patch.object(AUTODISPATCH, "HARVEST_FETCH_RETRY_DELAY", 0), \
             mock.patch.object(AUTODISPATCH.time, "sleep"), \
             mock.patch.object(AUTODISPATCH, "log"):
            ok, err, failures = AUTODISPATCH.harvest_fetch(
                "CX10", 1719, ["git", "fetch", "executor"], state, "govpn", ["git", "fetch", "seed"])

        self.assertTrue(ok)
        self.assertEqual((err, failures), ("", 0))
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[-1][-1], "seed")
        self.assertEqual(AUTODISPATCH.dispatch_fetch_health()["govpn"]["recent_failures"], 3)

    def test_tls_errors_are_retryable(self):
        self.assertEqual(AUTODISPATCH.classify_harvest_fetch_error("gnutls_handshake failed"), "transient")


if __name__ == "__main__":
    unittest.main()
