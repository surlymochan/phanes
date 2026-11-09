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


    def test_runs_reject_incomplete_identities_with_physical_line_number(self):
        for field in ("instance_id", "arm", "rep"):
            for value in (None, "", "   "):
                record = {"instance_id": "i", "arm": "a", "rep": 0}
                record[field] = value
                path = self.write_jsonl("runs.jsonl", [record])
                with open(path, "r+", encoding="utf-8") as stream:
                    body = stream.read()
                    stream.seek(0)
                    stream.write("\n\n" + body)
                with self.assertRaisesRegex(ValueError, rf"{path}:3.*{field}"):
                    apply_regrade.load_runs(path)

    def test_invalid_shape_and_unhashable_identity_have_location(self):
        for record in (None, [], "record", {"instance_id": [], "arm": "a", "rep": 1}):
            path = self.write_jsonl("invalid.jsonl", [record])
            with self.assertRaisesRegex(ValueError, rf"{path}:1"):
                apply_regrade.load_regrades([path])

    def test_invalid_harness_error_record_is_still_rejected_at_load(self):
        path = self.write_jsonl("error.jsonl", [{"harness_error": "failed"}])
        with self.assertRaisesRegex(ValueError, rf"{path}:1.*instance_id"):
            apply_regrade.load_regrades([path])

    def test_duplicate_keys_preserve_last_valid_result_and_zero_rep(self):
        key = {"instance_id": "i", "arm": "a", "rep": 0}
        path = self.write_jsonl("grades.jsonl", [dict(key, resolved=False),
            dict(key, resolved=True), dict(key, resolved=False, harness_error="failed")])
        runs = [dict(key, resolved=False)]
        output, changed = apply_regrade.apply_regrades(runs, apply_regrade.load_regrades([path]))
        self.assertTrue(output[0]["resolved"])
        self.assertFalse(runs[0]["resolved"])
        self.assertEqual(len(changed), 1)

    def test_main_does_not_overwrite_output_for_invalid_inputs(self):
        key = {"instance_id": "i", "arm": "a", "rep": 1}
        for invalid_runs in (True, False):
            runs = self.write_jsonl("runs.jsonl", [{} if invalid_runs else key])
            grades = self.write_jsonl("grades.jsonl", [key if invalid_runs else {}])
            out = self.write_jsonl("out.jsonl", [{"sentinel": True}])
            with self.assertRaises(ValueError):
                apply_regrade.main(["--runs", runs, "--regrade", grades, "--out", out])
            with open(out, encoding="utf-8") as stream:
                self.assertEqual(json.loads(stream.read()), {"sentinel": True})


if __name__ == "__main__":
    unittest.main()
