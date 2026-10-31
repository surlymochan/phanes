"""The gocc relay only forwards to its configured outbound executor targets."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class HubRelayTests(unittest.TestCase):
    def invoke(self, request, targets, run, ssh_config=None):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'relay.json'
            data = {'targets': targets}
            if ssh_config:
                data['ssh_config'] = ssh_config
            config.write_text(json.dumps(data))
            with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                    mock.patch.dict(os.environ, {'OPENSWARM_FLEET_RELAY_CONFIG': str(config)}), \
                    mock.patch.object(subprocess, 'run', side_effect=run), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as exit_code:
                    runpy.run_path(str(ROOT / 'bin/hub-fleet-relay'))
            return exit_code.exception.code, json.loads(output.getvalue())

    def test_forwards_only_configured_target(self):
        seen = []
        def run(command, input=None, **_kwargs):
            seen.append((command, json.loads(input)))
            return subprocess.CompletedProcess(command, 0, json.dumps({'ok':True,'result':{'id':'run-1'}}), '')
        code, reply = self.invoke(
            {'op':'relay','target':'govpn','request':{'op':'run_info','run_id':'run-1'}},
            [{'host':'govpn','ssh_alias':'openswarm-govpn-gateway'}], run)
        self.assertEqual(code, 0)
        self.assertEqual(reply, {'ok':True,'result':{'id':'run-1'}})
        self.assertEqual(seen, [
            (['/usr/bin/ssh','-T','openswarm-govpn-gateway'], {'op':'run_info','run_id':'run-1'})])

    def test_service_config_can_select_existing_ssh_config(self):
        seen = []
        def run(command, input=None, **_kwargs):
            seen.append((command, json.loads(input)))
            return subprocess.CompletedProcess(command, 0, json.dumps({'ok':True,'result':{}}), '')
        code, _reply = self.invoke(
            {'op':'relay','target':'govpn','request':{'op':'lanes'}},
            [{'host':'govpn','ssh_alias':'openswarm-govpn-gateway'}], run,
            ssh_config='/etc/openswarm/outbound-ssh.conf')
        self.assertEqual(code, 0)
        self.assertEqual(seen[0][0], ['/usr/bin/ssh','-T','-F','/etc/openswarm/outbound-ssh.conf',
                                       'openswarm-govpn-gateway'])

    def test_rejects_unknown_target_without_transport(self):
        run = mock.Mock()
        code, reply = self.invoke(
            {'op':'relay','target':'unexpected','request':{'op':'lanes'}},
            [{'host':'govpn','ssh_alias':'openswarm-govpn-gateway'}], run)
        self.assertEqual(code, 1)
        self.assertFalse(reply['ok'])
        self.assertIn('invalid relay request', reply['error'])
        run.assert_not_called()

    def test_rejects_unlisted_operation_without_transport(self):
        run = mock.Mock()
        code, reply = self.invoke(
            {'op':'relay','target':'govpn','request':{'op':'shell','command':'anything'}},
            [{'host':'govpn','ssh_alias':'openswarm-govpn-gateway'}], run)
        self.assertEqual(code, 1)
        self.assertFalse(reply['ok'])
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
