"""Offline contracts for box-channel.py: the box channel keeper (Refs #1717).

The tests never touch a real tunnel: the primary endpoint is a closed port
(the "Connection refused" of the field) and the backup endpoint is a local
listening socket.
"""

import contextlib
import importlib.util
import io
import json
import socket
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "box-channel.py"
SPEC = importlib.util.spec_from_file_location("box_channel", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def closed_port():
    """Return a port nothing listens on."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


class Listening:
    """A socket that accepts connections: a tunnel endpoint that answers."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.port = self.socket.getsockname()[1]

    def close(self):
        self.socket.close()


class BoxChannelToolTests(unittest.TestCase):
    """The keeper switches the shim target and leaves the paper trail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="box-channel-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.backup = Listening()
        self.addCleanup(self.backup.close)
        self.config = self.root / "box-channel.json"
        self.state = self.root / "state" / "box-channel.json"
        self.target = self.root / "state" / "box-channel.target"
        self.brain = self.root / "brain" / "log.md"
        self.write_config()

    def write_config(self, **overrides):
        config = {
            "primary": {"host": "127.0.0.1", "port": closed_port()},
            "backup": {"host": "127.0.0.1", "port": self.backup.port},
            "failure_threshold": 2,
            "recovery_threshold": 2,
            "state_file": str(self.state),
            "target_file": str(self.target),
            "brain_log": str(self.brain),
        }
        config.update(overrides)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        return config

    def run_tool(self, *args):
        """Run the tool and return (exit code, stdout)."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = MODULE.main(list(args))
        return code, output.getvalue()

    def test_a_broken_configuration_is_a_hard_error(self):
        self.config.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_tool("tick", "--config", str(self.config))

    def test_a_configuration_without_a_backup_channel_is_refused(self):
        self.write_config()
        payload = json.loads(self.config.read_text(encoding="utf-8"))
        del payload["backup"]
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_tool("tick", "--config", str(self.config))

    def test_the_shim_moves_to_the_backup_and_the_switch_is_logged(self):
        code, output = self.run_tool("tick", "--config", str(self.config))
        self.assertEqual(code, 0)
        self.assertIn("channel=primary", output)

        # The second failed probe reaches the threshold: the shim moves.
        code, output = self.run_tool("tick", "--config", str(self.config))
        self.assertEqual(code, 0)
        self.assertIn("channel=backup", output)
        self.assertIn("switch primary->backup", output)

        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["current"], "backup")
        self.assertEqual(state["switch_count"], 1)
        self.assertEqual(state["target"], "127.0.0.1:%d" % self.backup.port)
        self.assertFalse(state["channels"]["primary"]["reachable"])
        self.assertTrue(state["channels"]["backup"]["reachable"])

        target = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(target["channel"], "backup")
        self.assertEqual(target["target"], "127.0.0.1:%d" % self.backup.port)

        brain = self.brain.read_text(encoding="utf-8")
        self.assertIn("box-channel: switch primary->backup", brain)
        self.assertIn("failed 2 consecutive times", brain)

    def test_status_reports_the_channel_state(self):
        self.run_tool("tick", "--config", str(self.config))
        self.run_tool("tick", "--config", str(self.config))
        code, output = self.run_tool("status", "--config", str(self.config))
        self.assertEqual(code, 0)
        state = json.loads(output)
        self.assertEqual(state["current"], "backup")
        self.assertEqual(state["switch_count"], 1)
        self.assertIn("last_success_at", state["channels"]["backup"])

    def test_the_state_survives_a_restart_of_the_keeper(self):
        self.run_tool("tick", "--config", str(self.config))
        self.run_tool("tick", "--config", str(self.config))
        # A fresh process reads the saved state and stays on the backup.
        code, output = self.run_tool("tick", "--config", str(self.config))
        self.assertEqual(code, 0)
        self.assertIn("channel=backup", output)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["switch_count"], 1)


if __name__ == "__main__":
    unittest.main()
