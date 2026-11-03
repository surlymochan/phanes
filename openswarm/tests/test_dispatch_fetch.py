"""Dispatch fetch resilience (Refs #1719): per-host retries, the seed push fallback and the
per-host failure attribution that the health output and the blocking diagnostics read."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


E = _load("dispatch_engine_under_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
FP = _load("dispatch_fetch_policy_under_test", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "fetch_policy.py"))

SHA = "a" * 40
TLS_ERROR = "gnutls_handshake: TLS connection was non-properly closed"


class FakeRunner:
    """Answers commands by the first matching substring rule; records every call."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for needle, result in self.rules:
            if needle in text:
                return result(text) if callable(result) else result
        return 0, "", ""

    def called(self, needle):
        return [c for c in self.calls if needle in c]


class PolicyTest(unittest.TestCase):
    """The pure policy: error types, backoff, the fallback threshold and the health rows."""

    def test_tls_failure_is_a_diagnosable_type_not_a_fatal_one(self):
        self.assertEqual(FP.classify_error(TLS_ERROR), "tls")
        self.assertEqual(FP.classify_error("Could not resolve host: github.com"), "dns")
        self.assertEqual(FP.classify_error("fatal: unable to access ...: Connection reset by peer"), "network")
        self.assertEqual(FP.classify_error("fatal: Authentication failed"), "auth")
        self.assertTrue(FP.is_retryable("tls"))
        self.assertFalse(FP.is_retryable("auth"))

    def test_backoff_doubles_and_is_capped(self):
        policy = FP.policy_for({"backoff_s": 5, "backoff_cap_s": 20})
        self.assertEqual([FP.backoff_seconds(n, policy) for n in (1, 2, 3, 4)], [5, 10, 20, 20])

    def test_per_host_policy_overrides_the_defaults(self):
        policy = FP.policy_for({"attempts": 3, "hosts": {"govpn": {"attempts": 5, "fallback_after": 1}}}, "govpn")
        self.assertEqual(policy["attempts"], 5)
        self.assertEqual(policy["fallback_after"], 1)
        self.assertEqual(FP.policy_for({"attempts": 3}, "gores1")["attempts"], 3)

    def test_fallback_threshold(self):
        policy = FP.policy_for({"fallback_after": 3})
        self.assertFalse(FP.should_fallback(2, policy))
        self.assertTrue(FP.should_fallback(3, policy))
        self.assertFalse(FP.should_fallback(9, FP.policy_for({"fallback_after": 0})))

    def test_health_rows_count_the_recent_failures_and_the_main_error_type(self):
        events = {}
        FP.record_event(events, "govpn", "fetch_main", TLS_ERROR, now=1000)
        FP.record_event(events, "govpn", "fetch_main", TLS_ERROR, now=1010)
        FP.record_event(events, "govpn", "seed_push_fallback", TLS_ERROR, "tls", now=1020, fallback=True)
        FP.record_event(events, "ib1", "fetch_main", "Connection reset by peer", now=1025)
        rows = FP.health_rows(events, now=1030, window_s=1800)
        self.assertEqual(rows["govpn"]["failures"], 2)
        self.assertEqual(rows["govpn"]["types"], {"tls": 2})
        self.assertEqual(rows["govpn"]["last_type"], "tls")
        self.assertEqual(rows["govpn"]["fallbacks"], 1)
        self.assertEqual(rows["ib1"]["failures"], 1)
        self.assertIn("tls", FP.summarize(rows["govpn"]))

    def test_the_window_drops_the_old_failures(self):
        events = {}
        FP.record_event(events, "govpn", "fetch_main", TLS_ERROR, now=0)
        policy = FP.policy_for({"health_window_s": 60})
        self.assertEqual(FP.prune_events(events, policy, now=30)["govpn"], events["govpn"])
        self.assertEqual(FP.prune_events(events, policy, now=600)["govpn"], [])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-git-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"],
               "fetch": {"attempts": 3, "backoff_s": 5, "fallback_after": 3}}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as stream:
            json.dump(raw, stream)
        self.cfg = E.load_config(path)
        self.sleeps = []

    def make(self, rules=None, fetch=None):
        if fetch is not None:
            self.cfg["fetch"] = fetch
        self.runner = FakeRunner(rules)
        dispatcher = E.Dispatcher(self.cfg, runner=self.runner, clock=lambda: 1000.0,
                                  sleep=self.sleeps.append, out=open(os.devnull, "w"))
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    @staticmethod
    def item(n=1, body=""):
        return {"n": n, "title": f"card {n}", "body": body, "backend": None, "strict": False, "prio": 1}

    @staticmethod
    def lane(host="govpn", **extra):
        lane = {"host": host, "backend": "codex"}
        lane.update(extra)
        return lane


