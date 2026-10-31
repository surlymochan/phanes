"""Restricted broker stdio keeps capacity ownership with its authenticated caller."""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BROKER = ROOT / "bin" / "pi-broker-stdio"


class PiBrokerStdioTests(unittest.TestCase):
    def test_capacity_json_uses_gores1_owner(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            relay = root / "pi-broker-stdio"
            capacity = root / "fleet-capacity"
            shutil.copy2(BROKER, relay)
            capacity.write_text("#!/bin/sh\nprintf '{\"ok\":true,\"owner\":\"%s\"}\\n' \"$1\"\n")
            relay.chmod(0o700); capacity.chmod(0o700)
            result = subprocess.run([str(relay)], input='{"op":"status"}\n', text=True,
                                    capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), {"ok": True, "owner": "gores1"})

