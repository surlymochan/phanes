#!/usr/bin/env python3
"""Serve the static diagnostic site on IPv4 loopback only."""
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
import socket
import subprocess
import argparse
import sys

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'diagnosis.json'
ALLOWED_PATHS = ('/', '/index.html', '/app.bundle.js', '/styles.css', '/data/diagnosis.json')
DEFAULT_PORT = 8765

class PortInUseError(RuntimeError):
    """端口已被占用：明确报错并退出，不静默改用其他端口。"""

class LocalOnlyHandler(SimpleHTTPRequestHandler):
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
        return super().send_head()

    def translate_path(self, path):
        if path.split('?', 1)[0] == '/data/diagnosis.json':
            return str(self.data_file)
        return super().translate_path(path)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, format, *args):
        # Keep requests local and avoid writing request details to a log.
        pass

def parse_args(argv=None):
    """解析命令行参数，--data 指向服务使用的诊断数据文件，--root 指向静态站点根目录。"""
    parser = argparse.ArgumentParser(
        description="Serve the static diagnostic site on IPv4 loopback only.",
    )
    parser.add_argument(
        '--port',
        type=int,
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
    if not 0 <= args.port <= 65535:
        parser.error(f'--port 需要在 0-65535 之间，收到 {args.port}')
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
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
    except OSError as cause:
        print(f'错误: 端口 {args.port} 无法绑定（{cause}）；请先停止占用进程，或用 --port 指定其他端口',
              file=sys.stderr, flush=True)
        return 1
    print_preview(server, *resolve_paths(root_path, args.data))
    server.serve_forever()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
