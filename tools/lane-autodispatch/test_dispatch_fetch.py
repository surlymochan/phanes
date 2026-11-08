"""Regression tests for resilient dispatch base fetches (Refs #1719).

Run offline with:

    cd tools/lane-autodispatch && python3 -m unittest test_dispatch_fetch -v
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("autodispatch.py")


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_fetch_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DispatchFetchTests(unittest.TestCase):
    def test_transient_tls_failure_retries_base_fetch_and_starts_lane(self):
        """A flaky HTTPS fetch should be retried before dispatch is abandoned."""
        module = load_module()
        item = {"n": 1719, "title": "resilient fetch", "body": "", "labels": ["ops"]}
        cfg = {"host": "govpn", "fleet": "swrm-fleet", "repo": "/repo"}
        calls = []
        fetched = False

        def fake_sh(command, timeout=120, inp=None):
            nonlocal fetched
            rendered = " ".join(command) if not isinstance(command, str) else command
            calls.append(rendered)
            if "cat-file -e" in rendered:
                if fetched:
                    return 0, '{"id":"run-1719"}', ""
                return 1, "", "fatal: Not a valid object name abc^{commit}"
            if " fetch -q https://github.com/" in rendered:
                fetches = sum(" fetch -q https://github.com/" in call for call in calls)
                if fetches == 1:
                    return 128, "", "fatal: gnutls_handshake() failed: Error in the pull function."
                fetched = True
                return 0, "", ""
            if "lane assign" in rendered:
                return 0, '{"id":"run-1719"}', ""
            return 0, "", ""

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(module, "D", tmp), \
                mock.patch.object(module, "sh", side_effect=fake_sh), \
                mock.patch.object(module, "ssh_opts", return_value=[]), \
                mock.patch.object(module, "ssh_argv", side_effect=lambda opts, host, cmd: cmd), \
                mock.patch.object(module.time, "sleep"):
            run_id, verify = module.dispatch("GH2", cfg, item, "abc")

        fetch_count = sum(" fetch -q https://github.com/" in call for call in calls)
        self.assertEqual(fetch_count, 2, "transient TLS failure should trigger another fetch")
        self.assertEqual(run_id, "run-1719")
        self.assertIsNone(verify)
        self.assertGreaterEqual(sum("cat-file -e" in call for call in calls), 2,
                                "base must be checked again after fetch succeeds")

    def test_fetch_failures_are_exposed_as_recent_host_health(self):
        module = load_module()
        module.DISPATCH_FETCH_FAILURES.clear()
        module.record_dispatch_fetch_failure("govpn", "gnutls_handshake() failed", at=10000)
        module.record_dispatch_fetch_failure("govpn", "Could not resolve host", at=10001)
        module.record_dispatch_fetch_failure("govpn", "fatal: bad ref", at=1)

        health = module.dispatch_fetch_health({"CX1": {"host": "govpn"}}, now=10002)

        self.assertEqual(health["govpn"]["recent_failures"], 2)
        self.assertEqual(health["govpn"]["primary_error"], "network_transient")
        self.assertEqual(health["govpn"]["window_seconds"], 3600)

    def test_base_fetch_backoff_is_linear_per_retry(self):
        module = load_module()
        item = {"n": 1719, "title": "resilient fetch", "body": "", "labels": ["ops"]}
        cfg = {"host": "govpn", "fleet": "swrm-fleet", "repo": "/repo"}
        sleeps = []

        def failed_sh(command, timeout=120, inp=None):
            rendered = " ".join(command) if not isinstance(command, str) else command
            if "cat-file -e" in rendered:
                return 1, "", "fatal: Not a valid object name abc^{commit}"
            if " fetch -q https://github.com/" in rendered:
                if isinstance(command, list):
                    return 0, "", ""  # controller source is reachable for seed push
                return 128, "", "gnutls_handshake() failed"
            if " push -q " in rendered:
                return 128, "", "ssh: Could not resolve hostname govpn"
            return 0, "", ""

        module.DISPATCH_BASE_FETCH_ATTEMPTS = 3
        module.DISPATCH_BASE_FETCH_DELAY = 2
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(module, "D", tmp), \
                mock.patch.object(module, "sh", side_effect=failed_sh), \
                mock.patch.object(module, "ssh_opts", return_value=[]), \
                mock.patch.object(module, "ssh_argv", side_effect=lambda opts, host, cmd: cmd), \
                mock.patch.object(module.time, "sleep", side_effect=sleeps.append):
            with self.assertRaisesRegex(RuntimeError, "recent fetch failures=4"):
                module.dispatch("GH2", cfg, item, "abc")

        self.assertEqual(sleeps[:2], [2, 4])
        health = module.dispatch_fetch_health({"GH2": cfg})
        self.assertEqual(health["govpn"]["recent_failures"], 4)


if __name__ == "__main__":
    unittest.main()
