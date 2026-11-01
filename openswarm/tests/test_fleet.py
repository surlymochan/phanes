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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-")
        self.root = pathlib.Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def test_status_does_not_contact_or_wake_paused_instabox(self):
        self.config([{"name": "local", "kind": "local"}, {"name": "box", "kind": "instabox", "box": "task-box"},
                     {"name": "ssh", "kind": "ssh", "ssh": "host-alias"}])
        def status(host, command, **kw):
            self.assertNotEqual(host["name"], "box")
            if host["name"] == "ssh":
                return 255, b"", b"unreachable"
            return 0, json.dumps({"lanes": [{"name": "L1", "state": "idle", "model": "m", "current_run": None}],
                                  "active_runs": [{"id": "r1", "state": "running"}]}).encode(), b""
        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "task-box  Paused\n", "")) as ls, \
                mock.patch.object(xs, "host_run", side_effect=status) as transport, contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=True))
        rows = json.loads(output.getvalue())["hosts"]
        self.assertTrue(rows[0]["reachable"])
        self.assertEqual(rows[0]["lanes"][0]["model"], "m")
        self.assertTrue(rows[1]["paused"])
        self.assertFalse(rows[1]["reachable"])
        self.assertFalse(rows[2]["reachable"])
        self.assertEqual(transport.call_count, 2)
        ls.assert_called_once_with(["instabox", "ls"], timeout=20)

    def test_unknown_instabox_state_does_not_open_ssh(self):
        self.config([{"name": "box", "kind": "instabox", "box": "task-box"}])
        with mock.patch.object(xs, "sh", return_value=(1, "", "offline")), \
                mock.patch.object(xs, "host_run") as transport, contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        transport.assert_not_called()

    def test_text_status_uses_reachable_snapshot_without_lanes(self):
        self.config([{"name": "box", "kind": "instabox", "box": "task-box"}])
        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "task-box  Running\n", "")), \
                mock.patch.object(xs, "host_run", return_value=(0, b'{"lanes": [], "active_runs": []}', b"")), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertEqual(output.getvalue(), "box  no lanes  lanes=0 active=0\n")

    def test_empty_lane_snapshot_keeps_capacity_fields_and_status(self):
        self.config([{"name": "local", "kind": "local"},
                     {"name": "paused-box", "kind": "instabox", "box": "paused-box"}])
        snapshot = {"lanes": [], "active_runs": [], "provider": "minimax-cn", "total": 4,
                    "lane_config_valid": True}

        def status(host, command, **kw):
            self.assertEqual(host["name"], "local")
            return 0, json.dumps(snapshot).encode(), b""

        structured = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "paused-box  Paused\n", "")) as ls, \
                mock.patch.object(xs, "host_run", side_effect=status) as transport, \
                contextlib.redirect_stdout(structured):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=True))
        rows = json.loads(structured.getvalue())["hosts"]
        self.assertTrue(rows[0]["reachable"])
        self.assertEqual(rows[0]["status"], "no lanes")
        self.assertEqual(rows[0]["provider"], "minimax-cn")
        self.assertEqual(rows[0]["total"], 4)
        self.assertTrue(rows[1]["paused"])
        self.assertEqual(transport.call_count, 1)
        ls.assert_called_once_with(["instabox", "ls"], timeout=20)

        output = io.StringIO()
        with mock.patch.object(xs, "sh", return_value=(0, "paused-box  Paused\n", "")), \
                mock.patch.object(xs, "host_run", side_effect=status), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertIn("local  no lanes  lanes=0 active=0", output.getvalue())
        self.assertNotIn("config invalid", output.getvalue())

    def test_empty_lane_config_is_valid(self):
        config = self.root / "lanes.json"
        config.write_text('{"lanes": []}')
        with mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                               str(config) if name == "LANES_FILE" else default):
            self.assertEqual(xs.lane_config(), [])

    def duplicate_lane_config(self):
        """A lane topology that repeats a lane name must stay unreadable for operators."""
        repo = self.root / "repo"
        repo.mkdir()
        config = self.root / "lanes.json"
        config.write_text(json.dumps({"lanes": [
            {"name": "L1", "backend": "cursor", "model": "composer-2.5", "repo": str(repo)},
            {"name": "L1", "backend": "cursor", "model": "composer-2.5", "repo": str(repo)},
        ]}))
        return config

    def test_duplicate_lane_name_is_rejected_with_readable_error(self):
        config = self.duplicate_lane_config()
        with mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                               str(config) if name == "LANES_FILE" else default):
            with self.assertRaises(ValueError) as raised:
                xs.lane_config()
        message = str(raised.exception)
        self.assertIn("invalid lane config", message)
        self.assertIn(str(config), message)
        self.assertIn("lanes[1]", message)
        self.assertIn("duplicate lane name 'L1'", message)

    def test_capacity_snapshot_reports_duplicate_lane_name_reason(self):
        config = self.duplicate_lane_config()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"OPENSWARM_FLEET_CAPACITY_ONLY": "1"}))
            stack.enter_context(mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                                                  str(config) if name == "LANES_FILE" else default))
            stack.enter_context(mock.patch.object(xs, "execution_provider", return_value="provider"))
            stack.enter_context(mock.patch.object(xs, "load_policy", return_value={}))
            snapshot = xs.fleet_snapshot()
        self.assertEqual(snapshot["lanes"], [])
        self.assertFalse(snapshot["lane_config_valid"])
        self.assertIn("invalid lane config", snapshot["lane_config_error"])
        self.assertIn("duplicate lane name 'L1'", snapshot["lane_config_error"])
        self.assertIn("lanes[1]", snapshot["lane_config_error"])

    def test_fleet_snapshot_preserves_invalid_lane_config_reason(self):
        config = self.root / "lanes.json"
        config.write_text('{"lanes": [')
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                                                  str(config) if name == "LANES_FILE" else default))
            stack.enter_context(mock.patch.object(xs, "execution_provider", return_value="provider"))
            stack.enter_context(mock.patch.object(xs, "load_policy", return_value={}))
            stack.enter_context(mock.patch.object(xs, "agy_status", return_value={}))
            snapshot = xs.fleet_snapshot()
        self.assertEqual(snapshot["lanes"], [])
        self.assertFalse(snapshot["lane_config_valid"])
        self.assertIn("invalid lane config", snapshot["lane_config_error"])
        self.assertIn("lanes.json", snapshot["lane_config_error"])

    def test_capacity_snapshot_skips_model_probe_but_keeps_capacity_fields(self):
        with mock.patch.dict(os.environ, {"OPENSWARM_FLEET_CAPACITY_ONLY": "1"}), \
                mock.patch.object(xs, "agy_status") as status:
            snapshot = xs.fleet_snapshot()
        status.assert_not_called()
        self.assertIn("active_runs", snapshot)
        self.assertIn("providers", snapshot)
        self.assertEqual(snapshot["backends"], {})

    def test_capacity_snapshot_skips_completed_run_logs(self):
        runs = self.root / "runs"
        for name, state in (("done", "finished"), ("active", "running")):
            directory = runs / name
            directory.mkdir(parents=True)
            (directory / "status.json").write_text(json.dumps({"state": state}))
        with mock.patch.dict(os.environ, {"OPENSWARM_FLEET_CAPACITY_ONLY": "1"}), \
                mock.patch.object(xs, "RUNS", str(runs)), \
                mock.patch.object(xs, "run_digest", side_effect=lambda rid: {"id": rid, "state": "running"}) as digest:
            snapshot = xs.fleet_snapshot()
        digest.assert_called_once_with("active")
        self.assertEqual([row["id"] for row in snapshot["active_runs"]], ["active"])

    def test_text_status_shows_config_invalid_with_reason(self):
        self.config([{"name": "host", "kind": "local"}])
        output = io.StringIO()
        snapshot = {"lanes": [], "active_runs": [], "lane_config_valid": False,
                    "lane_config_error": "invalid lane config /etc/openswarm/lanes.json: bad JSON"}
        with mock.patch.object(xs, "host_json", return_value=snapshot), contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertIn("host  config invalid  lanes=0 active=0", output.getvalue())
        self.assertIn("invalid lane config /etc/openswarm/lanes.json: bad JSON", output.getvalue())

    def test_transport_argv_binary_input_and_timeout(self):
        for host, prefix in [({"kind": "ssh", "ssh": "alias"}, ["ssh", "alias", "--"]),
                             ({"kind": "instabox", "box": "task-box"}, ["instabox", "ssh", "task-box", "--"])]:
            with mock.patch.object(xs.subprocess, "run", return_value=subprocess.CompletedProcess([], 3, b"out", b"err")) as run:
                self.assertEqual(xs.host_run(host, "command", timeout=7, input=b"\x00bundle"), (3, b"out", b"err"))
                self.assertEqual(run.call_args.args[0], prefix + ["command"])
                self.assertEqual(run.call_args.kwargs["input"], b"\x00bundle")
        self.assertEqual(xs.host_run({"kind": "local"}, "exec sleep 5", timeout=0.01)[0], 124)

    def test_exec_passes_arguments_and_exit_code_without_shell_expansion(self):
        binary = self.root / "stub"
        binary.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n")
        binary.chmod(0o755)
        self.config([{"name": "local", "kind": "local", "openswarm": str(binary)}])
        proc = subprocess.run([sys.executable, str(pathlib.Path(_load.ROOT) / "bin" / "openswarm"),
                               "fleet", "exec", "local", "--", "run", "--prompt", "$(false); literal"],
                              env={**xs.os.environ, "OPENSWARM_HOME": str(self.root)}, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(json.loads(proc.stdout), ["run", "--prompt", "$(false); literal"])

    def fake_fleet_transport(self):
        """A fake ssh that answers the fleet snapshot and run-info calls of the harvest."""
        fake_bin = self.root / "bin"
        fake_bin.mkdir(exist_ok=True)
        ssh = fake_bin / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "alias = sys.argv[1]\n"
            "command = ' '.join(sys.argv[3:])\n"
            "root = pathlib.Path(os.environ['OPENSWARM_TEST_FLEET_ROOT'])\n"
            "spec = json.loads((root / (alias + '.json')).read_text())\n"
            "if '_snapshot' in command:\n"
            "    print(json.dumps(spec['snapshot']))\n"
            "elif '_run-info' in command:\n"
            "    if spec.get('exit_code'):\n"
            "        sys.stderr.write(spec.get('stderr', ''))\n"
            "        raise SystemExit(spec['exit_code'])\n"
            "    print(json.dumps(spec['document']))\n"
            "else:\n"
            "    raise SystemExit(9)\n"
        )
        ssh.chmod(0o755)
        return fake_bin

    def harvest(self, fake_bin, destination):
        """Run the fleet harvest against the fake fleet and return its exit code and manifest."""
        original_argv = sys.argv
        try:
            sys.argv = ["openswarm", "fleet", "harvest", "--json", "--into", str(destination)]
            with mock.patch.dict(xs.os.environ, {"PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                                                  "OPENSWARM_TEST_FLEET_ROOT": str(self.root)}), \
                    contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                try:
                    xs.main()
                    code = 0
                except SystemExit as exit_code:
                    code = exit_code.code
            return code, json.loads(output.getvalue())
        finally:
            sys.argv = original_argv

    def test_harvest_keeps_completed_bundle_when_a_host_fails(self):
        fake_bin = self.fake_fleet_transport()
        (self.root / "good.json").write_text(json.dumps({
            "snapshot": {"lanes": [{"name": "L1", "state": "idle", "current_run": "run-1"}], "active_runs": []},
            "document": {"id": "run-1", "state": "finished",
                         "result": {"id": "run-1", "status": "done", "branch": "openswarm/run-1",
                                    "commit": "a" * 40, "summary": "harvested good result"}}}))
        (self.root / "bad.json").write_text(json.dumps({
            "snapshot": {"lanes": [{"name": "L2", "state": "idle", "current_run": "run-2"}], "active_runs": []},
            "exit_code": 3, "stderr": "lane L2: result bundle unavailable\n"}))
        self.config([{"name": "good", "kind": "ssh", "ssh": "good"},
                     {"name": "bad", "kind": "ssh", "ssh": "bad"}])
        destination = self.root / "harvest"
        code, report = self.harvest(fake_bin, destination)
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual([entry["host"] for entry in report["completed"]], ["good"])
        self.assertEqual([entry["host"] for entry in report["failed"]], ["bad"])
        completed = report["completed"][0]
        self.assertEqual(completed["run_id"], "run-1")
        self.assertEqual(completed["lane"], "L1")
        bundle = pathlib.Path(completed["bundle"])
        self.assertTrue(bundle.is_file())
        self.assertGreater(bundle.stat().st_size, 0)
        self.assertEqual(json.loads(bundle.read_text())["result"]["summary"], "harvested good result")
        failed = report["failed"][0]
        self.assertEqual(failed["run_id"], "run-2")
        self.assertEqual(failed["lane"], "L2")
        self.assertEqual(failed["exit_code"], 3)
        self.assertIn("result bundle unavailable", failed["stderr"])
        # A failed host writes no bundle, so it cannot overwrite the completed one.
        self.assertFalse((destination / "bad" / "run-2.json").exists())
        self.assertEqual(sorted(path.name for path in (destination / "good").iterdir()), ["run-1.json"])

    def test_harvest_marks_a_bundle_without_result_json_as_failed(self):
        fake_bin = self.fake_fleet_transport()
        (self.root / "stale.json").write_text(json.dumps({
            "snapshot": {"lanes": [{"name": "L3", "state": "idle", "current_run": "run-3"}], "active_runs": []},
            "document": {"id": "run-3", "state": "finished"}}))
        self.config([{"name": "stale", "kind": "ssh", "ssh": "stale"}])
        destination = self.root / "harvest"
        code, report = self.harvest(fake_bin, destination)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["completed"], [])
        failed = report["failed"][0]
        self.assertEqual(failed["host"], "stale")
        self.assertEqual(failed["run_id"], "run-3")
        self.assertEqual(failed["exit_code"], 0)
        self.assertIn("result.json", failed["error"])
        self.assertFalse(destination.exists())

    def test_invalid_config_rejected(self):
        self.config([{"name": "x", "kind": "ssh", "ssh": "-unsafe"}])
        with self.assertRaises(ValueError):
            xs.fleet_config()

    def test_ledger_pull_merges_redacts_deduplicates_and_skips_unreachable_hosts(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, sys\n"
            "host = sys.argv[1]\n"
            "if host == 'down':\n"
            "    print('unreachable', file=sys.stderr)\n"
            "    raise SystemExit(255)\n"
            "assert sys.argv[2] == '--' and sys.argv[3].startswith('cat ')\n"
            "path = pathlib.Path(os.environ['OPENSWARM_TEST_SSH_ROOT']) / (host + '.jsonl')\n"
            "sys.stdout.buffer.write(path.read_bytes())\n"
        )
        ssh.chmod(0o755)
        self.config([{"name": "local", "kind": "local"},
                     {"name": "remote", "kind": "ssh", "ssh": "good"},
                     {"name": "down", "kind": "ssh", "ssh": "down"}])
        local_ledger = self.root / "local.jsonl"
        ledger_patch = mock.patch.object(xs, "LEDGER", str(local_ledger))
        ledger_patch.start()
        self.addCleanup(ledger_patch.stop)
        local_ledger.write_text(json.dumps({"run_id": "shared", "host": "same-host", "title": "local"}) + "\n")
        (self.root / "good.jsonl").write_text("\n".join([
            json.dumps({"run_id": "shared", "host": "same-host", "title": "duplicate"}),
            json.dumps({"run_id": "remote-run", "title": "token sk-12345678901234567890"}),
            "",
        ]))
        destination = self.root / "ledger-fleet.jsonl"
        original_argv = sys.argv
        output, errors = io.StringIO(), io.StringIO()
        ledger_output = io.StringIO()
        try:
            sys.argv = ["openswarm", "fleet", "ledger", "pull"]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                with self.assertRaises(SystemExit) as partial:
                    xs.main()
            self.assertEqual(partial.exception.code, 1)
            rows = [json.loads(line) for line in destination.read_text().splitlines()]
            first_pull = destination.read_bytes()
            sys.argv = ["openswarm", "fleet", "ledger", "pull"]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as repeated_partial:
                    xs.main()
            self.assertEqual(repeated_partial.exception.code, 1)
            self.assertEqual(destination.read_bytes(), first_pull)

            custom_destination = self.root / "custom.jsonl"
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--hosts", "remote",
                        "--into", str(custom_destination)]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                xs.main()
            self.assertEqual([json.loads(line)["run_id"] for line in custom_destination.read_text().splitlines()],
                             ["shared", "remote-run"])

            sys.argv = ["openswarm", "ledger", "--fleet", "--json"]
            with contextlib.redirect_stdout(ledger_output):
                xs.main()
        finally:
            sys.argv = original_argv

        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["run_id"] == "shared" for row in rows), 1)
        remote_row = next(row for row in rows if row["run_id"] == "remote-run")
        self.assertEqual(remote_row["host"], "remote")
        self.assertEqual(remote_row["title"], "token [REDACTED]")
        self.assertIn("down: unreachable", errors.getvalue())
        # The --fleet reader uses the merged file, not the local source ledger.
        self.assertEqual(json.loads(ledger_output.getvalue()), rows)

    def test_ledger_pull_json_manifest_marks_partial_and_retry_is_idempotent(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            "import os, pathlib, sys\n"
            "if sys.argv[1] == 'down': raise SystemExit(255)\n"
            "path = pathlib.Path(os.environ['OPENSWARM_TEST_SSH_ROOT']) / (sys.argv[1] + '.jsonl')\n"
            "sys.stdout.buffer.write(path.read_bytes())\n"
        )
        ssh.chmod(0o755)
        self.config([{"name": "good", "kind": "ssh", "ssh": "good"},
                     {"name": "down", "kind": "ssh", "ssh": "down"}])
        (self.root / "good.jsonl").write_text(json.dumps({"run_id": "task-1", "status": "done"}) + "\n")
        destination = self.root / "harvest.jsonl"
        original_argv = sys.argv
        try:
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--json", "--into", str(destination)]
            with mock.patch.dict(xs.os.environ, {"PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                                                  "OPENSWARM_TEST_SSH_ROOT": str(self.root)}), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as partial:
                    xs.main()
            report = json.loads(output.getvalue())
            self.assertEqual(partial.exception.code, 1)
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["entries"][0]["run_id"], "task-1")
            self.assertEqual(report["failures"][0]["lane"], "down")
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--json", "--hosts", "good",
                        "--into", str(destination)]
            with mock.patch.dict(xs.os.environ, {"PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                                                  "OPENSWARM_TEST_SSH_ROOT": str(self.root)}), \
                    contextlib.redirect_stdout(io.StringIO()) as retry_output:
                xs.main()
            self.assertEqual(len(destination.read_text().splitlines()), 1)
            self.assertEqual(json.loads(retry_output.getvalue())["status"], "success")
        finally:
            sys.argv = original_argv

    def test_ledger_pull_json_unknown_lane_is_non_retryable(self):
        self.config([{"name": "good", "kind": "ssh", "ssh": "good"}])
        original_argv = sys.argv
        try:
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--json", "--hosts", "missing"]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    xs.main()
            report = json.loads(output.getvalue())
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(report["failures"][0]["lane"], "missing")
            self.assertFalse(report["failures"][0]["retryable"])
        finally:
            sys.argv = original_argv

    def test_ledger_pull_fails_when_every_host_is_unreachable(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        ssh.write_text(f"#!{sys.executable}\nraise SystemExit(255)\n")
        ssh.chmod(0o755)
        self.config([{"name": "down", "kind": "ssh", "ssh": "down"}])
        original_argv = sys.argv
        try:
            sys.argv = ["openswarm", "fleet", "ledger", "pull", "--into", str(self.root / "out.jsonl")]
            with mock.patch.dict(xs.os.environ, {"PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", "")}):
                with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
                    xs.main()
        finally:
            sys.argv = original_argv
        self.assertEqual(error.exception.code, 1)
        self.assertFalse((self.root / "out.jsonl").exists())

    def test_ledger_pull_enforces_line_and_record_size_limits(self):
        oversized = b"{" + (b" " * xs.LEDGER_MAX_LINE_BYTES) + b"}\n"
        valid = json.dumps({"run_id": "kept"}).encode() + b"\n"
        self.assertEqual([row["run_id"] for row in xs.ledger_records(oversized + valid, "h")], ["kept"])
        self.assertEqual(xs.ledger_records(b"\n" * xs.LEDGER_MAX_HOST_LINES + valid, "h"), [])


if __name__ == "__main__":
    unittest.main()
