"""Tests for the per-host deploy version record (Refs #1482)."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "deploy" / "version_record.py"
spec = importlib.util.spec_from_file_location("deploy_version_record", MODULE)
version_record = importlib.util.module_from_spec(spec)
spec.loader.exec_module(version_record)

SHA = "9f1c0dea1b2c3d4e5f60718293a4b5c6d7e8f90"
OTHER_SHA = "1122334455667788990011223344556677889900"
TS = "2026-11-01T09:12:33+00:00"


class VersionRecordTests(unittest.TestCase):
    """A record survives the round trip and a broken one is rejected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deploy-version-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def test_write_and_read_round_trip(self):
        path = self.root / "deploy-version.json"
        version_record.write_record(str(path), "ib1", SHA, TS)
        record = version_record.read_record(str(path))
        self.assertEqual(record["host"], "ib1")
        self.assertEqual(record["sha"], SHA)
        self.assertEqual(record["ts"], TS)
        self.assertNotIn("extra", record)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["host"], "ib1")

    def test_extra_fields_are_kept_under_extra(self):
        path = self.root / "deploy-version.json"
        version_record.write_record(str(path), "box", SHA, TS, {"role": "box", "port": 22})
        record = version_record.read_record(str(path))
        self.assertEqual(record["extra"], {"role": "box", "port": 22})

    def test_write_creates_missing_parent_directories(self):
        path = self.root / "nested" / "dir" / "deploy-version.json"
        version_record.write_record(str(path), "ib2", SHA, TS)
        self.assertTrue(path.exists())

    def test_write_replaces_the_record_and_leaves_no_temporary_file(self):
        path = self.root / "deploy-version.json"
        version_record.write_record(str(path), "ib1", SHA, TS)
        version_record.write_record(str(path), "ib1", OTHER_SHA, TS)
        self.assertEqual(version_record.read_record(str(path))["sha"], OTHER_SHA)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["deploy-version.json"])

    def test_read_rejects_a_record_without_the_required_fields(self):
        path = self.root / "broken.json"
        path.write_text(json.dumps({"host": "ib1", "ts": TS}), encoding="utf-8")
        with self.assertRaises(version_record.RecordError) as caught:
            version_record.read_record(str(path))
        self.assertIn("sha", str(caught.exception))

    def test_read_rejects_broken_json_and_missing_files(self):
        path = self.root / "garbage.json"
        path.write_text("not json\n", encoding="utf-8")
        with self.assertRaises(version_record.RecordError):
            version_record.read_record(str(path))
        with self.assertRaises(version_record.RecordError):
            version_record.read_record(str(self.root / "absent.json"))

    def test_write_rejects_unusable_arguments(self):
        path = self.root / "deploy-version.json"
        with self.assertRaises(version_record.RecordError):
            version_record.write_record(str(path), "", SHA, TS)
        with self.assertRaises(version_record.RecordError):
            version_record.write_record(str(path), "ib1", SHA, "not-a-timestamp")
        with self.assertRaises(version_record.RecordError):
            version_record.write_record(str(path), "ib1", SHA, TS, extra="nope")

    def test_write_rejects_an_impossible_date(self):
        path = self.root / "deploy-version.json"
        for ts in ("2026-99-99", "2026-13-01", "2026-02-30", "2026-00-10T09:12:33+00:00"):
            with self.assertRaises(version_record.RecordError):
                version_record.write_record(str(path), "ib1", SHA, ts)
        self.assertFalse(path.exists())

    def test_write_rejects_an_impossible_time(self):
        path = self.root / "deploy-version.json"
        for ts in ("2026-11-01T25:00:00+00:00", "2026-11-01T09:61:00+00:00",
                   "2026-11-01T09:12:61+00:00", "2026-11-01T09:12:33+99:00"):
            with self.assertRaises(version_record.RecordError):
                version_record.write_record(str(path), "ib1", SHA, ts)
        self.assertFalse(path.exists())

    def test_write_rejects_trailing_characters(self):
        path = self.root / "deploy-version.json"
        for ts in ("2026-99-99garbage", "2026-11-01T09:12:33garbage",
                   "2026-11-01T09:12:33+00:00x", "2026-11-01T09:12:33Zx"):
            with self.assertRaises(version_record.RecordError):
                version_record.write_record(str(path), "ib1", SHA, ts)
        self.assertFalse(path.exists())

    def test_write_accepts_the_usual_iso_forms(self):
        path = self.root / "deploy-version.json"
        for ts in ("2026-11-01T09:12:33+00:00", "2026-11-01T09:12:33Z",
                   "2026-11-01T09:12:33", "2026-11-01T09:12Z", "2026-11-01",
                   "2026-11-01T09:12:33.123456+00:00"):
            version_record.write_record(str(path), "ib1", SHA, ts)
            self.assertEqual(version_record.read_record(str(path))["ts"], ts)

    def test_read_rejects_a_record_with_an_invalid_timestamp(self):
        path = self.root / "broken-ts.json"
        path.write_text(json.dumps({"host": "ib1", "sha": SHA, "ts": "2026-99-99"}),
                        encoding="utf-8")
        with self.assertRaises(version_record.RecordError):
            version_record.read_record(str(path))

    def test_assert_same_sha_reports_the_hosts_on_another_commit(self):
        records = [
            {"host": "box", "sha": SHA},
            {"host": "ib1", "sha": SHA},
            {"host": "ib2", "sha": OTHER_SHA},
        ]
        self.assertEqual(version_record.assert_same_sha(records), ["ib2"])

    def test_assert_same_sha_accepts_one_commit(self):
        records = [{"host": "box", "sha": SHA}, {"host": "ib1", "sha": SHA}]
        self.assertEqual(version_record.assert_same_sha(records), [])

    def test_assert_same_sha_reports_a_record_without_a_sha(self):
        records = [{"host": "box", "sha": SHA}, {"host": "ib1"}]
        self.assertEqual(version_record.assert_same_sha(records), ["ib1"])

    def test_assert_same_sha_rejects_records_without_a_host(self):
        with self.assertRaises(version_record.RecordError):
            version_record.assert_same_sha([{"sha": SHA}])


