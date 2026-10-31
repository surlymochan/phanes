#!/usr/bin/env python3
"""Serve the static diagnostic site on IPv4 loopback only."""
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
import subprocess
import argparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'diagnosis.json'
ALLOWED_PATHS = ('/', '/index.html', '/app.bundle.js', '/styles.css', '/data/diagnosis.json')

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
        if self.path.split('?', 1)[0] not in ALLOWED_PATHS:
            self.send_error(404)
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--root', type=Path, default=None)
    parser.add_argument('--data', type=Path, default=None)
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

def create_server(port=8765, data=None, root=None):
    """创建只监听 loopback 的服务；调用方决定何时 serve_forever。"""
    root_path = Path(root).resolve() if root is not None else ROOT
    data_path = Path(data).resolve() if data is not None else (root_path / 'data' / 'diagnosis.json')
    handler = partial(LocalOnlyHandler, data=data_path, directory=root_path)
    return ThreadingHTTPServer(('127.0.0.1', port), handler)

def build_bundle():
    """编译浏览器包到示例目录。"""
    subprocess.run(['bun', 'build', 'app.js', '--target=browser', '--outfile=app.bundle.js'],
                   check=True, cwd=ROOT)

def main(argv=None):
    # 保持原有行为：相对 --data 与 bundle 输出都以示例目录为基准；指定 --root 时不再构建且直接服务该目录。
    args = parse_args(argv)
    if args.root is None:
        os.chdir(ROOT)
        build_bundle()
        server = create_server(args.port, args.data, root=ROOT)
    else:
        server = create_server(args.port, args.data, root=args.root)
    print(f'http://127.0.0.1:{server.server_address[1]}', flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
