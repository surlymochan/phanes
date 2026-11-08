"""The Hub bridge must serve the configured control-plane host locally (Refs #1480).

Since 2026-11-02 the control plane runs on `box` and the controlled fleet is
remote. The bridge therefore has to recognise `OPENSWARM_AUTO_LOCAL_HOST` in
its direct route and in its local command path, so the status, stop and bundle
calls of a `box` task do not fall back to the legacy gateway.
"""

import os
from pathlib import Path
import runpy
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MOD = runpy.run_path(str(ROOT / 'bin' / 'hub-openswarm-bridge'))
G = MOD['dispatch_gateway'].__globals__

ENVIRONMENT = {
    'OPENSWARM_HUB_LOCAL_ROUTER': '1',
    'OPENSWARM_AUTO_LOCAL_HOST': 'box',
    'OPENSWARM_AUTO_FLEET_HOSTS': '["gores1","gores2"]',
}


class HubBridgeLocalHostTests(unittest.TestCase):
    def test_configured_box_host_routes_local_lifecycle_directly(self):
        responses = [{'id': 'box-run', 'result': {}}, {'ok': True}, {'bundle': 'AA=='}]
        command = mock.Mock(side_effect=responses)
        with mock.patch.dict(os.environ, ENVIRONMENT), \
                mock.patch.dict(G, {'local_command': command}):
            for op in ('info', 'stop', 'bundle'):
                self.assertEqual(MOD['dispatch_gateway']({'op': op, 'id': 'box-run', 'host': 'box'}),
                                 responses[('info', 'stop', 'bundle').index(op)])
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            [G['native'], 'fleet', '_run-info', 'box-run'],
            [G['native'], 'stop', 'box-run'],
            [G['native'], 'fleet', '_bundle', 'box-run'],
        ])

    def test_fleet_host_is_still_dispatched_over_ssh(self):
        command = mock.Mock(return_value={'id': 'run-1', 'result': {}})
        with mock.patch.dict(os.environ, ENVIRONMENT), \
                mock.patch.dict(G, {'local_command': command}):
            MOD['dispatch_gateway']({'op': 'info', 'id': 'run-1', 'host': 'gores1'})
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            [G['native'], 'fleet', 'exec', 'gores1', '--', 'fleet', '_run-info', 'run-1'],
        ])

    def test_unconfigured_host_stays_on_the_legacy_gateway(self):
        gateway = mock.Mock(return_value={'ok': True})
        payload = {'op': 'info', 'id': 'run-1', 'host': 'mystery'}
        with mock.patch.dict(os.environ, ENVIRONMENT), \
                mock.patch.dict(G, {'gateway': gateway}):
            MOD['dispatch_gateway'](payload)
        gateway.assert_called_once_with(payload)


if __name__ == '__main__':
    unittest.main()
