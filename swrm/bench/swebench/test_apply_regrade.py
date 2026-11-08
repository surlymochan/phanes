#!/usr/bin/env python3
"""Tests for apply_regrade.py using synthetic JSONL fixtures."""

import json
import os
import tempfile
import unittest

import apply_regrade


class ApplyRegradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write_jsonl(self, filename, records):
        path = os.path.join(self.root, filename)
        with open(path, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")
        return path

    def test_regrade_missing_identity_field_is_rejected_with_location(self):
        for field in ("instance_id", "arm", "rep"):
            with self.subTest(field=field):
                record = {"instance_id": "inst-1", "arm": "baseline", "rep": 1}
                del record[field]
                path = self.write_jsonl("missing-{}.jsonl".format(field), [record])

                with self.assertRaisesRegex(
                    ValueError, r"{}:1.*{}".format(path, field)
                ):
                    apply_regrade.load_regrades([path])

    def test_regrade_empty_identity_field_is_rejected_with_location(self):
        for field in ("instance_id", "arm", "rep"):
            for empty_value in (None, "", "   "):
                with self.subTest(field=field, value=empty_value):
                    record = {
                        "instance_id": "inst-1",
                        "arm": "baseline",
                        "rep": 1,
                    }
                    record[field] = empty_value
                    path = self.write_jsonl(
                        "empty-{}-{}.jsonl".format(field, repr(empty_value)),
                        [record],
                    )

                    with self.assertRaisesRegex(
                        ValueError, r"{}:1.*{}".format(path, field)
                    ):
                        apply_regrade.load_regrades([path])

    def test_duplicate_complete_identity_keeps_later_file_override(self):
        first = self.write_jsonl("first.jsonl", [{
            "instance_id": "inst-1",
            "arm": "baseline",
            "rep": 1,
            "resolved": False,
        }])
        second = self.write_jsonl("second.jsonl", [{
            "instance_id": "inst-1",
            "arm": "baseline",
            "rep": 1,
            "resolved": True,
        }])

        regrades = apply_regrade.load_regrades([first, second])

        self.assertIs(regrades[("inst-1", "baseline", 1)]["resolved"], True)


if __name__ == "__main__":
    unittest.main()
