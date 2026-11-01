"""本机诊断服务的边界单测：只允许 loopback Host 与白名单路径，禁止目录列表。

仅使用标准库且不调用 bun：在临时端口起服务后逐条检查响应状态码。
"""
import http.client
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
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

    def request_with_type(self, path, method='GET', host=None, port=None):
        """发起一次请求并返回 (状态码, Content-Type, body)；用于比较 GET 与 HEAD 的响应头。"""
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=5)
        try:
            headers = {} if host is None else {'Host': host}
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            connection.close()

    def raw_head_response(self, path, port=None):
        """用裸 socket 发一次 HEAD，返回响应原始字节；用于确认响应头之后没有 body 字节。"""
        target_port = port or self.port
        with socket.create_connection(('127.0.0.1', target_port), timeout=5) as sock:
            sock.sendall(
                f'HEAD {path} HTTP/1.0\r\nHost: 127.0.0.1:{target_port}\r\nConnection: close\r\n\r\n'.encode()
            )
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b''.join(chunks)

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

    def test_non_get_head_methods_return_stable_405_and_allow(self):
        for method in ('POST', 'PUT', 'OPTIONS', 'PATCH'):
            with self.subTest(method=method):
                status, content_type, body = self.request_with_type('/', method=method)
                self.assertEqual(status, 405)
                self.assertEqual(content_type, 'text/plain; charset=utf-8')
                self.assertEqual(body, b'Method Not Allowed\n')

                connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
                try:
                    connection.request(method, '/')
                    response = connection.getresponse()
                    self.assertEqual(response.getheader('Allow'), 'GET, HEAD')
                    response.read()
                finally:
                    connection.close()

    def test_head_matches_get_status_and_content_type(self):
        # HEAD 只回响应头：状态码与 Content-Type 与 GET 一致，body 为空。
        for path in ('/', '/index.html', '/styles.css', '/data/diagnosis.json'):
            with self.subTest(path=path):
                get_status, get_type, get_body = self.request_with_type(path)
                self.assertEqual(get_status, 200)
                self.assertTrue(get_body)
                head_status, head_type, head_body = self.request_with_type(path, method='HEAD')
                self.assertEqual(head_status, get_status)
                self.assertEqual(head_type, get_type)
                self.assertEqual(head_body, b'')
                # HTTP 客户端本来就不读 HEAD 的 body，所以再看原始字节：响应头之后必须没有内容。
                raw_head, _, raw_body = self.raw_head_response(path).partition(b'\r\n\r\n')
                self.assertIn(b'200', raw_head.split(b'\r\n', 1)[0])
                self.assertEqual(raw_body, b'')

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

    def test_nonexistent_path_returns_404_with_nonempty_body(self):
        for path in ('/nonexistent', '/missing.html', '/not_found'):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertTrue(body)
                self.assertGreater(len(body), 0)

    def test_path_traversal_is_not_found(self):
        for path in ('/../serve.py', '/%2e%2e/serve.py', '/..%2fserve.py', '/data/../serve.py'):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(str(serve.ROOT).encode(), body)
                self.assertNotIn(str(self.data).encode(), body)

    def test_translate_path_does_not_resolve_traversal(self):
        handler = object.__new__(serve.LocalOnlyHandler)
        handler.directory = str(serve.ROOT)
        for path in ('/../serve.py', '/%2e%2e/serve.py', '/data/../serve.py'):
            with self.subTest(path=path):
                resolved = Path(handler.translate_path(path))
                self.assertNotEqual(resolved, serve.ROOT / 'serve.py')
                self.assertFalse(resolved.is_file())

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

    def test_query_string_on_allowed_paths_is_served(self):
        # 查询串不参与白名单判定：路径部分仍在 ALLOWED_PATHS 内即可照常服务。
        cases = (
            ('/?x=1', INDEX.read_bytes()),
            ('/index.html?x=1', INDEX.read_bytes()),
            ('/styles.css?x=1', (serve.ROOT / 'styles.css').read_bytes()),
            ('/data/diagnosis.json?x=1', self.data.read_bytes()),
        )
        for path, expected_body in cases:
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_body)

    def test_query_string_on_unknown_paths_is_not_found(self):
        # 白名单外的路径即使带查询串依然 404。
        for path in ('/serve.py?x=1', '/app.js?x=1', '/README.md?x=1',
                     '/data?x=1', '/unknown?x=1'):
            with self.subTest(path=path):
                status, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_query_string_does_not_change_host_check(self):
        # Host 校验先于路径判定，带查询串时结论不变：非法 Host 一律 403。
        for path in ('/?x=1', '/data/diagnosis.json?x=1'):
            for host in ('evil.example', f'127.0.0.1:{self.port + 1}', 'localhost'):
                with self.subTest(path=path, host=host):
                    status, _ = self.request(path, host)
                    self.assertEqual(status, 403)
        # 未知路径 + 非法 Host：Host 校验在前，仍是 403 而不是 404。
        status, _ = self.request('/unknown?x=1', 'evil.example')
        self.assertEqual(status, 403)

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

    def test_diagnosis_api_matches_builder_data_fields(self):
        status, body = self.request('/api/diagnosis')
        self.assertEqual(status, 200)
        expected = json.loads(self.data.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(body), expected)


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
        # SimpleHTTPRequestHandler 在所有 Python 3 版本里都会用目录下的 index.html 回应 "/"。
        status, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.index.read_bytes())

    def test_concurrent_diagnosis_gets_return_consistent_json_and_shutdown_cleanly(self):
        """真实服务线程下并发读取诊断数据时，响应稳定且服务可干净关闭。"""
        server = serve.create_server(0, root=self.dist)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={'poll_interval': 0.05},
            daemon=True,
        )
        thread.start()
        port = server.server_address[1]
        try:
            def get_diagnosis():
                return self.request('/data/diagnosis.json', port=port)

            with ThreadPoolExecutor(max_workers=16) as executor:
                responses = list(executor.map(lambda _: get_diagnosis(), range(64)))

            expected = json.loads(self.data.read_text(encoding='utf-8'))
            self.assertTrue(expected)
            for status, body in responses:
                with self.subTest(status=status):
                    self.assertEqual(status, 200)
                    self.assertTrue(body)
                    self.assertEqual(json.loads(body.decode('utf-8')), expected)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertFalse(thread.is_alive())

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

    def test_root_defaults_data_path_and_serves_that_file(self):
        args = serve.parse_args(['--port', '0', '--root', str(self.dist)])
        self.assertEqual(args.data, self.data.resolve())

        status, body = self.request('/data/diagnosis.json')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data.read_bytes())

    def test_unlisted_files_are_not_found(self):
        for path in ('/build-report.json', '/secret.txt', '/app.js', '/README.md', '/data', '/data/'):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'Directory listing', body)
                self.assertNotIn(b'build-report.json', body)
                self.assertNotIn(b'secret.txt', body)

    def test_dist_nonexistent_paths_return_404_without_directory_listing(self):
        """临时静态根目录下请求不存在路径：返回稳定 404、正文非空且不列出目录内容。"""
        for path in ('/nonexistent', '/missing.html', '/not_found', '/data/missing.json', '/missing_dir/file.txt'):
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertTrue(body)
                self.assertGreater(len(body), 0)
                self.assertNotIn(b'Directory listing', body)
                self.assertNotIn(b'build-report.json', body)
                self.assertNotIn(b'secret.txt', body)
                self.assertNotIn(b'index.html', body)

    def test_dist_server_thread_shuts_down_cleanly_after_nonexistent_path_request(self):
        """请求不存在路径后，服务线程可正常关闭。"""
        server = serve.create_server(0, root=self.dist)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                conn.request('GET', '/missing_target_file')
                response = conn.getresponse()
                body = response.read()
                self.assertEqual(response.status, 404)
                self.assertNotIn(b'Directory listing', body)
                self.assertNotIn(b'build-report.json', body)
                self.assertNotIn(b'secret.txt', body)
            finally:
                conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertFalse(thread.is_alive())

    def test_empty_dist_root_returns_404_without_listing_and_thread_shuts_down(self):
        """临时静态根目录为空时，请求白名单路径亦稳定返回 404、不列出目录内容且服务线程可正常关闭。"""
        empty_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(empty_tmp.cleanup)
        empty_dist = Path(empty_tmp.name) / 'empty_dist'
        empty_dist.mkdir()
        (empty_dist / 'hidden_secret.txt').write_text('hidden-content', encoding='utf-8')

        server = serve.create_server(0, root=empty_dist)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                for path in ('/', '/index.html', '/styles.css', '/data/diagnosis.json', '/nonexistent'):
                    with self.subTest(path=path):
                        conn.request('GET', path)
                        response = conn.getresponse()
                        body = response.read()
                        self.assertEqual(response.status, 404)
                        self.assertNotIn(b'Directory listing', body)
                        self.assertNotIn(b'hidden_secret.txt', body)
                        self.assertNotIn(b'hidden-content', body)
            finally:
                conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertFalse(thread.is_alive())

    def test_dist_query_string_on_allowed_paths_is_served(self):
        cases = (
            ('/?x=1', self.index.read_bytes()),
            ('/index.html?x=1', self.index.read_bytes()),
            ('/data/diagnosis.json?x=1', self.data.read_bytes()),
        )
        for path, expected_body in cases:
            with self.subTest(path=path):
                status, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_body)

    def test_dist_query_string_on_unknown_paths_is_not_found(self):
        for path in ('/build-report.json?x=1', '/secret.txt?x=1', '/app.js?x=1'):
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
                status, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(str(self.dist).encode(), body)
                self.assertNotIn(str(self.tmp.name).encode(), body)

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


