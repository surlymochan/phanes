"""End-to-end smoke test for the local organization health pipeline."""
import http.client
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import serve  # noqa: E402


class E2ESmokeTest(unittest.TestCase):
    def test_pipeline_artifacts_validate_and_serve(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            workdir = temp_root / 'work'
            bundle = temp_root / 'fake.bundle.js'
            bundle.write_text('// smoke test bundle\n', encoding='utf-8')

            result = subprocess.run(
                [sys.executable, str(ROOT / 'run_e2e.py'), '--workdir', str(workdir),
                 '--bundle', str(bundle)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            brief = workdir / 'brief.json'
            diagnosis = workdir / 'diagnosis.json'
            dist = workdir / 'dist'
            for path in (brief, diagnosis, dist / 'index.html'):
                with self.subTest(path=path):
                    self.assertTrue(path.is_file(), 'missing {}'.format(path))

            validation = subprocess.run(
                [sys.executable, str(ROOT / 'validate.py'), str(diagnosis)],
                capture_output=True, text=True,
            )
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

            server = serve.create_server(port=0, root=dist)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                for path in ('/', '/data/diagnosis.json'):
                    with self.subTest(request_path=path):
                        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                        try:
                            connection.request('GET', path)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 200)
                            response.read()
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), 'server thread did not stop')


if __name__ == '__main__':
    unittest.main()
