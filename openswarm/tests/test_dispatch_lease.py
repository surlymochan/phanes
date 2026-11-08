"""`openswarm/lib/dispatch/lease.py`: the in-progress lease of a dispatched issue (Refs #1400).

The dispatch loop labels an issue `in-progress` before the run is really alive.
The lease store bounds that window: the lease expires unless the holder renews
it, and an expired lease is only reclaimable when the run it points at is not
running any more (Refs #1400, part of #1364).

The module is loaded by path: `openswarm/lib/dispatch/` is a namespace package
without an `__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
LEASE_MODULE = HERE.parent / "lib" / "dispatch" / "lease.py"

spec = importlib.util.spec_from_file_location("dispatch_lease", LEASE_MODULE)
lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lease)


class LeaseStoreTests(unittest.TestCase):
    """A lease is taken, renewed, and only reclaimed when its run is gone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-lease-")
        self.addCleanup(self.tmp.cleanup)
        self.store = lease.LeaseStore(self.tmp.name)

    def test_grant_records_the_lease_and_survives_a_reload(self):
        record = self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        self.assertEqual(record["issue"], 1400)
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["lane"], "M12")
        self.assertEqual(record["expires_at"], 1600.0)
        self.assertEqual(self.store.get(1400)["run_id"], "run-1")
        # a second store on the same directory sees the same lease
        self.assertEqual(lease.LeaseStore(self.tmp.name).get(1400)["lane"], "M12")

    def test_renew_pushes_the_expiry_further_away(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        renewed = self.store.renew(1400, "run-1", 1200.0)

        self.assertIsNotNone(renewed)
        self.assertEqual(renewed["expires_at"], 1800.0)
        # the deadline the grant had set is gone, the issue stays in-progress
        self.assertEqual(self.store.expired(1600.0, []), [])
        self.assertEqual(self.store.expired(1799.0, []), [])
        self.assertEqual(self.store.expired(1800.0, []), [1400])

    def test_expired_lease_is_reclaimable_when_the_run_is_gone(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)
        self.store.grant(1401, "run-2", "M12", 600, 1000.0)

        self.assertEqual(self.store.expired(1599.0, []), [])
        self.assertEqual(self.store.expired(1600.0, []), [1400, 1401])

    def test_expired_lease_is_kept_while_its_run_is_still_running(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        # the lease is over, but the run still lives: the issue is not reclaimable
        self.assertEqual(self.store.expired(1600.0, ["run-1"]), [])
        self.assertEqual(self.store.expired(1600.0, {"run-1": "running"}), [])
        self.assertEqual(self.store.expired(1600.0, "run-1"), [])
        # another run being alive does not keep the lease of this one
        self.assertEqual(self.store.expired(1600.0, ["run-2"]), [1400])

    def test_renew_with_a_different_run_id_is_rejected(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        self.assertIsNone(self.store.renew(1400, "run-2", 1200.0))
        # the rejected renewal did not move the deadline of the holder
        self.assertEqual(self.store.get(1400)["expires_at"], 1600.0)
        self.assertEqual(self.store.expired(1600.0, []), [1400])

    def test_renew_without_a_lease_returns_none(self):
        self.assertIsNone(self.store.renew(1400, "run-1", 1000.0))

    def test_release_drops_the_lease(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        self.assertTrue(self.store.release(1400))
        self.assertIsNone(self.store.get(1400))
        self.assertEqual(self.store.expired(1600.0, []), [])
        self.assertFalse(self.store.release(1400))

    def test_the_lease_file_is_written_atomically(self):
        self.store.grant(1400, "run-1", "M12", 600, 1000.0)

        # no half written temporary file is left behind
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["leases.json"])
        payload = json.loads((pathlib.Path(self.tmp.name) / "leases.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["leases"]["1400"]["run_id"], "run-1")
        self.assertEqual(payload["leases"]["1400"]["expires_at"], 1600.0)

    def test_an_unreadable_lease_file_does_not_break_the_store(self):
        (pathlib.Path(self.tmp.name) / "leases.json").write_text("{not json", encoding="utf-8")

        self.assertEqual(self.store.expired(1600.0, []), [])
        record = self.store.grant(1400, "run-1", "M12", 600, 1000.0)
        self.assertEqual(record["expires_at"], 1600.0)


if __name__ == "__main__":
    unittest.main()
