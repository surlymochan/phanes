#!/usr/bin/env python3
"""Tests for report.py using synthetic, temporary fixtures."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import report


class RateLimitStatsTest(unittest.TestCase):
    """rate_limit_stats must judge the stop status by the globally last event.

    report.py documents `ended_in_error` as "True if the LAST assistant
    `message_end` of any events file has stopReason 'error'", i.e. one global
    last event across all event files, not an OR over the last event of each
    file.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = self.tmp.name
        self.raw = os.path.join(self.root, "raw")

    def tearDown(self):
        self.tmp.cleanup()

    def add_events(self, filename, messages):
        run_dir = os.path.join(self.raw, "swrm", "inst-1", "r1")
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            for message in messages:
                fh.write(json.dumps({
                    "type": "message_end",
                    "message": message,
                }, ensure_ascii=False) + "\n")

    def test_rate_limit_stop_status_uses_globally_last_event(self):
        # The earlier file ends in an error, the later file ends successfully:
        # the run did not end in an error.
        self.add_events("2026-01-01T00-00-00-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "Connection error."},
        ])
        self.add_events("2026-01-01T00-00-01-events.jsonl", [
            {"role": "assistant", "stopReason": "stop"},
        ])

        stats = report.rate_limit_stats(self.raw, "swrm", "inst-1", 1)

        self.assertFalse(stats["ended_in_error"])

    def test_rate_limit_stop_status_uses_globally_last_event_with_429(self):
        # Same shape, but the earlier error is a 429: the retry is still counted
        # and the run did not end in an error.
        self.add_events("2026-01-01T00-00-00-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "429 rate_limit_error"},
        ])
        self.add_events("2026-01-01T00-00-01-events.jsonl", [
            {"role": "assistant", "stopReason": "stop"},
        ])

        stats = report.rate_limit_stats(self.raw, "swrm", "inst-1", 1)

        self.assertFalse(stats["ended_in_error"])
        self.assertEqual(stats["retries"], 1)

    def test_rate_limit_stop_status_true_when_last_event_is_error(self):
        # The globally last event is an error: the run ended in an error.
        self.add_events("2026-01-01T00-00-00-events.jsonl", [
            {"role": "assistant", "stopReason": "stop"},
        ])
        self.add_events("2026-01-01T00-00-01-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "Connection error."},
        ])

        stats = report.rate_limit_stats(self.raw, "swrm", "inst-1", 1)

        self.assertTrue(stats["ended_in_error"])

    def test_rate_limit_stop_status_uses_last_event_of_a_single_file(self):
        # One file, error first and success last: the run did not end in an error.
        self.add_events("2026-01-01T00-00-00-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "429 rate_limit_error"},
            {"role": "assistant", "stopReason": "stop"},
        ])

        stats = report.rate_limit_stats(self.raw, "swrm", "inst-1", 1)

        self.assertFalse(stats["ended_in_error"])
        self.assertEqual(stats["retries"], 1)

    def test_rate_limit_retries_accumulate_across_event_files(self):
        # Every 429 of every event file counts, whatever the final stop reason is.
        self.add_events("2026-01-01T00-00-00-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "429 rate_limit_error"},
            {"role": "assistant", "stopReason": "error", "errorMessage": "429 rate_limit_error"},
        ])
        self.add_events("2026-01-01T00-00-01-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "Connection error."},
        ])

        stats = report.rate_limit_stats(self.raw, "swrm", "inst-1", 1)

        self.assertEqual(stats["retries"], 2)


class RateLimitSummaryTest(unittest.TestCase):
    """The summary must not report a run as ended in error when it did not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = self.tmp.name
        self.raw = os.path.join(self.root, "raw")

    def tearDown(self):
        self.tmp.cleanup()

    def add_events(self, filename, messages):
        run_dir = os.path.join(self.raw, "swrm", "inst-1", "r1")
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            for message in messages:
                fh.write(json.dumps({
                    "type": "message_end",
                    "message": message,
                }, ensure_ascii=False) + "\n")

    def test_rate_limit_summary_runs_ended_in_error(self):
        self.add_events("2026-01-01T00-00-00-events.jsonl", [
            {"role": "assistant", "stopReason": "error", "errorMessage": "429 rate_limit_error"},
        ])
        self.add_events("2026-01-01T00-00-01-events.jsonl", [
            {"role": "assistant", "stopReason": "stop"},
        ])
        runs_path = os.path.join(self.root, "runs.jsonl")
        with open(runs_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "instance_id": "inst-1",
                "arm": "swrm",
                "rep": 1,
                "resolved": True,
                "wall_s": 1.0,
                "calls": [],
                "timed_out": False,
                "tokens": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
            }, ensure_ascii=False) + "\n")
        out_dir = os.path.join(self.root, "out")

        argv = ["report.py", runs_path, "--out", out_dir, "--raw", self.raw, "--price", "minimax"]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            report.main()

        with open(os.path.join(out_dir, "summary.json"), "r", encoding="utf-8") as fh:
            summary = json.load(fh)
        arm = summary["arms"]["swrm"]
        self.assertEqual(arm["runs_ended_in_error"], 0)
        self.assertEqual(arm["rate_limit_retries_total"], 1)


if __name__ == "__main__":
    unittest.main()