class VersionRecordCliTests(unittest.TestCase):
    """The CLI writes a record and fails when the hosts disagree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deploy-version-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def run_cli(self, *args):
        """Run the module as a script and return the completed process."""
        return subprocess.run([sys.executable, str(MODULE), *args],
                              capture_output=True, text=True, timeout=60)

    def test_write_then_check_exits_zero_for_one_commit(self):
        first = self.root / "box.json"
        second = self.root / "ib1.json"
        written = self.run_cli("write", "--path", str(first), "--host", "box",
                               "--sha", SHA, "--ts", TS)
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(json.loads(written.stdout)["sha"], SHA)
        again = self.run_cli("write", "--path", str(second), "--host", "ib1",
                             "--sha", SHA, "--ts", TS, "--extra", '{"role": "remote"}')
        self.assertEqual(again.returncode, 0, again.stderr)

        checked = self.run_cli("check", "--paths", "{},{}".format(first, second))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        report = json.loads(checked.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["mismatched"], [])

    def test_check_exits_one_when_the_hosts_disagree(self):
        first = self.root / "box.json"
        second = self.root / "ib2.json"
        self.assertEqual(self.run_cli("write", "--path", str(first), "--host", "box",
                                      "--sha", SHA, "--ts", TS).returncode, 0)
        self.assertEqual(self.run_cli("write", "--path", str(second), "--host", "ib2",
                                      "--sha", OTHER_SHA, "--ts", TS).returncode, 0)
        checked = self.run_cli("check", "--paths", "{},{}".format(first, second))
        self.assertEqual(checked.returncode, 1, checked.stdout)
        report = json.loads(checked.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["mismatched"], ["ib2"])

    def test_check_exits_two_when_a_record_is_missing(self):
        checked = self.run_cli("check", "--paths", str(self.root / "absent.json"))
        self.assertEqual(checked.returncode, 2, checked.stdout)
        self.assertIn("error:", checked.stderr)

    def test_write_exits_two_on_broken_extra(self):
        broken = self.run_cli("write", "--path", str(self.root / "box.json"),
                              "--host", "box", "--sha", SHA, "--extra", "not json")
        self.assertEqual(broken.returncode, 2, broken.stdout)

    def test_write_exits_two_on_an_invalid_timestamp(self):
        for ts in ("2026-99-99", "2026-99-99garbage", "2026-11-01T25:00:00+00:00"):
            target = self.root / "box.json"
            broken = self.run_cli("write", "--path", str(target), "--host", "box",
                                  "--sha", SHA, "--ts", ts)
            self.assertEqual(broken.returncode, 2, broken.stdout)
            self.assertIn("error:", broken.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
