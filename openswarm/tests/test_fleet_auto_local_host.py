"""The automatic dispatcher must not probe or dispatch to the control plane (Refs #1480).

`box` is the control plane itself. With an empty backend allowlist for `box`
the dispatcher has to skip the local lane runtime probe and must never use
`box` as a dispatch target, so the remote fleet keeps the whole capacity.
"""

import os
from pathlib import Path
import runpy
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MOD = runpy.run_path(str(ROOT / 'bin' / 'openswarm-auto'))
G = MOD['main'].__globals__

ENVIRONMENT = {
    'OPENSWARM_AUTO_LOCAL_HOST': 'box',
    'OPENSWARM_AUTO_FLEET_HOSTS': '["gores1","gores2"]',
    'OPENSWARM_AUTO_HOST_BACKENDS': '{"box":[],"gores1":["pi"],"gores2":["pi"]}',
}


class FleetAutoLocalHostTests(unittest.TestCase):
    def test_box_allowlist_skips_local_probe_and_never_dispatches_to_box(self):
        local = mock.Mock(side_effect=RuntimeError('local lane status unavailable'))
        fleet = mock.Mock(return_value=[{'name': 'M1', 'state': 'idle', 'backend': 'pi', 'model': 'MiniMax'}])
        with mock.patch.dict(os.environ, ENVIRONMENT), \
                mock.patch.dict(G, {'local_lanes': local, 'fleet_lanes': fleet}):
            rows = G['discovered_lanes']()
        self.assertEqual([row['_fleet_host'] for row in rows], ['gores1', 'gores2'])
        local.assert_not_called()
        self.assertEqual([call.args[0] for call in fleet.call_args_list], ['gores1', 'gores2'])

    def test_local_host_defaults_to_the_control_plane(self):
        environment = {key: value for key, value in os.environ.items()
                       if key != 'OPENSWARM_AUTO_LOCAL_HOST'}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(G['local_host'](), 'box')


if __name__ == '__main__':
    unittest.main()
