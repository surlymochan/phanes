"""The reverse-forward socket transport preserves coordinator semantics."""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest


HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin'
SERVER = BIN / 'fleet-capacity-stdio'
CLIENT = BIN / 'fleet-capacity-socket-client'
CAPACITY = BIN / 'fleet-capacity'


class FleetSocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)

    def start_server(self, caller='govpn', coordinator=None, name='gocc.sock'):
        root = pathlib.Path(self.temp.name)
        if coordinator is None:
            coordinator = root / 'coordinator'
            coordinator.write_text(
                '#!' + sys.executable + '\n'
                'import json,sys\n'
                'print(json.dumps({"ok":True,"caller":sys.argv[1],"request":json.load(sys.stdin)}))\n'
            )
            coordinator.chmod(0o755)
        socket_path = root / name
        server = subprocess.Popen([str(SERVER), '--socket', str(socket_path), '--caller', caller,
                                   '--coordinator', str(coordinator)], env=self.env)
        self.addCleanup(self.stop, server)
        for _ in range(50):
            if socket_path.exists():
                return socket_path, server
            time.sleep(0.02)
        server.terminate()
        self.fail('socket coordinator did not start')

    def client(self, socket_path, payload):
        return subprocess.run([str(CLIENT), '--socket', str(socket_path)], input=json.dumps(payload),
                              text=True, capture_output=True, timeout=10)

    def test_fixed_remote_host_identity_reaches_shared_coordinator(self):
        socket_path, server = self.start_server('govpn')
        payload = {'op': 'acquire', 'token': 'a' * 32, 'run_id': 'run-1',
                   'backend': 'cursor', 'provider': None}
        result = self.client(socket_path, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertEqual(answer['caller'], 'govpn')
        self.assertEqual(answer['request'], payload)
        server.terminate()
        server.wait(timeout=5)

    def test_missing_reverse_forward_fails_closed(self):
        result = self.client(pathlib.Path(self.temp.name) / 'missing.sock', {'op': 'acquire'})
        self.assertNotEqual(result.returncode, 0)
        answer = json.loads(result.stdout)
        self.assertFalse(answer['ok'])
        self.assertIn('unavailable', answer['error'])

    def test_server_recovers_a_stale_socket_but_preserves_a_live_one(self):
        root = pathlib.Path(self.temp.name)
        stale = root / 'stale.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(stale))
        listener.close()
        coordinator = root / 'replacement-coordinator'
        coordinator.write_text('#!' + sys.executable + '\nimport json\nprint(json.dumps({"ok":True}))\n')
        coordinator.chmod(0o755)
        replacement = subprocess.Popen([str(SERVER), '--socket', str(stale), '--caller', 'govpn2',
                                        '--coordinator', str(coordinator)], env=self.env)
        self.addCleanup(replacement.kill)
        for _ in range(50):
            if stale.exists():
                break
            time.sleep(0.02)
        self.assertIsNone(replacement.poll())
        replacement.terminate()
        replacement.wait(timeout=5)

        live = root / 'live.sock'
        active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        active.bind(str(live))
        active.listen(1)
        self.addCleanup(active.close)
        refused = subprocess.run([str(SERVER), '--socket', str(live), '--caller', 'govpn'],
                                 text=True, capture_output=True, timeout=5)
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue(live.exists())

    def test_shared_database_survives_server_restart_and_releases(self):
        socket_path, server = self.start_server('govpn', coordinator=CAPACITY, name='shared-1.sock')
        first = {'op': 'acquire', 'token': '1' * 32, 'run_id': 'first',
                 'backend': 'cursor', 'provider': None}
        second = {'op': 'acquire', 'token': '2' * 32, 'run_id': 'second',
                  'backend': 'cursor', 'provider': None}
        for payload in (first, second):
            self.assertTrue(json.loads(self.client(socket_path, payload).stdout)['admitted'])
        server.terminate()
        server.wait(timeout=5)
        socket_path, restarted = self.start_server('govpn', coordinator=CAPACITY, name='shared-2.sock')
        status_result = self.client(socket_path, {'op': 'status'})
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)
        self.assertEqual(len(status['reservations']), 2)
        release = {'op': 'release', 'token': first['token'], 'run_id': first['run_id']}
        self.assertTrue(json.loads(self.client(socket_path, release).stdout)['released'])
        third = {'op': 'acquire', 'token': '3' * 32, 'run_id': 'third',
                 'backend': 'cursor', 'provider': None}
        self.assertTrue(json.loads(self.client(socket_path, third).stdout)['admitted'])
        restarted.terminate()
        restarted.wait(timeout=5)

    def test_slow_request_does_not_block_other_guard_calls(self):
        root = pathlib.Path(self.temp.name)
        coordinator = root / 'slow-coordinator'
        coordinator.write_text(
            '#!' + sys.executable + '\n'
            'import json,sys,time\n'
            'request=json.load(sys.stdin)\n'
            'if request.get("run_id") == "slow": time.sleep(0.6)\n'
            'print(json.dumps({"ok":True,"run_id":request.get("run_id")}))\n'
        )
        coordinator.chmod(0o755)
        socket_path, server = self.start_server('govpn', coordinator=coordinator)
        slow = subprocess.Popen([str(CLIENT), '--socket', str(socket_path)], stdin=subprocess.PIPE,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        slow.stdin.write(json.dumps({'run_id': 'slow'}))
        slow.stdin.close()
        time.sleep(0.05)
        started = time.monotonic()
        fast = self.client(socket_path, {'run_id': 'fast'})
        elapsed = time.monotonic() - started
        self.assertEqual(fast.returncode, 0, fast.stderr)
        self.assertLess(elapsed, 0.45)
        self.assertEqual(slow.wait(timeout=5), 0)
        slow.stdout.close()
        slow.stderr.close()
        server.terminate()
        server.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
