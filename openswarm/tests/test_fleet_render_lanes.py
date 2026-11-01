"""`openswarm fleet render-lanes`: one fleet inventory renders (and checks) every host's lanes.json."""
import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"
INVENTORY = HERE / "fixtures" / "fleet-inventory.json"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

# Running bin/openswarm as a script recompiles the whole source on every invocation;
# loading it as a module reuses the cached bytecode, the same way tests/_load.py does.
CLI_BOOTSTRAP = (
    "import importlib.machinery, importlib.util;"
    "loader = importlib.machinery.SourceFileLoader('openswarm_cli', {path!r});"
    "spec = importlib.util.spec_from_loader('openswarm_cli', loader);"
    "module = importlib.util.module_from_spec(spec);"
    "loader.exec_module(module);"
    "module.main()"
)


class FleetRenderLanesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-render-lanes-")
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def inventory(self, hosts, name="fleet.json"):
        path = self.root / name
        path.write_text(json.dumps({"hosts": hosts}, indent=2))
        return str(path)

    def lane(self, name, **fields):
        return {"name": name, "backend": "codex", "model": "gpt-5-codex", "effort": "high",
                "repo": "/srv/phanes", **fields}

    def render(self, inventory, host=None, check=False):
        """Run the command and return its JSON report and exit code."""
        output = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(output):
            try:
                xs.cmd_fleet_render_lanes(argparse.Namespace(inventory=inventory, host=host, check=check))
            except SystemExit as exit_code:
                code = exit_code.code or 0
        return json.loads(output.getvalue()), code

    def test_render_writes_one_lanes_json_per_host(self):
        report, code = self.render(str(INVENTORY))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(report["hosts"]), ["gorelay", "ib1", "local"])
        for host in report["hosts"].values():
            self.assertEqual(sorted(host), ["lanes"])
            for lane in host["lanes"]:
                self.assertTrue(set(lane) <= set(xs.LANE_CONFIG_FIELDS), lane)
        gorelay = report["hosts"]["gorelay"]["lanes"]
        self.assertEqual([lane["name"] for lane in gorelay], ["A1", "A2"])
        self.assertEqual(list(gorelay[0]), ["name", "backend", "model", "repo", "effort"])
        self.assertEqual(gorelay[0]["repo"], "/home/me/work/phanes")
        self.assertEqual(gorelay[1]["memory_max"], "700M")
        self.assertEqual(report["hosts"]["local"]["lanes"][0]["name"], "K1")

    def test_render_of_one_host_leaves_the_other_hosts_out(self):
        report, code = self.render(str(INVENTORY), host="gorelay")
        self.assertEqual(code, 0)
        self.assertEqual(list(report["hosts"]), ["gorelay"])

    def test_render_rejects_a_host_that_the_inventory_does_not_know(self):
        with self.assertRaises(ValueError) as raised:
            self.render(str(INVENTORY), host="nosuchhost")
        self.assertIn("unknown fleet host in inventory: nosuchhost", str(raised.exception))

    def test_render_rejects_a_duplicate_lane_name(self):
        inventory = self.inventory({"gorelay": {"kind": "ssh", "ssh": "gorelay", "lanes": [
            self.lane("A1"), self.lane("A1", backend="cursor", model="composer-2.5")]}})
        with self.assertRaises(ValueError) as raised:
            self.render(inventory)
        message = str(raised.exception)
        self.assertIn("duplicate lane name 'A1'", message)
        self.assertIn("lanes[1]", message)

    def test_render_rejects_an_unknown_lane_field(self):
        inventory = self.inventory({"gorelay": {"kind": "ssh", "ssh": "gorelay", "lanes": [
            self.lane("A1", backnd="codex")]}})
        with self.assertRaises(ValueError) as raised:
            self.render(inventory)
        message = str(raised.exception)
        self.assertIn("unknown field 'backnd'", message)
        self.assertIn("lanes[0]", message)

    def test_render_rejects_an_unknown_host_field(self):
        inventory = self.inventory({"gorelay": {"ssh": "gorelay", "ssh_host": "gorelay",
                                                 "lanes": [self.lane("A1")]}})
        with self.assertRaises(ValueError) as raised:
            self.render(inventory)
        self.assertIn("unknown field 'ssh_host'", str(raised.exception))

    def test_render_rejects_a_host_without_a_transport(self):
        inventory = self.inventory({"gorelay": {"kind": "ssh", "lanes": [self.lane("A1")]}})
        with self.assertRaises(ValueError) as raised:
            self.render(inventory)
        self.assertIn("ssh host must be a non-empty name", str(raised.exception))

    def test_rendered_lanes_are_readable_by_the_lane_config_reader(self):
        repo = self.root / "repo"
        repo.mkdir()
        inventory = self.inventory({"local": {"kind": "local", "lanes": [
            self.lane("K1", repo=str(repo))]}})
        report, code = self.render(inventory)
        self.assertEqual(code, 0)
        config = self.root / "lanes.json"
        config.write_text(json.dumps(report["hosts"]["local"]))
        with mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                               str(config) if name == "LANES_FILE" else default):
            lanes = xs.lane_config()
        self.assertEqual([lane["name"] for lane in lanes], ["K1"])
        self.assertEqual(lanes[0]["effort"], "high")
        self.assertEqual(lanes[0]["repo"], str(repo))

    def test_check_reports_stale_missing_and_changed_lanes(self):
        lanes_file = self.root / "lanes.json"
        lanes_file.write_text(json.dumps({"lanes": [
            self.lane("A1", model="gpt-5-codex-mini"), self.lane("A2")]}) )
        inventory = self.inventory({"gorelay": {"kind": "local", "lanes_file": str(lanes_file),
                                                 "lanes": [self.lane("A1"), self.lane("M12")]}})
        report, code = self.render(inventory, check=True)
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "failed")
        host = report["hosts"]["gorelay"]
        self.assertEqual(host["extra"], ["A2"])
        self.assertEqual(host["missing"], ["M12"])
        self.assertEqual(host["changed"], [{"lane": "A1", "fields": ["model"]}])

    def test_check_is_clean_when_the_host_matches_the_inventory(self):
        lanes_file = self.root / "lanes.json"
        lanes_file.write_text(json.dumps({"lanes": [self.lane("A1"), self.lane("A2")]}))
        inventory = self.inventory({"gorelay": {"kind": "local", "lanes_file": str(lanes_file),
                                                 "lanes": [self.lane("A1"), self.lane("A2")]}})
        report, code = self.render(inventory, check=True)
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["hosts"]["gorelay"],
                         {"ok": True, "extra": [], "missing": [], "changed": []})

    def test_check_reports_a_host_without_a_lanes_file(self):
        inventory = self.inventory({"gorelay": {
            "kind": "local", "lanes_file": str(self.root / "absent.json"), "lanes": [self.lane("A1")]}})
        report, code = self.render(inventory, check=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["hosts"]["gorelay"]["missing"], ["A1"])

    def test_check_reads_the_lanes_file_of_a_remote_host(self):
        inventory = self.inventory({"gorelay": {"kind": "ssh", "ssh": "gorelay",
                                                 "lanes": [self.lane("A1")]}})
        remote = json.dumps({"lanes": [self.lane("A1"), self.lane("A2")]}).encode()
        with mock.patch.object(xs, "host_run", return_value=(0, remote, b"")) as transport:
            report, code = self.render(inventory, check=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["hosts"]["gorelay"]["extra"], ["A2"])
        host, command = transport.call_args.args[:2]
        self.assertEqual(host["kind"], "ssh")
        self.assertEqual(host["ssh"], "gorelay")
        self.assertIn("cat", command)
        self.assertIn("lanes.json", command)

    def test_check_fails_when_the_remote_lanes_file_cannot_be_read(self):
        inventory = self.inventory({"gorelay": {"kind": "ssh", "ssh": "gorelay",
                                                 "lanes": [self.lane("A1")]}})
        with mock.patch.object(xs, "host_run", return_value=(255, b"", b"unreachable")):
            with self.assertRaises(ValueError) as raised:
                self.render(inventory, check=True)
        self.assertIn("cannot read lanes file on gorelay", str(raised.exception))

    def test_cli_renders_and_checks_one_host(self):
        fixture = json.loads(INVENTORY.read_text())
        local_lane = fixture["hosts"]["local"]["lanes"][0]
        lanes_file = self.root / "lanes.json"
        lanes_file.write_text(json.dumps({"lanes": [local_lane, self.lane("A9")]}))
        env = dict(os.environ, OPENSWARM_HOME=str(self.root / "home"),
                   OPENSWARM_LANES_FILE=str(lanes_file))
        rendered = subprocess.run(
            [sys.executable, "-c", CLI_BOOTSTRAP.format(path=str(CLI)), "fleet", "render-lanes",
             "--inventory", str(INVENTORY), "--host", "gorelay"],
            env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual(list(json.loads(rendered.stdout)["hosts"]), ["gorelay"])
        checked = subprocess.run(
            [sys.executable, "-c", CLI_BOOTSTRAP.format(path=str(CLI)), "fleet", "render-lanes",
             "--inventory", str(INVENTORY), "--host", "local", "--check"],
            env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(checked.returncode, 1)
        report = json.loads(checked.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["hosts"]["local"]["extra"], ["A9"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
