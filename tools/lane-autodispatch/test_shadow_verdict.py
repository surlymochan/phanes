#!/usr/bin/env python3
"""Tester shadow verdict evidence (Refs #1986): append-only, never a merge authority.

The file is written for both runners of the repository: the pytest command of the
acceptance card (`python3 -m pytest -q tools/lane-autodispatch/test_shadow_verdict.py`)
and the unittest discovery the Swrm gate runs in this directory
(`python -m unittest discover -s . -p 'test_*.py'`). No pytest-only feature
(fixture, parametrize, monkeypatch) is used here, so the gate stays green on a
Python without pytest installed.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("shadow_ad", Path(__file__).with_name("autodispatch.py"))
ad = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ad)

SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"  # gitignored scratch root
BASE = dict(pr=1, lane_verdict="pass", tester_verdict="fail", timestamp="2026-01-01T00:00:00Z")


def record(path, **changes):
    fields = dict(BASE)
    fields.update(changes)
    return ad.record_shadow_verdict(path, **fields)


class BrokenFsync:
    """os proxy whose fsync always fails: a lost sample must not look like a merge."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def fsync(self, fd):
        raise OSError("simulated storage failure")


class ShadowVerdictTest(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.path = Path(self.tmp.name) / "shadow.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_idempotence_and_backfill(self):
        # Recording is observational: no dispatch, no merge, no external action.
        with mock.patch.object(ad, "sh", side_effect=AssertionError("external action")):
            self.assertTrue(record(self.path))
            first = self.path.read_bytes()
            # A repeated report (even with a new timestamp) is not a new sample.
            self.assertFalse(record(self.path, timestamp="2026-01-02T00:00:00+00:00"))
            self.assertEqual(self.path.read_bytes(), first)
            self.assertEqual(ad.shadow_verdict_stats(self.path),
                             dict(samples=1, disagreements=1, resolved=0, lane_hits=0,
                                  tester_hits=0, lane_accuracy=None, tester_accuracy=None))
            # The final outcome is backfilled exactly once.
            self.assertTrue(record(self.path, final_outcome="fail"))
            filled = self.path.read_bytes()
            self.assertTrue(filled.startswith(first))
            self.assertFalse(record(self.path, final_outcome="fail"))
            self.assertEqual(self.path.read_bytes(), filled)
            self.assertEqual(ad.shadow_verdict_stats(self.path),
                             dict(samples=1, disagreements=1, resolved=1, lane_hits=0,
                                  tester_hits=1, lane_accuracy=0.0, tester_accuracy=1.0))
            # A second, contradicting outcome is refused, the log stays as it was.
            with self.assertRaises(ValueError):
                record(self.path, final_outcome="pass")
            self.assertEqual(self.path.read_bytes(), filled)

    def test_invalid_records_do_not_write(self):
        invalid = [dict(pr=True), dict(pr=0), dict(pr="1"), dict(lane_verdict="approve"),
                   dict(tester_verdict="unknown"), dict(timestamp="yesterday"),
                   dict(timestamp="2026-01-01T00:00:00"), dict(final_outcome="merged"),
                   dict(final_outcome="insufficient_evidence")]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    record(self.path, **changes)
                self.assertFalse(self.path.exists())

    def test_insufficient_evidence_and_concurrent_retries(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: record(self.path, tester_verdict="insufficient_evidence"),
                                    range(8)))
        self.assertEqual(sum(results), 1)
        record(self.path, tester_verdict="insufficient_evidence", final_outcome="pass")
        stats = ad.shadow_verdict_stats(self.path)
        # An abstention is never a hit; the lane opinion that matched is.
        self.assertEqual((stats["samples"], stats["lane_hits"], stats["tester_hits"]), (1, 1, 0))
        with self.assertRaises(ValueError):
            record(self.path, tester_verdict="pass")
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_io_and_corrupt_log_fail_without_merge(self):
        with mock.patch.object(ad, "sh", side_effect=AssertionError("external action")):
            with self.assertRaises(OSError):
                record(self.path.parent / "missing" / "shadow.jsonl")
            self.path.write_text("{broken")
            with self.assertRaises(ValueError):
                record(self.path)
            self.assertEqual(self.path.read_text(), "{broken")
        self.assertEqual(ad.shadow_verdict_stats(self.path.parent / "absent")["samples"], 0)

    def test_failed_flush_never_calls_external_actions(self):
        with mock.patch.object(ad, "sh", side_effect=AssertionError("external action")), \
                mock.patch.object(ad, "os", BrokenFsync(os)):
            with self.assertRaisesRegex(OSError, "storage failure"):
                record(self.path)

    def test_default_log_path_is_the_control_plane_shadow_log(self):
        self.assertTrue(ad.SHADOW_VERDICT_LOG.endswith("/shadow-verdicts.jsonl"))

    def test_default_log_accumulates_thirty_samples_and_backfills(self):
        with mock.patch.object(ad, "SHADOW_VERDICT_LOG", str(self.path)), \
                mock.patch.object(ad, "sh", side_effect=AssertionError("external action")):
            for pr in range(1, 31):
                fields = dict(BASE, pr=pr)
                self.assertTrue(ad.record_shadow_verdict(**fields))
                self.assertFalse(ad.record_shadow_verdict(**fields))
            first = self.path.read_bytes()
            self.assertEqual(ad.shadow_verdict_stats(),
                             dict(samples=30, disagreements=30, resolved=0, lane_hits=0,
                                  tester_hits=0, lane_accuracy=None, tester_accuracy=None))
            for pr in range(1, 31):
                fields = dict(BASE, pr=pr, final_outcome="pass" if pr <= 20 else "fail")
                self.assertTrue(ad.record_shadow_verdict(**fields))
                self.assertFalse(ad.record_shadow_verdict(**fields))
                # Replaying the original report must retain its resolved outcome.
                self.assertFalse(ad.record_shadow_verdict(**dict(BASE, pr=pr)))
            self.assertTrue(self.path.read_bytes().startswith(first))
            self.assertEqual(len(self.path.read_text().splitlines()), 60)
            self.assertEqual(ad.shadow_verdict_stats(),
                             dict(samples=30, disagreements=30, resolved=30, lane_hits=20,
                                  tester_hits=10, lane_accuracy=20 / 30, tester_accuracy=10 / 30))

    def test_multiple_samples_and_initial_final_outcome(self):
        record(self.path, final_outcome="pass")
        record(self.path, pr=2, lane_verdict="fail", tester_verdict="fail", final_outcome="fail")
        record(self.path, pr=3, tester_verdict="pass")
        stats = ad.shadow_verdict_stats(self.path)
        self.assertEqual(stats, dict(samples=3, disagreements=1, resolved=2, lane_hits=2,
                                     tester_hits=1, lane_accuracy=1.0, tester_accuracy=0.5))
        for line in self.path.read_text().splitlines():
            self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main()
