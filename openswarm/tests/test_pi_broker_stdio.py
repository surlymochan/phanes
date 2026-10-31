"""Restricted broker stdio keeps capacity ownership with its authenticated caller."""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BROKER = ROOT / "bin" / "pi-broker-stdio"


class PiBrokerStdioTests(unittest.TestCase):
    def test_capacity_json_uses_gores1_owner(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as root:
            root = pathlib.Path(root)
            relay = root / "pi-broker-stdio"
            capacity = root / "fleet-capacity"
            shutil.copy2(BROKER, relay)
            capacity.write_text("#!/bin/sh\nprintf '{\"ok\":true,\"owner\":\"%s\"}\\n' \"$1\"\n")
            relay.chmod(0o700); capacity.chmod(0o700)
            result = subprocess.run([str(relay)], input='{"op":"status"}\n', text=True,
                                    capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), {"ok": True, "owner": "gores1"})

    def test_http_first_byte_still_relays_to_pi_socket(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as root:
            root = pathlib.Path(root)
            relay = root / "pi-broker-stdio"; shutil.copy2(BROKER, relay); relay.chmod(0o700)
            sock_path = root / "Library/Caches/openswarm-pi-broker/broker.sock"
            sock_path.parent.mkdir(parents=True)
            listener = socket.socket(socket.AF_UNIX); listener.bind(str(sock_path)); listener.listen(1)
            received = []
            def serve():
                client, _ = listener.accept()
                received.append(client.recv(4096))
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                client.close(); listener.close()
            worker = threading.Thread(target=serve); worker.start()
            raw = b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n\r\n"
            process = subprocess.run([str(relay)], input=raw, capture_output=True,
                                     env=dict(os.environ, HOME=str(root)), check=True)
            worker.join(timeout=2)
            self.assertEqual(received, [raw])
            self.assertTrue(process.stdout.endswith(b"\r\n\r\nok"))

