"""Hub bridge must route the configured control plane host locally (Refs #1480).

The 2026-11-02 baseline names the control plane host (`box`) explicitly. The
bridge has to treat that host like the local lane runtime instead of routing
it through the remote fleet.

    cd openswarm && python3 -m unittest discover -s tests -p 'test_hub_bridge.py' -v
"""
import os
import pathlib
import runpy
import unittest
from unittest import mock


MOD = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'hub-openswarm-bridge'))
G = MOD['dispatch_gateway'].__globals__


class HubBridgeTests(unittest.TestCase):
    def test_configured_box_host_routes_local_lifecycle_directly(self):
        responses = [{'id': 'box-run', 'result': {}}, {'ok': True}, {'bundle': 'AA=='}]
        command = mock.Mock(side_effect=responses)
        with mock.patch.dict(os.environ, {'OPENSWARM_HUB_LOCAL_ROUTER': '1',
                                          'OPENSWARM_AUTO_LOCAL_HOST': 'box',
                                          'OPENSWARM_AUTO_FLEET_HOSTS': '["worker","gores2"]'}), \
                mock.patch.dict(G, {'local_command': command}):
            for op in ('info', 'stop', 'bundle'):
                self.assertEqual(MOD['dispatch_gateway']({'op': op, 'id': 'box-run', 'host': 'box'}),
                                 responses[('info', 'stop', 'bundle').index(op)])
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            [G['native'], 'fleet', '_run-info', 'box-run'],
            [G['native'], 'stop', 'box-run'],
            [G['native'], 'fleet', '_bundle', 'box-run'],
        ])


if __name__ == '__main__':
    unittest.main()
