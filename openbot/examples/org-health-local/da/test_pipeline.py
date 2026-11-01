"""build_diagnosis.py 与 write_method.py 的串联测试。

用一份含坏行与好行的原始 CSV 跑通 DA 管道：诊断快照照常生成，坏行进可审计警告，
方法说明引用快照里的实际指标。任务书用 boss/make_brief.py 生成的 Boss 格式，
与 DA 流水线实际吃到的输入一致；只用 Python 标准库，不依赖网络、Bun 与真实组织数据。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = PACKAGE_DIR.parent
for entry in (str(EXAMPLE_DIR), str(PACKAGE_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import validate  # noqa: E402

BUILD_SCRIPT = PACKAGE_DIR / "build_diagnosis.py"
METHOD_SCRIPT = PACKAGE_DIR / "write_method.py"
MAKE_BRIEF = EXAMPLE_DIR / "boss" / "make_brief.py"
SAMPLE_RAW = PACKAGE_DIR / "sample-raw"
SCHEMA_PATH = EXAMPLE_DIR / "schema" / "diagnosis.schema.json"

# 坏行：成本行里的取值不是数值。好行与坏行同一张表、同一批月份，用来对比取数结果。
CLEAN_COST_ROW = "2026-03,10.8,8.6,2.2"
DIRTY_COST_ROW = "2026-03,待补,8.6,2.2"


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


class DirtyCsvPipelineTest(unittest.TestCase):
    """脏 CSV 上的 DA 管道：build_diagnosis.py → write_method.py。"""

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.name)
        self.raw_dir = self.folder / "raw"
        shutil.copytree(SAMPLE_RAW, self.raw_dir)
        cost = self.raw_dir / "cost.csv"
        cost.write_text(
            cost.read_text(encoding="utf-8").replace(CLEAN_COST_ROW, DIRTY_COST_ROW),
            encoding="utf-8",
        )
        self.brief_path = self.folder / "brief.json"
        generated = subprocess.run(
            [sys.executable, str(MAKE_BRIEF), "--out", str(self.brief_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.diagnosis_path = self.folder / "diagnosis.json"
        self.method_path = self.folder / "method.md"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_diagnosis(self) -> subprocess.CompletedProcess:
        """跑 build_diagnosis.py，返回命令行结果。"""
        return subprocess.run(
            [
                sys.executable,
                str(BUILD_SCRIPT),
                "--brief",
                str(self.brief_path),
                "--raw",
                str(self.raw_dir),
                "--out",
                str(self.diagnosis_path),
            ],
            capture_output=True,
            text=True,
        )

    def write_method(self) -> subprocess.CompletedProcess:
        """跑 write_method.py，返回命令行结果。"""
        return subprocess.run(
            [
                sys.executable,
                str(METHOD_SCRIPT),
                "--brief",
                str(self.brief_path),
                "--data",
                str(self.diagnosis_path),
                "--raw",
                str(self.raw_dir),
                "--out",
                str(self.method_path),
            ],
            capture_output=True,
            text=True,
        )

    def test_dirty_csv_still_produces_valid_snapshot(self) -> None:
        """含坏行的原始 CSV 仍生成通过结构校验的诊断快照，好行照常取数。"""
        result = self.build_diagnosis()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.diagnosis_path.is_file())

        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))
        self.assertEqual(validate.validate_data(data, self.schema), [])
        self.assertEqual(data["scope"]["schema版本"], 4)
        self.assertEqual(data["scope"]["观察月份"], "2026-09")

        # 好行照常进入指标；坏行所在期次按缺数据处理，不填估算值。
        overall_cost = data["metrics"][0]["数值"]
        self.assertEqual(overall_cost["2026-01实际"], 10.4)
        self.assertEqual(overall_cost["2026-09实际"], 12.4)
        self.assertIsNone(overall_cost["2026-05H1参考"])
        self.assertIn("整体技术人力成本 缺 2026-03、2026-05 数据，按 null 处理", data["口径说明"])

    def test_dirty_row_enters_auditable_warnings(self) -> None:
        """坏行进可审计警告：文件、行号、期次、列名、取值与原因都能查到。"""
        result = self.build_diagnosis()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))

        warning = "原始数据 cost.csv 第 4 行（2026-03）的 整体技术人力成本万元 不是数值（取值“待补”）"
        notes = [note for note in data["口径说明"] if note.startswith("原始数据问题行已跳过：")]
        self.assertEqual(len(notes), 1)
        self.assertIn(warning, notes[0])

        risks = [risk for risk in data["risks"] if risk["标题"] == "原始数据 cost.csv 第 4 行已跳过"]
        self.assertEqual(len(risks), 1)
        self.assertIn(warning, risks[0]["内容"])

        insights = [item for item in data["insights"] if item["标题"] == "原始数据存在被跳过的问题行"]
        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0]["类型"], "边界")
        self.assertIn(warning, insights[0]["内容"])

        recommendations = [
            item for item in data["recommendations"] if item["标题"] == "修正原始数据中被跳过的问题行"
        ]
        self.assertEqual(len(recommendations), 1)
        for field in ("优先级", "标题", "依据", "缺失证据", "验证方式"):
            self.assertTrue(recommendations[0][field].strip())

        # 命令行也留一份提示，便于人工核对。
        self.assertIn("警告：原始数据 cost.csv 第 4 行", result.stderr)

    def test_method_note_references_actual_metrics(self) -> None:
        """方法说明引用快照里的实际指标与字段路径，且不复述诊断数值。"""
        built = self.build_diagnosis()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))

        result = self.write_method()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = self.method_path.read_text(encoding="utf-8")

        # 实际指标：快照里的四个指标标题与字段路径都在方法说明里。
        for metric in data["metrics"]:
            self.assertIn(f"### {metric['标题']}", content)
        self.assertIn("`metrics[0].数值`", content)
        self.assertIn("`metrics[3].数值.整体技术人力成本率`", content)
        # 每个指标按标题引用任务书口径里的同名条目。
        self.assertIn("计算口径：引用任务书“口径”的「整体技术人力成本」条目", content)
        self.assertIn("计算口径：引用任务书“口径”的「收入与成本率」条目", content)
        # 数据来源列出原始 CSV 与行数：坏行仍在文件里，快照只是跳过它。
        self.assertIn("- `cost.csv`：8 行数据（不含表头）", content)
        # 不复述诊断数值。
        for value in ("10.4", "12.4", "13.4", "11.2", "13.6"):
            self.assertNotIn(value, content)


if __name__ == "__main__":
    unittest.main()
