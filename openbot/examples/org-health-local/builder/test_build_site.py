"""build_site.py 的单元测试。

只用标准库，不调用 bun：单测一律传入假的 bundle 文件，构建目录为 TMPDIR 下的临时目录。
覆盖：示例数据构建出齐全的站点文件；坏数据退出码非 0 且不生成 dist。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

EX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EX_ROOT))

import builder.build_site as build_site  # noqa: E402  (需要先让示例目录可被导入)

EXAMPLE_PATH = EX_ROOT / "data" / "diagnosis.json"
FAKE_BUNDLE = "/* fake bundle */\nconsole.log('fake');\n"


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


def sha256_of(path: Path) -> str:
    """返回文件的 sha256 十六进制摘要。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BuildSiteTest(unittest.TestCase):
    """用合成示例数据构建静态站点。"""

    def setUp(self) -> None:
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "dist"
        self.bundle = self.root / "fake.bundle.js"
        self.bundle.write_text(FAKE_BUNDLE, encoding="utf-8")

    def build(self, data: Path = EXAMPLE_PATH, bundle: Path | None = None) -> int:
        """调用 build_site.main，返回退出码。"""
        argv = ["--data", str(data), "--out", str(self.out)]
        if bundle is not None:
            argv += ["--bundle", str(bundle)]
        return build_site.main(argv)

    def run_cli(
        self, data: Path = EXAMPLE_PATH, out: Path | None = None, bundle: Path | None = None
    ) -> subprocess.CompletedProcess:
        """以子进程运行 build_site.py 命令行。"""
        script = EX_ROOT / "builder" / "build_site.py"
        target_out = self.out if out is None else out
        argv = [sys.executable, str(script), "--data", str(data), "--out", str(target_out)]
        if bundle is not None:
            argv += ["--bundle", str(bundle)]
        return subprocess.run(argv, capture_output=True, text=True)

    def test_build_writes_all_files(self) -> None:
        """构建产物包含页面、样式、包、数据与构建报告。"""
        self.assertEqual(self.build(bundle=self.bundle), build_site.EXIT_OK)
        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "build-report.json"):
            with self.subTest(relative=relative):
                self.assertTrue((self.out / relative).is_file(), f"缺少 {relative}")

    def test_copied_files_match_sources(self) -> None:
        """复制的文件与来源逐字节一致。"""
        self.assertEqual(self.build(bundle=self.bundle), build_site.EXIT_OK)
        self.assertEqual((self.out / "index.html").read_bytes(), (EX_ROOT / "index.html").read_bytes())
        self.assertEqual((self.out / "styles.css").read_bytes(), (EX_ROOT / "styles.css").read_bytes())
        self.assertEqual((self.out / "app.bundle.js").read_bytes(), self.bundle.read_bytes())
        self.assertEqual((self.out / "data/diagnosis.json").read_bytes(), EXAMPLE_PATH.read_bytes())

    def test_existing_output_overwrites_required_files_and_keeps_unrelated_files(self) -> None:
        """已有输出目录中的固定产物会更新，无关旧文件按当前实现保留。"""
        old_content = b"old build artifact\n"
        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "build-report.json"):
            path = self.out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old_content)
        unrelated = self.out / "keep-me.txt"
        unrelated.write_bytes(b"unrelated old file\n")

        self.assertEqual(self.build(bundle=self.bundle), build_site.EXIT_OK)

        self.assertEqual((self.out / "index.html").read_bytes(), (EX_ROOT / "index.html").read_bytes())
        self.assertEqual((self.out / "styles.css").read_bytes(), (EX_ROOT / "styles.css").read_bytes())
        self.assertEqual((self.out / "app.bundle.js").read_bytes(), self.bundle.read_bytes())
        self.assertEqual((self.out / "data/diagnosis.json").read_bytes(), EXAMPLE_PATH.read_bytes())
        self.assertNotEqual((self.out / "build-report.json").read_bytes(), old_content)
        self.assertEqual(unrelated.read_bytes(), b"unrelated old file\n")

    def test_utf8_diagnosis_copy_matches_input_and_report(self) -> None:
        """含 UTF-8 中文内容的诊断快照复制后保持字节与摘要一致。"""
        diagnosis = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        diagnosis["conclusion"] = "中文诊断快照：复制后字节必须完全一致。"
        input_path = self.root / "utf8-diagnosis.json"
        input_path.write_text(json.dumps(diagnosis, ensure_ascii=False), encoding="utf-8")

        self.assertEqual(self.build(data=input_path, bundle=self.bundle), build_site.EXIT_OK)
        copied = self.out / "data/diagnosis.json"
        self.assertEqual(copied.read_bytes(), input_path.read_bytes())

        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        listed = next(entry for entry in report["文件清单"] if entry["路径"] == "data/diagnosis.json")
        self.assertEqual(report["sha256"], sha256_of(input_path))
        self.assertEqual(listed["sha256"], sha256_of(input_path))
        self.assertEqual(listed["字节数"], input_path.stat().st_size)

    def test_report_records_input_and_files(self) -> None:
        """构建报告记录输入路径、输入 sha256、文件清单与生成时间。"""
        self.assertEqual(self.build(bundle=self.bundle), build_site.EXIT_OK)
        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["输入路径"], str(EXAMPLE_PATH.resolve()))
        self.assertEqual(report["sha256"], sha256_of(EXAMPLE_PATH))
        self.assertRegex(report["生成时间"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        listed = {entry["路径"]: entry for entry in report["文件清单"]}
        self.assertEqual(
            sorted(listed), sorted(["index.html", "app.bundle.js", "styles.css", "data/diagnosis.json"])
        )
        for relative, entry in listed.items():
            with self.subTest(relative=relative):
                target = self.out / relative
                self.assertEqual(entry["sha256"], sha256_of(target))
                self.assertEqual(entry["字节数"], target.stat().st_size)

    def test_report_records_source_commit_when_provided(self) -> None:
        """指定来源提交时写入报告顶层字段。"""
        self.assertEqual(self.build(bundle=self.bundle), build_site.EXIT_OK)
        report = build_site.build_site(EXAMPLE_PATH, self.out, self.bundle, "abc123")
        self.assertEqual(report["来源提交"], "abc123")

    def test_report_omits_source_commit_when_not_provided(self) -> None:
        """未指定来源提交时保持既有报告字段。"""
        self.assertEqual(self.build(bundle=self.bundle), build_site.EXIT_OK)
        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        self.assertNotIn("来源提交", report)

    def test_invalid_data_fails_and_writes_nothing(self) -> None:
        """数据不合格时退出码非 0，且不生成 dist。"""
        broken = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        broken["scope"]["schema版本"] = 3
        path = self.root / "broken.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        self.assertNotEqual(self.build(data=path, bundle=self.bundle), build_site.EXIT_OK)
        self.assertFalse(self.out.exists(), "坏数据时不应生成 dist")

    def test_static_copy_error_returns_build_exit_and_cleans_new_output(self) -> None:
        """复制静态文件失败时返回构建错误并清理新建的半成品目录。"""
        from contextlib import redirect_stderr
        from io import StringIO

        stderr = StringIO()
        with unittest.mock.patch.object(
            build_site, "copy_static_files", side_effect=OSError("Simulated disk error")
        ), redirect_stderr(stderr):
            code = self.build(bundle=self.bundle)

        self.assertEqual(code, build_site.EXIT_BUILD)
        self.assertIn("构建失败", stderr.getvalue())
        self.assertIn("Simulated disk error", stderr.getvalue())
        self.assertFalse(self.out.exists(), "新建输出目录中的半成品应被清理")

    def test_missing_data_file_fails(self) -> None:
        """数据文件不存在时退出码非 0，且不生成 dist。"""
        self.assertNotEqual(self.build(data=self.root / "nope.json", bundle=self.bundle), build_site.EXIT_OK)
        self.assertFalse(self.out.exists(), "数据缺失时不应生成 dist")

    def test_missing_bundle_file_fails(self) -> None:
        """bundle 文件不存在时以 1 退出并提示示例 bundle，且不生成 dist。"""
        from contextlib import redirect_stderr
        from io import StringIO

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = self.build(bundle=self.root / "nope.js")
        self.assertEqual(code, 1)
        self.assertIn("demo/vendor/app.bundle.js", stderr.getvalue())
        self.assertFalse(self.out.exists(), "bundle 缺失时不应生成 dist")

    def test_bundle_path_is_directory_fails(self) -> None:
        """bundle 路径是目录时以 1 退出，报错含路径且不生成 dist。"""
        from contextlib import redirect_stderr
        from io import StringIO

        bundle_dir = self.root / "bundle_dir"
        bundle_dir.mkdir()
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = self.build(bundle=bundle_dir)
        self.assertEqual(code, build_site.EXIT_INVALID)
        self.assertIn(str(bundle_dir), stderr.getvalue())
        self.assertIn("不是有效文件", stderr.getvalue())
        self.assertFalse(self.out.exists(), "bundle 路径是目录时不应生成 dist")

    def test_data_path_is_directory_fails(self) -> None:
        """输入路径是目录时以 2 退出，且不生成 dist。"""
        from contextlib import redirect_stderr
        from io import StringIO

        folder = self.root / "data_dir"
        folder.mkdir()
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = self.build(data=folder, bundle=self.bundle)
        self.assertEqual(code, build_site.EXIT_USAGE)
        self.assertIn(str(folder), stderr.getvalue())
        self.assertFalse(self.out.exists(), "输入是目录时不应生成 dist")

    def test_corrupted_json_data_fails(self) -> None:
        """数据不是合法 JSON 时以 1 退出，且不生成 dist。"""
        from contextlib import redirect_stderr
        from io import StringIO

        path = self.root / "corrupted.json"
        path.write_text("{不是合法 JSON", encoding="utf-8")
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = self.build(data=path, bundle=self.bundle)
        self.assertEqual(code, build_site.EXIT_INVALID)
        self.assertIn(str(path), stderr.getvalue())
        self.assertIn("不是合法 JSON", stderr.getvalue())
        self.assertFalse(self.out.exists(), "坏 JSON 时不应生成 dist")

    def test_bun_command_matches_serve(self) -> None:
        """不带 --bundle 时使用与 serve.py 相同的 bun 命令。"""
        serve = build_site.load_module("org_health_serve", EX_ROOT / "serve.py")
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return None

        with unittest.mock.patch.object(serve.subprocess, "run", fake_run):
            serve.build_bundle()
        expected = captured["command"]
        actual = build_site.bun_command(Path("/tmp/dist/app.bundle.js"))
        self.assertEqual(actual[:4], expected[:4])
        self.assertEqual(expected[:4], ["bun", "build", "app.js", "--target=browser"])
        self.assertTrue(actual[4].startswith("--outfile="))

    def test_build_calls_bun_without_bundle_option(self) -> None:
        """不给 --bundle 时由 build_site 调用 bun 打包，输出落在 dist 内。"""
        recorded = {}

        def fake_run(command, **kwargs):
            recorded["command"] = command
            recorded["kwargs"] = kwargs
            outfile = Path(next(part for part in command if part.startswith("--outfile="))[len("--outfile=") :])
            outfile.write_text(FAKE_BUNDLE, encoding="utf-8")
            return None

        with unittest.mock.patch.object(subprocess, "run", fake_run):
            code = self.build()
        self.assertEqual(code, build_site.EXIT_OK)
        self.assertEqual(recorded["command"][:4], ["bun", "build", "app.js", "--target=browser"])
        self.assertEqual(
            recorded["command"][4],
            f"--outfile={(self.out / 'app.bundle.js').resolve()}",
        )
        self.assertEqual((self.out / "app.bundle.js").read_text(encoding="utf-8"), FAKE_BUNDLE)

    def test_command_line_exit_codes(self) -> None:
        """命令行入口：好数据退出码为 0，坏数据退出码非 0 且不生成 dist。"""
        script = EX_ROOT / "builder" / "build_site.py"
        result = subprocess.run(
            [sys.executable, str(script), "--data", str(EXAMPLE_PATH), "--out", str(self.out),
             "--bundle", str(self.bundle)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.out / "build-report.json").is_file())

        broken = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        broken["scope"]["schema版本"] = 3
        path = self.root / "broken.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        failed = subprocess.run(
            [sys.executable, str(script), "--data", str(path), "--out", str(self.out / "bad"),
             "--bundle", str(self.bundle)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.out / "bad").exists(), "坏数据时不应生成 dist")

    def test_command_line_help_lists_required_options(self) -> None:
        """帮助信息成功退出，并说明 data、out 与 bundle 参数。"""
        script = EX_ROOT / "builder" / "build_site.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--data", result.stdout)
        self.assertIn("--out", result.stdout)
        self.assertIn("--bundle", result.stdout)

    def test_command_line_missing_required_data_fails(self) -> None:
        """命令行缺少 --data 时以非 0 退出并提示必需参数。"""
        script = EX_ROOT / "builder" / "build_site.py"
        result = subprocess.run(
            [sys.executable, str(script), "--out", str(self.out)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--data", result.stderr)
        self.assertIn("required", result.stderr)

    def test_command_line_missing_required_out_fails(self) -> None:
        """命令行缺少 --out 时以非 0 退出并提示必需参数。"""
        script = EX_ROOT / "builder" / "build_site.py"
        result = subprocess.run(
            [sys.executable, str(script), "--data", str(EXAMPLE_PATH)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--out", result.stderr)
        self.assertIn("required", result.stderr)

    def test_command_line_creates_deep_missing_output_parent(self) -> None:
        """输出目录的多级父目录不存在时，命令行会创建完整站点。"""
        deep_out = self.root / "level-one" / "level-two" / "level-three" / "dist"
        result = self.run_cli(out=deep_out, bundle=self.bundle)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((deep_out / "index.html").is_file())
        self.assertTrue((deep_out / "data/diagnosis.json").is_file())
        self.assertTrue((deep_out / "build-report.json").is_file())

    def test_command_line_data_path_is_directory_fails(self) -> None:
        """输入路径是目录时命令行返回输入错误契约退出码，报错含定位路径且不生成 dist。"""
        dir_path = self.root / "input_directory"
        dir_path.mkdir()
        out_dir = self.root / "dist_from_dir"
        result = self.run_cli(data=dir_path, out=out_dir, bundle=self.bundle)
        self.assertIn(result.returncode, (build_site.EXIT_INVALID, build_site.EXIT_USAGE))
        self.assertEqual(result.returncode, build_site.EXIT_USAGE)
        output = result.stdout + result.stderr
        self.assertIn(str(dir_path), output)
        self.assertFalse(out_dir.exists(), "输入路径是目录时不应生成 dist 或留下半成品")

    def test_command_line_corrupted_json_fails(self) -> None:
        """诊断 JSON 损坏时命令行返回输入错误契约退出码，报错含定位路径且不生成 dist。"""
        corrupted = self.root / "corrupted_diagnosis.json"
        corrupted.write_text("{不是合法 JSON", encoding="utf-8")
        out_dir = self.root / "dist_from_corrupted_json"
        result = self.run_cli(data=corrupted, out=out_dir, bundle=self.bundle)
        self.assertIn(result.returncode, (build_site.EXIT_INVALID, build_site.EXIT_USAGE))
        self.assertEqual(result.returncode, build_site.EXIT_INVALID)
        output = result.stdout + result.stderr
        self.assertIn(str(corrupted), output)
        self.assertIn("不是合法 JSON", output)
        self.assertFalse(out_dir.exists(), "诊断 JSON 损坏时不应生成 dist 或留下半成品")

    def test_command_line_missing_diagnosis_json_fails_with_readable_stderr(self) -> None:
        """缺少 data/diagnosis.json 时命令行非零退出，stderr 包含可读错误和路径。"""
        missing = self.root / "data" / "diagnosis.json"
        out_dir = self.root / "dist_from_missing_diagnosis"
        result = self.run_cli(data=missing, out=out_dir, bundle=self.bundle)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("数据文件不存在", result.stderr)
        self.assertIn(str(missing), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(out_dir.exists(), "诊断 JSON 缺失时不应生成 dist 或留下半成品")


if __name__ == "__main__":
    unittest.main()
