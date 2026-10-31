#!/usr/bin/env python3
"""Serve the static diagnostic site on IPv4 loopback only."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
import subprocess
import argparse

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
DATA = ROOT / 'data' / 'diagnosis.json'

class LocalOnlyHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        port = self.server.server_address[1]
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            self.send_error(403)
            return None
        if self.path.split('?', 1)[0] not in ('/', '/index.html', '/app.bundle.js', '/styles.css', '/data/diagnosis.json'):
            self.send_error(404)
            return None
        return super().send_head()

    def translate_path(self, path):
        if path.split('?', 1)[0] == '/data/diagnosis.json':
            return str(DATA)
        return super().translate_path(path)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, format, *args):
        # Keep requests local and avoid writing request details to a log.
        pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data', type=Path, default=DATA)
    args = parser.parse_args()
    DATA = args.data.resolve()
    subprocess.run(['bun', 'build', 'app.js', '--target=browser', '--outfile=app.bundle.js'], check=True)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), LocalOnlyHandler)
    print(f'http://127.0.0.1:{server.server_address[1]}', flush=True)
    server.serve_forever()
