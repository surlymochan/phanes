"""本机诊断服务的边界单测：只允许 loopback Host 与白名单路径，禁止目录列表。

仅使用标准库且不调用 bun：在临时端口起服务后逐条检查响应状态码。
"""
import http.client
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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
        server = serve.create_server(args.port, args.data, root=args.root)
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
        status, body = self.request('/')
        # Python 3.12 起目录请求会用 index.html 回应，更早版本由 list_directory 直接 404。
        if getattr(serve.SimpleHTTPRequestHandler, 'index_pages', None):
            self.assertEqual(status, 200)
            self.assertEqual(body, INDEX.read_bytes())
        else:
            self.assertEqual(status, 404)

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


class DistRootServeTests(unittest.TestCase):
    """验证 --root 指定产物目录时的服务边界行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / 'dist'
        self.dist.mkdir()
        (self.dist / 'data').mkdir()

        self.index = self.dist / 'index.html'
        self.index.write_bytes(b'<!doctype html><title>dist index</title>')
        self.bundle = self.dist / 'app.bundle.js'
        self.bundle.write_bytes(b'console.log("dist bundle");')
        self.styles = self.dist / 'styles.css'
        self.styles.write_bytes(b'body { background: #000; }')
        self.data = self.dist / 'data' / 'diagnosis.json'
        self.data.write_bytes(b'{"source": "dist-diagnosis"}')

        # 在 dist 内写入未列入白名单的文件
        self.report = self.dist / 'build-report.json'
        self.report.write_bytes(b'{"builder": "passed"}')
        self.secret = self.dist / 'secret.txt'
        self.secret.write_bytes(b'dist secret')

        self.server, self.port = self.start_server(root=self.dist)

    def start_server(self, data=None, root=None):
        argv = ['--port', '0']
        if root is not None:
            argv.extend(['--root', str(root)])
        if data is not None:
            argv.extend(['--data', str(data)])
        args = serve.parse_args(argv)
        server = serve.create_server(args.port, args.data, root=args.root)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server, server.server_address[1]

    def request(self, path, host=None, port=None):
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            headers = {} if host is None else {'Host': host}
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_dist_whitelisted_files_are_served(self):
        cases = (
            ('/index.html', self.index.read_bytes()),
            ('/app.bundle.js', self.bundle.read_bytes()),
            ('/styles.css', self.styles.read_bytes()),
            ('/data/diagnosis.json', self.data.read_bytes()),
        )
        for path, expected_body in cases:
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_body)

    def test_dist_root_path_serves_the_index_page(self):
        status, body = self.request('/')
        if getattr(serve.SimpleHTTPRequestHandler, 'index_pages', None):
            self.assertEqual(status, 200)
            self.assertEqual(body, self.index.read_bytes())
        else:
            self.assertEqual(status, 404)

    def test_dist_data_option_overrides_default_dist_data(self):
        override_data = Path(self.tmp.name) / 'override.json'
        override_data.write_bytes(b'{"source": "override-data"}')

        server, port = self.start_server(root=self.dist, data=override_data)
        status, body = self.request('/data/diagnosis.json', port=port)
        self.assertEqual(status, 200)
        self.assertEqual(body, override_data.read_bytes())
        self.assertNotEqual(body, self.data.read_bytes())

        # index.html 仍正常从 dist 服务
        status_index, body_index = self.request('/index.html', port=port)
        self.assertEqual(status_index, 200)
        self.assertEqual(body_index, self.index.read_bytes())

    def test_unlisted_files_are_not_found(self):
        for path in ('/build-report.json', '/secret.txt', '/app.js', '/README.md', '/data', '/data/'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_other_hosts_are_forbidden(self):
        for host in ('evil.example', f'127.0.0.1:{self.port + 1}', 'localhost'):
            with self.subTest(host=host):
                status, _ = self.request('/index.html', host)
                self.assertEqual(status, 403)

    def test_path_traversal_is_not_found(self):
        for path in ('/../index.html', '/%2e%2e/index.html', '/..%2findex.html',
                     '/data/../index.html', '/data/../data/diagnosis.json', '/../build-report.json'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_root_option_does_not_call_build_bundle(self):
        mock_server = MagicMock()
        mock_server.server_address = ('127.0.0.1', 9999)
        mock_server.serve_forever.return_value = None
        with patch.object(serve, 'build_bundle') as mock_bundle, \
             patch.object(serve, 'create_server', return_value=mock_server) as mock_create, \
             patch('builtins.print'):
            serve.main(['--port', '0', '--root', str(self.dist)])
            mock_bundle.assert_not_called()
            mock_create.assert_called_once_with(0, self.data.resolve(), root=self.dist.resolve())

    def test_default_main_calls_build_bundle(self):
        mock_server = MagicMock()
        mock_server.server_address = ('127.0.0.1', 9999)
        mock_server.serve_forever.return_value = None
        with patch.object(serve, 'build_bundle') as mock_bundle, \
             patch.object(serve, 'create_server', return_value=mock_server) as mock_create, \
             patch('os.chdir') as mock_chdir, \
             patch('builtins.print'):
            serve.main(['--port', '0'])
            mock_bundle.assert_called_once()
            mock_chdir.assert_called_once_with(serve.ROOT)
            mock_create.assert_called_once_with(0, serve.DATA, root=serve.ROOT)

    def test_parse_args_root(self):
        override_data = Path(self.tmp.name) / 'override.json'
        override_data.touch()

        # 默认取 <root>/data/diagnosis.json
        args = serve.parse_args(['--port', '1234', '--root', str(self.dist)])
        self.assertEqual(args.port, 1234)
        self.assertEqual(args.root, self.dist.resolve())
        self.assertEqual(args.data, self.data.resolve())

        # --data 可覆盖
        args_with_data = serve.parse_args(['--root', str(self.dist), '--data', str(override_data)])
        self.assertEqual(args_with_data.root, self.dist.resolve())
        self.assertEqual(args_with_data.data, override_data.resolve())


if __name__ == '__main__':
    unittest.main()
