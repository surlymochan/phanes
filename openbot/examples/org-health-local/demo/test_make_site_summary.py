import contextlib
import importlib.util
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("make_site_summary.py")
SPEC = importlib.util.spec_from_file_location("make_site_summary", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
make_site_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(make_site_summary)


class MakeSiteSummaryTests(unittest.TestCase):
    def test_main_generates_summary_for_default_site(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site = Path(temp_dir) / "site"
            (site / "data").mkdir(parents=True)
            source_root = SCRIPT_PATH.parent.parent
            (site / "index.html").write_bytes((source_root / "index.html").read_bytes())
            (site / "data" / "diagnosis.json").write_bytes(
                (SCRIPT_PATH.parent / "diagnosis.json").read_bytes()
            )
            output = Path(temp_dir) / "site-summary.md"
            with mock.patch.object(make_site_summary, "DEFAULT_SITE", site):
                result = make_site_summary.main(["--out", str(output)])

            self.assertEqual(result, make_site_summary.EXIT_OK)
            summary = output.read_text(encoding="utf-8")
            self.assertIn("## 2. 指标卡片（#metric-grid 共 6 张", summary)
            self.assertIn("## 3. 口径说明（“数据口径”折叠面板 #calibration-panel，共 10 条）", summary)
            self.assertIn("## 4. 各区块条数", summary)
            self.assertIn("洞察（#insight-grid）：15 条", summary)
            self.assertIn("建议（#recommendations）：4 条", summary)
            self.assertIn("证据缺口与风险（#risk-grid）：5 条", summary)
            self.assertIn("来源口径（#definitions-grid）：4 条", summary)

    def test_main_reports_missing_index_as_input_error(self):
        self.assert_missing_site_file("index.html")

    def test_main_reports_missing_diagnosis_as_input_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            site = Path(temp_dir) / "site"
            site.mkdir()
            (site / "index.html").write_text("<html></html>", encoding="utf-8")
            result, stderr = self.run_main_for_site(site)

        self.assertEqual(result, make_site_summary.EXIT_INPUT)
        self.assertIn("先构建站点", stderr)

    def assert_missing_site_file(self, filename):
        with tempfile.TemporaryDirectory() as temp_dir:
            site = Path(temp_dir) / "site"
            site.mkdir()
            if filename == "diagnosis.json":
                (site / "index.html").write_text("<html></html>", encoding="utf-8")
            result, stderr = self.run_main_for_site(site)

        self.assertEqual(result, make_site_summary.EXIT_INPUT)
        self.assertIn("先构建站点", stderr)

    @staticmethod
    def run_main_for_site(site):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = make_site_summary.main(
                ["--site", str(site), "--out", str(site / "summary.md")]
            )
        return result, stderr.getvalue()

    def test_format_helpers_handle_empty_and_boundary_values(self):
        self.assertEqual(make_site_summary.format_number(1234.567, 2), "1,234.57")
        self.assertEqual(make_site_summary.format_number(-0.005, 2), "-0.01")
        self.assertEqual(make_site_summary.format_number(0, 2), "0")

        self.assertEqual(make_site_summary.format_value(None, 1), "未提供")
        self.assertEqual(make_site_summary.format_value("约 2 万元", 1), "约 2 万元")
        self.assertEqual(make_site_summary.format_value(12.34, 1), "12.3")

        self.assertEqual(make_site_summary.difference({}, "start", "end", 1), "未提供")
        self.assertEqual(
            make_site_summary.difference({"start": 10, "end": 12.34}, "start", "end", 1),
            "+2.3",
        )
        self.assertEqual(
            make_site_summary.difference({"start": 10, "end": 8}, "start", "end", 1),
            "-2",
        )
        self.assertEqual(
            make_site_summary.difference({"start": 10, "end": 10}, "start", "end", 1),
            "0",
        )

        self.assertEqual(make_site_summary.percent_change({}, "start", "end"), "未提供")
        self.assertEqual(
            make_site_summary.percent_change({"start": 0, "end": 1}, "start", "end"),
            "未提供",
        )
        self.assertEqual(
            make_site_summary.percent_change({"start": 10, "end": 12.34}, "start", "end"),
            "+23.4%",
        )
        self.assertEqual(
            make_site_summary.percent_change({"start": 10, "end": 8}, "start", "end"),
            "-20%",
        )
        self.assertEqual(
            make_site_summary.percent_change({"start": 10, "end": 10}, "start", "end"),
            "0%",
        )


if __name__ == "__main__":
    unittest.main()
