"""`openswarm/lib/transport/channel.py`: the box channel failover (Refs #1717).

The module is loaded by path: `openswarm/lib/transport/` is a namespace
package without an `__init__.py`, so it must not be imported as a package.

Coverage asked for by the card: the shim moves to the backup channel after
the primary tunnel failed repeatedly, the backup carries the dispatch on its
own while the primary tunnel service is down, the primary is used again only
after it recovered repeatedly (no ping-pong), every switch leaves a brain log
line, and the channel state is part of the health output.
"""

import importlib.util
import json
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "transport" / "websocket.py"
CHANNEL_MODULE = HERE.parent / "lib" / "transport" / "channel.py"

spec = importlib.util.spec_from_file_location("transport_websocket", MODULE)
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)

spec = importlib.util.spec_from_file_location("transport_channel", CHANNEL_MODULE)
channel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(channel)

TOKEN = "test-token-do-not-use-in-production"
CARDS = {9001: {"title": "fixture card for the channel failover"}}


class ScriptedProbe:
    """A probe whose answer is scripted, so a tick sequence is deterministic."""

    def __init__(self, answers=None, default=True, error=None):
        self.answers = list(answers or [])
        self.default = default
        self.error = error
        self.calls = 0

    def __call__(self, _channel):
        self.calls += 1
        if self.error is not None:
            raise OSError(self.error)
        if self.answers:
            return self.answers.pop(0)
        return self.default


class Clock:
    """A clock that moves one second per reading."""

    def __init__(self, start=1762000000.0):
        self.now = start

    def __call__(self):
        self.now += 1.0
        return self.now


def build_controller(primary_probe=None, backup_probe=None, failure_threshold=3,
                     recovery_threshold=3, brain_log=None, clock=None):
    """Build a controller with two scripted channels."""
    primary = channel.TunnelChannel("primary", "127.0.0.1", 8765, probe=primary_probe or ScriptedProbe())
    backup = channel.TunnelChannel("backup", "127.0.0.1", 8766, probe=backup_probe or ScriptedProbe())
    controller = channel.ChannelFailover(primary, backup,
                                         failure_threshold=failure_threshold,
                                         recovery_threshold=recovery_threshold,
                                         brain_log=brain_log, clock=clock or Clock())
    return controller