class DispatchFetchTest(Base):
    def test_tls_failure_falls_back_to_the_seed_push_and_the_run_starts(self):
        """The acceptance scenario: a govpn-class TLS failure must not kill the dispatch."""
        def host_fetch_fails(text):
            if "fetch" in text and "ssh" in text:
                return 1, "", TLS_ERROR
            return 0, "", ""

        d = self.make([("fetch", host_fetch_fails),
                       ("lane assign", (0, '{"id": "run-7"}', "")),
                       ("cat-file -e", (0, "", "")),
                       ("push", (0, "", ""))])
        run, _ = d.dispatch("M5", self.lane(), self.item(), SHA)
        self.assertEqual(run, "run-7")
        # the host fetch was retried with the policy backoff before the fallback
        self.assertEqual(self.sleeps, [5, 10])
        # the seed push path took over: the box pushed the base into the host's repo
        self.assertTrue(d.runner.called("push"))
        # and the base validation is unchanged: the assign still checks the base commit
        assign = d.runner.called("lane assign")[0]
        self.assertIn("cat-file -e", assign)
        self.assertIn(SHA + "^{commit}", assign)

    def test_the_fallback_decision_is_logged(self):
        logs = []

        def host_fetch_fails(text):
            if "fetch" in text and "ssh" in text:
                return 1, "", TLS_ERROR
            return 0, "", ""

        d = self.make([("fetch", host_fetch_fails), ("lane assign", (0, '{"id": "run-7"}', ""))])
        d.log = lambda msg, brain=True: logs.append(msg)
        d.dispatch("M5", self.lane(), self.item(), SHA)
        self.assertTrue(any("falling back to the seed push path" in m for m in logs), logs)

    def test_a_fetch_failure_below_the_threshold_is_not_silently_retried_forever(self):
        d = self.make([("fetch", (1, "", TLS_ERROR))], fetch={"attempts": 3, "fallback_after": 9})
        with self.assertRaises(E.DispatchFetchError) as raised:
            d.dispatch("M5", self.lane(), self.item(), SHA)
        self.assertEqual(raised.exception.error_type, "tls")
        self.assertNotIn("lane assign", " ".join(d.runner.calls))

    def test_an_auth_failure_is_not_retried(self):
        d = self.make([("fetch", (1, "", "fatal: Authentication failed for 'https://github.com'"))],
                      fetch={"attempts": 3, "fallback_after": 9})
        with self.assertRaises(E.DispatchFetchError):
            d.dispatch("M5", self.lane(), self.item(), SHA)
        self.assertEqual(self.sleeps, [])  # auth failures are not retried
        self.assertEqual(len(d.runner.called("fetch")), 1)

    def test_the_pr_head_fetch_runs_through_the_same_policy(self):
        def host_fetch_fails(text):
            if "pull/42/head" in text:
                return 1, "", TLS_ERROR
            return 0, "", ""

        d = self.make([("fetch", host_fetch_fails), ("lane assign", (0, '{"id": "run-9"}', "")),
                       ("cat-file -e", (0, "", "")), ("push", (0, "", ""))])
        d.dispatch("M5", self.lane(), self.item(body="Rebase-PR: #42 fleet/M5-issue1-r1"), SHA)
        self.assertTrue(d.runner.called("pull/42/head"))
        self.assertTrue(d.runner.called("rebase-pr-42"))

    def test_the_failures_are_attributed_to_the_host(self):
        d = self.make([("fetch", (1, "", TLS_ERROR)), ("lane assign", (0, '{"id": "run-7"}', ""))],
                      fetch={"attempts": 3, "fallback_after": 9})
        with self.assertRaises(E.DispatchFetchError):
            d.dispatch("M5", self.lane(), self.item(), SHA)
        row = d.fetch_health()["govpn"]
        self.assertEqual(row["failures"], 3)
        self.assertEqual(row["last_type"], "tls")
        self.assertEqual(row["types"], {"tls": 3})

    def test_a_fetch_failure_is_not_an_assign_failure(self):
        d = self.make([("fetch", (1, "", TLS_ERROR))], fetch={"attempts": 3, "fallback_after": 9})
        with self.assertRaises(E.DispatchFetchError):
            d.dispatch("M5", self.lane(), self.item(), SHA)
        d2 = self.make([("fetch", (0, "", "")), ("lane assign", (1, "", "lane assign failed: no runner"))])
        with self.assertRaises(RuntimeError) as raised:
            d2.dispatch("M5", self.lane(), self.item(), SHA)
        self.assertNotIsInstance(raised.exception, E.DispatchFetchError)


class HealthOutputTest(Base):
    def test_the_health_output_carries_the_per_host_fetch_failures(self):
        d = self.make([("fetch", (1, "", TLS_ERROR))], fetch={"attempts": 3, "fallback_after": 9})
        with self.assertRaises(E.DispatchFetchError):
            d.dispatch("M5", self.lane(), self.item(), SHA)
        rows = d.fetch_health()
        self.assertIn("govpn", rows)
        self.assertEqual(rows["govpn"]["failures"], 3)
        self.assertEqual(rows["govpn"]["last_type"], "tls")
        self.assertIn("tls", FP.summarize(rows["govpn"]))

    def test_the_health_output_survives_a_restart(self):
        """The attribution is part of the state: a new dispatcher still sees the recent failures."""
        d = self.make([("fetch", (1, "", TLS_ERROR))], fetch={"attempts": 3, "fallback_after": 9})
        with self.assertRaises(E.DispatchFetchError):
            d.dispatch("M5", self.lane(), self.item(), SHA)
        state = {"_fetch_events": d.fetch_events}
        d2 = self.make()
        d2.fetch_events = {h: list(rows) for h, rows in state["_fetch_events"].items()}
        self.assertEqual(d2.fetch_health()["govpn"]["failures"], 3)


if __name__ == "__main__":
    unittest.main()
