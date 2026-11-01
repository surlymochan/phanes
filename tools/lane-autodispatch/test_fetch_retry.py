#!/usr/bin/env python3
"""Bounded harvest retries for the lane autodispatcher (Refs #746).

A `git fetch` from an executor host can fail with a transient SSH error (`Connection closed`,
banner exchange). The dispatcher retries a bounded number of times and reports how many attempts
failed, so the failure count reaches the lane's result summary.
"""
import importlib.util
import pathlib
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("autodispatch", HERE / "autodispatch.py")
autodispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autodispatch)


class FetchRetryTest(unittest.TestCase):
    def setUp(self):
        self.logs = []
        log_patch = mock.patch.object(autodispatch, "log", lambda msg, brain=True: self.logs.append(msg))
        sleep_patch = mock.patch.object(autodispatch.time, "sleep")
        log_patch.start()
        self.sleep = sleep_patch.start()
        self.addCleanup(log_patch.stop)
        self.addCleanup(sleep_patch.stop)

    def fake_sh(self, results):
        """A `sh` stub that returns the queued (rc, out, err) results in order."""
        calls = []

        def sh(cmd, timeout=120, inp=None):
            calls.append(cmd)
            return results[min(len(calls) - 1, len(results) - 1)]

        return sh, calls

    def test_transient_failure_is_retried_until_it_succeeds(self):
        sh, calls = self.fake_sh([(1, "", "ssh: connect to host ib1: Connection closed by remote host"),
                                  (0, "", "")])
        with mock.patch.object(autodispatch, "sh", sh):
            rc, err, failures = autodispatch.fetch_with_retry(["git", "fetch"], "M1", 42)
        self.assertEqual(rc, 0)
        self.assertEqual(failures, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_retries_are_bounded_and_the_failed_count_is_reported(self):
        sh, calls = self.fake_sh([(1, "", "kex_exchange_identification: Connection closed during banner exchange")])
        with mock.patch.object(autodispatch, "sh", sh):
            rc, err, failures = autodispatch.fetch_with_retry(["git", "fetch"], "M1", 42)
        self.assertEqual(rc, 1)
        self.assertEqual(failures, autodispatch.FETCH_ATTEMPTS)
        self.assertEqual(len(calls), autodispatch.FETCH_ATTEMPTS)
        self.assertEqual(self.sleep.call_count, autodispatch.FETCH_ATTEMPTS - 1)

    def test_permanent_failure_is_not_retried(self):
        sh, calls = self.fake_sh([(1, "", "fatal: repository not found")])
        with mock.patch.object(autodispatch, "sh", sh):
            rc, err, failures = autodispatch.fetch_with_retry(["git", "fetch"], "M1", 42)
        self.assertEqual(rc, 1)
        self.assertEqual(failures, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_failure_count_is_written_in_readable_wording(self):
        self.assertEqual(autodispatch.plural(1, "attempt"), "1 attempt")
        self.assertEqual(autodispatch.plural(3, "attempt"), "3 attempts")


if __name__ == "__main__":
    unittest.main()
