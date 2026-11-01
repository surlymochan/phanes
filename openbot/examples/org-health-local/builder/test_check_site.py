"""check_site.py 的静态站点验收测试。"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import boss.make_brief as make_brief
import builder.build_site as build_site
import builder.check_site as check_site
import da.build_diagnosis as build_diagnosis

CHECKER = ROOT / "builder" / "check_site.py"
SOURCE_DATA = ROOT / "data" / "diagnosis.json"
SAMPLE_RAW_DIR = ROOT / "da" / "sample-raw"


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


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

    def write_report(self, dist: Path, report: object) -> None:
        (dist / "build-report.json").write_text(json.dumps(report), encoding="utf-8")

    def setUp(self) -> None:
        self.tmp = temporary_directory()
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

    def test_unregistered_nonempty_file_fails_with_repair_action(self) -> None:
        dist = self.make_dist()
        (dist / "debug.log").write_text("fixture", encoding="utf-8")
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("失败：debug.log 未在 build-report 中登记", result.stdout)
        self.assertIn("修复：", result.stdout)

    def test_report_duplicate_and_normalized_duplicate_paths_fail(self) -> None:
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        styles_hash = report["files"]["styles.css"]["sha256"]
        report["files"]["./styles.css"] = {"sha256": styles_hash}
        self.write_report(dist, report)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("规范化后指向同一文件", result.stdout)
        self.assertIn("styles.css", result.stdout)

    def test_report_registered_missing_file_fails_with_repair_action(self) -> None:
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        report["files"]["missing.js"] = {"sha256": "0" * 64}
        self.write_report(dist, report)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.js 已在 build-report 中登记但文件不存在", result.stdout)
        self.assertIn("修复：", result.stdout)

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

    def test_top_level_sha256_report_passes(self) -> None:
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        self.write_report(dist, {"sha256": {
            name: entry["sha256"] for name, entry in report["files"].items()
        }})
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("站点检查通过", result.stdout)

    def test_files_with_direct_hash_strings_passes(self) -> None:
        dist = self.make_dist()
        report = json.loads((dist / "build-report.json").read_text(encoding="utf-8"))
        self.write_report(dist, {"files": {
            name: entry["sha256"] for name, entry in report["files"].items()
        }})
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("站点检查通过", result.stdout)

    def test_manifest_item_missing_path_or_hash_fails(self) -> None:
        dist = self.make_dist()
        for item in ({"sha256": "0" * 64}, {"路径": "styles.css"}):
            with self.subTest(item=item):
                self.write_report(dist, {"文件清单": [item]})
                result = self.run_checker(dist)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("无法读取 build-report.json 中的 sha256", result.stdout)

    def test_files_with_non_string_key_or_value_fails(self) -> None:
        dist = self.make_dist()
        self.write_report(dist, {"files": {"styles.css": 12345}})
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法读取 build-report.json 中的 sha256", result.stdout)

        spec = importlib.util.spec_from_file_location("check_site_under_test", CHECKER)
        assert spec is not None and spec.loader is not None
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        output = io.StringIO()
        with mock.patch.object(checker.json, "loads", return_value={"files": {1: "0" * 64}}):
            with contextlib.redirect_stdout(output):
                returncode = checker.main([str(dist)])
        self.assertNotEqual(returncode, 0)
        self.assertIn("无法读取 build-report.json 中的 sha256", output.getvalue())

    def test_report_without_hash_object_fails(self) -> None:
        dist = self.make_dist()
        self.write_report(dist, {})
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-report.json 缺少 files 或 sha256 对象", result.stdout)

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

    def test_local_reference_with_fragment_passes(self) -> None:
        """带 #fragment（及片段与查询串共存）的本地引用在剥离后校验真实文件存在。"""
        dist = self.make_dist()
        (dist / "app bundle.js").write_text("console.log('encoded');", encoding="utf-8")
        (dist / "index.html").write_text(
            '<link rel="stylesheet" href="./styles.css#theme">\n'
            '<script src="./app.bundle.js?v=2024#main"></script>\n'
            '<script src="./app%20bundle.js#section"></script>\n'
            '<link rel="stylesheet" href="#pure-hash">',
            encoding="utf-8",
        )
        self.refresh_report(dist, "app bundle.js")
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("通过：index.html 引用的本地文件 ./styles.css 存在", result.stdout)
        self.assertIn("通过：index.html 引用的本地文件 ./app.bundle.js 存在", result.stdout)
        self.assertIn("通过：index.html 引用的本地文件 ./app%20bundle.js 存在", result.stdout)
        self.assertNotIn("未在 build-report 中登记", result.stdout)
        self.assertIn("站点检查通过", result.stdout)

    def test_missing_local_reference_with_fragment_fails(self) -> None:
        """带 #fragment 的缺失本地引用仍必须失败并定位缺失文件。"""
        dist = self.make_dist()
        (dist / "index.html").write_text(
            '<link rel="stylesheet" href="./missing.css#theme">\n'
            '<script src="./missing.js?v=1#section"></script>\n'
            '<script src="./missing%20bundle.js#frag"></script>',
            encoding="utf-8",
        )
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：index.html 引用的本地文件 ./missing.css 不存在", result.stdout)
        self.assertIn("失败：index.html 引用的本地文件 ./missing.js 不存在", result.stdout)
        self.assertIn("失败：index.html 引用的本地文件 ./missing%20bundle.js 不存在", result.stdout)
        self.assertIn("站点检查失败：3 项", result.stdout)

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

    def load_checker(self):
        spec = importlib.util.spec_from_file_location("check_site_module", CHECKER)
        assert spec is not None and spec.loader is not None
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        return checker

    def test_complete_valid_site_reports_zero_defects(self) -> None:
        """完整合法的站点检查通过，check_site 返回零缺陷且退出码为 0。"""
        dist = self.make_dist()
        checker = self.load_checker()
        failures = checker.check_site(dist)
        self.assertEqual(failures, [])
        result = self.run_checker(dist)
        self.assertEqual(result.returncode, 0)
        self.assertIn("站点检查通过", result.stdout)
        self.assertNotIn("站点检查失败", result.stdout)

    def test_defective_dist_aggregates_multiple_defects_and_exits_non_zero(self) -> None:
        """构造缺 CSS/JS、坏 JSON、空章节的残缺 dist，断言 check_site 聚合多条缺陷且退出非零。"""
        dist = self.make_dist("defective_dist")
        # 1. 缺 CSS 与 JS
        (dist / "styles.css").unlink()
        (dist / "app.bundle.js").unlink()
        # 2. 坏 JSON（损坏的 diagnosis.json）
        (dist / "data/diagnosis.json").write_text("{ unclosed bad json content ...", encoding="utf-8")
        # 3. 空章节（index.html 中包含空的 <section> 标签）
        (dist / "index.html").write_text(
            '<section id="overview"><h2>概览</h2></section>\n'
            '<section id="empty-metrics"></section>\n'
            '<section class="empty-rules">   \n  </section>',
            encoding="utf-8",
        )

        # 直接调用 check_site 验证聚合缺陷列表
        checker = self.load_checker()
        failures = checker.check_site(dist)
        self.assertGreaterEqual(len(failures), 4)
        self.assertTrue(any("styles.css 不存在" in msg for msg in failures), failures)
        self.assertTrue(any("app.bundle.js 不存在" in msg for msg in failures), failures)
        self.assertTrue(any("data/diagnosis.json 无法通过 validate.py" in msg for msg in failures), failures)
        self.assertTrue(any("index.html 存在空章节" in msg and "empty-metrics" in msg for msg in failures), failures)
        self.assertTrue(any("index.html 存在空章节" in msg and "empty-rules" in msg for msg in failures), failures)

        # 运行 CLI 验证退出非零、聚合缺陷输出及无 Traceback 崩溃
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：styles.css 不存在", result.stdout)
        self.assertIn("失败：app.bundle.js 不存在", result.stdout)
        self.assertIn("失败：data/diagnosis.json 无法通过 validate.py", result.stdout)
        self.assertIn("失败：index.html 存在空章节", result.stdout)
        self.assertIn("empty-metrics", result.stdout)
        self.assertIn(f"站点检查失败：{len(failures)} 项", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_defective_dist_with_empty_data_sections_aggregates_defects(self) -> None:
        """构造缺 CSS、坏 build-report JSON、空数据章节的残缺 dist，断言缺陷聚合且退出非零。"""
        dist = self.make_dist("defective_data_sections")
        # 1. 缺 CSS
        (dist / "styles.css").unlink()
        # 2. 坏 JSON（损坏的 build-report.json）
        (dist / "build-report.json").write_text("not a valid json report", encoding="utf-8")
        # 3. 空章节（diagnosis.json 中 insights 和 recommendations 章节为空列表）
        data = json.loads((dist / "data/diagnosis.json").read_text(encoding="utf-8"))
        data["insights"] = []
        data["recommendations"] = []
        (dist / "data/diagnosis.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        checker = self.load_checker()
        failures = checker.check_site(dist)
        self.assertGreaterEqual(len(failures), 4)
        self.assertTrue(any("styles.css 不存在" in msg for msg in failures), failures)
        self.assertTrue(any("build-report.json" in msg for msg in failures), failures)
        self.assertTrue(any("data/diagnosis.json 存在空章节" in msg and "insights" in msg for msg in failures), failures)
        self.assertTrue(any("data/diagnosis.json 存在空章节" in msg and "recommendations" in msg for msg in failures), failures)

        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：styles.css 不存在", result.stdout)
        self.assertIn("build-report.json", result.stdout)
        self.assertIn("data/diagnosis.json 存在空章节（insights 为空）", result.stdout)
        self.assertIn("data/diagnosis.json 存在空章节（recommendations 为空）", result.stdout)
        self.assertIn(f"站点检查失败：{len(failures)} 项", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_section_in_html_fails_check(self) -> None:
        """HTML 页面包含空章节标签时检查失败并精确定位空章节。"""
        dist = self.make_dist("dist_empty_html_sec")
        (dist / "index.html").write_text(
            '<link rel="stylesheet" href="./styles.css">\n'
            '<section id="empty-content"></section>',
            encoding="utf-8",
        )
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：index.html 存在空章节（<section id='empty-content'> 为空）", result.stdout)
        self.assertIn("站点检查失败：1 项", result.stdout)

    def test_empty_section_in_diagnosis_json_fails_check(self) -> None:
        """诊断数据中任一核心章节（如 risks）为空时检查失败并精确定位该章节。"""
        dist = self.make_dist("dist_empty_data_sec")
        data = json.loads((dist / "data/diagnosis.json").read_text(encoding="utf-8"))
        data["risks"] = []
        (dist / "data/diagnosis.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.refresh_report(dist)
        result = self.run_checker(dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("失败：data/diagnosis.json 存在空章节（risks 为空）", result.stdout)
        self.assertIn("站点检查失败：1 项", result.stdout)


class MinimalBriefPipelineCheckSiteTest(unittest.TestCase):
    """合法 brief 管道产出到 Builder 再到 check_site 的集成测试（#775）。

    覆盖：
    1. 用最小合法 brief 到 build_site 到 check_site 走通成功路径；
    2. 故意坏站点失败路径（校验失败与空章节），禁止只测空输入。
    """

    def setUp(self) -> None:
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name)
        self.brief_path = self.workdir / "brief.json"
        self.diag_path = self.workdir / "diagnosis.json"
        self.dist_path = self.workdir / "dist"
        self.bundle_path = self.workdir / "fake.bundle.js"
        self.bundle_path.write_text("/* fake bundle */\nconsole.log('pipeline');\n", encoding="utf-8")

    @staticmethod
    def minimal_legal_brief() -> dict:
        """返回仅包含 schema v4 必填项的最小合法 brief 结构，禁止只测空输入。"""
        return {
            "目标": ["复核观察期间内整体技术人力成本与折算人头"],
            "观察期间": {
                "观察月份": "2026-09",
                "数据截止": "2026-09-30",
                "期次": ["2026-01实际", "2026-05H1参考", "2026-09实际", "年末测算", "年末目标"],
                "说明": "2026-09 最小期间说明",
            },
            "口径": {
                "整体技术人力成本": {"单位": "万元/月", "定义": "技术人力成本", "期间": "2026", "缺失处理": "null"},
                "整体折算人头": {"单位": "人", "定义": "折算人头", "期间": "2026", "缺失处理": "null"},
                "营收业务（含共享平台）技术人力成本与折算人头": {"单位": "万元/月", "定义": "营收技术成本", "期间": "2026", "缺失处理": "null"},
                "收入与成本率": {"单位": "万美元/月", "定义": "收入成本率", "期间": "2026", "缺失处理": "null"},
                "汇率": 7.2,
            },
            "必答问题": ["观察期间内整体技术人力成本是多少？相对期初实际增减多少？"],
            "数据需求": [
                {
                    "字段路径": "月度成本/技术人力成本",
                    "单位": "万元/月",
                    "口径": "计入技术人力的成本合计",
                    "对应指标": "整体技术人力成本",
                }
            ],
            "禁止事项": ["不得写入真实组织数据", "不作个人/薪酬/编制判断"],
            "schema版本": 4,
        }

    def _build_pipeline_site(self) -> None:
        """用最小合法 brief 经 DA 归并后由 Builder 生成完整静态站点。"""
        brief_data = self.minimal_legal_brief()
        self.brief_path.write_text(json.dumps(brief_data, ensure_ascii=False), encoding="utf-8")

        # 校验 brief 符合 brief.schema.json
        brief_errors = make_brief.validate_brief(brief_data)
        self.assertEqual(brief_errors, [], f"最小合法 brief 结构不合法：{brief_errors}")

        # DA 从原始样本数据归并生成快照
        snapshot = build_diagnosis.build_from_files(self.brief_path, SAMPLE_RAW_DIR)
        self.diag_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(self.diag_path.is_file())
        self.assertGreater(self.diag_path.stat().st_size, 0)

        # Builder 生成站点静态资源
        exit_code = build_site.main(
            [
                "--data",
                str(self.diag_path),
                "--out",
                str(self.dist_path),
                "--bundle",
                str(self.bundle_path),
            ]
        )
        self.assertEqual(exit_code, build_site.EXIT_OK)

    def test_minimal_legal_brief_pipeline_passes_check_site(self) -> None:
        """最小合法 brief 到 build_site 到 check_site 走通成功路径，断言零缺陷且非空。"""
        self._build_pipeline_site()

        # 验证站点骨架完整且非空（禁止只测空输入）
        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "build-report.json"):
            file_path = self.dist_path / relative
            self.assertTrue(file_path.is_file(), f"产物缺少 {relative}")
            self.assertGreater(file_path.stat().st_size, 0, f"{relative} 为空文件")

        # Python API 调用 check_site：返回零缺陷
        failures = check_site.check_site(self.dist_path)
        self.assertEqual(failures, [], f"站点检查未通过：{failures}")

        # CLI 子进程调用 check_site：退出码 0，包含通过标志
        cli_result = subprocess.run(
            [sys.executable, str(CHECKER), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
        self.assertIn("站点检查通过", cli_result.stdout)
        self.assertIn("通过：data/diagnosis.json 通过 validate.py", cli_result.stdout)
        self.assertNotIn("Traceback", cli_result.stderr)

    def test_real_build_rejects_extra_tampered_and_duplicate_manifest_paths(self) -> None:
        """真实 Builder 产物覆盖新增、篡改、重复登记三条清单失败路径。"""
        cases = ("extra", "tampered", "duplicate")
        for case in cases:
            with self.subTest(case=case):
                if self.dist_path.exists():
                    shutil.rmtree(self.dist_path)
                self._build_pipeline_site()
                report_path = self.dist_path / "build-report.json"
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if case == "extra":
                    (self.dist_path / "debug.log").write_text("unexpected artifact", encoding="utf-8")
                    expected = "debug.log 未在 build-report 中登记"
                elif case == "tampered":
                    (self.dist_path / "styles.css").write_text("tampered", encoding="utf-8")
                    expected = "styles.css 的 sha256 与 build-report.json 不一致"
                else:
                    report["文件清单"].append(dict(report["文件清单"][0]))
                    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
                    expected = "重复路径或规范化后指向同一文件"
                failures = check_site.check_site(self.dist_path)
                self.assertTrue(any(expected in failure for failure in failures), failures)

    def test_deliberately_defective_site_fails_check_site(self) -> None:
        """故意坏站点失败路径：篡改管道产物使 validate.py 校验失败，check_site 检出缺陷且非零退出。"""
        self._build_pipeline_site()

        # 故意损坏站点快照（篡改 schema 版本导致 validate.py 失败）
        dist_diag_path = self.dist_path / "data" / "diagnosis.json"
        dist_data = json.loads(dist_diag_path.read_text(encoding="utf-8"))
        dist_data["scope"]["schema版本"] = 999
        dist_diag_path.write_text(json.dumps(dist_data, ensure_ascii=False), encoding="utf-8")
        build_site.write_report(self.dist_path, dist_diag_path)

        # 确认不是空输入测试：站点各文件仍非空存在
        self.assertGreater(dist_diag_path.stat().st_size, 0)
        self.assertGreater((self.dist_path / "index.html").stat().st_size, 0)

        # Python API 检出 validate 错误
        failures = check_site.check_site(self.dist_path)
        self.assertGreater(len(failures), 0)
        self.assertTrue(any("未通过 validate.py" in f for f in failures), failures)

        # CLI 退出非 0，报告站点检查失败，无 Traceback
        cli_result = subprocess.run(
            [sys.executable, str(CHECKER), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(cli_result.returncode, 0)
        self.assertIn("未通过 validate.py", cli_result.stdout)
        self.assertIn("站点检查失败", cli_result.stdout)
        self.assertNotIn("Traceback", cli_result.stderr)

    def test_deliberately_defective_site_with_empty_section_fails_check_site(self) -> None:
        """故意坏站点失败路径：清空快照核心章节（如 risks 赋空），check_site 报错并精确定位。"""
        self._build_pipeline_site()

        # 故意将诊断数据 risks 核心章节清空
        dist_diag_path = self.dist_path / "data" / "diagnosis.json"
        dist_data = json.loads(dist_diag_path.read_text(encoding="utf-8"))
        dist_data["risks"] = []
        dist_diag_path.write_text(json.dumps(dist_data, ensure_ascii=False), encoding="utf-8")
        build_site.write_report(self.dist_path, dist_diag_path)

        # 确认不是空输入：index.html 与其他核心章节依然饱满
        self.assertGreater(len(dist_data["metrics"]), 0)
        self.assertGreater((self.dist_path / "index.html").stat().st_size, 0)

        failures = check_site.check_site(self.dist_path)
        self.assertGreater(len(failures), 0)
        self.assertTrue(any("data/diagnosis.json 存在空章节（risks 为空）" in f for f in failures), failures)

        cli_result = subprocess.run(
            [sys.executable, str(CHECKER), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(cli_result.returncode, 0)
        self.assertIn("data/diagnosis.json 存在空章节（risks 为空）", cli_result.stdout)
        self.assertIn("站点检查失败", cli_result.stdout)
        self.assertNotIn("Traceback", cli_result.stderr)


if __name__ == "__main__":
    unittest.main()
