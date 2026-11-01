import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[2]
SCRIPT = ROOT / "tools" / "ib-refresh.py"


class FleetRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fleet-refresh-")
        self.root = pathlib.Path(self.tmp.name)
        self.renew = self.root / "renew.py"
        self.renewed_cert = self.root / "renewed.cert"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(self.root / "key.pem"), "-out", str(self.renewed_cert),
                        "-days", "10", "-subj", "/CN=fake"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.renew.write_text(
            "import pathlib, sys\n"
            "if pathlib.Path(__file__).with_name(sys.argv[1] + '.fail').exists(): raise SystemExit(7)\n"
            f"sys.stdout.buffer.write(pathlib.Path({str(self.renewed_cert)!r}).read_bytes())\n"
        )
        self.config = self.root / "fleet.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_refresh(self, hosts, threshold=86400):
        self.config.write_text(json.dumps({"hosts": hosts}))
        return subprocess.run([
            sys.executable, str(SCRIPT), "--fleet-config", str(self.config),
            "--renew-command", f"{sys.executable} {self.renew}",
            "--threshold-seconds", str(threshold),
        ], capture_output=True, text=True)

    def test_nonempty_fleet_success_and_statuses(self):
        cert = self.root / "valid.cert"
        cert.write_bytes(self.renewed_cert.read_bytes())
        result = self.run_refresh([{"name": "valid", "cert": str(cert)},
                                  {"name": "missing", "cert": str(self.root / "missing.cert")}])
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["start_lane_autodispatch"])
        self.assertEqual([row["status"] for row in payload["hosts"]], ["valid", "missing"])
        self.assertEqual(payload["hosts"][1]["final_status"], "valid")
        self.assertTrue((self.root / "missing.cert").read_text().startswith("-----BEGIN CERTIFICATE-----"))

    def test_partial_failure_preserves_old_certificates(self):
        old = self.root / "old.cert"
        old.write_text("old certificate")
        (self.root / "bad.fail").touch()
        result = self.run_refresh([{"name": "good", "cert": str(self.root / "good.cert")},
                                  {"name": "bad", "cert": str(self.root / "bad.cert")}])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "good.cert").exists())
        self.assertFalse((self.root / "bad.cert").exists())
        self.assertEqual(old.read_text(), "old certificate")
        self.assertFalse(json.loads(result.stdout)["start_lane_autodispatch"])

    def test_restart_is_idempotent(self):
        cert = self.root / "cert"
        cert.write_bytes(self.renewed_cert.read_bytes())
        first = self.run_refresh([{"name": "host", "cert": str(cert)}])
        self.assertEqual(first.returncode, 0)
        second = self.run_refresh([{"name": "host", "cert": str(cert)}])
        self.assertEqual(second.returncode, 0)
        self.assertEqual(json.loads(second.stdout)["renewed"], [])

    def test_need_ok_writes_only_the_reachable_hosts(self):
        hosts = [{"name": "ib1", "cert": str(self.root / "ib1.cert"), "cooldown": True},
                 {"name": "ib2", "cert": str(self.root / "ib2.cert"), "cooldown": True},
                 {"name": "ib3", "cert": str(self.root / "ib3.cert")},
                 {"name": "ib4", "cert": str(self.root / "ib4.cert")},
                 {"name": "ib5", "cert": str(self.root / "ib5.cert")}]
        self.config.write_text(json.dumps({"hosts": hosts}))
        output = self.root / "fleet-out.json"
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--fleet-config", str(self.config),
            "--renew-command", f"{sys.executable} {self.renew}",
            "--threshold-seconds", "86400",
            "--need-ok", "3", "--output-fleet-config", str(output),
        ], capture_output=True, text=True)
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(payload["start_lane_autodispatch"])
        self.assertEqual(payload["renewed"], ["ib3", "ib4", "ib5"])
        self.assertEqual(payload["errors"], [])
        self.assertIn("ok=1 hosts_ok=3 skipped_cooldown=2", payload["status_line"])
        written = json.loads(output.read_text())
        self.assertEqual([host["name"] for host in written["hosts"]], ["ib3", "ib4", "ib5"])


if __name__ == "__main__":
    unittest.main()
