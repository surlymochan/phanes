import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "openswarm"


class PauseCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-pause-cli-")
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.env = dict(os.environ, OPENSWARM_HOME=str(self.home))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([str(CLI), "pause", *args], env=self.env,
                              text=True, capture_output=True)

    def test_set_list_clear(self):
        self.assertEqual(self.run_cli("set", "lane:CX5", "--ttl", "30m", "--reason", "drain").returncode, 0)
        result = self.run_cli("list", "--json")
        self.assertEqual(result.returncode, 0)
        rows = json.loads(result.stdout)
        self.assertEqual(rows[0]["scope"], "lane:CX5")
        self.assertEqual(rows[0]["reason"], "drain")
        self.assertGreater(rows[0]["remaining_ttl_s"], 0)
        self.assertEqual(self.run_cli("clear", "lane:CX5").returncode, 0)
        self.assertEqual(json.loads(self.run_cli("list", "--json").stdout), [])

    def test_expired_pause_is_hidden(self):
        self.assertEqual(self.run_cli("set", "mint", "--ttl", "1s").returncode, 0)
        pause_file = next((self.home / "pauses").glob("*.json"))
        data = json.loads(pause_file.read_text())
        data["expires_at"] = time.time() - 1
        pause_file.write_text(json.dumps(data))
        self.assertEqual(json.loads(self.run_cli("list", "--json").stdout), [])

    def test_legacy_markers(self):
        legacy = pathlib.Path(self.tmp.name) / "legacy"
        legacy.mkdir()
        (legacy / "PAUSE-CX5").touch()
        (legacy / "PAUSE-BACKEND-pi").touch()
        result = self.run_cli("list", "--json", "--legacy-dir", str(legacy))
        rows = json.loads(result.stdout)
        self.assertEqual({row["scope"] for row in rows}, {"lane:CX5", "backend:pi"})
        self.assertTrue(all(row["legacy"] for row in rows))

    def test_invalid_scope_exits_two(self):
        self.assertEqual(self.run_cli("set", "bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
