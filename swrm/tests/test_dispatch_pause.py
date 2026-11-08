"""Pause-store callers must see independent health holds (Refs #1367)."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "pause.py"
spec = importlib.util.spec_from_file_location("dispatch_pause", MODULE)
pause = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pause)
SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"


class PauseStoreTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="pause-store-", dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = pause.PauseStore(self.home)

    def test_independent_holds_match_only_their_dispatch_target(self):
        for scope in ("host:worker@health", "lane:L1", "backend:pi"):
            self.store.pause(scope, now=10)
        self.assertTrue(self.store.is_paused(host="worker", now=10))
        self.assertTrue(self.store.is_paused(lane="L1", now=10))
        self.assertTrue(self.store.is_paused(backend="pi", now=10))
        self.assertFalse(self.store.is_paused(host="work", lane="L2", backend="codex", now=10))
        self.assertFalse(self.store.resume("host:worker"))
        self.assertTrue(self.store.is_paused(host="worker", now=10))
        self.assertTrue(self.store.resume("host:worker@health"))
        self.assertFalse(self.store.is_paused(host="worker", now=10))

    def test_expiry_and_indefinite_hold_are_independent(self):
        self.store.pause("host:worker", ttl_s=30, now=100)
        self.store.pause("host:worker@health", now=100)
        self.assertEqual(len(self.store.active(129)), 2)
        self.assertEqual(len(self.store.active(130)), 1)
        self.assertTrue(self.store.is_paused(host="worker", now=100000))
        self.store.resume("host:worker@health")
        self.assertFalse(self.store.is_paused(host="worker", now=100000))

    def test_mint_does_not_pause_dispatch_but_all_does(self):
        self.store.pause("mint", now=10)
        self.assertFalse(self.store.is_paused(host="worker", now=10))
        self.store.pause("all", now=10)
        self.assertTrue(self.store.is_paused(host="worker", now=10))

    def test_legacy_filename_can_be_read_and_resumed(self):
        self.store.dir.mkdir()
        (self.store.dir / "host_worker.json").write_text(json.dumps(
            {"scope": "host:worker", "created_at": 10, "expires_at": None}))
        (self.store.dir / "broken.json").write_text("{")
        self.assertTrue(self.store.is_paused(host="worker", now=10))
        self.assertTrue(self.store.resume("host:worker"))
        self.assertFalse(self.store.is_paused(host="worker", now=10))

    def test_shared_directory_environment_and_aliases(self):
        self.assertEqual(pause.PauseStore().dir, Path.home() / ".claude/swrm/pauses")
        os.environ["XSWARM_HOME"] = str(self.home / "legacy")
        self.assertEqual(pause.PauseStore().dir, self.home / "legacy/pauses")
        os.environ["OPENSWARM_HOME"] = str(self.home)
        self.assertEqual(pause.PauseStore().dir, self.home / "pauses")
        for key in ("XSWARM_PAUSE_DIR", "OPENSWARM_PAUSE_DIR"):
            os.environ[key] = str(self.home / key)
            store = pause.PauseStore(self.home)
            self.assertEqual(store.dir, self.home / key)
            store.pause("host:worker@health", now=10)
            self.assertTrue(pause.PauseStore().is_paused(host="worker", now=10))


if __name__ == "__main__":
    unittest.main()
