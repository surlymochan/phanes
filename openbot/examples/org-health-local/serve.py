#!/usr/bin/env python3
"""Serve the static diagnostic site on IPv4 loopback only."""
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
import socket
import subprocess
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'diagnosis.json'
ALLOWED_PATHS = ('/', '/index.html', '/app.bundle.js', '/styles.css', '/data/diagnosis.json', '/api/diagnosis')
DEFAULT_PORT = 8765
JSON_CONTENT_TYPE = 'application/json'


def parse_port(value):
    """解析并校验监听端口，返回 argparse 可读的范围错误。"""
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f'端口需要在 0-65535 之间，收到 {port}'
        )
    return port

class PortInUseError(RuntimeError):
    """端口已被占用：明确报错并退出，不静默改用其他端口。"""

class LocalOnlyHandler(SimpleHTTPRequestHandler):
    """只允许本地 loopback 访问与白名单路径；请求不存在路径返回稳定 404 且不列出目录内容。"""

    def handle_one_request(self):
        """客户端提前断开时结束当前请求，不让 ThreadingHTTPServer 线程报错退出。"""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def finish(self):
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_error(self, code, message=None, explain=None):
        """返回稳定的错误正文，避免未知路径出现空响应。"""
        if code == 404 and message is None:
            body = b'Not Found\n'
            self.send_response(404, 'Not Found')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if getattr(self, 'command', 'GET') != 'HEAD':
                self.wfile.write(body)
            return
        if code == 501:
            body = b'Method Not Allowed\n'
            self.send_response(405, 'Method Not Allowed')
            self.send_header('Allow', 'GET, HEAD')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
            return
        super().send_error(code, message, explain)

    def __init__(self, *args, data=DATA, **kwargs):
        # 请求在 super().__init__() 内即被处理，诊断数据路径必须先就位。
        self.data_file = Path(data)
        super().__init__(*args, **kwargs)

    def send_head(self):
        port = self.server.server_address[1]
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            self.send_error(403)
            return None
        clean_path = self.path.split('?', 1)[0]
        if clean_path not in ALLOWED_PATHS:
            self.send_error(404)
            return None
        if clean_path == '/data/diagnosis.json' and not self.data_file.is_file():
            self.send_error(404, 'File not found')
            return None
        try:
            return super().send_head()
        except PermissionError:
            # A broken/unreadable static tree must still produce an HTTP
            # response; it must not abort the request handler before the API
            # can serve its independent diagnosis snapshot.
            self.send_error(403)
            return None
        except OSError:
            # The root may disappear while a request is in flight. Treat that
            # as a stable static 404 instead of leaking a handler traceback.
            self.send_error(404)
            return None

    def _serve_diagnosis_api(self, head=False):
        """返回与静态页面使用的 diagnosis.json 相同字段的 JSON 响应。"""
        payload, status = self._read_diagnosis()
        self._send_json(payload, status, head=head)

    def _read_diagnosis_bytes(self):
        """读取并校验完整快照，避免把写入中的文件发给客户端。"""
        if not self.data_file.is_file():
            return None, {'error': 'diagnosis data not found'}, 404
        for attempt in range(3):
            try:
                # Open before checking the size: an atomic os.replace() may swap
                # the pathname while this request is in flight, but the open
                # descriptor must continue reading one complete inode.
                with self.data_file.open('rb') as stream:
                    expected_size = os.fstat(stream.fileno()).st_size
                    body = stream.read()
                if len(body) != expected_size:
                    raise OSError('diagnosis file changed during read')
                payload = json.loads(body.decode('utf-8'))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                if attempt < 2:
                    time.sleep(0.001)
                    continue
                return None, {'error': 'diagnosis data is unavailable'}, 503
            if not isinstance(payload, dict):
                return None, {'error': 'diagnosis data must be a JSON object'}, 422
            return body, payload, 200
        return None, {'error': 'diagnosis data is unavailable'}, 503

    def _read_diagnosis(self):
        """读取诊断快照，确保 API 错误是稳定 JSON 而不是请求线程 traceback。"""
        _, payload, status = self._read_diagnosis_bytes()
        return payload, status

    def _serve_diagnosis_file(self, head=False):
        body, payload, status = self._read_diagnosis_bytes()
        if status != 200:
            if status == 404:
                self.send_error(404, 'File not found')
                return
            self._send_json(payload, status, head=head)
            return
        self.send_response(200)
        self.send_header('Content-Type', JSON_CONTENT_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_json(self, payload, status, head=False):
        """发送 API JSON；HEAD 与 GET 保持状态和头部契约但不发送正文。"""
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', JSON_CONTENT_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_GET(self):
        clean_path = self.path.split('?', 1)[0]
        if clean_path == '/api/diagnosis':
            if not self._check_request():
                return
            self._serve_diagnosis_api()
            return
        if clean_path == '/data/diagnosis.json':
            if not self._check_request():
                return
            self._serve_diagnosis_file()
            return
        super().do_GET()

    def do_HEAD(self):
        clean_path = self.path.split('?', 1)[0]
        if clean_path == '/api/diagnosis':
            if not self._check_request():
                return
            self._serve_diagnosis_api(head=True)
            return
        if clean_path == '/data/diagnosis.json':
            if not self._check_request():
                return
            self._serve_diagnosis_file(head=True)
            return
        super().do_HEAD()

    def _check_request(self):
        port = self.server.server_address[1]
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            self.send_error(403)
            return False
        return True

    def translate_path(self, path):
        clean_path = path.split('?', 1)[0]
        # Keep the path guard effective even if this method is called directly;
        # SimpleHTTPRequestHandler otherwise resolves ../ against its directory.
        if clean_path not in ALLOWED_PATHS:
            return str(Path(self.directory) / '__forbidden_path__')
        if clean_path == '/data/diagnosis.json':
            return str(self.data_file)
        return super().translate_path(path)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, format, *args):
        # Keep requests local and avoid writing request details (or disk paths) to a log.
        pass

