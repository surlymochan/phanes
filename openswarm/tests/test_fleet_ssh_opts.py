"""The fleet SSH transport: one option vector for status, exec, harvest and lane assign.

The fleet host entries may carry `jump` (a hop chain), `ssh_options` (a key/value table),
`connect_timeout` and `control_persist`. Every command builds the same ssh argv and the
same GIT_SSH_COMMAND from them, a ProxyCommand with spaces stays one argument, and a host
without those fields keeps the transport it had before (Refs #1353).
"""
import argparse
import contextlib
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()


class FakeSsh:
    """Record every transport argv and answer with a fixed payload."""

    def __init__(self, stdout=b"{}"):
        self.calls = []
        self.stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.stdout, b"")

    @property
    def argv(self):
        return self.calls[0]


class FleetSshOptionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-fleet-ssh-")
        self.root = pathlib.Path(self.tmp.name)
        self.patch = mock.patch.object(xs, "DATA", str(self.root))
        self.patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.patch.stop)

    def config(self, hosts):
        (self.root / "fleet.json").write_text(json.dumps({"hosts": hosts}))

    def worker(self, **fields):
        host = {"name": "worker", "kind": "ssh", "ssh": "worker-alias"}
        host.update(fields)
        return host

    def test_ssh_argv_carries_the_configured_options(self):
        host = self.worker(jump=["gorelay-root", "ctl@127.0.0.1:22029"], connect_timeout=20,
                           control_persist=600, ssh_options={"ServerAliveInterval": 15})
        argv = xs.fleet_ssh_argv(host, "--", "true")
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-3:], ["worker-alias", "--", "true"])
        self.assertIn("ProxyJump=gorelay-root,ctl@127.0.0.1:22029", argv)
        self.assertIn("ConnectTimeout=20", argv)
        self.assertIn("ControlMaster=auto", argv)
        self.assertIn("ControlPersist=600", argv)
        self.assertIn("ServerAliveInterval=15", argv)
        control_path = next(item for item in argv if item.startswith("ControlPath="))
        self.assertTrue(control_path.startswith("ControlPath=" + os.path.expanduser("~")))
        self.assertIn("%C", control_path)
        # Every option is one `-o Key=Value` pair, and the options precede the destination.
        self.assertEqual(argv[1:len(argv) - 3:2], ["-o"] * ((len(argv) - 4) // 2))

    def test_jump_accepts_a_single_hop_and_a_chain(self):
        self.assertIn("ProxyJump=bastion", xs.fleet_ssh_options(self.worker(jump="bastion")))
        self.assertIn("ProxyJump=bastion,relay", xs.fleet_ssh_options(self.worker(jump=["bastion", "relay"])))

    def test_proxy_command_with_spaces_stays_one_argv_item(self):
        host = self.worker(ssh_options={"ProxyCommand": "ssh -W %h:%p bastion"})
        argv = xs.fleet_ssh_argv(host, "--", "true")
        self.assertIn("ProxyCommand=ssh -W %h:%p bastion", argv)
        self.assertEqual(argv.count("ProxyCommand=ssh -W %h:%p bastion"), 1)
        # The quoted command string splits back into the very same argv.
        self.assertEqual(shlex.split(shlex.join(argv)), argv)

    def test_git_ssh_command_matches_the_ssh_argv(self):
        host = self.worker(jump="bastion", connect_timeout=20, control_persist=600,
                           ssh_options={"ProxyCommand": "ssh -W %h:%p bastion"})
        command = xs.fleet_ssh_command(host)
        self.assertTrue(command.startswith("ssh "))
        self.assertEqual(shlex.split(command), ["ssh", *xs.fleet_ssh_options(host)])
        argv = xs.fleet_ssh_argv(host, "--", "true")
        self.assertEqual(argv[:len(shlex.split(command))], shlex.split(command))
        # The space-bearing ProxyCommand survives the shell round trip of the git value.
        self.assertIn("ProxyCommand=ssh -W %h:%p bastion", shlex.split(command))

    def test_unconfigured_host_keeps_the_current_transport(self):
        host = self.worker()
        self.assertEqual(xs.fleet_ssh_options(host), [])
        self.assertEqual(xs.fleet_ssh_argv(host, "--", "true"), ["ssh", "worker-alias", "--", "true"])
        self.assertEqual(xs.fleet_ssh_command(host), "ssh")
        fake = FakeSsh()
        with mock.patch.object(xs.subprocess, "run", side_effect=fake):
            self.assertEqual(xs.host_run(host, "true", timeout=7), (0, b"{}", b""))
        self.assertEqual(fake.argv, ["ssh", "worker-alias", "--", "true"])

    def test_fleet_config_accepts_the_new_fields(self):
        self.config([self.worker(jump="bastion", connect_timeout=20, control_persist="10m",
                                 ssh_options={"ServerAliveInterval": 15}),
                     {"name": "local", "kind": "local"}])
        hosts = xs.fleet_config()
        self.assertEqual(hosts["worker"]["jump"], "bastion")
        self.assertEqual(hosts["worker"]["connect_timeout"], 20)
        self.assertEqual(hosts["worker"]["control_persist"], "10m")
        self.assertEqual(hosts["worker"]["ssh_options"], {"ServerAliveInterval": 15})

    def test_fleet_config_rejects_broken_ssh_fields(self):
        cases = [
            {"jump": []},
            {"jump": ["-oProxyCommand=evil"]},
            {"jump": 7},
            {"ssh_options": {"not an option": "value"}},
            {"ssh_options": {"ControlPath": True}},
            {"connect_timeout": 0},
            {"connect_timeout": "20"},
            {"control_persist": -1},
            {"control_persist": "forever"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self.config([self.worker(**fields)])
                with self.assertRaises(ValueError):
                    xs.fleet_config()

    def test_ssh_fields_require_an_ssh_host(self):
        self.config([{"name": "box", "kind": "instabox", "box": "task-box", "jump": "bastion"}])
        with self.assertRaises(ValueError) as raised:
            xs.fleet_config()
        self.assertIn("ssh options require an ssh host", str(raised.exception))

    def test_fleet_exec_uses_the_configured_options(self):
        self.config([self.worker(jump="bastion", connect_timeout=20)])
        fake = FakeSsh(b'{"lanes": [], "active_runs": []}')
        with mock.patch.object(xs.subprocess, "run", side_effect=fake), \
                contextlib.redirect_stdout(io.TextIOWrapper(io.BytesIO())), \
                contextlib.redirect_stderr(io.TextIOWrapper(io.BytesIO())):
            with self.assertRaises(SystemExit):
                xs.cmd_fleet(argparse.Namespace(fleet_cmd="exec", host="worker", timeout=30,
                                                args=["--", "fleet", "_snapshot"]))
        self.assertEqual(fake.argv[:7], ["ssh", "-o", "ConnectTimeout=20", "-o", "ProxyJump=bastion",
                                         "worker-alias", "--"])

    def test_fleet_status_uses_the_configured_options(self):
        self.config([self.worker(jump="bastion")])
        fake = FakeSsh(b'{"lanes": [], "active_runs": []}')
        with mock.patch.object(xs.subprocess, "run", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_fleet(argparse.Namespace(fleet_cmd="status", json=True))
        self.assertEqual(fake.argv[:5], ["ssh", "-o", "ProxyJump=bastion", "worker-alias", "--"])

    def test_fleet_harvest_uses_the_configured_options(self):
        self.config([self.worker(jump="bastion")])
        fake = FakeSsh(b'{"lanes": [], "active_runs": []}')
        with mock.patch.object(xs.subprocess, "run", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_fleet_harvest(argparse.Namespace(hosts=None, into=str(self.root / "harvest"), json=True))
        self.assertEqual(fake.argv[:5], ["ssh", "-o", "ProxyJump=bastion", "worker-alias", "--"])

    def test_lane_assign_uses_the_configured_options(self):
        card = self.root / "card.md"
        card.write_text("do the thing")
        self.config([self.worker(jump="bastion")])
        fake = FakeSsh()
        with mock.patch.object(xs.subprocess, "run", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()):
            code = xs.fleet_lane_assign(xs.fleet_host("worker"),
                                         ["lane", "assign", "L1", "--task-file", str(card)])
        self.assertEqual(code, 1)  # the fake transport answers no verified card
        self.assertEqual(fake.argv[:5], ["ssh", "-o", "ProxyJump=bastion", "worker-alias", "--"])

    def test_git_ssh_command_is_one_shell_word(self):
        host = self.worker(ssh_options={"ProxyCommand": "ssh -W %h:%p bastion"})
        value = xs.fleet_ssh_command(host)
        # Git runs core.sshCommand through the shell: the value stays one word and splits
        # back into the very same ssh argv.
        self.assertEqual(shlex.split(shlex.quote(value)), [value])
        self.assertEqual(shlex.split(value), ["ssh", *xs.fleet_ssh_options(host)])

    def test_remote_git_command_stays_plain(self):
        fake = FakeSsh()
        with mock.patch.object(xs.subprocess, "run", side_effect=fake):
            xs.host_git(self.worker(), "~/work/project", ["rev-parse", "HEAD"])
        self.assertNotIn("GIT_SSH_COMMAND", fake.argv[-1])
        self.assertTrue(fake.argv[-1].startswith("git -C"))


if __name__ == "__main__":
    unittest.main()
