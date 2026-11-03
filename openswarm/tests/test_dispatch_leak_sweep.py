"""Regression coverage for in-progress leak-sweep decisions (#1493)."""
import importlib.util
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lease = load("dispatch_lease", ROOT / "lib" / "dispatch" / "lease.py")
sweep = load("dispatch_leak_sweep", ROOT / "lib" / "dispatch" / "leak_sweep.py")


class LeakSweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-sweep-")
        self.addCleanup(self.tmp.cleanup)
        self.leases = lease.LeaseStore(self.tmp.name)
        self.label = {"issue": 1403, "updatedAt": 1000}

    def action(self, **kwargs):
        decisions = sweep.decide([self.label], [], self.leases, None, 3000, **kwargs)
        return decisions[0]

    def test_live_lease_keeps_label(self):
        self.leases.grant(1403, "run", "M1", 600, 2500)
        self.assertEqual(self.action()["reason"], "lease_alive")

    def test_expired_lease_strips_only_when_not_held(self):
        self.leases.grant(1403, "run", "M1", 600, 1000)
        self.assertEqual(self.action()["action"], "strip")
        result = sweep.decide([self.label], [1403], self.leases, None, 3000)[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "held"))

    def test_resume_grace_keeps_then_allows_strip_afterward(self):
        in_grace = sweep.decide([self.label], [], self.leases, 2900, 3000)[0]
        after = sweep.decide([self.label], [], self.leases, 2000, 3000)[0]
        self.assertEqual(in_grace["reason"], "resume_grace")
        self.assertEqual(after["action"], "strip")

    def test_resume_after_now_is_clock_rollback(self):
        result = sweep.decide([self.label], [], self.leases, 3500, 3000)[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "clock_rollback"))

    def test_label_debt_keeps_issue(self):
        result = sweep.decide([self.label], [], self.leases, None, 3000, debt=[1403])[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "label_debt"))

    def test_clock_rollback_keeps_issue(self):
        label = {"issue": 1403, "updatedAt": 3100}
        result = sweep.decide([label], [], self.leases, None, 3000)[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "clock_rollback"))

    def test_non_finite_timestamps_keep_issue(self):
        label = {"issue": 1403, "updatedAt": "nan"}
        result = sweep.decide([label], [], self.leases, None, 3000)[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "no_timestamp"))
        result = sweep.decide([self.label], [], self.leases, None, float("nan"))[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "clock_rollback"))

    def test_invalid_issue_row_is_kept(self):
        result = sweep.decide([{}], [], self.leases, None, 3000)[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "invalid_issue"))

    def test_unsupported_issue_type_is_kept_not_raised(self):
        label = {"issue": 1.5, "updatedAt": 1000}
        result = sweep.decide([label], [], self.leases, None, 3000)[0]
        self.assertEqual((result["action"], result["reason"]), ("keep", "invalid_issue"))

    def test_load_resume_marker_accepts_epoch_and_timestamp(self):
        path = pathlib.Path(self.tmp.name) / "box-resume"
        self.assertIsNone(sweep.load_resume_at(path))
        path.write_text("2026-11-02 12:00:00\nignored", encoding="utf-8")
        expected = 1793620800.0
        self.assertEqual(sweep.load_resume_at(path), expected)
        path.write_text("2026-11-02T12:00:00\nignored", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)
        path.write_text("2026-11-02T12:00:00Z\nignored", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)
        path.write_text("1793620800\nignored", encoding="utf-8")
        self.assertEqual(sweep.load_resume_at(path), expected)


if __name__ == "__main__":
    unittest.main()
