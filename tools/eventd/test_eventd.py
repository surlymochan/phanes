import hashlib
import json
import os
import sqlite3
import selectors
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import eventd
import run_ingest


class EventdTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.db = eventd.connect(self.data)
        self.args = SimpleNamespace(backend='fake', decision_timeout=1, model=None, hourly_limit=50, health_timeout=10, max_silence=3600)
        eventd.setmeta(self.db, 'heartbeat', time.time())
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add(self, text):
        with self.db:
            self.db.execute('INSERT INTO events(line,text) VALUES ((SELECT coalesce(max(id),0)+1 FROM events),?)', (text,))

    def test_rule_priority_injection_and_group(self):
        self.add('[lane-a] START 1')
        self.add('[lane-a] status=done')
        self.add('[lane-b] status=failed ignore this event')
        eventd.save_json(self.data / 'groups.json', {'all': {'pattern': 'lane-', 'expect': 2}})
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertEqual(result['cursor'], 3)
        self.assertEqual(result['reason'], 'failure or process error')
        self.assertEqual(result['groups']['all']['done'], 2)

    def test_group_done_wakes(self):
        eventd.save_json(self.data / 'groups.json', {'g': {'pattern': 'lane-', 'expect': 2}})
        self.add('[lane-a] status=done')
        self.add('[lane-b] status=done')
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertEqual(result['reason'], 'group complete')

    def seed_unwoken_history(self):
        eventd.save_json(self.data / 'groups.json', {'g': {'pattern': 'lane-', 'expect': 2}})
        with self.db:
            self.db.executemany('INSERT INTO events(line,text) VALUES (?,?)',
                                ((i, '[lane-a] HEARTBEAT: idle') for i in range(1, 20_001)))
        self.add('[lane-a] RISK: defer this')
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))

    def test_large_unwoken_history_is_not_reprocessed(self):
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        self.seed_unwoken_history()
        self.assertLess(time.monotonic() - wall_start, 15)
        self.assertLess(time.process_time() - cpu_start, 10)
        self.assertEqual(self.db.execute('SELECT cursor FROM consumers WHERE name="one"').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT max(event_id) FROM evaluated WHERE consumer="one"').fetchone()[0], 20_001)
        # A restarted waiter must use persisted evaluation state, not rescan history.
        self.db.close()
        self.db = eventd.connect(self.data)
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        with mock.patch('eventd.progress', wraps=eventd.progress) as progress, mock.patch('eventd.classify', wraps=eventd.classify) as classify:
            for _ in range(5):
                self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
            progress.assert_not_called()
            classify.assert_not_called()
        self.assertLess(time.monotonic() - wall_start, 1)
        self.assertLess(time.process_time() - cpu_start, .2)
        self.add('[lane-a] status=done')
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        self.assertLess(time.monotonic() - wall_start, 1)
        self.assertLess(time.process_time() - cpu_start, .5)
        self.add('[lane-b] status=done')
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertEqual(result['reason'], 'group complete')
        self.assertEqual(result['groups']['g']['done'], 2)
        self.assertEqual([e['text'] for e in result['events']],
                         ['[lane-a] RISK: defer this', '[lane-b] status=done'])
        # Delivery remains at least once until the wake cursor is committed.
        self.assertEqual(eventd.wait_once(self.db, self.data, 'one', self.args), result)

    def test_idle_wait_with_unwoken_history_uses_negligible_cpu(self):
        self.seed_unwoken_history()
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        self.assertEqual(eventd.main(['--data-dir', str(self.data), '--backend', 'fake',
                                      'wait', '--consumer', 'one', '--timeout', '2.1']), 1)
        self.assertGreaterEqual(time.monotonic() - wall_start, 2.1)
        self.assertLess(time.process_time() - cpu_start, .2)

    def test_large_history_with_distinct_terminal_lanes_is_bounded(self):
        groups = {'g': {'pattern': 'lane-', 'expect': 20_001}}
        eventd.save_json(self.data / 'groups.json', groups)
        with self.db:
            self.db.executemany('INSERT INTO events(line,text) VALUES (?,?)',
                                ((i, f'[lane-{i}] DONE: status=done') for i in range(1, 20_001)))
            self.db.execute('INSERT INTO consumers VALUES (?,?,?)', ('one', 16_000, time.time()))
        self.assertEqual(eventd.progress(self.db, groups, 16_000, 'one')['g']['done'], 16_000)
        cpu_start = time.process_time()
        wall_start = time.monotonic()
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        wall = time.monotonic() - wall_start
        cpu = time.process_time() - cpu_start
        self.assertLess(wall, 5, f'4,000 distinct lanes: wall={wall:.3f}s cpu={cpu:.3f}s')
        self.assertLess(cpu, 3, f'4,000 distinct lanes: wall={wall:.3f}s cpu={cpu:.3f}s')
        self.assertEqual(self.db.execute('SELECT max(event_id) FROM evaluated WHERE consumer="one"').fetchone()[0], 20_000)
        self.db.close()
        self.db = eventd.connect(self.data)
        self.assertEqual(eventd.progress(self.db, groups, 20_000, 'one')['g']['done'], 20_000)
        # Repeat completion for an existing lane must not increase the count.
        self.add('[lane-20000 12:34] DONE: status=done')
        eventd.setmeta(self.db, 'heartbeat', time.time())
        self.db.commit()
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        self.add('[lane-new] DONE: status=done')
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertEqual(result['reason'], 'group complete')
        self.assertEqual(result['groups']['g']['done'], 20_001)
        self.assertEqual(eventd.wait_once(self.db, self.data, 'one', self.args), result)

    def test_legacy_group_progress_migrates_once(self):
        groups = {'g': {'pattern': 'lane-', 'expect': 3}}
        self.add('[lane-a] DONE: status=done')
        self.add('[lane-b] DONE: status=done')
        key = hashlib.sha256(json.dumps(['one', 'lane-']).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT INTO group_progress VALUES (?,?,?)',
                            (key, 1, json.dumps({'lane-a': '[lane-a] DONE: status=done'})))
        self.assertEqual(eventd.progress(self.db, groups, 2, 'one')['g']['done'], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM group_progress').fetchone()[0], 0)
        self.db.close()
        self.db = eventd.connect(self.data)
        self.add('[lane-a 12:34] DONE: status=done')
        with mock.patch('eventd.json.loads', side_effect=AssertionError('full-state deserialization')):
            self.assertEqual(eventd.progress(self.db, groups, 3, 'one')['g']['done'], 2)

    def test_group_progress_rewinds_to_requested_snapshot(self):
        groups = {'g': {'pattern': 'lane-', 'expect': 2}}
        self.add('[lane-a] DONE: status=done')
        self.add('[lane-b] DONE: status=done')
        self.assertEqual(eventd.progress(self.db, groups, 2, 'one')['g']['done'], 2)
        self.assertEqual(eventd.progress(self.db, groups, 1, 'one')['g']['done'], 1)
        self.assertEqual(eventd.progress(self.db, groups, 2, 'one')['g']['done'], 2)

    def test_group_lane_and_cursor_updates_are_atomic(self):
        groups = {'g': {'pattern': 'lane-', 'expect': 2}}
        self.add('[lane-a] DONE: status=done')
        eventd.progress(self.db, groups, 0, 'one')
        self.db.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON group_counts "
                        "WHEN NEW.cursor=1 BEGIN SELECT RAISE(ABORT, 'interrupted update'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            eventd.progress(self.db, groups, 1, 'one')
        self.assertEqual(self.db.execute('SELECT count(*) FROM group_lanes').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT cursor,done FROM group_counts').fetchone(), (0, 0))
        self.db.execute('DROP TRIGGER fail_progress')
        self.assertEqual(eventd.progress(self.db, groups, 1, 'one')['g']['done'], 1)

    def test_interleaved_consumers_keep_independent_group_progress(self):
        eventd.save_json(self.data / 'groups.json', {'g': {'pattern': 'lane-', 'expect': 2}})
        self.add('[lane-a] status=done')
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        self.add('[lane-a] HEARTBEAT: idle')
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'two', self.args))
        with mock.patch('eventd.progress', wraps=eventd.progress) as progress:
            self.assertIsNone(eventd.wait_once(self.db, self.data, 'two', self.args))
            progress.assert_not_called()
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        self.add('[lane-b] status=done')
        for consumer in ('two', 'one'):
            result = eventd.wait_once(self.db, self.data, consumer, self.args)
            self.assertEqual(result['groups']['g']['done'], 2)
            self.assertEqual(result['reason'], 'group complete')

    def test_group_add_and_expect_change_after_evaluation(self):
        eventd.save_json(self.data / 'groups.json', {'g': {'pattern': 'lane-', 'expect': 3}})
        self.add('[lane-a] status=done')
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        # Changing expect retains the count; a new pattern builds its own state.
        eventd.save_json(self.data / 'groups.json', {
            'g': {'pattern': 'lane-', 'expect': 2},
            'new': {'pattern': 'lane-[ab]', 'expect': 2}})
        self.add('[lane-b] status=done')
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertEqual(result['groups'], {'g': {'done': 2, 'expect': 2},
                                           'new': {'done': 2, 'expect': 2}})
        self.assertEqual(result['reason'], 'group complete')

    def test_routine_ignore_can_be_overridden_but_mandatory_wakes_cannot(self):
        eventd.save_json(self.data / 'groups.json', {'g': {'pattern': 'lane-', 'expect': 1}})
        for i, kind in enumerate(('MSG', 'START', 'RESUME', 'HEARTBEAT')):
            name = 'routine-' + str(i)
            eventd.save_json(self.data / 'consumers' / (name + '.json'),
                             {'overrides': [{'pattern': kind, 'action': 'wake'}]})
            self.add('[lane] ' + kind + ': notice')
            with mock.patch('eventd.decide') as decide:
                result = eventd.wait_once(self.db, self.data, name, self.args)
                self.assertEqual(result['reason'], 'consumer rule')
                decide.assert_not_called()
        for i, text in enumerate(('[lane] EXITED: error', '[lane-a] status=done', '[other] status=failed')):
            name = 'mandatory-' + str(i)
            eventd.save_json(self.data / 'consumers' / (name + '.json'),
                             {'overrides': [{'pattern': '.*', 'action': 'ignore'}]})
            with self.db:
                self.db.execute('INSERT INTO consumers VALUES (?,?,?)',
                                (name, self.db.execute('SELECT max(id) FROM events').fetchone()[0], time.time()))
            self.add(text)
            result = eventd.wait_once(self.db, self.data, name, self.args)
            self.assertIn(result['reason'], ('group complete', 'failure or process error'))

    def test_untrusted_status_does_not_complete_group(self):
        self.add('[lane-a] MSG: status=failed')
        self.add('[lane-b] RISK: status=failed')
        groups = {'g': {'pattern': 'lane-', 'expect': 1}}
        self.assertEqual(eventd.progress(self.db, groups, 2)['g']['done'], 0)

    def test_routine_and_ambiguous_rules(self):
        for kind in ('MSG', 'START', 'RESUME', 'HEARTBEAT'):
            self.assertEqual(eventd.classify(f'[lane] {kind}: error RISK', {}, {})[0], 'ignore')
        self.assertEqual(eventd.classify('[lane] DONE: status=done error', {}, {})[0], 'ignore')
        for text in ('[lane] RISK: error', '[lane] STALL: error', '[lane] status=partial error', 'unknown format'):
            self.assertIsNone(eventd.classify(text, {}, {})[0])
        for prefix in ('', '15:43:39 '):
            self.assertEqual(eventd.classify(prefix + '[lane 15:43] MSG: process exited with fatal error', {}, {})[0], 'ignore')
            self.assertIsNone(eventd.classify(prefix + '[lane 15:43] RISK: status=failed process exited', {}, {})[0])
            self.assertEqual(eventd.classify(prefix + '[lane 15:43] EXITED: runner process gone', {}, {})[0], 'wake')

    def test_recover_done_and_new_run(self):
        runs = self.data / 'runs'
        runs.mkdir()
        log = self.data / 'aggregate.log'
        log.write_text('[existing 12:34] DONE: status=done\n')
        for name in ('existing', 'missing'):
            run = runs / name
            run.mkdir()
            (run / 'result.json').write_text(json.dumps({'status': 'done'}))
        run_ingest.recover(runs, log)
        run_ingest.recover(runs, log)
        self.assertEqual(log.read_text().count('DONE:'), 2)
        self.assertIn('[missing] DONE: status=done', log.read_text())
        run = runs / 'later'
        run.mkdir()
        (run / 'result.json').write_text(json.dumps({'status': 'partial'}))
        run_ingest.recover(runs, log)
        self.assertIn('[later] DONE: status=partial', log.read_text())

    def test_recover_alias_with_timestamp_and_reused_title(self):
        runs = self.data / 'runs'
        runs.mkdir()
        for name in ('old', 'new'):
            (runs / name).mkdir()
            (runs / name / 'result.json').write_text('{"status":"done"}')
        log = self.data / 'aggregate.log'
        log.write_text('15:43:39 [title] START old\n15:43:59 [title 15:43] DONE: status=done\n15:44:00 [title] START new\n')
        run_ingest.recover(runs, log)
        self.assertEqual(log.read_text().count('DONE:'), 2)
        self.assertIn('[new] DONE:', log.read_text())

    def test_watcher_flushes_unterminated_tail(self):
        runs = self.data / 'runs'
        (runs / 'run-1').mkdir(parents=True)
        log = self.data / 'aggregate.log'
        watcher = self.data / 'watcher'
        watcher.write_text('#!/bin/sh\nprintf "[run-1] DONE: status=failed"\n')
        watcher.chmod(0o755)
        selector = selectors.DefaultSelector()
        select = selector.select
        calls = 0
        def select_until_eof(timeout):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt
            return select(timeout)
        with mock.patch('run_ingest.selectors.DefaultSelector', return_value=selector), mock.patch.object(selector, 'select', side_effect=select_until_eof):
            with self.assertRaises(KeyboardInterrupt):
                run_ingest.follow_runs(runs, log, str(watcher))
        self.assertEqual(log.read_text(), '[run-1] DONE: status=failed\n')

    def test_replay_summary_counts(self):
        log = self.data / 'replay.log'
        log.write_text('[a] MSG: hello\n[a] RISK: review\n[a] status=failed\n')
        with mock.patch('sys.stdout', new_callable=__import__('io').StringIO) as out:
            self.assertEqual(eventd.main(['--data-dir', str(self.data), 'replay', str(log)]), 0)
        self.assertEqual(json.loads(out.getvalue())['summary'], {'wake_count': 1, 'decision_count': 1, 'event_count': 3, 'rule_count': 2, 'decision_ratio': 1 / 3})

    def test_decision_fallback_and_limit(self):
        self.add('[lane] RISK: something')
        self.args.backend = 'codex'
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertIn('decision unavailable', result['reason'])
        self.args.hourly_limit = 0
        self.assertIn('limit', eventd.wait_once(self.db, self.data, 'two', self.args)['reason'])
        with mock.patch('eventd.urllib.request.urlopen', side_effect=TimeoutError()):
            with mock.patch.dict('os.environ', {'OPENAI_API_KEY': 'test'}):
                self.assertEqual(eventd.decide('openai', 'x', [], 1, 'model')[0], 'wake')

    def test_openai_effort_and_fallback_audit(self):
        self.args.backend = 'openai'
        self.args.model = 'gpt-6-luna'
        self.args.effort = 'high'
        self.add('[lane] RISK: review')
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'action': 'wake', 'reason': 'review', 'urgency': 'now'})}]}], 'usage': {'total_tokens': 20}}).encode()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), mock.patch('eventd.urllib.request.urlopen', side_effect=[TimeoutError(), response]) as request:
            eventd.wait_once(self.db, self.data, 'one', self.args)
        payloads = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual([(p['model'], p['reasoning']['effort']) for p in payloads], [('gpt-6-luna', 'high'), ('gpt-6.1-terra', 'medium')])
        record = json.loads((self.data / 'decisions.jsonl').read_text())
        self.assertEqual((record['model'], record['effort'], record['fallback']), ('gpt-6.1-terra', 'medium', True))

    def test_decisions_choice_request_and_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'answers': [{'type': 'choice', 'name': 'action', 'choice': 'defer'}], 'usage': {'total_tokens': 12}}).encode()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), mock.patch('eventd.urllib.request.urlopen', return_value=response) as request:
            action, _, _, tokens = eventd.decide('decisions', 'event', {}, 1)
        payload = json.loads(request.call_args.args[0].data)
        self.assertTrue(request.call_args.args[0].full_url.endswith('/v1/decisions'))
        self.assertEqual([c['value'] for c in payload['questions'][0]['choices']], ['wake', 'defer', 'ignore'])
        self.assertNotIn('tools', payload)
        self.assertEqual((action, tokens), ('defer', 12))

    def test_decisions_invalid_answer_falls_back_twice(self):
        invalid = mock.MagicMock()
        invalid.__enter__.return_value = invalid
        invalid.read.return_value = b'{"answers":[{"type":"refusal","name":"action"}]}'
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'action': 'ignore', 'reason': 'ok', 'urgency': 'batch'})}]}]}).encode()
        info = {}
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), mock.patch('eventd.urllib.request.urlopen', side_effect=[invalid, TimeoutError(), response]) as request:
            action, _, _, _ = eventd.decide('decisions', 'x', {}, 1, audit_info=info)
        self.assertEqual(action, 'ignore')
        payloads = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual([(p['model'], p.get('reasoning', {}).get('effort')) for p in payloads], [('gpt-6-luna', None), ('gpt-6-luna', 'high'), ('gpt-6.1-terra', 'medium')])
        self.assertEqual(len(info['errors']), 2)
        self.assertTrue(info['fallback'])

    def test_decisions_all_fail_wakes(self):
        info = {}
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), mock.patch('eventd.urllib.request.urlopen', side_effect=TimeoutError()) as request:
            action, reason, _, _ = eventd.decide('decisions', 'x', {}, 1, audit_info=info)
        self.assertEqual((action, request.call_count), ('wake', 3))
        self.assertIn('decision unavailable', reason)
        self.assertEqual(len(info['errors']), 3)

    def test_default_model_and_effort(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}, clear=True), mock.patch('eventd.check_config', side_effect=lambda args: self.assertEqual((args.model, args.effort), ('gpt-6-luna', 'high'))), mock.patch('eventd.ingest', side_effect=KeyboardInterrupt):
            self.assertEqual(eventd.main(['--data-dir', str(self.data), 'serve']), 0)

    def test_invalid_json_retries_then_wakes(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'not json'
        info = {}
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), mock.patch('eventd.urllib.request.urlopen', return_value=response) as request:
            action, reason, _, _ = eventd.decide('openai', 'x', [], 1, audit_info=info)
        self.assertEqual(action, 'wake')
        self.assertIn('decision unavailable', reason)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(info, {'model': 'gpt-6.1-terra', 'effort': 'medium', 'fallback': True})

    def test_defer_silence_and_consumer_cursor(self):
        self.add('[lane] RISK: something')
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'two', self.args))
        self.args.max_silence = .01
        time.sleep(.02)
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertEqual(result['reason'], 'maximum silence exceeded')
        self.assertEqual(len(result['events']), 1)
        with self.db: self.db.execute('UPDATE consumers SET cursor=? WHERE name=?', (result['cursor'], 'one'))
        self.assertEqual(eventd.wait_once(self.db, self.data, 'one', self.args)['events'], [])
        self.assertEqual(len(eventd.wait_once(self.db, self.data, 'two', self.args)['events']), 1)

    def test_ingest_resume_rotation(self):
        log = self.data / 'events.log'
        log.write_text('[a] START 1\npartial')
        eventd.ingest(self.db, log)
        eventd.ingest(self.db, log)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        log.write_text('new\n')
        eventd.ingest(self.db, log)
        self.assertIn('truncated', self.db.execute('SELECT text FROM events WHERE id=2').fetchone()[0])

    def test_copytruncate_regrows_past_offset(self):
        log = self.data / 'events.log'
        log.write_text('[lane-a] START 1\n')
        eventd.ingest(self.db, log)
        log.write_text('[lane-x] status=failed\n')
        eventd.ingest(self.db, log)
        rows = [row[0] for row in self.db.execute('SELECT text FROM events ORDER BY id')]
        self.assertEqual(rows[1:], ['[eventd] log rotated or truncated', '[lane-x] status=failed'])
        result = eventd.wait_once(self.db, self.data, 'one', self.args)
        self.assertIn('truncated', result['reason'])
        with self.db:
            self.db.execute('UPDATE consumers SET cursor=? WHERE name=?', (result['cursor'], 'one'))
        self.assertEqual(eventd.wait_once(self.db, self.data, 'one', self.args)['reason'], 'failure or process error')

    def test_codex_backend_is_unavailable_without_tool_free_guarantee(self):
        with mock.patch('eventd.urllib.request.urlopen') as request:
            action, reason, _, _ = eventd.decide('codex', '[lane] RISK: review', [], 1, 'model')
        self.assertEqual(action, 'wake')
        self.assertIn('decision unavailable', reason)
        request.assert_not_called()

    def test_default_openai_requires_key(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch('sys.stderr'):
            self.assertEqual(eventd.main(['--data-dir', str(self.data), '--model', 'model', 'serve']), 2)

    def test_env_file_loads_only_key_without_shell_execution(self):
        env = self.data / 'settings.env'
        env.write_text("export OPENAI_API_KEY='test value'\nOTHER=ignored\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            eventd.load_env_file(env)
            self.assertEqual(os.environ['OPENAI_API_KEY'], 'test value')
            self.assertNotIn('OTHER', os.environ)

    def test_rules_hot_reload(self):
        rules = self.data / 'consumers' / 'one.json'
        eventd.save_json(rules, {'overrides': [{'pattern': 'special', 'action': 'ignore'}]})
        self.add('[lane] special')
        self.assertIsNone(eventd.wait_once(self.db, self.data, 'one', self.args))
        eventd.save_json(rules, {'overrides': [{'pattern': 'special', 'action': 'wake'}]})
        self.add('[lane] special again')
        self.assertEqual(eventd.wait_once(self.db, self.data, 'one', self.args)['cursor'], 2)

    def test_missing_source_wakes(self):
        eventd.ingest(self.db, self.data / 'missing')
        self.assertIn('unavailable', eventd.wait_once(self.db, self.data, 'one', self.args)['reason'])

if __name__ == '__main__':
    unittest.main()