class FailoverTests(unittest.TestCase):
    """The shim target follows the thresholds, in both directions."""

    def test_the_primary_channel_is_used_first(self):
        controller = build_controller()
        self.assertEqual(controller.current, "primary")
        self.assertEqual(controller.target(), "127.0.0.1:8765")
        self.assertEqual(controller.switch_count, 0)

    def test_one_failure_does_not_move_the_shim(self):
        controller = build_controller(primary_probe=ScriptedProbe([False]))
        self.assertIsNone(controller.tick())
        self.assertEqual(controller.current, "primary")
        self.assertEqual(controller.state["primary"].consecutive_failures, 1)

    def test_the_shim_moves_to_the_backup_after_consecutive_failures(self):
        controller = build_controller(primary_probe=ScriptedProbe([False, False, False]))
        self.assertIsNone(controller.tick())
        self.assertIsNone(controller.tick())
        event = controller.tick()
        self.assertIsNotNone(event)
        self.assertEqual(event["from"], "primary")
        self.assertEqual(event["to"], "backup")
        self.assertEqual(event["primary_failures"], 3)
        self.assertEqual(controller.current, "backup")
        self.assertEqual(controller.target(), "127.0.0.1:8766")
        self.assertEqual(controller.switch_count, 1)

    def test_the_backup_must_answer_before_the_shim_moves(self):
        backup = ScriptedProbe(default=False)
        controller = build_controller(primary_probe=ScriptedProbe([False] * 4), backup_probe=backup)
        for _ in range(4):
            self.assertIsNone(controller.tick())
        self.assertEqual(controller.current, "primary")
        self.assertEqual(controller.switch_count, 0)

    def test_the_primary_is_used_again_after_consecutive_recoveries(self):
        controller = build_controller(primary_probe=ScriptedProbe([False] * 3 + [True, True, True]),
                                      recovery_threshold=3)
        for _ in range(3):
            controller.tick()
        self.assertEqual(controller.current, "backup")
        self.assertIsNone(controller.tick())
        self.assertIsNone(controller.tick())
        event = controller.tick()
        self.assertIsNotNone(event)
        self.assertEqual(event["from"], "backup")
        self.assertEqual(event["to"], "primary")
        self.assertEqual(event["primary_successes"], 3)
        self.assertEqual(controller.current, "primary")
        self.assertEqual(controller.switch_count, 2)

    def test_a_flapping_primary_never_ping_pongs(self):
        """One success between failures is not a recovery: the channel stays put."""
        answers = []
        for _ in range(20):
            answers.extend([False, True])
        controller = build_controller(primary_probe=ScriptedProbe(answers))
        for _ in range(20):
            controller.tick()
        self.assertEqual(controller.current, "primary")
        self.assertEqual(controller.switch_count, 0)

    def test_a_flapping_primary_does_not_bring_the_shim_back(self):
        """On the backup channel, an alternating primary never reaches the threshold."""
        primary = ScriptedProbe([False] * 3 + [True, False, True, False, True, False])
        controller = build_controller(primary_probe=primary, recovery_threshold=3)
        for _ in range(3):
            controller.tick()
        self.assertEqual(controller.current, "backup")
        for _ in range(6):
            controller.tick()
        self.assertEqual(controller.current, "backup")
        self.assertEqual(controller.switch_count, 1)

    def test_a_probe_error_is_a_failed_probe(self):
        controller = build_controller(primary_probe=ScriptedProbe(error="Connection refused"))
        for _ in range(3):
            controller.tick()
        self.assertEqual(controller.current, "backup")
        self.assertIn("Connection refused", controller.state["primary"].last_error)

    def test_the_health_output_carries_the_channel_state(self):
        clock = Clock()
        controller = build_controller(primary_probe=ScriptedProbe([False] * 3), clock=clock)
        for _ in range(3):
            controller.tick()
        health = controller.health()
        self.assertEqual(health["current"], "backup")
        self.assertEqual(health["target"], "127.0.0.1:8766")
        self.assertEqual(health["switch_count"], 1)
        self.assertEqual(sorted(health["channels"]), ["backup", "primary"])
        primary = health["channels"]["primary"]
        self.assertFalse(primary["reachable"])
        self.assertEqual(primary["consecutive_failures"], 3)
        self.assertIsNotNone(primary["last_failure_at"])
        backup = health["channels"]["backup"]
        self.assertTrue(backup["reachable"])
        self.assertIsNotNone(backup["last_success_at"])

    def test_the_state_survives_a_restart(self):
        controller = build_controller(primary_probe=ScriptedProbe([False] * 3))
        for _ in range(3):
            controller.tick()
        saved = controller.state_dict()
        restored = build_controller()
        restored.restore(saved)
        self.assertEqual(restored.current, "backup")
        self.assertEqual(restored.switch_count, 1)
        self.assertEqual(restored.state["primary"].consecutive_failures, 3)


class BrainLogTests(unittest.TestCase):
    """Every switch leaves one brain log line behind."""

    def test_a_switch_writes_one_line_with_the_time_and_the_failure_count(self):
        lines = []
        controller = build_controller(primary_probe=ScriptedProbe([False] * 3),
                                      brain_log=lines.append)
        for _ in range(3):
            controller.tick()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("switch primary->backup", line)
        self.assertIn("failed 3 consecutive times", line)
        self.assertIn("+08:00", line)

    def test_the_switch_back_writes_a_line_too(self):
        lines = []
        controller = build_controller(primary_probe=ScriptedProbe([False] * 3 + [True] * 3),
                                      brain_log=lines.append)
        for _ in range(6):
            controller.tick()
        self.assertEqual(len(lines), 2)
        self.assertIn("switch backup->primary", lines[1])
        self.assertIn("succeeded 3 consecutive times", lines[1])

    def test_no_line_without_a_switch(self):
        lines = []
        controller = build_controller(primary_probe=ScriptedProbe([False, True]),
                                      brain_log=lines.append)
        controller.tick()
        controller.tick()
        self.assertEqual(lines, [])

    def test_the_brain_logger_appends_to_the_log_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "log.md"
            write = channel.brain_logger(str(path))
            write("switch primary->backup at 2026-11-02T21:14:03+08:00")
            write("switch backup->primary at 2026-11-02T21:20:11+08:00")
            body = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(body), 2)
            self.assertTrue(body[0].startswith("- "))
            self.assertIn("UTC+8 box-channel: switch primary->backup", body[0])


