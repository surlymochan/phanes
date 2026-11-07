import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import eventd


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
