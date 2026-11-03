#!/usr/bin/env python3
"""Fetch resilience of the box autodispatcher (Refs #1719): the retry/backoff policy, the seed
push fallback, the base validation after the fallback and the per-host failure attribution."""
import importlib.util
import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

SCRATCH = Path(__file__).resolve().parents[2] / ".openswarm-tmp"  # gitignored scratch root
SHA = "a" * 40
TLS_ERROR = "gnutls_handshake: TLS connection was non-properly closed"
POLICY = {"attempts": 3, "backoff_s": 5, "fallback_after": 3, "health_window_s": 1800}


class PolicyTest(unittest.TestCase):
    """The pure policy: error types, backoff, the fallback threshold and the health rows."""

    def test_tls_failure_is_a_diagnosable_type_not_a_fatal_one(self):
        self.assertEqual(AUTODISPATCH.classify_fetch_error(TLS_ERROR), "tls")
        self.assertEqual(AUTODISPATCH.classify_fetch_error("Could not resolve host: github.com"), "dns")
        self.assertEqual(AUTODISPATCH.classify_fetch_error("fatal: unable to access ...: Connection reset by peer"),
                         "network")
        self.assertEqual(AUTODISPATCH.classify_fetch_error("fatal: Authentication failed"), "auth")
        self.assertTrue(AUTODISPATCH.retryable_fetch_error("tls"))
        self.assertFalse(AUTODISPATCH.retryable_fetch_error("auth"))

    def test_backoff_doubles_and_is_capped(self):
        policy = AUTODISPATCH.fetch_policy(None)
        policy.update({"backoff_s": 5, "backoff_cap_s": 20})
        self.assertEqual([AUTODISPATCH.fetch_backoff_seconds(n, policy) for n in (1, 2, 3, 4)], [5, 10, 20, 20])

    def test_the_fallback_threshold(self):
        policy = dict(POLICY)
        self.assertFalse(AUTODISPATCH.fetch_should_fallback(2, policy))
        self.assertTrue(AUTODISPATCH.fetch_should_fallback(3, policy))
        self.assertFalse(AUTODISPATCH.fetch_should_fallback(3, dict(POLICY, fallback_after=0)))

    def test_the_per_host_policy_overrides_the_defaults(self):
        with mock.patch.dict(os.environ, {"AD_FETCH_POLICY": json.dumps(
                {"attempts": 3, "hosts": {"govpn": {"attempts": 5, "fallback_after": 1}}})}):
            self.assertEqual(AUTODISPATCH.fetch_policy("govpn")["attempts"], 5)
            self.assertEqual(AUTODISPATCH.fetch_policy("govpn")["fallback_after"], 1)
            self.assertEqual(AUTODISPATCH.fetch_policy("gores1")["attempts"], 3)


