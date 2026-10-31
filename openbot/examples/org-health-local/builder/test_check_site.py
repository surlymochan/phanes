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

    def refresh_report(self, dist: Path, *extra: str) -> None:
        """重算 build-report.json 中已登记文件的 sha256，并把 extra 追加登记。"""
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        for relative in extra:
            report["files"][relative] = {"sha256": ""}
        for name in report["files"]:
            report["files"][name] = {"sha256": hashlib.sha256((dist / name).read_bytes()).hexdigest()}
        (dist / "build-report.json").write_text(json.dumps(report), encoding="utf-8")

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

    def test_local_reference_with_query_and_encoded_name_passes(self) -> None:
        """带查询串与 URL 编码文件名的本地引用必须落到真实文件（check_site.py:114-128）。"""
        dist = self.make_dist()
        (dist / "app bundle.js").write_text("console.log('encoded');", encoding="utf-8")
        (dist / "组织.js").write_text("console.log('cjk');", encoding="utf-8")
        (dist / "index.html").write_text(
            '<link rel="stylesheet" href="./styles.css?v=20240101">\n'
            '<script src="./app%20bundle.js?v=1"></script>\n'
            '<script src="./%E7%BB%84%E7%BB%87.js?v=2"></script>',
            encoding="utf-8",
        )
        self.refresh_report(dist, "app bundle.js", "组织.js")
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("通过：index.html 引用的本地文件 ./styles.css 存在", result.stdout)
        self.assertIn("通过：index.html 引用的本地文件 ./app%20bundle.js 存在", result.stdout)
        self.assertIn("通过：index.html 引用的本地文件 ./%E7%BB%84%E7%BB%87.js 存在", result.stdout)
        self.assertNotIn("未在 build-report 中登记", result.stdout)
        self.assertIn("站点检查通过", result.stdout)

    def test_missing_local_reference_with_query_fails(self) -> None:
        """带查询串/编码名的缺失引用仍必须失败（check_site.py:125-126）。"""
        dist = self.make_dist()
        (dist / "index.html").write_text(
            '<script src="./missing.js?v=1"></script>\n'
            '<script src="./missing%20bundle.js?v=2"></script>',
            encoding="utf-8",
        )
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：index.html 引用的本地文件 ./missing.js 不存在", result.stdout)
        self.assertIn("失败：index.html 引用的本地文件 ./missing%20bundle.js 不存在", result.stdout)
        self.assertIn("站点检查失败：2 项", result.stdout)

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
