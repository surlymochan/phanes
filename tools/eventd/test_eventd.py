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
