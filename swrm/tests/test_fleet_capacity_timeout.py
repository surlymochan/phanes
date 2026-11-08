"""Timeouts yield queued runs and retain enough evidence for safe recovery."""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _load

xs = _load.load()
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
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            xs.main()
        finalize.assert_not_called()
        status = json.loads((self.rdir / 'status.json').read_text())
        self.assertEqual(status['state'], 'queued')
        self.assertEqual(status['queue_reason'], 'capacity-unreachable')
        self.assertFalse((self.rdir / 'result.json').exists())

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
