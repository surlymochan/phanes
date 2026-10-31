"""The gocc relay only forwards to its configured outbound executor targets."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class HubRelayTests(unittest.TestCase):
    def invoke(self, request, targets, run):
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                mock.patch.dict(os.environ, {'OPENSWARM_FLEET_RELAY_TARGETS': json.dumps(targets)}), \
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
            (['/usr/bin/ssh','-T','-F',str(Path.home()/'.ssh/openswarm-fleet/config'),
              'openswarm-govpn-gateway'], {'op':'run_info','run_id':'run-1'})])

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
