"""Pi inference broker keeps its byte stream isolated from fleet control."""
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
    def test_http_and_json_first_bytes_relay_only_to_pi_socket(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as root:
            root = pathlib.Path(root)
            relay = root / "pi-broker-stdio"; shutil.copy2(BROKER, relay); relay.chmod(0o700)
            marker = root / 'capacity-invoked'
            (root / 'fleet-capacity').write_text('#!/bin/sh\ntouch "$MARKER"\n')
            (root / 'fleet-capacity').chmod(0o700)
            for raw in (b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\n\r\n",
                        b'{"not":"fleet-capacity"}\n'):
                sock_path = root / "Library/Caches/openswarm-pi-broker/broker.sock"
                sock_path.parent.mkdir(parents=True, exist_ok=True)
                listener = socket.socket(socket.AF_UNIX); listener.bind(str(sock_path)); listener.listen(1)
                received = []
                def serve():
                    client, _ = listener.accept()
                    request = bytearray()
                    while len(request) < len(raw):
                        chunk = client.recv(len(raw) - len(request))
                        if not chunk:
                            break
                        request.extend(chunk)
                    received.append(bytes(request))
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    client.close(); listener.close()
                worker = threading.Thread(target=serve); worker.start()
                process = subprocess.run([str(relay)], input=raw, capture_output=True,
                                         env=dict(os.environ, HOME=str(root), MARKER=str(marker)), check=True)
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                sock_path.unlink()
                self.assertEqual(received, [raw])
                self.assertTrue(process.stdout.endswith(b"\r\n\r\nok"))
            self.assertFalse(marker.exists())

