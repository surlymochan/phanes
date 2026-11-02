"""Executable contract between serve.py's diagnosis API and the static page."""

import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402


class ServeDiagnosisContractTests(unittest.TestCase):
    def setUp(self):
        self.server = serve.create_server(0, root=ROOT, data=ROOT / 'data' / 'diagnosis.json')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]

    def get_json(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode('utf-8'))
        finally:
            connection.close()

    def test_api_key_set_matches_static_page_data(self):
        api_status, api_data = self.get_json('/api/diagnosis')
        static_status, static_data = self.get_json('/data/diagnosis.json')

        self.assertEqual(api_status, 200)
        self.assertEqual(static_status, 200)
        self.assertIsNone(serve.diagnosis_contract_error(static_data))
        self.assertEqual(set(api_data), set(static_data))
        self.assertEqual(api_data, static_data)

    def test_missing_field_has_readable_contract_error(self):
        sample = json.loads((ROOT / 'data' / 'diagnosis.json').read_text(encoding='utf-8'))
        sample.pop('metrics')
        with tempfile.TemporaryDirectory() as temporary:
            data_path = Path(temporary) / 'diagnosis.json'
            data_path.write_text(json.dumps(sample, ensure_ascii=False), encoding='utf-8')
            error = serve.diagnosis_contract_error(json.loads(data_path.read_text(encoding='utf-8')))

        self.assertEqual(error, '诊断数据契约错误：静态页缺少字段：metrics')


if __name__ == '__main__':
    unittest.main()
