"""组织诊断端到端冒烟测试：验证三角色流水线与静态站点服务。"""
import http.client
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "run_e2e.py"
VALIDATOR = ROOT / "validate.py"

sys.path.insert(0, str(ROOT))

import serve  # noqa: E402  (导入示例目录内的服务模块)


class E2ESmokeTest(unittest.TestCase):
    """用一条命令验证 Boss、DA、Builder 的最小可用流水线。"""

    def test_pipeline_outputs_validate_and_serve(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            workdir = Path(temporary)
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--workdir", str(workdir)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            brief = workdir / "brief.json"
            diagnosis = workdir / "diagnosis.json"
            dist = workdir / "dist"
            for path in (brief, diagnosis, dist / "index.html"):
                with self.subTest(path=path):
                    self.assertTrue(path.is_file(), path)

            validation = subprocess.run(
                [sys.executable, str(VALIDATOR), str(diagnosis)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

            server = serve.create_server(port=0, root=dist)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)

            port = server.server_address[1]
            for path in ("/", "/data/diagnosis.json"):
                with self.subTest(path=path):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.request("GET", path)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertTrue(response.read())
                    finally:
                        connection.close()


if __name__ == "__main__":
    unittest.main()
