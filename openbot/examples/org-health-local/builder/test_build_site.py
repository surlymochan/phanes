"""build_site.py 的单元测试。

只用标准库，不调用 bun：单测一律传入假的 bundle 文件，构建目录为 TMPDIR 下的临时目录。
覆盖：示例数据构建出齐全的站点文件；坏数据退出码非 0 且不生成 dist；
结构定义文件缺失时给出可读错误（含路径）且不生成 dist；
同一份 brief 与诊断快照连续构建两次时，第二次仍成功，报告与站点关键文件依旧齐全可读；
Boss+Builder 管道：合法 brief 配合非空诊断样本生成可通过 check_site 的站点（零缺陷），
注入 brief 逻辑错误确认 check_site 失败。
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

EX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EX_ROOT))

import boss.make_brief as make_brief  # noqa: E402
import builder.build_site as build_site  # noqa: E402  (需要先让示例目录可被导入)
import builder.check_site as check_site  # noqa: E402
import da.build_diagnosis as build_diagnosis  # noqa: E402

EXAMPLE_PATH = EX_ROOT / "data" / "diagnosis.json"
# 仓库里同一对 brief → 诊断快照：重复构建场景用这同一份输入。
DEMO_BRIEF_PATH = EX_ROOT / "demo" / "brief.json"
DEMO_DIAGNOSIS_PATH = EX_ROOT / "demo" / "diagnosis.json"
FAKE_BUNDLE = "/* fake bundle */\nconsole.log('fake');\n"
BOSS_SCRIPT = EX_ROOT / "boss" / "make_brief.py"
DA_SCRIPT = EX_ROOT / "da" / "build_diagnosis.py"
BUILDER_SCRIPT = EX_ROOT / "builder" / "build_site.py"
CHECKER_SCRIPT = EX_ROOT / "builder" / "check_site.py"
SAMPLE_RAW_DIR = EX_ROOT / "da" / "sample-raw"


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

    def read_manifest(self) -> dict:
        """读取构建报告里的文件清单，返回 路径 -> sha256。"""
        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        return {entry["路径"]: entry["sha256"] for entry in report["文件清单"]}

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

    def test_repeated_build_keeps_site_and_report_readable(self) -> None:
        """同一份 brief 与诊断快照连续构建两次：第二次成功，报告与站点关键文件仍存在且可读。"""
        self.assertEqual(self.build(data=DEMO_DIAGNOSIS_PATH, bundle=self.bundle), build_site.EXIT_OK)
        first_report_bytes = (self.out / "build-report.json").read_bytes()
        first_report = json.loads(first_report_bytes)
        first_manifest = self.read_manifest()

        self.assertEqual(self.build(data=DEMO_DIAGNOSIS_PATH, bundle=self.bundle), build_site.EXIT_OK)

        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "build-report.json"):
            with self.subTest(relative=relative):
                path = self.out / relative
                self.assertTrue(path.is_file(), f"缺少 {relative}")
                self.assertGreater(path.stat().st_size, 0, f"{relative} 为空")

        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["输入路径"], str(DEMO_DIAGNOSIS_PATH.resolve()))
        self.assertEqual(report["sha256"], sha256_of(DEMO_DIAGNOSIS_PATH))
        self.assertEqual(
            [entry["路径"] for entry in report["文件清单"]], list(build_site.SITE_FILES)
        )
        self.assertEqual(
            [list(entry) for entry in report["文件清单"]],
            [["路径", "字节数", "sha256"]] * len(build_site.SITE_FILES),
        )
        manifest = {entry["路径"]: entry["sha256"] for entry in report["文件清单"]}
        for relative, digest in manifest.items():
            with self.subTest(relative=relative):
                self.assertEqual(digest, sha256_of(self.out / relative))
        self.assertEqual(manifest, first_manifest, "同一份输入两次构建的文件摘要应一致")
        self.assertEqual(
            (self.out / "build-report.json").read_bytes(),
            first_report_bytes,
            "输入、bundle 与诊断 JSON 不变时两次报告字节应一致",
        )
        self.assertEqual(list(report), list(first_report))

        # 站点内容仍可读：诊断快照与来源逐字节一致，页面与浏览器包没有损坏。
        self.assertEqual((self.out / "data/diagnosis.json").read_bytes(), DEMO_DIAGNOSIS_PATH.read_bytes())
        snapshot = json.loads((self.out / "data/diagnosis.json").read_text(encoding="utf-8"))
        brief = json.loads(DEMO_BRIEF_PATH.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["scope"]["数据截止"], brief["观察期间"]["数据截止"])
        self.assertEqual(snapshot["scope"]["schema版本"], 4)
        self.assertIn("<html", (self.out / "index.html").read_text(encoding="utf-8"))
        self.assertEqual((self.out / "app.bundle.js").read_bytes(), self.bundle.read_bytes())

    def test_repeated_build_recreates_missing_output_subdirectory(self) -> None:
        """已有输出目录里缺少 data 子目录或页面文件时，重复构建仍会补齐并写出报告。"""
        self.assertEqual(self.build(data=DEMO_DIAGNOSIS_PATH, bundle=self.bundle), build_site.EXIT_OK)
        shutil.rmtree(self.out / "data")
        (self.out / "index.html").unlink()

        self.assertEqual(self.build(data=DEMO_DIAGNOSIS_PATH, bundle=self.bundle), build_site.EXIT_OK)

        self.assertTrue((self.out / "index.html").is_file(), "重复构建应补齐缺失的页面文件")
        self.assertEqual((self.out / "data/diagnosis.json").read_bytes(), DEMO_DIAGNOSIS_PATH.read_bytes())
        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["sha256"], sha256_of(DEMO_DIAGNOSIS_PATH))
        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json"):
            with self.subTest(relative=relative):
                entry = next(item for item in report["文件清单"] if item["路径"] == relative)
                self.assertEqual(entry["sha256"], sha256_of(self.out / relative))

    def test_command_line_repeated_build_keeps_artifacts(self) -> None:
        """命令行对同一份诊断快照连续构建两次：第二次退出码为 0，报告与站点关键文件仍存在且可读。"""
        first = self.run_cli(data=DEMO_DIAGNOSIS_PATH, bundle=self.bundle)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        second = self.run_cli(data=DEMO_DIAGNOSIS_PATH, bundle=self.bundle)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("已生成站点", second.stdout)

        for relative in ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "build-report.json"):
            with self.subTest(relative=relative):
                path = self.out / relative
                self.assertTrue(path.is_file(), f"缺少 {relative}")
                self.assertGreater(path.stat().st_size, 0, f"{relative} 为空")
        report = json.loads((self.out / "build-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["sha256"], sha256_of(DEMO_DIAGNOSIS_PATH))

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

    def test_report_records_relative_inputs_bundle_hash_and_copy_list(self) -> None:
        """报告记录 brief/diagnosis 相对路径、输入摘要、bundle 摘要与复制清单。"""
        report = build_site.build_site(
            DEMO_DIAGNOSIS_PATH,
            self.out,
            self.bundle,
            brief_path=DEMO_BRIEF_PATH,
        )
        summary = report["输入摘要"]
        self.assertFalse(Path(summary["brief"]["路径"]).is_absolute())
        self.assertEqual(summary["brief"]["sha256"], sha256_of(DEMO_BRIEF_PATH))
        self.assertFalse(Path(summary["diagnosis"]["路径"]).is_absolute())
        self.assertEqual(summary["diagnosis"]["sha256"], sha256_of(DEMO_DIAGNOSIS_PATH))
        self.assertEqual(report["bundle"]["路径"], "app.bundle.js")
        self.assertEqual(report["bundle"]["sha256"], sha256_of(self.bundle))
        self.assertEqual(report["复制文件"], list(build_site.SITE_FILES))

    def test_oversized_chapter_brief_keeps_site_complete_and_summary_visible(self) -> None:
        """超长章节 brief 不应阻断真实构建，诊断摘要与 HTML 骨架仍完整。"""
        brief = json.loads(DEMO_BRIEF_PATH.read_text(encoding="utf-8"))
        key_summary = "关键摘要：超长章节构建回归仍需保留这段内容。"
        brief["章节"] = [
            {
                "标题": "组织诊断摘要",
                "摘要": key_summary,
                "正文": key_summary + "\n" + ("章节正文。" * 250_000),
            }
        ]
        long_brief = self.root / "long-brief.json"
        long_brief.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        self.assertGreater(long_brief.stat().st_size, 1_000_000, "测试 brief 应为非空超长输入")

        report = build_site.build_site(
            DEMO_DIAGNOSIS_PATH,
            self.out,
            self.bundle,
            brief_path=long_brief,
        )

        for relative in (*build_site.SITE_FILES, "build-report.json"):
            with self.subTest(relative=relative):
                self.assertTrue((self.out / relative).is_file(), f"缺少 {relative}")
                self.assertGreater((self.out / relative).stat().st_size, 0, f"{relative} 为空")

        copied_diagnosis = json.loads(
            (self.out / "data/diagnosis.json").read_text(encoding="utf-8")
        )
        source_diagnosis = json.loads(DEMO_DIAGNOSIS_PATH.read_text(encoding="utf-8"))
        self.assertTrue(copied_diagnosis["conclusion"])
        self.assertEqual(copied_diagnosis["conclusion"], source_diagnosis["conclusion"])
        self.assertEqual(report["输入摘要"]["brief"]["sha256"], sha256_of(long_brief))

        class StructureParser(HTMLParser):
            """记录 HTML 骨架标签，避免把完整性检查绑定到浏览器环境。"""

            def __init__(self) -> None:
                super().__init__()
                self.opened: list[str] = []
                self.closed: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                self.opened.append(tag)

            def handle_endtag(self, tag: str) -> None:
                self.closed.append(tag)

        html = (self.out / "index.html").read_text(encoding="utf-8")
        parser = StructureParser()
        parser.feed(html)
        parser.close()
        for tag in ("html", "head", "body", "main", "footer"):
            with self.subTest(tag=tag):
                self.assertIn(tag, parser.opened)
                self.assertIn(tag, parser.closed)
        self.assertTrue(html.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn('id="conclusion"', html, "HTML 应保留关键摘要的渲染位置")

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
        self.assertFalse((self.out / "app.bundle.js").exists(), "bundle 缺失时不应创建不完整的 app.bundle.js")

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

    def test_missing_schema_file_fails_with_readable_stderr(self) -> None:
        """结构定义文件缺失时以 2 退出，stderr 给出可读错误（含路径），且没有 Traceback。"""
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        missing = self.root / "schema" / "diagnosis.schema.json"
        stdout = StringIO()
        stderr = StringIO()
        with unittest.mock.patch.object(build_site, "SCHEMA_PATH", missing), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            code = self.build(bundle=self.bundle)

        self.assertEqual(code, build_site.EXIT_USAGE)
        message = stderr.getvalue()
        self.assertIn("结构定义不可用", message)
        self.assertIn("diagnosis.schema.json", message)
        self.assertIn(str(missing), message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("Traceback", stdout.getvalue())
        self.assertFalse(self.out.exists(), "结构定义缺失时不应生成 dist")

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

    def test_build_site_missing_bundle_raises_and_creates_no_bundle(self) -> None:
        """build_site 函数直接调用时若 bundle 不存在则抛出 FileNotFoundError，不创建或留下半成品 bundle。"""
        missing_bundle = self.root / "no_such_bundle.js"
        target_out = self.root / "dist_direct_call"
        with self.assertRaises(FileNotFoundError):
            build_site.build_site(EXAMPLE_PATH, target_out, bundle_path=missing_bundle)
        self.assertFalse(target_out.exists(), "build_site 在 bundle 不存在时不应创建半成品输出目录")
        self.assertFalse((target_out / "app.bundle.js").exists())

    def test_command_line_missing_bundle_fails_and_creates_no_bundle(self) -> None:
        """显式 --bundle 指向不存在文件时命令行非零退出且不会报告成功，不创建半成品 app.bundle.js。"""
        missing_bundle = self.root / "nonexistent.bundle.js"
        out_dir = self.root / "dist_missing_bundle"
        result = self.run_cli(bundle=missing_bundle, out=out_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, build_site.EXIT_INVALID)
        self.assertNotIn("已生成站点", result.stdout)
        self.assertIn("bundle 文件不存在或不是有效文件", result.stderr)
        self.assertIn(str(missing_bundle), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(out_dir.exists(), "bundle 缺失时不应生成 dist 或留下半成品站点")
        self.assertFalse((out_dir / "app.bundle.js").exists(), "不应创建不完整的 app.bundle.js")

    def test_command_line_missing_bundle_preserves_existing_output_and_snapshot(self) -> None:
        """显式 --bundle 指向不存在文件时，已有输出目录与输入快照保持原样，不创建不完整的 app.bundle.js。"""
        out_dir = self.root / "existing_dist"
        out_dir.mkdir(parents=True, exist_ok=True)
        unrelated = out_dir / "keep-me.txt"
        unrelated.write_bytes(b"existing output file\n")

        snapshot_content = EXAMPLE_PATH.read_bytes()
        missing_bundle = self.root / "absent.bundle.js"

        result = self.run_cli(data=EXAMPLE_PATH, bundle=missing_bundle, out=out_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, build_site.EXIT_INVALID)
        self.assertNotIn("已生成站点", result.stdout)
        self.assertIn("bundle 文件不存在或不是有效文件", result.stderr)
        self.assertIn(str(missing_bundle), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        self.assertFalse((out_dir / "app.bundle.js").exists(), "已有输出目录中不应创建不完整的 app.bundle.js")
        self.assertEqual(unrelated.read_bytes(), b"existing output file\n", "已有输出目录的既有文件应保持原样")
        self.assertEqual(EXAMPLE_PATH.read_bytes(), snapshot_content, "输入快照文件应保持原样")


class BossBuilderPipelineTest(unittest.TestCase):
    """Boss+Builder 管道集成测试：合法 brief 生成可通过 check_site 的站点，注入 brief 逻辑错误确认失败。"""

    def setUp(self) -> None:
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.workdir = Path(self.temp.name)
        self.brief_path = self.workdir / "brief.json"
        self.diag_path = self.workdir / "diagnosis.json"
        self.dist_path = self.workdir / "dist"
        self.bundle_path = self.workdir / "fake.bundle.js"
        self.bundle_path.write_text(FAKE_BUNDLE, encoding="utf-8")

    def test_pipeline_with_legal_brief_passes_check_site_with_zero_defects(self) -> None:
        """合法 brief 经 DA 归并样本后由 Builder 构建，check_site 断言站点零缺陷且退出码为 0。"""
        # 1. 调用 boss/make_brief.py 生成合法任务书
        boss_res = subprocess.run(
            [sys.executable, str(BOSS_SCRIPT), "--out", str(self.brief_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(boss_res.returncode, 0, boss_res.stderr)
        self.assertTrue(self.brief_path.is_file(), "任务书 brief.json 未生成")

        # 校验生成的 brief 符合 brief.schema.json
        brief_data = json.loads(self.brief_path.read_text(encoding="utf-8"))
        self.assertEqual(make_brief.validate_brief(brief_data), [])

        # 2. 调用 da/build_diagnosis.py 结合非空诊断样本生成诊断快照
        da_res = subprocess.run(
            [
                sys.executable,
                str(DA_SCRIPT),
                "--brief",
                str(self.brief_path),
                "--raw",
                str(SAMPLE_RAW_DIR),
                "--out",
                str(self.diag_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(da_res.returncode, 0, da_res.stderr)
        self.assertTrue(self.diag_path.is_file(), "诊断快照 diagnosis.json 未生成")
        self.assertGreater(self.diag_path.stat().st_size, 0, "诊断快照 diagnosis.json 为空")

        # 3. 调用 builder/build_site.py 构建静态站点
        builder_res = subprocess.run(
            [
                sys.executable,
                str(BUILDER_SCRIPT),
                "--data",
                str(self.diag_path),
                "--out",
                str(self.dist_path),
                "--bundle",
                str(self.bundle_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(builder_res.returncode, 0, builder_res.stderr)
        for relative in build_site.SITE_FILES:
            self.assertTrue((self.dist_path / relative).is_file(), f"缺少站点产物 {relative}")

        # 4. 调用 builder/check_site.py 进行站点验收
        checker_res = subprocess.run(
            [sys.executable, str(CHECKER_SCRIPT), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(checker_res.returncode, 0, checker_res.stderr)
        self.assertIn("站点检查通过", checker_res.stdout)

        # Python API 检查并断言站点零缺陷（failures 列表为空）
        failures = check_site.check_site(self.dist_path)
        self.assertEqual(failures, [], f"站点存在非预期缺陷：{failures}")

    def test_pipeline_with_injected_brief_period_logic_error_fails_check_site(self) -> None:
        """注入 brief 期次逆序逻辑错误后，管道中断且 check_site 确认失败并报告缺陷。"""
        # 1. 生成初始 brief
        boss_res = subprocess.run(
            [sys.executable, str(BOSS_SCRIPT), "--out", str(self.brief_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(boss_res.returncode, 0, boss_res.stderr)

        # 2. 注入 brief 逻辑错误：期次时间逆序（9月在前，1月在后）
        brief_data = json.loads(self.brief_path.read_text(encoding="utf-8"))
        brief_data["观察期间"]["期次"] = [
            "2026-09实际",
            "2026-01实际",
            "2026-05H1参考",
            "年末测算",
            "年末目标",
        ]
        self.brief_path.write_text(json.dumps(brief_data, ensure_ascii=False), encoding="utf-8")

        # 验证 make_brief.py --check 可检出该逻辑错误
        check_brief_res = subprocess.run(
            [sys.executable, str(BOSS_SCRIPT), "--check", str(self.brief_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(check_brief_res.returncode, 0)
        self.assertIn("必须按时间升序排列", check_brief_res.stdout + check_brief_res.stderr)

        # 3. DA 消费坏 brief 时报错中断，不生成诊断快照
        da_res = subprocess.run(
            [
                sys.executable,
                str(DA_SCRIPT),
                "--brief",
                str(self.brief_path),
                "--raw",
                str(SAMPLE_RAW_DIR),
                "--out",
                str(self.diag_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(da_res.returncode, 0)
        self.assertFalse(self.diag_path.exists(), "逻辑错误的任务书不应产出诊断快照")

        # 4. Builder 无法从缺失的快照构建站点
        builder_res = subprocess.run(
            [
                sys.executable,
                str(BUILDER_SCRIPT),
                "--data",
                str(self.diag_path),
                "--out",
                str(self.dist_path),
                "--bundle",
                str(self.bundle_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(builder_res.returncode, 0)
        self.assertFalse(self.dist_path.exists(), "诊断快照缺失时不应生成 dist")

        # 5. check_site 确认检查失败并报告具体错误
        checker_res = subprocess.run(
            [sys.executable, str(CHECKER_SCRIPT), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(checker_res.returncode, 0)
        self.assertIn("站点检查失败", checker_res.stdout)

        failures = check_site.check_site(self.dist_path)
        self.assertGreater(len(failures), 0)
        self.assertTrue(any("站点路径不存在" in f for f in failures))

    def test_pipeline_with_injected_brief_schema_version_error_fails_check_site(self) -> None:
        """注入 brief schema版本逻辑错误（如版本篡改为过时版本 3），Builder 与 check_site 确认失败。"""
        # 生成初始 brief 并篡改 schema 版本为不兼容的旧版本
        brief_data = make_brief.build_brief()
        brief_data["schema版本"] = 3
        make_brief.write_brief(brief_data, self.brief_path)

        # DA 解析阶段直接拒绝旧 schema 版本的 brief
        da_res = subprocess.run(
            [
                sys.executable,
                str(DA_SCRIPT),
                "--brief",
                str(self.brief_path),
                "--raw",
                str(SAMPLE_RAW_DIR),
                "--out",
                str(self.diag_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(da_res.returncode, 0)
        self.assertIn("schema版本", da_res.stderr)
        self.assertFalse(self.diag_path.exists())

        # 若将携带该版本错误的诊断快照交付 Builder，build_site 拒绝构建且不产出 dist
        corrupted_diag = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
        corrupted_diag["scope"]["schema版本"] = 3
        self.diag_path.write_text(json.dumps(corrupted_diag, ensure_ascii=False), encoding="utf-8")

        builder_res = subprocess.run(
            [
                sys.executable,
                str(BUILDER_SCRIPT),
                "--data",
                str(self.diag_path),
                "--out",
                str(self.dist_path),
                "--bundle",
                str(self.bundle_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(builder_res.returncode, 0)
        self.assertIn("校验失败", builder_res.stderr)
        self.assertFalse(self.dist_path.exists())

        # check_site 确认站点不存在并报告失败
        checker_res = subprocess.run(
            [sys.executable, str(CHECKER_SCRIPT), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(checker_res.returncode, 0)
        self.assertIn("站点检查失败", checker_res.stdout)

    def test_pipeline_with_injected_defective_diagnosis_fails_check_site(self) -> None:
        """站点若包含由坏 brief 产生的坏数据快照，check_site 检出缺陷并精准定位。"""
        # 1. 正常构建出站点骨架
        self.assertEqual(
            build_site.main(
                [
                    "--data",
                    str(EXAMPLE_PATH),
                    "--out",
                    str(self.dist_path),
                    "--bundle",
                    str(self.bundle_path),
                ]
            ),
            build_site.EXIT_OK,
        )

        # 2. 注入坏 brief 导致的缺陷（如 scope.schema版本 被篡改为 3）并更新 build-report 摘要
        dist_diag_path = self.dist_path / "data" / "diagnosis.json"
        dist_data = json.loads(dist_diag_path.read_text(encoding="utf-8"))
        dist_data["scope"]["schema版本"] = 3
        dist_diag_path.write_text(json.dumps(dist_data, ensure_ascii=False), encoding="utf-8")
        build_site.write_report(self.dist_path, dist_diag_path)

        # 3. 运行 check_site 断言失败并定位到具体缺陷
        failures = check_site.check_site(self.dist_path)
        self.assertGreater(len(failures), 0)
        self.assertTrue(any("未通过 validate.py" in f for f in failures), failures)

        checker_res = subprocess.run(
            [sys.executable, str(CHECKER_SCRIPT), str(self.dist_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(checker_res.returncode, 0)
        self.assertIn("未通过 validate.py", checker_res.stdout)
        self.assertIn("站点检查失败", checker_res.stdout)


if __name__ == "__main__":
    unittest.main()
