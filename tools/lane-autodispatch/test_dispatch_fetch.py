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


if __name__ == "__main__":
    unittest.main()
