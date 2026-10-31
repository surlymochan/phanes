"""check_site.py 的静态站点验收测试。"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "builder" / "check_site.py"
SOURCE_DATA = ROOT / "data" / "diagnosis.json"


class CheckSiteTest(unittest.TestCase):
    def make_dist(self) -> Path:
        folder = Path(self.tmp.name) / "dist"
        (folder / "data").mkdir(parents=True)
        (folder / "index.html").write_text('<link rel="stylesheet" href="./styles.css">', encoding="utf-8")
        (folder / "app.bundle.js").write_text("console.log('fixture');", encoding="utf-8")
        (folder / "styles.css").write_text("body { color: #333; }", encoding="utf-8")
        shutil.copyfile(SOURCE_DATA, folder / "data" / "diagnosis.json")
        hashes = {}
        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json"):
            hashes[relative] = hashlib.sha256((folder / relative).read_bytes()).hexdigest()
        (folder / "build-report.json").write_text(
            json.dumps({"files": {name: {"sha256": digest} for name, digest in hashes.items()}}),
            encoding="utf-8",
        )
        return folder

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_checker(self, dist: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CHECKER), str(dist)], capture_output=True, text=True
        )

    def test_valid_dist_passes(self) -> None:
        result = self.run_checker(self.make_dist())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("站点检查通过", result.stdout)

    def test_unregistered_file_warns_without_failing(self) -> None:
        dist = self.make_dist()
        (dist / "debug.log").write_text("fixture", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("警告：debug.log 未在 build-report 中登记", result.stdout)

    def test_missing_file_fails(self) -> None:
        dist = self.make_dist()
        (dist / "styles.css").unlink()
        self.assertNotEqual(self.run_checker(dist).returncode, 0)

    def test_changed_data_fails_validation(self) -> None:
        dist = self.make_dist()
        data = json.loads((dist / "data/diagnosis.json").read_text(encoding="utf-8"))
        data["scope"]["schema版本"] = 3
        (dist / "data/diagnosis.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertNotEqual(self.run_checker(dist).returncode, 0)

    def test_changed_hash_fails(self) -> None:
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        report["files"]["styles.css"]["sha256"] = "0" * 64
        (dist / "build-report.json").write_text(json.dumps(report), encoding="utf-8")
        self.assertNotEqual(self.run_checker(dist).returncode, 0)

    def test_corrupted_build_report_json_fails(self) -> None:
        """损坏的 JSON 报告必须让检查失败，失败信息能定位到 build-report.json。"""
        dist = self.make_dist()
        (dist / "build-report.json").write_text("{not json", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-report.json", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_non_string_hash_value_fails(self) -> None:
        """摘要值不是字符串时 report_hashes 报错，检查失败并指向 build-report.json。"""
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        report["files"]["styles.css"]["sha256"] = 12345
        (dist / "build-report.json").write_text(json.dumps(report), encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-report.json", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_non_hex_hash_value_fails(self) -> None:
        """摘要不是 64 位十六进制时检查失败，并指出缺失的有效 sha256。"""
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        report["files"]["styles.css"]["sha256"] = "not-a-sha256"
        (dist / "build-report.json").write_text(json.dumps(report), encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-report.json", result.stdout)
        self.assertIn("styles.css", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_external_resource_fails(self) -> None:
        dist = self.make_dist()
        (dist / "index.html").write_text(
            '<script src="https://example.invalid/app.js"></script>', encoding="utf-8"
        )
        self.assertNotEqual(self.run_checker(dist).returncode, 0)

    def test_missing_referenced_file_fails(self) -> None:
        dist = self.make_dist()
        (dist / "index.html").write_text(
            '<script src="./missing.js"></script>', encoding="utf-8"
        )
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.js 不存在", result.stdout)

    def test_build_site_manifest_passes(self) -> None:
        """build_site.py 写出的 文件清单 报告必须能被 check_site.py 接受（Builder→检查 契约）。"""
        bundle = Path(self.tmp.name) / "app.bundle.js"
        bundle.write_text("console.log('fixture');", encoding="utf-8")
        dist = Path(self.tmp.name) / "built"
        build = subprocess.run(
            [sys.executable, str(ROOT / "builder" / "build_site.py"), "--data", str(SOURCE_DATA),
             "--out", str(dist), "--bundle", str(bundle)],
            capture_output=True, text=True,
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        report["文件清单"][0]["sha256"] = "0" * 64
        (dist / "build-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        self.assertNotEqual(self.run_checker(dist).returncode, 0)


if __name__ == "__main__":
    unittest.main()
