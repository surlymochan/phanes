#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)

class DispatchFetchTests(unittest.TestCase):
    def setUp(self):
        AD.FETCH_FAILURES.clear()

    def test_tls_failure_is_classified_and_exposed_in_health(self):
        self.assertEqual(AD.record_dispatch_fetch_failure("govpn", "gnutls_handshake() failed", now=10), "tls")
        self.assertEqual(AD.dispatch_fetch_health(now=11), {"govpn": {"failures_24h": 1, "primary_error": "tls"}})

    def test_health_expires_old_failures(self):
        AD.record_dispatch_fetch_failure("h", "fatal: bad ref", now=1)
        self.assertEqual(AD.dispatch_fetch_health(now=AD.FETCH_HEALTH_WINDOW + 2), {})

    def test_retry_backoff_then_fallback(self):
        calls = []
        original_sh, original_sleep, original_log = AD.sh, AD.time.sleep, AD.log
        def fake_sh(command, timeout=120, inp=None):
            calls.append(command)
            return (1, "", "gnutls handshake failed") if command == "fetch" else (0, "", "")
        try:
            AD.sh, AD.time.sleep, AD.log = fake_sh, lambda seconds: calls.append(seconds), lambda *args, **kwargs: None
            ok, _, _ = AD.dispatch_fetch("govpn", "fetch", attempts=2, fallback="seed")
        finally:
            AD.sh, AD.time.sleep, AD.log = original_sh, original_sleep, original_log
        self.assertTrue(ok)
        self.assertEqual(calls, ["fetch", 5, "fetch", "seed"])
        self.assertEqual(AD.dispatch_fetch_health()["govpn"]["primary_error"], "tls")

if __name__ == "__main__": unittest.main()
