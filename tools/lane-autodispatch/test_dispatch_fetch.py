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

        def fake_sh(command, timeout=120, inp=None):
            rendered = " ".join(command) if not isinstance(command, str) else command
            calls.append(rendered)
            if "cat-file -e" in rendered:
                return 1, "", "fatal: Not a valid object name abc^{commit}"
            if " fetch -q https://github.com/" in rendered:
                fetches = sum(" fetch -q https://github.com/" in call for call in calls)
                if fetches == 1:
                    return 128, "", "fatal: gnutls_handshake() failed: Error in the pull function."
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


if __name__ == "__main__":
    unittest.main()