class TargetFileTests(unittest.TestCase):
    """The shim reads the endpoint in use from the target file."""

    def test_the_target_file_names_the_channel_in_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "box-channel.target"
            controller = build_controller(primary_probe=ScriptedProbe([False] * 3))
            for _ in range(3):
                controller.tick()
            payload = channel.write_target(str(path), controller.state_dict())
            self.assertEqual(payload["channel"], "backup")
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["target"], "127.0.0.1:8766")
            self.assertEqual(written["switch_count"], 1)


class BackupDispatchTests(unittest.TestCase):
    """The backup channel carries the dispatch on its own (real sockets)."""

    def test_the_backup_relay_serves_the_dispatch_while_the_primary_tunnel_is_down(self):
        primary_relay = ws.WebSocketRelay(ws.RelayState({"t": "exec"}, CARDS))
        primary_relay.__enter__()
        backup_relay = ws.WebSocketRelay(ws.RelayState({"t": "exec"}, CARDS))
        backup_relay.__enter__()
        self.addCleanup(primary_relay.stop)
        self.addCleanup(backup_relay.stop)
        primary = channel.TunnelChannel("primary", "127.0.0.1", primary_relay.port)
        backup = channel.TunnelChannel("backup", "127.0.0.1", backup_relay.port)
        controller = channel.ChannelFailover(primary, backup, failure_threshold=2,
                                             recovery_threshold=2, clock=Clock())
        self.assertEqual(controller.target(), "127.0.0.1:%d" % primary_relay.port)

        # The tunnel service on the far side of the primary tunnel dies.
        primary_relay.stop()
        self.assertIsNone(controller.tick())
        event = controller.tick()
        self.assertIsNotNone(event)
        self.assertEqual(controller.current, "backup")

        # The shim follows the target and the backup relay hands out the card.
        host, port = controller.target().rsplit(":", 1)
        client = ws.ExecutorClient(host, int(port), "exec", "t")
        client.connect()
        reply = client.claim(9001, "d-1")
        self.assertEqual(reply["type"], "assigned")
        self.assertEqual(reply["payload"]["issue"], 9001)

    def test_the_shim_returns_to_the_primary_when_the_tunnel_service_comes_back(self):
        primary_relay = ws.WebSocketRelay(ws.RelayState({"t": "exec"}, CARDS))
        primary_relay.__enter__()
        self.addCleanup(primary_relay.stop)
        backup_relay = ws.WebSocketRelay(ws.RelayState({"t": "exec"}, CARDS))
        backup_relay.__enter__()
        self.addCleanup(backup_relay.stop)
        primary_port = primary_relay.port
        primary = channel.TunnelChannel("primary", "127.0.0.1", primary_port)
        backup = channel.TunnelChannel("backup", "127.0.0.1", backup_relay.port)
        controller = channel.ChannelFailover(primary, backup, failure_threshold=2,
                                             recovery_threshold=2, clock=Clock())

        primary_relay.stop()
        controller.tick()
        controller.tick()
        self.assertEqual(controller.current, "backup")

        # The tunnel service is back on the same port.
        revived = ws.WebSocketRelay(ws.RelayState({"t": "exec"}, CARDS), port=primary_port)
        revived.__enter__()
        self.addCleanup(revived.stop)
        self.assertIsNone(controller.tick())
        event = controller.tick()
        self.assertIsNotNone(event)
        self.assertEqual(controller.current, "primary")
        self.assertEqual(controller.target(), "127.0.0.1:%d" % primary_port)
        self.assertEqual(controller.switch_count, 2)

        host, port = controller.target().rsplit(":", 1)
        client = ws.ExecutorClient(host, int(port), "exec", "t")
        client.connect()
        self.assertEqual(client.claim(9001, "d-2")["type"], "assigned")


class ProbeTests(unittest.TestCase):
    """The default probe is a TCP connect."""

    def test_a_closed_port_is_not_reachable(self):
        with socket_bound() as port:
            pass
        self.assertFalse(channel.probe_tcp("127.0.0.1", port))

    def test_an_open_port_is_reachable(self):
        with socket_bound() as port:
            self.assertTrue(channel.probe_tcp("127.0.0.1", port))


def socket_bound():
    """A listening socket on an ephemeral port, used as a fake tunnel endpoint."""
    import socket as socket_module
    listener = socket_module.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return _Bound(listener)


class _Bound:
    def __init__(self, listener):
        self.listener = listener

    @property
    def port(self):
        return self.listener.getsockname()[1]

    def __enter__(self):
        return self.port

    def __exit__(self, *exc):
        self.listener.close()
        return False


if __name__ == "__main__":
    unittest.main()
