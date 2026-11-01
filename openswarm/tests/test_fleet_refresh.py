import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[2]
SCRIPT = ROOT / "tools" / "ib-refresh.py"
SPEC = importlib.util.spec_from_file_location("ib_refresh", SCRIPT)
IB_REFRESH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IB_REFRESH)


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

    def test_batchmode_probe_failure_is_excluded_from_hosts_ok(self):
        """A non-zero BatchMode probe exit keeps the host out of hosts_ok (Refs #954)."""
        cert = self.root / "valid.cert"
        cert.write_bytes(self.renewed_cert.read_bytes())
        ssh_config = self.root / "ssh_config"
        ssh_config.write_text("Host ib1 ib2\n    User openswarm\n")
        hosts = [{"name": "ib1", "cert": str(cert)}, {"name": "ib2", "cert": str(cert)}]
        self.config.write_text(json.dumps({
            "need_ok": 1,
            "ssh_probe": {"enabled": True, "ssh_config": str(ssh_config)},
            "hosts": hosts,
        }))
        probes = []

        def fake_runner(argv):
            probes.append(list(argv))
            return 255, "", "Permission denied (publickey)"

        result = IB_REFRESH.refresh(self.config, "false", 86400, probe_runner=fake_runner)
        fields = dict(field.split("=", 1) for field in result["status_line"].split())
        self.assertEqual(fields["hosts_ok"], "0")
        self.assertEqual(fields["ok"], "0")
        self.assertFalse(result["start_lane_autodispatch"])
        self.assertEqual(len(probes), 2)
        for argv in probes:
            self.assertIn("BatchMode=yes", argv)
        self.assertTrue(all(row["probe_status"] == "failed" for row in result["hosts"]))

    def test_batchmode_probe_success_still_counts(self):
        """A zero BatchMode probe exit keeps the host in hosts_ok (Refs #954)."""
        cert = self.root / "valid.cert"
        cert.write_bytes(self.renewed_cert.read_bytes())
        ssh_config = self.root / "ssh_config"
        ssh_config.write_text("Host ib1\n    User openswarm\n")
        hosts = [{"name": "ib1", "cert": str(cert)}]
        self.config.write_text(json.dumps({
            "need_ok": 1,
            "ssh_probe": {"enabled": True, "ssh_config": str(ssh_config)},
            "hosts": hosts,
        }))

        def fake_runner(argv):
            return 0, "OK\n", ""

        result = IB_REFRESH.refresh(self.config, "false", 86400, probe_runner=fake_runner)
        fields = dict(field.split("=", 1) for field in result["status_line"].split())
        self.assertEqual(fields["hosts_ok"], "1")
        self.assertEqual(fields["ok"], "1")
        self.assertEqual(result["hosts"][0]["probe_status"], "ok")


if __name__ == "__main__":
    unittest.main()
