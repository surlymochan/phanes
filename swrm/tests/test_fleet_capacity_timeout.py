"""Timeouts yield queued runs and retain enough evidence for safe recovery."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _load

xs = _load.load()
import swrm_consult as consult
HERE = pathlib.Path(__file__).resolve().parent


class FleetCapacityTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.rdir = self.root / 'runs' / 'slow'
        self.rdir.mkdir(parents=True)
        self.addCleanup(xs._FLEET_RESERVATIONS.pop, str(self.rdir), None)

    def test_real_slow_acquire_yields_and_recovers_same_token(self):
        guard = self.root / 'guard.py'
        online = self.root / 'online'
        requests = self.root / 'requests.jsonl'
        guard.write_text(
            'import json,pathlib,sys,time\n'
            'p=json.load(sys.stdin)\n'
            f'with open({str(requests)!r}, "a") as f: f.write(json.dumps(p)+"\\n")\n'
            f'if not pathlib.Path({str(online)!r}).exists(): time.sleep(30)\n'
            'print(json.dumps({"ok":True,"admitted":True,"existing":True}))\n')
        command = [sys.executable, str(guard)]
        run = subprocess.run

        def short_timeout(*args, **kwargs):
            self.assertEqual(kwargs['timeout'], 8)
            kwargs['timeout'] = 0.2
            return run(*args, **kwargs)

        log = io.StringIO()
        executor = mock.Mock()
        with mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
                mock.patch.object(xs.subprocess, 'run', side_effect=short_timeout), \
                mock.patch.dict(xs.EXECUTORS, {'codex': executor}), \
                contextlib.redirect_stderr(log):
            attempts, final, error = xs.run_executor_with_rate_limit(
                'codex', {'backend': 'codex', 'model': 'fake'}, {'codex': {'limits': {}}}, 'task', str(self.root), str(self.rdir),
                xs.NOW() + 30, {})
        self.assertEqual(error, 'capacity-unreachable')
        self.assertEqual(attempts, [])
        self.assertIsNone(final)
        executor.assert_not_called()
        status = json.loads((self.rdir / 'status.json').read_text())
        self.assertEqual(status['state'], 'queued')
        self.assertTrue(status['capacity_unreachable'])
        self.assertIn('(3/3)', log.getvalue())
        rows = [json.loads(line) for line in requests.read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['token'] for row in rows}), 1)
        saved = json.loads((self.rdir / 'fleet-reservation.json').read_text())
        self.assertEqual(saved['payload']['token'], rows[0]['token'])
        online.touch()
        with mock.patch.object(xs, 'fleet_guard_command', return_value=command):
            self.assertTrue(xs.reserve_provider_slot(str(self.rdir), 'codex', None, {}))
        rows = [json.loads(line) for line in requests.read_text().splitlines()]
        self.assertEqual(rows[-1]['token'], rows[0]['token'])
        self.assertFalse(json.loads((self.rdir / 'status.json').read_text())['capacity_unreachable'])

    def test_release_detaches_and_retries_after_coordinator_recovers(self):
        guard = self.root / 'guard.py'
        online = self.root / 'online'
        released = self.root / 'released'
        guard.write_text(
            'import json,pathlib,sys\n'
            'p=json.load(sys.stdin)\n'
            f'if not pathlib.Path({str(online)!r}).exists(): sys.exit(1)\n'
            f'pathlib.Path({str(released)!r}).write_text(p["token"])\n'
            'print(json.dumps({"ok":True,"released":True}))\n')
        token = 'a' * 32
        xs._FLEET_RESERVATIONS[str(self.rdir)] = (
            [sys.executable, str(guard)], {'run_id': 'slow', 'token': token})
        started = time.monotonic()
        self.assertFalse(xs.release_fleet_slot(str(self.rdir)))
        self.assertLess(time.monotonic() - started, 1)
        pending = self.rdir / 'fleet-release-pending.json'
        self.assertEqual(json.loads(pending.read_text())['payload']['token'], token)
        xs.set_status(str(self.rdir), state='finished')
        deadline = time.monotonic() + 5
        while json.loads(pending.read_text()).get('attempts', 0) == 0:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        online.touch()
        while pending.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(released.read_text(), token)
        self.assertEqual(json.loads((self.rdir / 'status.json').read_text())['state'], 'finished')

    def test_release_transfers_parent_ownership_to_durable_queue(self):
        xs._FLEET_RESERVATIONS[str(self.rdir)] = (['guard'], {'run_id': 'slow', 'token': 'a' * 32})
        with mock.patch.object(xs, 'start_fleet_release_worker'):
            self.assertFalse(xs.release_fleet_slot(str(self.rdir)))
        self.assertNotIn(str(self.rdir), xs._FLEET_RESERVATIONS)
        self.assertTrue((self.rdir / 'fleet-release-pending.json').exists())

    def test_consult_lost_acquire_responses_keep_release_credentials(self):
        slot = self.root / 'runs' / 'consult-slot-lost'
        requests = []
        call = xs.fleet_guard_call
        command = [sys.executable, str(HERE.parent / 'bin' / 'fleet-capacity'), 'broker']

        def lost_response(command, payload):
            self.assertTrue(call(command, payload)['admitted'])
            requests.append(dict(payload))
            raise RuntimeError('response lost after commit')

        profile = {'backend': 'codex', 'binary': 'unused', 'cwd': str(self.root)}
        with mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': str(self.root)}):
            with mock.patch.object(xs, 'RUNS', str(self.root / 'runs')), \
                    mock.patch.object(xs, 'fleet_guard_command', return_value=command), \
                    mock.patch.object(xs, 'fleet_guard_call', side_effect=lost_response), \
                    mock.patch.object(xs, 'start_fleet_release_worker'), \
                    mock.patch.object(xs.time, 'sleep'):
                result = consult._ask_one(xs, profile, 'question', str(self.root), xs.NOW() + 60, {}, 'lost')
            self.assertFalse(result['valid'])
            self.assertEqual(len(requests), 3)
            self.assertEqual(len(call(command, {'op': 'status'})['reservations']), 1)
            pending = json.loads((slot / 'fleet-release-pending.json').read_text())
            self.assertEqual({r['token'] for r in requests}, {pending['payload']['token']})
            self.assertTrue(xs.retry_fleet_release(str(slot)))
            self.assertEqual(call(command, {'op': 'status'})['reservations'], [])

    @unittest.skipIf(os.name == 'nt', 'requires flock advisory locks')
    def test_slow_acquire_does_not_hold_local_admission_lock(self):
        entered, finish = threading.Event(), threading.Event()

        def slow_guard(command, payload):
            entered.set()
            self.assertTrue(finish.wait(5))
            return {'admitted': True}

        def other_admission():
            with open(self.root / 'provider-concurrency.lock', 'a') as lock:
                xs._flock(lock, xs.LOCK_EX | xs.LOCK_NB)

        def guard_command():
            # The unrelated caller does not need the remote coordinator.
            return ['guard'] if threading.current_thread() is worker else None

        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(xs.reserve_provider_slot(
            str(self.rdir), 'codex', None, {'concurrency': {'total': 2}})))
        with mock.patch.object(xs, 'DATA', str(self.root)), \
                mock.patch.object(xs, 'RUNS', str(self.root / 'runs')), \
                mock.patch.object(xs, 'fleet_guard_command', side_effect=guard_command), \
                mock.patch.object(xs, 'fleet_guard_call', side_effect=slow_guard):
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                other_admission()
                self.assertTrue(xs.reserve_provider_slot(str(self.root / 'runs' / 'other'), 'pi', None,
                                                        {'concurrency': {'total': 2}}))
            finally:
                finish.set()
                worker.join(5)
        self.assertEqual(outcome, [True])

    @unittest.skipIf(os.name == 'nt', 'requires flock advisory locks')
    def test_acquire_backoff_releases_local_admission_lock(self):
        def backoff(_seconds):
            with open(self.root / 'provider-concurrency.lock', 'a') as lock:
                xs._flock(lock, xs.LOCK_EX | xs.LOCK_NB)

        with mock.patch.object(xs, 'DATA', str(self.root)), \
                mock.patch.object(xs, 'fleet_guard_command', return_value=['guard']), \
                mock.patch.object(xs, 'fleet_guard_call', side_effect=RuntimeError('timeout')), \
                mock.patch.object(xs.time, 'sleep', side_effect=backoff) as sleep:
            self.assertFalse(xs.reserve_provider_slot(str(self.rdir), 'codex', None, {}))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.2, 0.4])

    def test_remote_admission_rechecks_local_limit_before_starting(self):
        other = self.root / 'runs' / 'other'

        def acquire(command, payload):
            xs.set_status(str(other), state='running', backend='pi', concurrency_keys=['pi', 'total'])
            return {'admitted': True}

        with mock.patch.object(xs, 'DATA', str(self.root)), \
                mock.patch.object(xs, 'RUNS', str(self.root / 'runs')), \
                mock.patch.object(xs, 'fleet_guard_command', return_value=['guard']), \
                mock.patch.object(xs, 'fleet_guard_call', side_effect=acquire), \
                mock.patch.object(xs, 'start_fleet_release_worker'):
            self.assertFalse(xs.reserve_provider_slot(str(self.rdir), 'codex', None,
                                                     {'concurrency': {'total': 1}},
                                                     keep_waiting=lambda: not other.exists()))
        self.assertEqual(xs.jload(str(self.rdir / 'status.json'), {})['state'], 'queued')
        self.assertTrue((self.rdir / 'fleet-release-pending.json').exists())

    def test_cli_startup_replays_durable_release_queue(self):
        released = self.root / 'released-on-restart'
        command = [sys.executable, '-c',
                   'import json,pathlib,sys; p=json.load(sys.stdin); '
                   f'pathlib.Path({str(released)!r}).write_text(p["token"]); '
                   'print(json.dumps({"ok": True}))']
        pending = self.rdir / 'fleet-release-pending.json'
        pending.write_text(json.dumps({'command': command,
                                       'payload': {'op': 'release', 'run_id': 'slow', 'token': 'b' * 32}}))
        env = dict(os.environ, OPENSWARM_HOME=str(self.root))
        result = subprocess.run([sys.executable, xs.__file__, '--help'], env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        while pending.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(released.read_text(), 'b' * 32)

    def test_cli_capacity_failure_keeps_run_queued(self):
        class StillQueued(Exception):
            pass

        ws = {'mode': 'cwd', 'repo': str(self.root), 'root': str(self.root), 'path': str(self.root)}
        argv = ['swrm', 'run', '--prompt', 'task', '--cwd', str(self.root), '--no-worktree']
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, 'argv', argv))
            for name, kwargs in {
                'RUNS': {'new': str(self.root / 'runs')},
                'new_run_id': {'return_value': 'slow'},
                'get_state': {'return_value': {}}, 'routing_advice': {'return_value': None},
                'route': {'return_value': {'ok': True, 'backend': 'pi', 'model': 'fake'}},
                'prepare_workspace': {'return_value': ws}, 'admit_budget': {},
                'run_workspace_prepare': {'return_value': None},
                'add_lessons_to_prompt': {'side_effect': lambda p, *a: (p, [])},
                'add_patterns_to_prompt': {'side_effect': lambda p, *a: (p, [])},
                'fleet_guard_command': {'return_value': ['guard']},
                'fleet_guard_call': {'side_effect': RuntimeError('timeout')},
            }.items():
                stack.enter_context(mock.patch.object(xs, name, **kwargs))
            finalize = stack.enter_context(mock.patch.object(xs, 'finalize_workspace'))
            wait = stack.enter_context(mock.patch.object(xs, 'wait_run_rate_limit', side_effect=StillQueued))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            with self.assertRaises(StillQueued):
                xs.main()
            wait.assert_called_once()
        finalize.assert_not_called()
        status = json.loads((self.rdir / 'status.json').read_text())
        self.assertEqual(status['state'], 'queued')
        self.assertEqual(status['queue_reason'], 'capacity-unreachable')
        self.assertFalse((self.rdir / 'result.json').exists())

    def test_cli_retries_same_run_after_coordinator_recovers(self):
        class Executed(Exception):
            pass

        ws = {'mode': 'cwd', 'repo': str(self.root), 'root': str(self.root), 'path': str(self.root)}
        executor = mock.Mock(return_value=(0, {'status': 'done'}, {}, ''))
        requests = []
        clock = [100.0]
        xs.set_status(str(self.rdir), queued_at=100.0, budget_minutes=1)

        def coordinator(command, payload):
            requests.append(dict(payload))
            if len(requests) <= 3:
                clock[0] += 8
                raise RuntimeError('timeout')
            return {'admitted': True}

        def queued_wait(*args):
            status = xs.jload(str(self.rdir / 'status.json'), {})
            self.assertEqual(status['state'], 'queued')
            self.assertTrue(status['capacity_unreachable'])
            executor.assert_not_called()
            clock[0] += 30
            return True

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, 'argv', [
                'swrm', 'run', '--prompt', 'task', '--cwd', str(self.root), '--no-worktree',
                '--budget-minutes', '1']))
            for name, kwargs in {
                'RUNS': {'new': str(self.root / 'runs')},
                'new_run_id': {'return_value': 'slow'},
                'get_state': {'return_value': {}}, 'routing_advice': {'return_value': None},
                'route': {'return_value': {'ok': True, 'backend': 'pi', 'model': 'fake'}},
                'prepare_workspace': {'return_value': ws}, 'admit_budget': {},
                'run_workspace_prepare': {'return_value': None},
                'add_lessons_to_prompt': {'side_effect': lambda p, *a: (p, [])},
                'add_patterns_to_prompt': {'side_effect': lambda p, *a: (p, [])},
                'fleet_guard_command': {'return_value': ['guard']},
                'fleet_guard_call': {'side_effect': coordinator},
                'wait_run_rate_limit': {'side_effect': queued_wait},
                'start_fleet_release_worker': {},
                'finalize_workspace': {'side_effect': Executed},
            }.items():
                stack.enter_context(mock.patch.object(xs, name, **kwargs))
            stack.enter_context(mock.patch.dict(xs.EXECUTORS, {'pi': executor}))
            stack.enter_context(mock.patch.object(xs, 'NOW', side_effect=lambda: clock[0]))
            stack.enter_context(mock.patch.object(xs.time, 'sleep',
                                                 side_effect=lambda s: clock.__setitem__(0, clock[0] + s)))
            with self.assertRaises(Executed):
                xs.main()
        executor.assert_called_once()
        self.assertEqual(len(requests), 4)
        self.assertEqual(len({r['token'] for r in requests}), 1)
        self.assertEqual({r['run_id'] for r in requests}, {'slow'})
        status = xs.jload(str(self.rdir / 'status.json'), {})
        self.assertFalse(status['capacity_unreachable'])
        self.assertAlmostEqual(status['queued_seconds'], 54.6)
        self.assertAlmostEqual(executor.call_args.args[5], 60.0)

    def test_agy_lane_retries_403_after_detached_release(self):
        lane = {'name': 'test', 'backend': 'agy', 'model': 'fake'}
        spec = {'run_id': 'slow', 'started': xs.NOW(), 'budget_minutes': 1, 'task': 'task'}
        ws = {'repo': str(self.root), 'root': str(self.root), 'path': str(self.root)}
        child = mock.Mock()
        child.proc.poll.return_value = None
        child.alive.side_effect = lambda: child.proc is None or child.proc.poll() is None
        child.close.side_effect = lambda: setattr(child.proc.poll, 'return_value', 1)
        turns = [
            (None, {}, 'upstream_forbidden', 1, 'events1', 'HTML response 403'),
            ({'status': 'done'}, {}, None, 0, 'events2', None),
        ]
        with mock.patch.object(xs, 'RUNS', str(self.root / 'runs')), \
                mock.patch.dict(os.environ, {'OPENSWARM_FLEET_CAPACITY_DIR': str(self.root)}), \
                mock.patch.object(xs, 'fleet_guard_command', return_value=[
                    sys.executable, str(HERE.parent / 'bin' / 'fleet-capacity'), 'broker']), \
                mock.patch.object(xs, 'lane_prepare_workspace', return_value=ws), \
                mock.patch.object(xs, 'run_workspace_prepare', return_value=None), \
                mock.patch.object(xs, 'add_lessons_to_prompt', side_effect=lambda p, *a: (p, [])), \
                mock.patch.object(xs, 'add_patterns_to_prompt', side_effect=lambda p, *a: (p, [])), \
                mock.patch.object(xs, 'lane_control_messages'), \
                mock.patch.object(xs, 'lane_write_state'), \
                mock.patch.object(xs, 'git', return_value=(0, '', '')), \
                mock.patch.object(xs, 'lane_run_turn', side_effect=turns) as turn, \
                mock.patch.object(xs, 'lane_finalize_run', return_value={'status': 'done'}) as finalize:
            outcome = xs.lane_do_run(lane, spec, child, None, spec)
        self.assertEqual(outcome['status'], 'done')
        self.assertEqual(turn.call_count, 2)
        self.assertIsNone(finalize.call_args.args[5])
        pending = self.rdir / 'fleet-release-pending.json'
        deadline = time.monotonic() + 5
        while pending.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertNotIn(str(self.rdir), xs._FLEET_RESERVATIONS)

    def test_lane_capacity_failure_does_not_finalize(self):
        lane = {'name': 'test', 'backend': 'pi', 'model': 'fake'}
        spec = {'run_id': 'slow', 'started': xs.NOW(), 'budget_minutes': 1, 'task': 'task'}
        ws = {'repo': str(self.root), 'root': str(self.root), 'path': str(self.root)}
        xs.set_status(str(self.rdir), state='queued', capacity_unreachable=True)
        child = mock.Mock()
        child.alive.return_value = True
        with mock.patch.object(xs, 'RUNS', str(self.root / 'runs')), \
                mock.patch.object(xs, 'lane_prepare_workspace', return_value=ws), \
                mock.patch.object(xs, 'run_workspace_prepare', return_value=None), \
                mock.patch.object(xs, 'add_lessons_to_prompt', side_effect=lambda p, *a: (p, [])), \
                mock.patch.object(xs, 'add_patterns_to_prompt', side_effect=lambda p, *a: (p, [])), \
                mock.patch.object(xs, 'lane_control_messages'), \
                mock.patch.object(xs, 'reserve_provider_slot', return_value=False), \
                mock.patch.object(xs, 'lane_finalize_run') as finalize:
            outcome = xs.lane_do_run(lane, spec, child, None, spec)
        self.assertEqual(outcome['state'], 'queued')
        finalize.assert_not_called()
        self.assertEqual(json.loads((self.rdir / 'status.json').read_text())['state'], 'queued')
