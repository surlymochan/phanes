#!/usr/bin/env python3
"""Regression tests for dispatch-time fetch resilience (Refs #1719)."""
import importlib.util
import json
import subprocess
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
        AUTODISPATCH.BACKOFF.clear()
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

    def test_fetch_and_seed_push_timeouts_still_start_run(self):
        self.run_seed_scenario(network_timeout=True)

    def test_pr_head_timeout_uses_seed_fallback(self):
        self.run_seed_scenario(pr=1749, network_timeout=True)

    def test_pr_head_seed_push_failure_cannot_start_run(self):
        self.run_seed_scenario(pr=1749, push_fails=True)

    def run_seed_scenario(self, missing=False, push_fails=False, pr=None, network_timeout=False):
        with tempfile.TemporaryDirectory(dir=self.scratch) as scratch:
            commands = []
            checks = 0
            pushes = 0
            seeded = False
            task = ""

            def fake_sh(command, timeout=None, inp=None):
                nonlocal checks, pushes, seeded, task
                text = " ".join(command)
                commands.append(text if command[0] == "ssh" else "local: " + text)
                if "cat > /tmp/task-" in text:
                    task = inp
                    return 0, "", ""
                if command[0] == "ssh" and "cat-file -e" in text:
                    checks += 1
                    if missing or not seeded:
                        return 1, "", "fatal: Not a valid object name abc123^{commit}"
                    return 0, '{"id":"run-seeded"}', ""
                if command[0] == "ssh" and " fetch -q https://github.com/" in text:
                    if network_timeout:
                        raise subprocess.TimeoutExpired(command, timeout, stderr=b"partial fetch output")
                    return 1, "", "gnutls_handshake failed"
                if command[:2] == ["git", "init"]:
                    return 0, "", ""
                if "core.sshCommand=" in text and "push" in command:
                    pushes += 1
                    if network_timeout and pushes == 1:
                        raise subprocess.TimeoutExpired(command, timeout)
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
                self.assertTrue(any("+refs/seed/head:refs/heads/rebase-pr-1749" in text for text in commands))
                self.assertIn("rebase-pr-1749", task)
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

    def test_network_timeout_exhaustion_is_attributed(self):
        policy = {"retries": 3, "delay": 0.25, "fallback_after": 2}
        for operation in ("seed fetch", "seed push"):
            with self.subTest(operation=operation), \
                 mock.patch.object(AUTODISPATCH, "sh", side_effect=subprocess.TimeoutExpired(["git"], 180)) as shell, \
                 mock.patch.object(AUTODISPATCH, "log"), \
                 mock.patch.object(AUTODISPATCH.time, "sleep") as sleep:
                AUTODISPATCH.DISPATCH_FETCH_FAILURES.clear()
                with self.assertRaisesRegex(RuntimeError, f"{operation}.*timed out"):
                    AUTODISPATCH.dispatch_git_network("govpn", ["git"], operation, policy)
                self.assertEqual(shell.call_count, 3)
                self.assertEqual(sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])
                health = AUTODISPATCH.dispatch_fetch_health()["govpn"]
                self.assertEqual(health["recent_failures"], 3)
                self.assertEqual(health["primary_error"], "network")

    def test_tick_base_lookup_retries_then_dispatches(self):
        self.run_base_lookup_scenario("retry")

    def test_tick_base_lookup_rejects_unverified_mirror(self):
        self.run_base_lookup_scenario("mirror")

    def test_tick_base_lookup_retries_upstream_with_mirror_configured(self):
        self.run_base_lookup_scenario("mirror-retry")

    def test_tick_base_lookup_failure_backs_off_and_writes_health(self):
        self.run_base_lookup_scenario("failure")

    def test_tick_base_lookup_timeout_backs_off_and_writes_health(self):
        self.run_base_lookup_scenario("timeout")

    def test_tick_empty_successful_base_lookup_cannot_dispatch(self):
        self.run_base_lookup_scenario("empty")

    def run_base_lookup_scenario(self, mode):
        base = "a" * 40
        cfg = {"host": "govpn", "backend": "codex",
               "fetch_policy": {"retries": 3, "delay": 0.25, "fallback_after": 2}}
        item = {"n": 1719, "title": "base lookup", "body": ""}
        calls = []

        def fake_sh(command, timeout=None, inp=None):
            self.assertEqual(command[:2], ["git", "ls-remote"])
            calls.append(command)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(command, timeout)
            if mode == "empty":
                return 0, "", ""
            if mode in {"retry", "mirror-retry"} and len(calls) > 1:
                return 0, f"{base}\trefs/heads/main\n", ""
            if command[-2] == "https://mirror.invalid/repo.git":
                return 0, f"{'b' * 40}\trefs/heads/main\n", ""
            return 1, "", "gnutls_handshake failed"

        with tempfile.TemporaryDirectory(dir=self.scratch) as scratch, \
             mock.patch.object(AUTODISPATCH, "D", scratch), \
             mock.patch.object(AUTODISPATCH, "audit_switches"), \
             mock.patch.object(AUTODISPATCH, "paused_scopes", return_value={"mint"}), \
             mock.patch.object(AUTODISPATCH, "remote", return_value={"lane": "idle"}), \
             mock.patch.object(AUTODISPATCH, "queue", return_value=[item]), \
             mock.patch.object(AUTODISPATCH, "deps_met", return_value=True), \
             mock.patch.object(AUTODISPATCH, "prefetch_labels"), \
             mock.patch.object(AUTODISPATCH, "can_take", return_value=True), \
             mock.patch.object(AUTODISPATCH, "rank", return_value=0), \
             mock.patch.object(AUTODISPATCH, "label_state", return_value=("fresh", None)), \
             mock.patch.object(AUTODISPATCH, "freshen_queue", side_effect=lambda q, state: q), \
             mock.patch.object(AUTODISPATCH, "AD_VERIFY_SHARE_ENABLED", False), \
             mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
             mock.patch.object(AUTODISPATCH, "gh") as gh, \
             mock.patch.object(AUTODISPATCH, "dispatch", return_value=("run-base", None)) as dispatch, \
             mock.patch.object(AUTODISPATCH, "log") as log, \
             mock.patch.object(AUTODISPATCH.time, "sleep") as sleep, \
             mock.patch.dict("os.environ", {"AD_BASE_FETCH_MIRROR": "https://mirror.invalid/repo.git" if mode.startswith("mirror") else ""}):
            state = {}
            AUTODISPATCH.tick({"CX10": cfg}, state, 0)
            health = json.loads((Path(scratch) / "lanes-status.json").read_text())["_dispatch_fetch_failures"]["govpn"]
            self.assertTrue(all(command[-2] == AUTODISPATCH.BASE_REPO_URL for command in calls),
                            "a mirror cannot establish the authoritative base")
            if mode in {"retry", "mirror-retry"}:
                dispatch.assert_called_once_with("CX10", cfg, item, base)
                self.assertEqual(state["CX10"]["base"], base)
                self.assertTrue(AUTODISPATCH.host_ok("govpn"))
                self.assertEqual(health["recent_failures"], 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(sleep.call_args_list, [mock.call(0.25)])
            else:
                dispatch.assert_not_called()
                gh.assert_not_called()
                self.assertNotIn("CX10", state)
                self.assertFalse(AUTODISPATCH.host_ok("govpn"))
                self.assertEqual(health["recent_failures"], 1 if mode == "empty" else 3)
                self.assertEqual(health["primary_error"], {"failure": "tls", "mirror": "tls", "timeout": "network", "empty": "other"}[mode])

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

    def test_finish_rejects_stale_harvest_mirror(self):
        self.run_harvest_mirror_scenario(stale=True)

    def test_finish_accepts_matching_harvest_mirror(self):
        self.run_harvest_mirror_scenario()

    def test_finish_rejects_unreadable_harvest_mirror_commit(self):
        self.run_harvest_mirror_scenario(unreadable=True)

    def run_harvest_mirror_scenario(self, stale=False, unreadable=False):
        with tempfile.TemporaryDirectory(dir=self.scratch) as scratch:
            root = Path(scratch)
            executor, mirror, controller, published = [root / name for name in
                                                      ("executor", "mirror", "controller", "published")]

            def git(repo, *args):
                result = subprocess.run(["git", "-C", str(repo), *args],
                                        capture_output=True, text=True, check=True)
                return result.stdout.strip()

            executor.mkdir()
            git(executor, "init", "-q", "-b", "work")
            git(executor, "config", "user.name", "Test")
            git(executor, "config", "user.email", "test@example.com")
            (executor / "result").write_text("base\n")
            git(executor, "add", "result")
            git(executor, "commit", "-qm", "base")
            base = git(executor, "rev-parse", "HEAD")
            (executor / "result").write_text("old result\n")
            git(executor, "commit", "-qam", "old result")
            old = git(executor, "rev-parse", "HEAD")
            (executor / "result").write_text("latest result\n")
            git(executor, "commit", "-qam", "fix #1719 fetch\n\nCloses #1719\n\nVerify: local tests passed")
            expected = git(executor, "rev-parse", "HEAD")
            # The controller even has the expected object: checking its existence
            # instead of comparing the harvested ref would still lose the result.
            git(root, "clone", "-q", str(executor), str(controller))
            git(root, "clone", "--bare", "-q", str(executor), str(mirror))
            if stale:
                git(mirror, "update-ref", "refs/heads/work", old)
            git(root, "init", "--bare", "-q", str(published))
            git(controller, "remote", "set-url", "origin", str(published))
            st = {"issue": 1719, "run": "run-mirror", "base": base}
            state = {"CX10": st}
            info = {"branch": "work", "commit": expected, "repo": str(executor), "status": "done"}
            real_sh = AUTODISPATCH.sh
            pushes = []

            def fake_sh(command, timeout=120, inp=None):
                if "fetch" in command and f"govpn:{executor}" in command:
                    return 1, "", "gnutls_handshake failed"
                if unreadable and "rev-parse" in command:
                    return 1, "", "cannot resolve harvested commit"
                if "push" in command:
                    pushes.append(command)
                return real_sh(command, timeout=timeout, inp=inp)

            with mock.patch.object(AUTODISPATCH, "LOCAL", str(controller)), \
                 mock.patch.object(AUTODISPATCH, "D", scratch), \
                 mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh), \
                 mock.patch.object(AUTODISPATCH, "gh", return_value=(0, "", "")) as gh, \
                 mock.patch.object(AUTODISPATCH, "log") as log, \
                 mock.patch.object(AUTODISPATCH.time, "sleep"), \
                 mock.patch.dict("os.environ", {"AD_HARVEST_FETCH_MIRROR": str(mirror)}):
                result = AUTODISPATCH.finish("CX10", {"host": "govpn"}, st, info, state)

            if stale or unreadable:
                self.assertFalse(result["done"], "unverified mirror must not complete the lane")
                self.assertEqual(result["status"], "fetch-commit-mismatch")
                self.assertIn(expected, result["error"])
                self.assertTrue(any("commit" in call.args[0] for call in log.call_args_list))
                self.assertEqual(pushes, [])
                gh.assert_not_called()
                self.assertNotIn(1719, state.get("_done", []))
                self.assertIs(state["CX10"], st)
                self.assertEqual(git(published, "for-each-ref", "--format=%(objectname)"), "")
            else:
                self.assertEqual(result["status"], "pr-created")
                self.assertTrue(result["done"])
                self.assertEqual(len(pushes), 1)
                self.assertEqual(git(published, "rev-parse", "refs/heads/fleet/CX10-issue1719-run-mirror"), expected)
                self.assertIn(1719, state["_done"])

    def test_harvest_timeouts_retry_and_attribute_seed_fallback_failure(self):
        for seed_succeeds in (True, False):
            with self.subTest(seed_succeeds=seed_succeeds):
                AUTODISPATCH.DISPATCH_FETCH_FAILURES.clear()
                state = {}

                def fake_sh(command, timeout=None, inp=None):
                    if seed_succeeds and command[-1] == "seed":
                        return 0, "", ""
                    raise subprocess.TimeoutExpired(command, timeout)

                with mock.patch.object(AUTODISPATCH, "sh", side_effect=fake_sh) as shell, \
                     mock.patch.object(AUTODISPATCH.time, "sleep"), \
                     mock.patch.object(AUTODISPATCH, "log"):
                    ok, err, failures = AUTODISPATCH.harvest_fetch(
                        "CX10", 1719, ["git", "fetch", "executor"], state, "govpn", ["git", "fetch", "seed"])
                self.assertEqual(ok, seed_succeeds)
                self.assertEqual(failures, 0 if seed_succeeds else 1)
                self.assertEqual(shell.call_count, 4)
                if not seed_succeeds:
                    self.assertIn("seed fallback: network command timed out", err)
                    self.assertEqual(state["fetch_failures"], 1)
                health = AUTODISPATCH.dispatch_fetch_health()["govpn"]
                self.assertEqual(health["recent_failures"], 3 if seed_succeeds else 4)
                self.assertEqual(health["primary_error"], "network")


if __name__ == "__main__":
    unittest.main()
