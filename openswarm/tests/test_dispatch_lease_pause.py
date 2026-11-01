"""Pause handling for dispatch leases (Refs #1511, part of #1364)."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
LEASE_MODULE = HERE.parent / "lib" / "dispatch" / "lease.py"
spec = importlib.util.spec_from_file_location("dispatch_lease_pause", LEASE_MODULE)
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)


class LeasePauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lease-pause-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.store = lease.LeaseStore(self.root)

    def test_pause_shifts_live_lease_but_not_previously_expired_lease(self):
        self.store.grant(1, "live", "M1", 600, 1000)
        self.store.grant(2, "old", "M1", 900, 0)

        self.assertEqual(self.store.shift(1585, 2585), 1)
        self.assertEqual(self.store.get(1)["expires_at"], 3185.0)
        self.assertEqual(self.store.get(1)["shifted_s"], 1585.0)
        self.assertEqual(self.store.get(2)["expires_at"], 900.0)
        self.assertEqual(self.store.expired(2585, []), [2])

    def test_pause_record_is_only_applied_once_and_bad_lines_are_skipped(self):
        self.store.grant(1, "run", "M1", 600, 1000)
        pauses = self.root / "box-pauses.jsonl"
        pauses.write_text(
            "{bad line}\n" + json.dumps({"started": 1000, "resumed": 2585, "gap_s": 1585}) + "\n",
            encoding="utf-8",
        )
        state = self.root / "pause-state.json"

        self.assertEqual(lease.shift_from_pauses(self.store, pauses, 3000, state), 1)
        self.assertEqual(lease.shift_from_pauses(self.store, pauses, 3000, state), 0)
        record = self.store.get(1)
        self.assertEqual(record["expires_at"], 3185.0)
        self.assertEqual(record["shifted_s"], 1585.0)

    def test_expiry_grace_hides_recently_expired_lease(self):
        self.store.grant(1, "run", "M1", 600, 1000)

        self.assertEqual(self.store.expired(1650, [], grace_s=60), [])
        self.assertEqual(self.store.expired(1660, [], grace_s=60), [1])


if __name__ == "__main__":
    unittest.main()
