"""Automatic dispatch must respect the configured fleet topology (Refs #1480).

The 2026-11-02 baseline names the control plane host (`box`) explicitly and
gives it an empty backend allowlist: the local lane runtime must not be
probed, and the control plane host must never receive a dispatch. The remote
fleet hosts keep their own backend allowlists.

    cd openswarm && python3 -m unittest discover -s tests -p 'test_fleet_auto.py' -v
"""
import os
import pathlib
import runpy
import unittest
from unittest import mock


MOD = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'openswarm-auto'))
G = MOD['main'].__globals__


class FleetAutoTests(unittest.TestCase):
    def test_box_allowlist_skips_local_probe_and_never_dispatches_to_box(self):
        environment = {
            'OPENSWARM_AUTO_LOCAL_HOST': 'box',
            'OPENSWARM_AUTO_FLEET_HOSTS': '["worker","gores2"]',
            'OPENSWARM_AUTO_HOST_BACKENDS': '{"box":[],"worker":["pi"],"gores2":["pi"]}',
            # No topology policy: this test covers the allowlist, not the drift guard.
            'OPENSWARM_AUTO_TOPOLOGY_POLICY': '',
            'OPENSWARM_FLEET_GUARD_CONFIG': '',
        }
        local = mock.Mock(side_effect=RuntimeError('local lane status unavailable'))
        fleet = mock.Mock(return_value=[{'name': 'M1', 'state': 'idle', 'backend': 'pi', 'model': 'MiniMax'}])
        with mock.patch.dict(os.environ, environment), mock.patch.dict(G, {
                'local_lanes': local, 'fleet_lanes': fleet}):
            rows = G['discovered_lanes']()
        self.assertEqual([row['_fleet_host'] for row in rows], ['worker', 'gores2'])
        local.assert_not_called()
        self.assertEqual([call.args[0] for call in fleet.call_args_list], ['worker', 'gores2'])


if __name__ == '__main__':
    unittest.main()
