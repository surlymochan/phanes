"""check_site.py 的静态站点验收测试。"""
from __future__ import annotations

import hashlib
import json
import os
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
    def make_dist(self, folder_name: str = "dist") -> Path:
        folder = Path(self.tmp.name) / folder_name
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

    def run_checker(self, *args: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(CHECKER)] + [str(arg) for arg in args]
        return subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd
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

    def test_file_dist_fails_with_path_error(self) -> None:
        dist = Path(self.tmp.name) / "dist-file"
        dist.write_text("not a directory", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"失败：站点路径不存在或不是目录：{dist}", result.stdout)

    def test_empty_file_fails(self) -> None:
        dist = self.make_dist()
        (dist / "styles.css").write_text("", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("styles.css 为空", result.stdout)

    def test_empty_dist_fails_with_readable_reason(self) -> None:
        """空 dist 目录必须以非 0 退出，并给出可读的失败原因（缺 index.html 等），不抛 Traceback。"""
        dist = Path(self.tmp.name) / "empty-dist"
        dist.mkdir()
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        message = result.stdout + result.stderr
        self.assertIn("失败：index.html 不存在", message)
        self.assertIn("站点检查失败", message)
        self.assertNotIn("Traceback", result.stderr)

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

    def test_truncated_build_report_json_fails(self) -> None:
        """截断的 JSON 报告必须让检查失败，失败信息能定位到 build-report.json。"""
        dist = self.make_dist()
        (dist / "build-report.json").write_text("{not json", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-report.json", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_array_build_report_json_fails(self) -> None:
        """顶层为数组的 JSON 报告必须因结构错误而失败。"""
        dist = self.make_dist()
        (dist / "build-report.json").write_text("[]", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-report.json", result.stdout)
        self.assertIn("必须是对象", result.stdout)
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

    def test_external_resource_in_stylesheet_fails(self) -> None:
        dist = self.make_dist()
        (dist / "styles.css").write_text(
            '@import url("https://fonts.googleapis.com/css?family=Open+Sans");',
            encoding="utf-8",
        )
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("styles.css 引用了外部 http(s) 资源", result.stdout)
        self.assertIn("站点检查失败", result.stdout)

    def test_missing_referenced_file_fails(self) -> None:
        dist = self.make_dist()
        (dist / "index.html").write_text(
            '<script src="./missing.js"></script>', encoding="utf-8"
        )
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.js 不存在", result.stdout)

    def test_absolute_and_protocol_relative_references_are_skipped(self) -> None:
        """绝对路径与协议相对地址跳过本地文件检查，相对路径缺失仍失败。"""
        dist = self.make_dist()
        (dist / "index.html").write_text(
            '<script src="/app.bundle.js"></script>\n'
            '<script src="//cdn.example.invalid/x.js"></script>',
            encoding="utf-8",
        )
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for relative in (
            "index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "build-report.json"
        ):
            self.assertIn(f"通过：{relative} 存在且非空", result.stdout)
        self.assertIn("通过：data/diagnosis.json 通过 validate.py", result.stdout)
        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json"):
            self.assertIn(f"通过：{relative} 的 sha256 一致", result.stdout)
        self.assertNotIn("引用的本地文件 /app.bundle.js 不存在", result.stdout)
        self.assertNotIn("引用的本地文件 //cdn.example.invalid/x.js 不存在", result.stdout)

        (dist / "index.html").write_text(
            '<script src="/app.bundle.js"></script>\n'
            '<script src="//cdn.example.invalid/x.js"></script>\n'
            '<script src="./missing.js"></script>',
            encoding="utf-8",
        )
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：index.html 引用的本地文件 ./missing.js 不存在", result.stdout)
        self.assertIn("站点检查失败：1 项", result.stdout)

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

    def test_cli_no_args_fails_and_requires_dist(self) -> None:
        """不传任何参数调用时退出码非 0，stderr 提示缺少必选位置参数 dist。"""
        result = self.run_checker()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dist", result.stderr)
        self.assertIn("required", result.stderr.lower())

    def test_cli_help_option_displays_usage_and_dist(self) -> None:
        """传入 --help 时退出码为 0，stdout 包含 dist 位置参数说明与用法。"""
        result = self.run_checker("--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("usage:", result.stdout.lower())
        self.assertIn("dist", result.stdout)
        self.assertIn("静态站点目录", result.stdout)

    def test_cli_path_with_spaces_and_relative_path(self) -> None:
        """传入包含空格的目录路径或相对路径时仍能正常读取并完成检查。"""
        # 1. 包含空格的目录路径
        dist_with_spaces = self.make_dist("dist with spaces")
        result_spaces = self.run_checker(dist_with_spaces)
        self.assertEqual(result_spaces.returncode, 0, result_spaces.stdout + result_spaces.stderr)
        self.assertIn("站点检查通过", result_spaces.stdout)

        # 2. 从当前工作目录计算的相对路径
        rel_dist = Path(os.path.relpath(dist_with_spaces, Path.cwd()))
        result_rel = self.run_checker(rel_dist)
        self.assertEqual(result_rel.returncode, 0, result_rel.stdout + result_rel.stderr)
        self.assertIn("站点检查通过", result_rel.stdout)

        # 3. 相对路径（在父目录下使用相对目录名）
        dist_rel_name = self.make_dist("dist_relative")
        result_cwd = self.run_checker(dist_rel_name.name, cwd=dist_rel_name.parent)
        self.assertEqual(result_cwd.returncode, 0, result_cwd.stdout + result_cwd.stderr)
        self.assertIn("站点检查通过", result_cwd.stdout)


if __name__ == "__main__":
    unittest.main()
