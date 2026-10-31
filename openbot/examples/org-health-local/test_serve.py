"""本机诊断服务的边界单测：只允许 loopback Host 与白名单路径，禁止目录列表。

仅使用标准库且不调用 bun：在临时端口起服务后逐条检查响应状态码。
"""
import http.client
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import serve  # noqa: E402  (需要先让示例目录可被导入)

INDEX = serve.ROOT / 'index.html'
BUNDLED_DATA = serve.ROOT / 'data' / 'diagnosis.json'


class ErrorRecorder:
    """替身：只记录 send_error 调用，用于直接检查 handler 的目录列举禁令。"""

    def __init__(self):
        self.errors = []

    def send_error(self, code, *args, **kwargs):
        self.errors.append(code)


class LocalServeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'diagnosis.json'
        self.data.write_text('{"source": "temporary-data"}', encoding='utf-8')
        self.server, self.port = self.start_server(self.data)

    def start_server(self, data=None):
        """按命令行参数在临时端口起服务，返回 (server, port)。"""
        argv = ['--port', '0'] if data is None else ['--port', '0', '--data', str(data)]
        args = serve.parse_args(argv)
        server = serve.create_server(args.port, args.data)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server, server.server_address[1]

    def request(self, path, host=None, port=None):
        """发起一次 GET；host 为 None 时沿用客户端默认的 loopback Host。"""
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            headers = {} if host is None else {'Host': host}
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_service_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_loopback_hosts_are_served(self):
        for host in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
            with self.subTest(host=host):
                status, body = self.request('/index.html', host)
                self.assertEqual(status, 200)
                self.assertEqual(body, INDEX.read_bytes())

    def test_root_path_serves_the_index_page(self):
        # SimpleHTTPRequestHandler 在所有 Python 3 版本里都会用目录下的 index.html 回应 "/"。
        status, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertEqual(body, INDEX.read_bytes())

    def test_other_hosts_are_forbidden(self):
        for host in ('evil.example', f'127.0.0.1:{self.port + 1}', 'localhost'):
            with self.subTest(host=host):
                status, _ = self.request('/index.html', host)
                self.assertEqual(status, 403)

    def test_unlisted_paths_are_not_found(self):
        for path in ('/serve.py', '/app.js', '/README.md', '/data'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_path_traversal_is_not_found(self):
        for path in ('/../serve.py', '/%2e%2e/serve.py', '/..%2fserve.py', '/data/../serve.py'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_directory_request_is_not_found(self):
        for path in ('/data/', '/styles.css/'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_directory_listing_is_disabled(self):
        # 目录列举被显式禁止，不依赖运行时的 index 处理。
        recorder = ErrorRecorder()
        self.assertIsNone(serve.LocalOnlyHandler.list_directory(recorder, '/data'))
        self.assertEqual(recorder.errors, [404])

    def test_data_option_serves_the_given_file(self):
        status, body = self.request('/data/diagnosis.json')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data.read_bytes())
        self.assertNotEqual(body, BUNDLED_DATA.read_bytes())

    def test_default_data_file_is_served(self):
        server, port = self.start_server()
        self.assertEqual(server.server_address[0], '127.0.0.1')
        status, body = self.request('/data/diagnosis.json', port=port)
        self.assertEqual(status, 200)
        self.assertEqual(body, BUNDLED_DATA.read_bytes())


if __name__ == '__main__':
    unittest.main()
