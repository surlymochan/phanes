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

    def test_invalid_data_fails_and_writes_nothing(self) -> None:
        """数据不合格时退出码非 0，且不生成 dist。"""
        broken = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        broken["scope"]["schema版本"] = 3
        path = self.root / "broken.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        self.assertNotEqual(self.build(data=path, bundle=self.bundle), build_site.EXIT_OK)
        self.assertFalse(self.out.exists(), "坏数据时不应生成 dist")

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


if __name__ == "__main__":
    unittest.main()