def parse_args(argv=None):
    """解析命令行参数，--data 指向服务使用的诊断数据文件，--root 指向静态站点根目录。"""
    parser = argparse.ArgumentParser(
        description="Serve the static diagnostic site on IPv4 loopback only.",
    )
    parser.add_argument(
        '--port',
        type=parse_port,
        default=DEFAULT_PORT,
        help=f'服务监听端口（0-65535，默认 {DEFAULT_PORT}）',
    )
    parser.add_argument(
        '--root',
        type=Path,
        default=None,
        help='静态站点根目录（默认当前示例目录）',
    )
    parser.add_argument(
        '--data',
        type=Path,
        default=None,
        help='诊断数据 JSON 文件路径（默认使用 root 下的 data/diagnosis.json）',
    )
    args = parser.parse_args(argv)
    if args.root is not None:
        args.root = args.root.resolve()
    if args.data is not None:
        args.data = args.data.resolve()
    elif args.root is not None:
        args.data = (args.root / 'data' / 'diagnosis.json').resolve()
    else:
        args.data = DATA
    return args

def resolve_paths(root=None, data=None):
    """返回 (站点根目录, 诊断数据文件) 的绝对路径，与 --root / --data 保持一致。"""
    root_path = Path(root).resolve() if root is not None else ROOT
    data_path = Path(data).resolve() if data is not None else (root_path / 'data' / 'diagnosis.json').resolve()
    return root_path, data_path

def ensure_port_available(port):
    """端口预检：端口被占用时抛 PortInUseError，绝不静默改用其他端口。"""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as cause:
        # 探测 socket 都创建不出来（文件句柄耗尽等）：同样给出可读中文提示，不抛裸 traceback。
        raise PortInUseError(
            f'无法检测端口 {port} 是否可用（{cause}）；请稍后重试，或用 --port 指定其他端口') from cause
    with probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as cause:
            raise PortInUseError(f'端口 {port} 已被占用，请先停止占用进程，或用 --port 指定其他端口') from cause

def create_server(port=DEFAULT_PORT, data=None, root=None):
    """创建只监听 loopback 的服务；调用方决定何时 serve_forever。"""
    root_path, data_path = resolve_paths(root, data)
    handler = partial(LocalOnlyHandler, data=data_path, directory=root_path)
    return ThreadingHTTPServer(('127.0.0.1', port), handler)

def build_bundle():
    """编译浏览器包到示例目录。"""
    subprocess.run(['bun', 'build', 'app.js', '--target=browser', '--outfile=app.bundle.js'],
                   check=True, cwd=ROOT)

def print_preview(server, root_path, data_path):
    """打印可点击的预览地址，以及当前服务的根目录、数据文件与路径白名单。"""
    port = server.server_address[1]
    print(f'预览地址: http://127.0.0.1:{port}/', flush=True)
    print(f'站点根目录: {root_path}', flush=True)
    print(f'诊断数据文件: {data_path}', flush=True)
    print(f'白名单路径: {", ".join(ALLOWED_PATHS)}', flush=True)

def main(argv=None):
    # 保持原有行为：相对 --data 与 bundle 输出都以示例目录为基准；指定 --root 时不再构建且直接服务该目录。
    args = parse_args(argv)
    try:
        ensure_port_available(args.port)
    except PortInUseError as cause:
        print(f'错误: {cause}', file=sys.stderr, flush=True)
        return 1
    if args.root is None:
        os.chdir(ROOT)
        try:
            build_bundle()
        except FileNotFoundError:
            # 未安装 bun：给出可执行提示并以非 0 退出，不抛 traceback。
            print('错误: 未找到 bun 命令，请先安装 bun 或使用 --root 指向已构建好的静态站点',
                  file=sys.stderr, flush=True)
            return 1
        root_path = ROOT
    else:
        root_path = args.root
        if not root_path.is_dir():
            print(f'错误: 静态站点根目录不存在: {root_path}', file=sys.stderr, flush=True)
            return 1
    root_path, data_path = resolve_paths(root_path, args.data)
    if not data_path.is_file():
        print(f'错误: 诊断数据文件不存在: {data_path}', file=sys.stderr, flush=True)
        return 1
    try:
        server = create_server(args.port, args.data, root=root_path)
    except PortInUseError as cause:
        print(f'错误: {cause}', file=sys.stderr, flush=True)
        return 1
    except OSError as cause:
        print(f'错误: 端口 {args.port} 无法绑定（{cause}）；请先停止占用进程，或用 --port 指定其他端口',
              file=sys.stderr, flush=True)
        return 1
    print_preview(server, *resolve_paths(root_path, args.data))
    server.serve_forever()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
