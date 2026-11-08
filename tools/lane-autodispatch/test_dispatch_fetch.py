"""Regression tests for resilient dispatch base fetches (Refs #1719).

Run offline with:

    cd tools/lane-autodispatch && python3 -m unittest test_dispatch_fetch -v
"""
from __future__ import annotations

import importlib.util
import subprocess
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
            with self.assertRaisesRegex(RuntimeError, "recent fetch failures=6"):
                module.dispatch("GH2", cfg, item, "abc")

        self.assertEqual(sleeps[:2], [2, 4])
        health = module.dispatch_fetch_health({"GH2": cfg})
        self.assertEqual(health["govpn"]["recent_failures"], 6)

    def run_fallback(self, *, missing_after=False, source_fails=False, push_fails=False,
                     mirror=False, timeouts=False):
        module = load_module()
        cfg = {"host": "govpn", "repo": "/repo", "controller_repo": "/controller",
               "fetch_policy": {"attempts": 3, "delay": 2, "fallback_after": 2}}
        if mirror: cfg["seed_url"] = "https://mirror.invalid/repo"
        calls, sleeps, logs = [], [], []
        counts = {"direct": 0, "source": 0, "push": 0, "mirror": 0, "assign": 0}
        seeded = False

        def transfer_failure(command, timeout, error):
            if timeouts:
                raise subprocess.TimeoutExpired(command, timeout, stderr=error.encode())
            return 128, "", error

        def fake_sh(command, timeout=120, inp=None):
            nonlocal seeded
            rendered = " ".join(command) if isinstance(command, list) else command
            calls.append(command)
            if "cat-file -e" in rendered:
                if seeded and not missing_after:
                    counts["assign"] += 1
                    return 0, '{"id":"seeded-run"}', ""
                return 1, "", "fatal: Not a valid object name abc^{commit}"
            if " push -q " in rendered:
                counts["push"] += 1
                if push_fails or counts["push"] == 1:
                    return transfer_failure(command, timeout, "Connection reset by peer")
                seeded = True
            elif "fetch" in rendered:
                kind = "source" if isinstance(command, list) else "mirror" if "mirror.invalid" in rendered else "direct"
                counts[kind] += 1
                if kind == "direct" or (kind == "source" and source_fails) or counts[kind] == 1:
                    return transfer_failure(command, timeout, "gnutls_handshake() failed")
                if kind == "mirror": seeded = True
            return 0, "", ""

        item = {"n": 1719, "title": "resilient fetch", "body": "", "labels": ["ops"]}
        (MODULE_PATH.parents[2] / ".swrm-tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MODULE_PATH.parents[2] / ".swrm-tmp") as tmp, \
                mock.patch.object(module, "D", tmp), \
                mock.patch.object(module, "sh", side_effect=fake_sh), \
                mock.patch.object(module, "JUMP_HOSTS", {"govpn": "jump-box"}), \
                mock.patch.object(module, "ssh_argv", side_effect=lambda opts, host, cmd: cmd), \
                mock.patch.object(module, "log", side_effect=lambda message, **kw: logs.append(message)), \
                mock.patch.object(module, "gh", return_value=(0, "", "")), \
                mock.patch.object(module.time, "sleep", side_effect=sleeps.append):
            try:
                result = module.dispatch("CX1", cfg, item, "abc")
            except RuntimeError as error:
                result = error
                module.dispatch_failed("CX1", cfg, item, error)
        return module, cfg, result, counts, calls, sleeps, logs

    def test_tls_threshold_falls_back_retries_source_and_push_then_checks_base(self):
        module, cfg, result, counts, calls, sleeps, logs = self.run_fallback()
        self.assertEqual(result, ("seeded-run", None))
        self.assertEqual(counts, {"direct": 2, "source": 2, "push": 2, "mirror": 0, "assign": 1})
        self.assertEqual(sleeps[:3], [2, 2, 2])
        pushes = [c for c in calls if isinstance(c, list) and "push" in c]
        self.assertIn("ProxyCommand=", pushes[0][2])
        self.assertIn("jump-box", pushes[0][2])
        self.assertGreaterEqual(sum("cat-file -e" in c for c in calls if isinstance(c, str)), 2)
        self.assertTrue(any("threshold reached" in line for line in logs))
        self.assertTrue(any("verifying base" in line for line in logs))
        self.assertEqual(module.dispatch_fetch_health({"CX1": cfg})["govpn"]["recent_failures"], 3)
        self.assertEqual(module.DISPATCH_FETCH_FAILURES["controller"][0]["type"], "network_transient")
        self.assertFalse(module.BACKOFF)

    def test_fallback_cannot_assign_when_base_stays_missing(self):
        _, _, result, counts, _, _, _ = self.run_fallback(missing_after=True)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("Not a valid object name", str(result))
        self.assertEqual(counts["assign"], 0)
        self.assertGreater(counts["push"], 0)

    def test_controller_failure_is_attributed_without_target_backoff(self):
        module, cfg, result, counts, _, _, logs = self.run_fallback(source_fails=True)
        self.assertRegex(str(result), "controller source fetch failed")
        self.assertEqual(counts["source"], 3)
        self.assertEqual(counts["push"], 0)
        self.assertEqual(len(module.DISPATCH_FETCH_FAILURES["govpn"]), 2)
        self.assertEqual(len(module.DISPATCH_FETCH_FAILURES["controller"]), 3)
        self.assertFalse(module.BACKOFF)
        self.assertTrue(any("recent=3" in line and "primary_error=network_transient" in line for line in logs))
        self.assertFalse(any("ssh transient error" in line for line in logs))

    def test_seed_push_exhaustion_retries_mirror_and_starts(self):
        _, _, result, counts, _, _, _ = self.run_fallback(push_fails=True, mirror=True)
        self.assertEqual(result, ("seeded-run", None))
        self.assertEqual(counts["push"], 3)
        self.assertEqual(counts["mirror"], 2)

    def test_fetch_blocking_diagnostic_includes_recent_cause(self):
        module, _, result, counts, _, _, logs = self.run_fallback(push_fails=True)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(counts["assign"], 0)
        self.assertIn("govpn", module.BACKOFF)
        self.assertTrue(any("recent=5" in line and "network_transient" in line for line in logs))
        self.assertFalse(any("ssh transient error" in line for line in logs))

    def test_network_timeout_obeys_direct_threshold_backoff_and_health(self):
        module = load_module()
        policy = {"attempts": 4, "delay": 2, "fallback_after": 2}
        timeout = subprocess.TimeoutExpired(["git", "fetch"], 180,
                                            output=b"partial output", stderr=b"partial error\xff")
        with mock.patch.object(module, "sh", side_effect=timeout) as shell, \
                mock.patch.object(module, "log") as log, \
                mock.patch.object(module.time, "sleep") as sleep:
            rc, out, err = module.git_network_operation(
                ["git", "fetch"], "govpn", "dispatch fetch #1719", policy, direct=True)
        self.assertEqual(shell.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "partial output")
        self.assertIn("timed out", err)
        self.assertIn("180", err)
        self.assertIn("partial error", err)
        self.assertEqual(module.dispatch_fetch_health({"CX1": {"host": "govpn"}})["govpn"],
                         {"recent_failures": 2, "primary_error": "network_transient",
                          "window_seconds": 3600})
        self.assertIn("exhausted", log.call_args.args[0])

    def test_network_timeouts_use_seed_fallback_and_preserve_failure_attribution(self):
        scenarios = [({}, True), ({"source_fails": True}, False),
                     ({"push_fails": True}, False),
                     ({"push_fails": True, "mirror": True}, True),
                     ({"missing_after": True}, False)]
        for options, succeeds in scenarios:
            with self.subTest(**options):
                module, cfg, result, counts, calls, sleeps, logs = self.run_fallback(
                    timeouts=True, **options)
                self.assertEqual(counts["direct"], 4 if options.get("missing_after") else 2)
                self.assertEqual(sleeps[0], 2)
                self.assertEqual(counts["assign"], int(succeeds))
                self.assertTrue(any("threshold reached" in line for line in logs))
                health = module.dispatch_fetch_health({"CX1": cfg})
                self.assertEqual(health["govpn"]["primary_error"], "network_transient")
                if succeeds:
                    self.assertEqual(result, ("seeded-run", None))
                    self.assertEqual(counts["push"], 3 if options.get("mirror") else 2)
                    self.assertEqual(counts["source"], 2)
                    self.assertGreaterEqual(sum("cat-file -e" in c for c in calls if isinstance(c, str)), 2)
                    self.assertFalse(module.BACKOFF)
                else:
                    self.assertIsInstance(result, RuntimeError)
                    if options.get("source_fails"):
                        self.assertIn("controller source fetch failed", str(result))
                        self.assertEqual(health["controller"]["recent_failures"], 3)
                        self.assertEqual(health["govpn"]["recent_failures"], 2)
                        self.assertFalse(module.BACKOFF)
                    elif options.get("push_fails"):
                        self.assertIn("dispatch fetch failed", str(result))
                        self.assertEqual(health["govpn"]["recent_failures"], 5)
                        self.assertIn("govpn", module.BACKOFF)
                    else:
                        self.assertIn("Not a valid object name", str(result))

    def test_pr_head_timeout_retries_as_transient_and_retains_health(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                module = load_module()
                cfg = {"host": "govpn", "fetch_policy": {"attempts": 2, "delay": 7}}
                timeout = subprocess.TimeoutExpired(["git", "fetch"], 180)
                state = {}
                with mock.patch.object(module, "sh", side_effect=[timeout, timeout if exhausted else (0, "", "")]) as shell, \
                        mock.patch.object(module, "log"), \
                        mock.patch.object(module.time, "sleep") as sleep:
                    ok, error, failures = module.harvest_fetch("CX1", 1719, ["git", "fetch"], state, cfg)
                self.assertEqual(shell.call_count, 2)
                sleep.assert_called_once_with(7)
                self.assertEqual(ok, not exhausted)
                self.assertEqual(failures, int(exhausted))
                if exhausted:
                    self.assertEqual(module.classify_harvest_fetch_error(error), "transient")
                self.assertEqual(module.dispatch_fetch_health({"CX1": cfg})["govpn"]["recent_failures"],
                                 2 if exhausted else 1)

    def test_pr_head_fetch_uses_host_policy_and_health(self):
        module = load_module()
        cfg = {"host": "govpn", "fetch_policy": {"attempts": 2, "delay": 7}}
        state = {}
        with mock.patch.object(module, "sh", side_effect=[(128, "", "gnutls_handshake failed"),
                                                         (0, "", "")]) as shell, \
                mock.patch.object(module, "log"), \
                mock.patch.object(module.time, "sleep") as sleep:
            self.assertEqual(module.harvest_fetch("CX1", 1719, ["git", "fetch"], state, cfg), (True, "", 0))
        self.assertEqual(shell.call_count, 2)
        sleep.assert_called_once_with(7)
        self.assertEqual(module.dispatch_fetch_health({"CX1": cfg})["govpn"]["recent_failures"], 1)


if __name__ == "__main__":
    unittest.main()
