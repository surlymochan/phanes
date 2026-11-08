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
                if "cat-file -e" in text:
                    base_checks += 1
                    if base_checks == 1:
                        return 1, "", "fatal: Not a valid object name abc123^{commit}"
                    return 0, '{"id":"run-1719"}', ""
                if " fetch -q https://github.com/" in text:
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

    def test_tls_fetch_falls_back_to_seed_and_dispatches_only_after_base_check(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / ".swrm-tmp") as scratch:
            calls = []
            fetch_attempts = 0
            base_checks = 0

            def fake_sh(command, timeout=None, inp=None):
                nonlocal fetch_attempts, base_checks
                text = " ".join(command) if isinstance(command, (list, tuple)) else command
                calls.append(text)
                if "cat > /tmp/task-CX10.md" in text:
                    return 0, "", ""
                if "cat-file -e" in text:
                    base_checks += 1
                    if base_checks == 1:
                        return 1, "", "fatal: Not a valid object name abc123^{commit}"
                    return 0, '{"id":"run-seed-fallback"}', ""
                if " fetch -q https://github.com/" in text:
                    fetch_attempts += 1
                    return 1, "", "gnutls_handshake() failed: Error in the pull function"
                if " fetch -q ssh://seed/phanes.git " in text:
                    return 0, "", ""
                raise AssertionError(f"unexpected dispatch command: {text}")

            with mock.patch.object(AUTODISPATCH, "D", scratch), \
                 mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
                 mock.patch.object(AUTODISPATCH, "DISPATCH_FETCH_RETRIES", 3), \
                 mock.patch.object(AUTODISPATCH, "DISPATCH_FETCH_FALLBACK_AFTER", 2), \
                 mock.patch.dict("os.environ", {"AD_BASE_FETCH_MIRROR": "ssh://seed/phanes.git"}), \
                 mock.patch.object(AUTODISPATCH.time, "sleep"), \
                 mock.patch.object(AUTODISPATCH, "log"):
                run, verify = AUTODISPATCH.dispatch(
                    "CX10", {"host": "govpn"}, {"n": 1719, "title": "fetch resilience", "body": ""}, "abc123")

            self.assertEqual((run, verify), ("run-seed-fallback", None))
            self.assertEqual(fetch_attempts, 2, "mirror fallback should begin at its configured failure threshold")
            self.assertEqual(base_checks, 2, "the fetched base must be verified before lane assignment")
            self.assertTrue(any("ssh://seed/phanes.git" in command for command in calls))
            self.assertIn("lane assign", calls[-1])

    def test_fetch_failure_health_is_recent_and_classified(self):
        AUTODISPATCH.DISPATCH_FETCH_FAILURES.clear()
        with mock.patch.object(AUTODISPATCH.time, "time", return_value=1000):
            AUTODISPATCH.record_dispatch_fetch_failure("govpn", "gnutls_handshake failed")
            health = AUTODISPATCH.dispatch_fetch_health(now=1000)
        self.assertEqual(health["govpn"]["recent_failures"], 1)
        self.assertEqual(health["govpn"]["primary_error"], "tls")
        self.assertEqual(AUTODISPATCH.dispatch_fetch_health(now=4601), {})

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

    def test_failed_fetch_uses_configured_mirror_after_retry_threshold(self):
        commands = []

        def fake_sh(command, timeout=None, inp=None):
            text = " ".join(command) if isinstance(command, (list, tuple)) else command
            commands.append(text)
            return (1, "", "gnutls_handshake failed") if "github.com" in text else (0, "", "")

        with mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
             mock.patch.object(AUTODISPATCH, "log"), \
             mock.patch.object(AUTODISPATCH, "DISPATCH_FETCH_RETRIES", 2), \
             mock.patch.object(AUTODISPATCH, "DISPATCH_FETCH_FALLBACK_AFTER", 2), \
             mock.patch.dict("os.environ", {"AD_BASE_FETCH_MIRROR": "ssh://seed/phanes.git"}), \
             mock.patch.object(AUTODISPATCH.time, "sleep"):
            AUTODISPATCH.dispatch_base_fetch("govpn", "~/repo", "abc123")

        self.assertEqual(sum("github.com" in command for command in commands), 2)
        self.assertTrue(any("ssh://seed/phanes.git" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
