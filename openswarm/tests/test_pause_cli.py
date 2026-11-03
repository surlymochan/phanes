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

    def test_set_records_the_creator_and_the_basis(self):
        result = self.run_cli("set", "lane:CX5", "--ttl", "30m", "--reason", "drain",
                              "--source", "operator:main", "--basis", "issue #1669")
        self.assertEqual(result.returncode, 0)
        record = json.loads(result.stdout)
        self.assertEqual(record["source"], "operator:main")
        self.assertEqual(record["basis"], "issue #1669")
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual(rows[0]["source"], "operator:main")
        self.assertFalse(rows[0]["needs_review"])

    def test_switch_without_provenance_is_reported_for_review(self):
        # a record restored from an older snapshot: no creator, no basis
        pause_file = self.home / "pauses" / "mint.json"
        pause_file.parent.mkdir(parents=True, exist_ok=True)
        pause_file.write_text(json.dumps({"scope": "mint", "created_at": time.time() - 10,
                                          "expires_at": None, "reason": "was missing on gocc ctl"}))
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["needs_review"])
        self.assertIn("no-source", rows[0]["problems"])
        self.assertIn("no-basis", rows[0]["problems"])

    def test_set_without_provenance_asks_for_a_review(self):
        # no creator and no authority: no value is invented for them
        result = self.run_cli("set", "lane:CX5")
        self.assertEqual(result.returncode, 0)
        record = json.loads(result.stdout)
        self.assertEqual(record["source"], "")
        self.assertEqual(record["basis"], "")
        self.assertTrue(record["needs_review"])
        self.assertIn("no-source", record["problems"])
        self.assertIn("no-basis", record["problems"])
        self.assertIn("needs review", result.stderr)

    def test_set_with_a_reason_but_no_creator_asks_for_a_review(self):
        result = self.run_cli("set", "lane:CX5", "--reason", "drain")
        self.assertEqual(result.returncode, 0)
        record = json.loads(result.stdout)
        self.assertEqual(record["basis"], "drain")
        self.assertIn("no-source", record["problems"])

    def test_marker_set_records_the_creator_and_the_basis(self):
        state = pathlib.Path(self.tmp.name) / "state"
        result = self.run_cli("marker", "set", "lane:CX5", "--source", "operator:main",
                              "--basis", "issue #1669", "--dir", str(state))
        self.assertEqual(result.returncode, 0)
        body = (state / "PAUSE-CX5").read_text()
        self.assertIn("source=operator:main", body)
        self.assertIn("basis=issue #1669", body)
        self.assertIn("created_at=", body)
        rows = json.loads(self.run_cli("marker", "list", "--json", "--dir", str(state)).stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "operator:main")
        self.assertFalse(rows[0]["needs_review"])

    def test_marker_without_provenance_is_reported_for_review(self):
        state = pathlib.Path(self.tmp.name) / "state"
        result = self.run_cli("marker", "set", "lane:CX5", "--dir", str(state))
        self.assertEqual(result.returncode, 0)
        self.assertIn("needs review", result.stderr)
        rows = json.loads(self.run_cli("marker", "list", "--json", "--dir", str(state)).stdout)
        self.assertTrue(rows[0]["needs_review"])
        self.assertIn("no-source", rows[0]["problems"])
        self.assertIn("no-basis", rows[0]["problems"])

    def test_marker_clear_removes_the_marker(self):
        state = pathlib.Path(self.tmp.name) / "state"
        self.run_cli("marker", "set", "lane:CX5", "--source", "operator:main",
                     "--basis", "issue #1669", "--dir", str(state))
        self.assertEqual(self.run_cli("marker", "clear", "lane:CX5", "--dir", str(state)).returncode, 0)
        self.assertFalse((state / "PAUSE-CX5").exists())


if __name__ == "__main__":
    unittest.main()
