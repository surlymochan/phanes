"""Fencing tokens make a stale fleet reservation unable to write a result back.

The coordinator hands out a monotonically increasing fence with every
reservation and can be asked, read-only, whether a (run_id, token, fence)
triple still holds the reservation. A refused fence must name the reason so
the caller can record it instead of dropping the write-back silently.
"""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / 'bin' / 'fleet-capacity'
BOOT = '11111111-1111-1111-1111-111111111111'
NEXT_BOOT = '22222222-2222-2222-2222-222222222222'


class FleetCapacityFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.env = dict(os.environ, OPENSWARM_FLEET_CAPACITY_DIR=self.temp.name)
        # The coordinator reads the kernel boot id over its own trusted channel.
        self.boot_files = {}
        probes = {}
        for host in ('broker', 'worker', 'local'):
            path = pathlib.Path(self.temp.name, host + '.boot')
            path.write_text(BOOT)
            self.boot_files[host] = path
            probes[host] = [sys.executable, '-c',
                            'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())', str(path)]
        self.probe_config = pathlib.Path(self.temp.name, 'boot-probes.json')
        self.probe_config.write_text(json.dumps(probes))

    def call(self, authenticated_host, **payload):
        if payload.get('op') == 'acquire':
            payload.setdefault('boot_id', BOOT)
        argv = [str(BIN), authenticated_host]
        if os.name == "nt":  # Windows cannot CreateProcess a shebang script (WinError 193)
            argv = [sys.executable, *argv]
        p = subprocess.run(argv, input=json.dumps(payload), text=True,
                           capture_output=True, env=self.env, timeout=15)
        return json.loads(p.stdout)

    def expire(self, token, lease_expires=0):
        db = sqlite3.connect(pathlib.Path(self.temp.name, 'capacity.sqlite'))
        db.execute('UPDATE reservations SET lease_expires=? WHERE token=?', (lease_expires, token))
        db.commit()
        db.close()

    @staticmethod
    def reason(answer):
        """The coordinator states why a fence was refused."""
        for key in ('reason', 'error', 'fence_rejected', 'rejection'):
            value = answer.get(key)
            if isinstance(value, str) and value:
                return value
        return ''

    def acquire(self, host, token, run_id, backend, boot_id=None):
        payload = {'op': 'acquire', 'token': token, 'run_id': run_id,
                   'backend': backend, 'provider': None}
        if boot_id:
            payload['boot_id'] = boot_id
        answer = self.call(host, **payload)
        self.assertTrue(answer.get('admitted'), answer)
        fence = answer.get('fence')
        self.assertIsInstance(fence, int, 'acquire did not hand out a fence: %r' % (answer,))
        self.assertGreater(fence, 0)
        return fence

    def test_acquire_hands_out_a_fence_the_current_holder_may_write_back_with(self):
        token = 'a' * 32
        fence = self.acquire('worker', token, 'run-1', 'cursor')
        answer = self.call('worker', op='validate', run_id='run-1', token=token, fence=fence)
        self.assertTrue(answer.get('ok'), answer)
        self.assertEqual(self.reason(answer), '')

    def test_fence_is_monotonic_and_never_reused_across_runs(self):
        first = self.acquire('worker', 'a' * 32, 'run-1', 'cursor')
        second = self.acquire('local', 'b' * 32, 'run-2', 'codex')
        self.assertGreater(second, first)
        # A released reservation never lends its fence to the next run.
        self.assertTrue(self.call('local', op='release', token='b' * 32, run_id='run-2')['released'])
        third = self.acquire('local', 'c' * 32, 'run-3', 'codex')
        self.assertGreater(third, second)
        self.assertEqual(len({first, second, third}), 3)

    def test_expired_lease_cannot_write_back(self):
        token = 'a' * 32
        fence = self.acquire('worker', token, 'run-1', 'cursor')
        self.expire(token)
        answer = self.call('worker', op='validate', run_id='run-1', token=token, fence=fence)
        self.assertFalse(answer.get('ok'), answer)
        self.assertIn('expired', self.reason(answer))

    def test_another_run_cannot_write_back_with_a_foreign_fence(self):
        token = 'a' * 32
        fence = self.acquire('worker', token, 'run-1', 'cursor')
        answer = self.call('worker', op='validate', run_id='run-1', token=token, fence=fence + 1)
        self.assertFalse(answer.get('ok'), answer)
        self.assertIn('fence_mismatch', self.reason(answer))

    def test_a_foreign_token_cannot_write_back(self):
        token = 'a' * 32
        fence = self.acquire('worker', token, 'run-1', 'cursor')
        answer = self.call('worker', op='validate', run_id='run-1', token='b' * 32, fence=fence)
        self.assertFalse(answer.get('ok'), answer)
        self.assertIn(self.reason(answer), ('token_mismatch', 'missing'))

    def test_recovered_reservation_refuses_the_old_fence_and_the_reissue_gets_a_bigger_one(self):
        token = 'a' * 32
        old_fence = self.acquire('worker', token, 'orphan', 'cursor')
        # Keep this boot-recovery case inside grace: otherwise attest_boot or
        # status reclaims the expired lease before explicit recovery (#2160).
        self.env['OPENSWARM_FLEET_CAPACITY_GRACE_SECONDS'] = '3600'
        self.expire(token, time.time())
        self.boot_files['worker'].write_text(NEXT_BOOT)
        self.assertTrue(self.call('worker', op='attest_boot', boot_id=NEXT_BOOT)['ok'])
        row = self.call('broker', op='status')['reservations'][0]
        self.assertEqual(self.call('broker', op='recover', host='worker', run_id='orphan',
                                   recovery_id=row['recovery_id'])['recovered'], 1)
        refused = self.call('worker', op='validate', run_id='orphan', token=token, fence=old_fence)
        self.assertFalse(refused.get('ok'), refused)
        self.assertIn(self.reason(refused), ('missing', 'fence_mismatch', 'token_mismatch'))
        # The re-issued reservation fences the old one out for good.
        fresh = self.acquire('worker', 'b' * 32, 'orphan', 'cursor', boot_id=NEXT_BOOT)
        self.assertGreater(fresh, old_fence)
        accepted = self.call('worker', op='validate', run_id='orphan', token='b' * 32, fence=fresh)
        self.assertTrue(accepted.get('ok'), accepted)
        still_refused = self.call('worker', op='validate', run_id='orphan', token=token, fence=old_fence)
        self.assertFalse(still_refused.get('ok'), still_refused)

    def test_validate_does_not_change_the_reservation(self):
        token = 'a' * 32
        fence = self.acquire('worker', token, 'run-1', 'cursor')
        before = self.call('worker', op='status')['reservations']
        self.call('worker', op='validate', run_id='run-1', token=token, fence=fence)
        after = self.call('worker', op='status')['reservations']
        self.assertEqual(len(after), 1)
        self.assertEqual(before[0]['run_id'], after[0]['run_id'])
        self.assertEqual(before[0]['lease_expires'], after[0]['lease_expires'])

    def test_released_reservation_refuses_its_own_fence(self):
        token = 'a' * 32
        fence = self.acquire('worker', token, 'run-1', 'cursor')
        self.assertTrue(self.call('worker', op='release', token=token, run_id='run-1')['released'])
        answer = self.call('worker', op='validate', run_id='run-1', token=token, fence=fence)
        self.assertFalse(answer.get('ok'), answer)
        self.assertIn(self.reason(answer), ('missing', 'fence_mismatch', 'token_mismatch'))


if __name__ == '__main__':
    unittest.main()