class DispatchFetchTest(unittest.TestCase):
    """The dispatch path: a TLS failure must not kill the run (the seed push takes over)."""

    LANES = {"CX10": {"host": "govpn", "backend": "codex", "repo": "~/work/phanes-fleet"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH)) if SCRATCH.exists() else tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.logs, self.sleeps = [], [], []
        self.checks = 0
        env = mock.patch.dict(os.environ, {"AD_FETCH_POLICY": json.dumps(POLICY)})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("D", self.tmp.name), ("sh", self.fake_sh), ("log", self.log),
                            ("gh", lambda *a, **k: (0, "", ""))):
            self.patch(name, value)
        sleep = mock.patch.object(AUTODISPATCH.time, "sleep", self.sleeps.append)
        sleep.start()
        self.addCleanup(sleep.stop)
        warnings.filterwarnings("ignore", category=ResourceWarning)
        self.addCleanup(warnings.resetwarnings)

    def log(self, msg, brain=True):
        self.logs.append(msg)

    def patch(self, name, value):
        old = getattr(AUTODISPATCH, name)
        setattr(AUTODISPATCH, name, value)
        self.addCleanup(lambda: setattr(AUTODISPATCH, name, old))
        AUTODISPATCH.FETCH_EVENTS.clear()

    def fake_sh(self, cmd, timeout=120, inp=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        if "cat > /tmp/task-" in text:
            return 0, "", ""
        if "cat-file -e" in text:
            # the first check finds no base in the host's repo; after the seed push it is there
            self.checks += 1
            if self.checks == 1:
                return 1, "", f"fatal: Not a valid object name {SHA}^{{commit}}"
            return 0, '{"id": "run-3"}', ""
        if "fetch" in text and "ssh" in text:
            return 1, "", TLS_ERROR
        if "fetch" in text:
            return 0, "", ""       # the box-side fetch of the seed push
        if "push" in text:
            return 0, "", ""       # the seed push into the host's repo
        return 0, "", ""

    def item(self, n=7):
        return {"n": n, "title": "card", "body": "验收: `true`"}

    def test_a_tls_failure_falls_back_to_the_seed_push_and_the_run_starts(self):
        # the task file of the dispatch is written without closing it: the warning is pre-existing
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            run, _ = AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        self.assertEqual(run, "run-3")
        # the host fetch was retried with the policy backoff before the fallback
        self.assertEqual(self.sleeps[:2], [5, 10])
        # the seed push carried the base into the host's repo
        self.assertTrue(any("push" in c and "refs/ad/base" in c for c in self.calls))
        # the base validation is unchanged: the assign still checks the base commit
        assign = [c for c in self.calls if "cat-file -e" in c][-1]
        self.assertIn(f"{SHA}^{{commit}}", assign)
        self.assertIn("lane assign", assign)

    def test_the_fallback_decision_is_logged(self):
        AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        self.assertTrue(any("falling back to the seed push path" in m for m in self.logs), self.logs)

    def test_the_failures_are_attributed_to_the_host(self):
        AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        row = AUTODISPATCH.dispatch_fetch_health()["govpn"]
        self.assertEqual(row["failures"], 3)
        self.assertEqual(row["types"], {"tls": 3})
        self.assertEqual(row["last_type"], "tls")
        self.assertEqual(row["fallbacks"], 1)
        self.assertIn("tls", AUTODISPATCH.fetch_health_summary(row))

    def test_a_fetch_failure_below_the_threshold_is_a_fetch_error(self):
        with mock.patch.dict(os.environ, {"AD_FETCH_POLICY": json.dumps(dict(POLICY, fallback_after=9))}):
            with self.assertRaises(AUTODISPATCH.FetchError) as raised:
                AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        self.assertEqual(raised.exception.error_type, "tls")
        # a fetch failure is its own class, not the generic assign failure
        self.assertIs(type(raised.exception), AUTODISPATCH.FetchError)

    def test_an_assign_failure_stays_an_assign_failure(self):
        def failing_assign(cmd, timeout=120, inp=None):
            text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
            self.calls.append(text)
            if "cat > /tmp/task-" in text:
                return 0, "", ""
            return 1, "", "lane assign failed: no runner"
        self.patch("sh", failing_assign)
        with self.assertRaises(RuntimeError) as raised:
            AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        self.assertNotIsInstance(raised.exception, AUTODISPATCH.FetchError)

    def test_the_health_output_carries_the_per_host_fetch_failures(self):
        AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        status = AUTODISPATCH.status_snapshot(self.LANES, {}, {"CX10": {"lane": "idle"}}, 1, 0)
        self.assertIn("govpn", status["_fetch_health"])
        self.assertEqual(status["_fetch_health"]["govpn"]["failures"], 3)

    def test_the_attribution_survives_a_restart(self):
        AUTODISPATCH.dispatch("CX10", self.LANES["CX10"], self.item(), SHA)
        AUTODISPATCH.save_fetch_health()
        AUTODISPATCH.FETCH_EVENTS.clear()
        AUTODISPATCH.load_fetch_health()
        self.assertEqual(AUTODISPATCH.dispatch_fetch_health()["govpn"]["failures"], 3)


if __name__ == "__main__":
    unittest.main()