class StartupPreviewTests(unittest.TestCase):
    """启动提示：可点击预览地址、站点根目录、诊断数据文件与路径白名单。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / 'dist'
        (self.dist / 'data').mkdir(parents=True)
        (self.dist / 'index.html').write_bytes(b'<!doctype html><title>dist index</title>')
        (self.dist / 'data' / 'diagnosis.json').write_bytes(b'{"source": "dist-diagnosis"}')
        self.ports = []

    def run_main(self, argv):
        """运行 main 并捕获输出；serve_forever 被替换为记录端口后关闭服务。"""
        def fake_serve_forever(server, *args, **kwargs):
            self.ports.append(server.server_address[1])
            server.server_close()

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(serve.ThreadingHTTPServer, 'serve_forever', fake_serve_forever):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = serve.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_startup_prints_preview_address_and_paths(self):
        with patch.object(serve, 'build_bundle'), patch('os.chdir'):
            code, out, err = self.run_main(['--port', '0'])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.ports), 1)
        self.assertGreater(self.ports[0], 0)
        self.assertIn(f'预览地址: http://127.0.0.1:{self.ports[0]}/', out)
        self.assertIn(f'站点根目录: {serve.ROOT}', out)
        self.assertIn(f'诊断数据文件: {serve.DATA}', out)
        for path in serve.ALLOWED_PATHS:
            self.assertIn(path, out)

    def test_dist_startup_prints_dist_root_and_data_file(self):
        override = Path(self.tmp.name) / 'override.json'
        override.write_bytes(b'{"source": "override-data"}')
        code, out, err = self.run_main(['--port', '0', '--root', str(self.dist), '--data', str(override)])
        self.assertEqual(code, 0)
        self.assertIn(f'预览地址: http://127.0.0.1:{self.ports[0]}/', out)
        self.assertIn(f'站点根目录: {self.dist.resolve()}', out)
        self.assertIn(f'诊断数据文件: {override.resolve()}', out)

    def test_dist_startup_defaults_data_file_inside_dist(self):
        code, out, err = self.run_main(['--port', '0', '--root', str(self.dist)])
        self.assertEqual(code, 0)
        self.assertIn(f'站点根目录: {self.dist.resolve()}', out)
        self.assertIn(f'诊断数据文件: {(self.dist / "data" / "diagnosis.json").resolve()}', out)

    def test_default_port_is_8765(self):
        self.assertEqual(serve.parse_args([]).port, 8765)
        self.assertEqual(serve.parse_args(['--port', '1234']).port, 1234)


class PortRangeArgumentTests(unittest.TestCase):
    """--port 边界：负数与超过 65535 的端口由 argparse 以非 0 退出并把范围提示写到 stderr。"""

    def run_cli(self, port):
        """运行 serve.py 命令行，返回 (退出码, stdout, stderr)。"""
        result = subprocess.run(
            [sys.executable, str(serve.ROOT / 'serve.py'), '--port', str(port)],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    def test_negative_port_fails_with_range_hint(self):
        for port in (-1, -65535):
            with self.subTest(port=port):
                code, out, err = self.run_cli(port)
                self.assertNotEqual(code, 0)
                self.assertIn('0-65535', err)
                self.assertIn(str(port), err)
                self.assertNotIn('预览地址', out)

    def test_port_above_maximum_fails_with_range_hint(self):
        for port in (65536, 70000):
            with self.subTest(port=port):
                code, out, err = self.run_cli(port)
                self.assertNotEqual(code, 0)
                self.assertIn('0-65535', err)
                self.assertIn(str(port), err)
                self.assertNotIn('预览地址', out)

    def test_parse_args_reports_range_error_to_stderr(self):
        # argparse 用 SystemExit(非 0) 结束，范围提示只出现在 stderr。
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                serve.parse_args(['--port', '-1'])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertIn('0-65535', stderr.getvalue())

    def test_port_zero_is_accepted_as_dynamic_port(self):
        # 端口 0 是既有约定：由内核分配动态端口，不算非法参数。
        args = serve.parse_args(['--port', '0'])
        self.assertEqual(args.port, 0)
        server = serve.create_server(args.port, args.data, root=args.root)
        self.addCleanup(server.server_close)
        self.assertGreater(server.server_address[1], 0)

    def test_boundary_ports_are_accepted(self):
        self.assertEqual(serve.parse_args(['--port', '0']).port, 0)
        self.assertEqual(serve.parse_args(['--port', '65535']).port, 65535)


class CliContractTests(unittest.TestCase):
    """CLI 契约单测：--help 选项参数说明与未知参数报错。"""

    def run_cli(self, *args):
        """运行 serve.py 命令行，返回 (退出码, stdout, stderr)。"""
        result = subprocess.run(
            [sys.executable, str(serve.ROOT / 'serve.py'), *args],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    def test_help_option_displays_description_and_arguments(self):
        # 运行 python3 serve.py --help 断言退出码为 0，stdout 包含 --port、--root、--data 参数说明与描述
        code, out, err = self.run_cli('--help')
        self.assertEqual(code, 0)
        self.assertIn("Serve the static diagnostic site on IPv4 loopback only.", out)
        self.assertIn('--port', out)
        self.assertIn('--root', out)
        self.assertIn('--data', out)
        self.assertIn('服务监听端口', out)
        self.assertIn('静态站点根目录', out)
        self.assertIn('诊断数据', out)
        self.assertEqual(err, '')

    def test_unknown_argument_fails_with_usage_error(self):
        # 传入未知参数（如 --bogus）断言退出码非 0，stderr 输出用法错误提示
        code, out, err = self.run_cli('--bogus')
        self.assertNotEqual(code, 0)
        self.assertIn('usage:', err.lower())
        self.assertIn('unrecognized arguments: --bogus', err)
        self.assertNotIn('预览地址', out)

    def test_parse_args_help(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as caught:
                serve.parse_args(['--help'])
        self.assertEqual(caught.exception.code, 0)
        out = stdout.getvalue()
        self.assertIn("Serve the static diagnostic site on IPv4 loopback only.", out)
        self.assertIn('--port', out)
        self.assertIn('--root', out)
        self.assertIn('--data', out)

    def test_parse_args_unknown_argument_reports_usage_error(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                serve.parse_args(['--bogus'])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertIn('unrecognized arguments: --bogus', stderr.getvalue())


class PortInUseTests(unittest.TestCase):
    """端口被占用时明确报错并以非 0 退出，不静默改用其他端口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / 'dist'
        (self.dist / 'data').mkdir(parents=True)
        (self.dist / 'index.html').write_bytes(b'<!doctype html><title>dist index</title>')
        self.dist_data = self.dist / 'data' / 'diagnosis.json'
        self.dist_data.write_bytes(b'{"source": "dist-diagnosis"}')
        # 先绑定的 socket 占用端口，模拟端口冲突。
        self.occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self.occupied.close)
        self.occupied.bind(('127.0.0.1', 0))
        self.occupied.listen(1)
        self.port = self.occupied.getsockname()[1]

    def test_ensure_port_available_raises_for_occupied_port(self):
        with self.assertRaises(serve.PortInUseError) as caught:
            serve.ensure_port_available(self.port)
        self.assertIn(str(self.port), str(caught.exception))

    def test_ensure_port_available_accepts_free_port(self):
        serve.ensure_port_available(0)

    def test_main_exits_nonzero_when_port_is_occupied(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(serve, 'create_server') as mock_create:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = serve.main(['--port', str(self.port), '--root', str(self.dist)])
        self.assertNotEqual(code, 0)
        self.assertIn(f'端口 {self.port} 已被占用', stderr.getvalue())
        self.assertNotIn('预览地址', stdout.getvalue())
        mock_create.assert_not_called()


class MissingBunTests(unittest.TestCase):
    """未安装 bun 时：打印友好提示并以退出码 1 结束，不出现 traceback。"""

    def run_main(self, argv):
        """运行 main 并捕获输出，返回 (退出码, stdout, stderr)。"""
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = serve.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_missing_bun_exits_with_hint(self):
        # build_bundle 找不到 bun 命令时抛 FileNotFoundError；main 必须捕获并给出提示。
        with patch.object(serve, 'build_bundle',
                          side_effect=FileNotFoundError(2, 'No such file or directory', 'bun')), \
             patch('os.chdir'), \
             patch.object(serve, 'create_server') as mock_create:
            code, out, err = self.run_main(['--port', '0'])
        self.assertEqual(code, 1)
        self.assertIn('未找到 bun 命令', err)
        self.assertIn('--root', err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('Traceback', out)
        self.assertNotIn('预览地址', out)
        mock_create.assert_not_called()

    def test_cli_without_bun_exits_with_hint(self):
        # 端到端：PATH 指向空目录，命令行找不到 bun，退出码 1 且 stderr 只有提示没有 traceback。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        empty_path = Path(tmp.name)
        env = {'PATH': str(empty_path), 'HOME': str(empty_path)}
        result = subprocess.run(
            [sys.executable, str(serve.ROOT / 'serve.py'), '--port', '0'],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('未找到 bun 命令', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('预览地址', result.stdout)


class MissingDiagnosisDataTests(unittest.TestCase):
    """--root 指向存在但缺少 data/diagnosis.json 的静态目录时：

    - 服务初始化（main / CLI）给出明确错误并以非 0 退出，不抛 traceback；
    - 运行中请求缺失的诊断数据时返回明确 404 错误，不崩溃或抛 traceback；
    - 通过 --data 显式指定合法数据文件时仍可正常启动。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dist = Path(self.tmp.name) / 'empty_dist'
        self.dist.mkdir()
        self.index = self.dist / 'index.html'
        self.index.write_bytes(b'<!doctype html><title>empty dist index</title>')
        self.expected_data = (self.dist / 'data' / 'diagnosis.json').resolve()

    def run_main(self, argv):
        """运行 main 并捕获输出，返回 (退出码, stdout, stderr)。"""
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = serve.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def request_with_type(self, path, method='GET', port=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.getheader('Content-Type'), response.read()
        finally:
            connection.close()

    def request(self, path, port=None):
        status, _, body = self.request_with_type(path, port=port)
        return status, body

    def test_main_exits_nonzero_when_root_missing_diagnosis_data(self):
        with patch.object(serve, 'create_server') as mock_create:
            code, out, err = self.run_main(['--port', '0', '--root', str(self.dist)])
        self.assertEqual(code, 1)
        self.assertIn('错误: 诊断数据文件不存在', err)
        self.assertIn(str(self.expected_data), err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('Traceback', out)
        self.assertNotIn('预览地址', out)
        mock_create.assert_not_called()

    def test_cli_exits_nonzero_when_root_missing_diagnosis_data(self):
        result = subprocess.run(
            [sys.executable, str(serve.ROOT / 'serve.py'), '--port', '0', '--root', str(self.dist)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('错误: 诊断数据文件不存在', result.stderr)
        self.assertIn(str(self.expected_data), result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('预览地址', result.stdout)

    def test_main_exits_nonzero_when_root_directory_does_not_exist(self):
        nonexistent = Path(self.tmp.name) / 'nonexistent'
        with patch.object(serve, 'create_server') as mock_create:
            code, out, err = self.run_main(['--port', '0', '--root', str(nonexistent)])
        self.assertEqual(code, 1)
        self.assertIn('错误: 静态站点根目录不存在', err)
        self.assertIn(str(nonexistent.resolve()), err)
        self.assertNotIn('Traceback', err)
        self.assertNotIn('预览地址', out)
        mock_create.assert_not_called()

    def test_request_missing_diagnosis_file_returns_404_not_traceback(self):
        args = serve.parse_args(['--port', '0', '--root', str(self.dist)])
        server = serve.create_server(args.port, args.data, root=args.root)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        port = server.server_address[1]
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            conn.request('GET', '/data/diagnosis.json')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)
            body = resp.read()
            self.assertTrue(body)
            self.assertIn(b'404', body)

            conn.request('HEAD', '/data/diagnosis.json')
            head_resp = conn.getresponse()
            self.assertEqual(head_resp.status, 404)
            self.assertEqual(head_resp.read(), b'')

            # 其他已存在的静态文件仍正常服务
            conn.request('GET', '/index.html')
            index_resp = conn.getresponse()
            self.assertEqual(index_resp.status, 200)
            self.assertEqual(index_resp.read(), self.index.read_bytes())
        finally:
            conn.close()

    def test_diagnosis_api_missing_file_returns_stable_404_json(self):
        args = serve.parse_args(['--port', '0', '--root', str(self.dist)])
        server = serve.create_server(args.port, args.data, root=args.root)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        try:
            conn.request('GET', '/api/diagnosis')
            response = conn.getresponse()
            self.assertEqual(response.status, 404)
            self.assertEqual(response.getheader('Content-Type'), 'application/json; charset=utf-8')
            self.assertEqual(json.loads(response.read()), {'error': 'diagnosis data not found'})
        finally:
            conn.close()

    def test_missing_static_diagnosis_has_explicit_error_content_type(self):
        args = serve.parse_args(['--port', '0', '--root', str(self.dist)])
        server = serve.create_server(args.port, args.data, root=args.root)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        status, content_type, body = self.request_with_type('/data/diagnosis.json', port=server.server_address[1])
        self.assertEqual(status, 404)
        self.assertEqual(content_type, 'text/html;charset=utf-8')
        self.assertIn(b'404', body)

    def test_client_disconnect_does_not_prevent_followup_request(self):
        server = serve.create_server(0, root=self.dist)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            sock = socket.create_connection(('127.0.0.1', port), timeout=5)
            sock.sendall(f'GET /data/diagnosis.json HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
            sock.close()

            status, body = self.request('/index.html', port=port)
            self.assertEqual(status, 200)
            self.assertEqual(body, self.index.read_bytes())
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertFalse(thread.is_alive())

    def test_root_missing_diagnosis_data_succeeds_with_explicit_data_option(self):
        override_data = Path(self.tmp.name) / 'custom.json'
        override_data.write_bytes(b'{"source": "custom"}')

        mock_server = MagicMock()
        mock_server.server_address = ('127.0.0.1', 8888)
        mock_server.serve_forever.return_value = None
        with patch.object(serve, 'create_server', return_value=mock_server) as mock_create, \
             patch('builtins.print'):
            code = serve.main(['--port', '0', '--root', str(self.dist), '--data', str(override_data)])
        self.assertEqual(code, 0)
        mock_create.assert_called_once_with(0, override_data.resolve(), root=self.dist.resolve())


if __name__ == '__main__':
    unittest.main()
