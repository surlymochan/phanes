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

import _load

xs = _load.load()


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-fleet-")
        self.root = pathlib.Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def test_status_reports_reachable_and_unreachable_hosts(self):
        self.config([{"name": "local", "kind": "local"}, {"name": "ssh", "kind": "ssh", "ssh": "host-alias"}])
        def status(host, command, **kw):
            if host["name"] == "ssh":
                return 255, b"", b"unreachable"
            return 0, json.dumps({"lanes": [{"name": "L1", "state": "idle", "model": "m", "current_run": None}],
                                  "active_runs": [{"id": "r1", "state": "running"}]}).encode(), b""
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", side_effect=status), contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=True))
        rows = json.loads(output.getvalue())["hosts"]
        self.assertTrue(rows[0]["reachable"])
        self.assertEqual(rows[0]["lanes"][0]["model"], "m")
        self.assertFalse(rows[1]["reachable"])

    def test_text_status_uses_reachable_snapshot_without_lanes(self):
        self.config([{"name": "local", "kind": "local"}])
        output = io.StringIO()
        with mock.patch.object(xs, "host_run", return_value=(0, b'{"lanes": [], "active_runs": []}', b"")), \
                contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertEqual(output.getvalue(), "local  no lanes  lanes=0 active=0\n")

    def test_removed_transport_rejected(self):
        self.config([{"name": "box", "kind": "instabox", "box": "task-box"}])
        with self.assertRaises(ValueError):
            xs.fleet_config()

    def test_empty_lane_config_is_valid(self):
        config = self.root / "lanes.json"
        config.write_text('{"lanes": []}')
        with mock.patch.object(xs, "envvar", side_effect=lambda name, default=None:
                               str(config) if name == "LANES_FILE" else default):
            self.assertEqual(xs.lane_config(), [])

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
                mock.patch.object(xs, "run_digest", side_effect=lambda rid, parse_events=True: {"id": rid, "state": "running"}) as digest:
            snapshot = xs.fleet_snapshot()
        digest.assert_called_once_with("active", parse_events=False)
        self.assertEqual([row["id"] for row in snapshot["active_runs"]], ["active"])

    def test_full_snapshot_never_parses_event_logs(self):
        # Parsing every run's event log made `fleet status` time out (host shown unreachable, runs hidden).
        runs = self.root / "runs"
        events = self.root / "events.jsonl"
        events.write_text("{}\n")
        for name, state in (("done", "finished"), ("active", "running")):
            directory = runs / name
            directory.mkdir(parents=True)
            (directory / "status.json").write_text(json.dumps({"state": state, "events": str(events)}))
        with mock.patch.object(xs, "RUNS", str(runs)), \
                mock.patch.object(xs.WatchMessages, "feed", return_value=[]) as feed:
            snapshot = xs.fleet_snapshot()
        feed.assert_not_called()
        self.assertEqual([row["id"] for row in snapshot["active_runs"]], ["active"])

    def test_text_status_shows_config_invalid_with_reason(self):
        self.config([{"name": "host", "kind": "local"}])
        output = io.StringIO()
        snapshot = {"lanes": [], "active_runs": [], "lane_config_valid": False,
                    "lane_config_error": "invalid lane config /etc/swrm/lanes.json: bad JSON"}
        with mock.patch.object(xs, "host_json", return_value=snapshot), contextlib.redirect_stdout(output):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=False))
        self.assertIn("host  config invalid  lanes=0 active=0", output.getvalue())
        self.assertIn("invalid lane config /etc/swrm/lanes.json: bad JSON", output.getvalue())

    def test_transport_argv_binary_input_and_timeout(self):
        for host, prefix in [({"kind": "ssh", "ssh": "alias"}, ["ssh", "alias", "--"])]:
            with mock.patch.object(xs.subprocess, "run", return_value=subprocess.CompletedProcess([], 3, b"out", b"err")) as run:
                self.assertEqual(xs.host_run(host, "command", timeout=7, input=b"\x00bundle"), (3, b"out", b"err"))
                self.assertEqual(run.call_args.args[0], prefix + ["command"])
                self.assertEqual(run.call_args.kwargs["input"], b"\x00bundle")
        self.assertEqual(xs.host_run({"kind": "local"}, "exec sleep 5", timeout=0.01)[0], 124)

    def test_exec_passes_arguments_and_exit_code_without_shell_expansion(self):
        binary = self.root / "stub"
        binary.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n")
        binary.chmod(0o755)
        self.config([{"name": "local", "kind": "local", "swrm": str(binary)}])
        proc = subprocess.run([sys.executable, str(pathlib.Path(_load.ROOT) / "bin" / "swrm"),
                               "fleet", "exec", "local", "--", "run", "--prompt", "$(false); literal"],
                              env={**xs.os.environ, "OPENSWARM_HOME": str(self.root)}, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(json.loads(proc.stdout), ["run", "--prompt", "$(false); literal"])

    def test_invalid_config_rejected(self):
        self.config([{"name": "x", "kind": "ssh", "ssh": "-unsafe"}])
        with self.assertRaises(ValueError):
            xs.fleet_config()

    @unittest.skipIf(os.name == "nt", 'the test drives an extensionless `ssh` shim placed on PATH; Windows process creation only resolves PATHEXT executables, so the fake remote transport cannot be discovered (local-only ledger behaviour is covered by other fleet tests)')
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
            sys.argv = ["swrm", "fleet", "ledger", "pull"]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                xs.main()
            rows = [json.loads(line) for line in destination.read_text().splitlines()]
            first_pull = destination.read_bytes()
            sys.argv = ["swrm", "fleet", "ledger", "pull"]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                xs.main()
            self.assertEqual(destination.read_bytes(), first_pull)

            custom_destination = self.root / "custom.jsonl"
            sys.argv = ["swrm", "fleet", "ledger", "pull", "--hosts", "remote",
                        "--into", str(custom_destination)]
            with mock.patch.dict(xs.os.environ, {
                "PATH": str(fake_bin) + os.pathsep + xs.os.environ.get("PATH", ""),
                "OPENSWARM_TEST_SSH_ROOT": str(self.root),
            }), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                xs.main()
            self.assertEqual([json.loads(line)["run_id"] for line in custom_destination.read_text().splitlines()],
                             ["shared", "remote-run"])

            sys.argv = ["swrm", "ledger", "--fleet", "--json"]
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

    def test_ledger_pull_fails_when_every_host_is_unreachable(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        ssh = fake_bin / "ssh"
        ssh.write_text(f"#!{sys.executable}\nraise SystemExit(255)\n")
        ssh.chmod(0o755)
        self.config([{"name": "down", "kind": "ssh", "ssh": "down"}])
        original_argv = sys.argv
        try:
            sys.argv = ["swrm", "fleet", "ledger", "pull", "--into", str(self.root / "out.jsonl")]
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
