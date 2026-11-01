"""DA 管道端到端测试：脏 CSV → 诊断快照 → 方法说明。

`da/build_diagnosis.py --skip-bad-rows` 跳过原始数据里的坏行，把问题写进快照的
`warnings`；`da/write_method.py` 消费这份快照生成方法说明：说明引用快照里的实际指标，
并把坏行列成可审计的“数据质量警告”。

原始数据是 `da/sample-raw/` 的合成样本加几行坏行，只用标准库，不引入真实数据。
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
SAMPLE_RAW = PACKAGE_DIR / "sample-raw"
SCHEMA_PATH = EXAMPLE_DIR / "schema" / "diagnosis.schema.json"

# Boss 格式的任务书：观察期间是对象，口径按指标标题分组，与 da/build_diagnosis.py 的消费方式一致。
BRIEF = {
    "目标": ["复核技术人力成本与成本率的变化"],
    "观察期间": {
        "观察月份": "2026-09",
        "数据截止": "2026-09-30",
        "期次": ["2026-01实际", "2026-05H1参考", "2026-09实际", "年末测算", "年末目标"],
    },
    "口径": {
        "技术人力成本": "技术人力成本=固定薪酬+社保公积金+外包人力，按月汇总，单位万元/月。",
        "折算人头": "折算人头=当月投入技术交付的人力当量，1 人月=1 个当量，非自然人数。",
        "收入": "收入=当月确认的营收业务收入，单位万美元/月。",
        "成本率": "成本率=技术人力成本÷同期收入按汇率折人民币。",
        "汇率": 7.2,
    },
    "必答问题": ["整体技术人力成本从期初到期末如何变化？", "技术人力成本率是否改善？"],
    "数据需求": ["原始目录里的四张 CSV"],
    "禁止事项": ["禁止编造数字"],
    "schema版本": 4,
}

# 坏行样本：数值不是数值、字段数多于表头、期次重复、人头为负数。
DIRTY_COST_VALUE = "2026-03,待补,8.6,2.2"
DIRTY_COST_FIELDS = "2026-04,11.0,8.8,2.2,备注"
DIRTY_COST_DUPLICATE = "2026-01,10.4,8.3,2.1"
DIRTY_HEADS_NEGATIVE = "2026-07,-1,9.4"


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


def write_csv_lines(path: Path, lines: list[str]) -> None:
    """写回 CSV 的文本行。"""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def csv_lines(path: Path) -> list[str]:
    """读出 CSV 的文本行。"""
    return path.read_text(encoding="utf-8").splitlines()


def drop_csv_column(text: str, column: str) -> str:
    """从 CSV 文本里删掉一列：表头与每行的取值一起去掉。"""
    lines = text.splitlines()
    index = lines[0].split(",").index(column)
    kept = []
    for line in lines:
        cells = line.split(",")
        kept.append(",".join(cells[:index] + cells[index + 1 :]))
    return "\n".join(kept) + "\n"


class DirtyCsvPipelineTest(unittest.TestCase):
    """脏 CSV 上的 DA 管道：坏行可恢复，问题可审计，方法说明引用实际指标。"""

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.name)
        self.brief_path = self.folder / "brief.json"
        self.brief_path.write_text(
            json.dumps(BRIEF, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.raw_dir = self.folder / "raw"
        shutil.copytree(SAMPLE_RAW, self.raw_dir)
        self.diagnosis_path = self.folder / "diagnosis.json"
        self.method_path = self.folder / "method.md"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dirty_raw(self) -> Path:
        """在合成样本里插入坏行：数值非法、字段数不符、期次重复、人头为负。

        2026-03 的好行被换成坏值（该月因此按缺数据处理），2026-04 多一行字段数不符的
        脏行，2026-01 多一行重复期次，2026-07 的折算人头为负数。
        """
        cost = self.raw_dir / "cost.csv"
        lines = csv_lines(cost)
        lines[3] = DIRTY_COST_VALUE
        lines.insert(4, DIRTY_COST_FIELDS)
        lines.append(DIRTY_COST_DUPLICATE)
        write_csv_lines(cost, lines)
        heads = self.raw_dir / "heads.csv"
        head_lines = csv_lines(heads)
        head_lines[6] = DIRTY_HEADS_NEGATIVE
        write_csv_lines(heads, head_lines)
        return self.raw_dir

    def build(self, skip_bad_rows: bool = True) -> subprocess.CompletedProcess:
        """跑一次 build_diagnosis.py。"""
        command = [
            sys.executable,
            str(BUILD_SCRIPT),
            "--brief",
            str(self.brief_path),
            "--raw",
            str(self.raw_dir),
            "--out",
            str(self.diagnosis_path),
        ]
        if skip_bad_rows:
            command.append("--skip-bad-rows")
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def write_method(self) -> subprocess.CompletedProcess:
        """跑一次 write_method.py，读取上一步生成的诊断快照。"""
        return subprocess.run(
            [
                sys.executable,
                str(METHOD_SCRIPT),
                "--brief",
                str(self.brief_path),
                "--data",
                str(self.diagnosis_path),
                "--out",
                str(self.method_path),
                "--raw",
                str(self.raw_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_snapshot_is_generated_from_dirty_csv(self) -> None:
        """脏 CSV 仍生成诊断快照，且快照通过结构校验。"""
        self.dirty_raw()

        result = self.build()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.diagnosis_path.is_file())
        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))
        self.assertEqual(validate.validate_data(data, self.schema), [])

    def test_bad_rows_become_auditable_warnings(self) -> None:
        """坏行不中断流程，而是作为可审计的 warnings 写进快照。"""
        self.dirty_raw()

        result = self.build()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))
        warnings = data["warnings"]
        self.assertEqual(len(warnings), 4)
        joined = "\n".join(warnings)
        self.assertIn("cost.csv", joined)
        self.assertIn("整体技术人力成本万元 不是数值：待补", joined)
        self.assertIn("cost.csv 第 5 行的字段数多于表头", joined)
        self.assertIn("heads.csv", joined)
        self.assertIn("非负数", joined)
        # 每条警告都指明文件与行号，便于回原始 CSV 逐条核对。
        for warning in warnings:
            self.assertIn("原始数据 ", warning)
            self.assertIn("已跳过该行", warning)
        # 坏行同样在命令行输出里可见，不只留在快照里。
        self.assertIn("已跳过该行", result.stderr)

    def test_good_rows_are_still_used(self) -> None:
        """坏行被跳过，好行照常取数：期末数值与缺口说明都不受坏行影响。"""
        self.dirty_raw()

        result = self.build()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))
        overall_cost = data["metrics"][0]["数值"]
        self.assertEqual(overall_cost["2026-09实际"], 12.4)
        self.assertEqual(overall_cost["2026-01实际"], 10.4)
        # 被跳过的 2026-03 按缺数据处理，不拿估算值补。
        self.assertIn("整体技术人力成本 缺 2026-03、2026-05 数据，按 null 处理", data["口径说明"])
        self.assertIn("整体折算人头 缺 2026-05、2026-07 数据，按 null 处理", data["口径说明"])

    def test_missing_column_still_fails(self) -> None:
        """表头缺列是输入错误：即便开了跳过坏行也不放行。"""
        cost = self.raw_dir / "cost.csv"
        write_csv_lines(
            cost, drop_csv_column(cost.read_text(encoding="utf-8"), "运维设施成本万元").splitlines()
        )

        result = self.build()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少必需列：运维设施成本万元", result.stdout + result.stderr)
        self.assertFalse(self.diagnosis_path.exists())

    def test_pipeline_without_skip_flag_stops_at_build(self) -> None:
        """不开跳过坏行时，管道在构建这步就停住，不产出看似成功的快照。"""
        self.dirty_raw()

        result = self.build(skip_bad_rows=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cost.csv", result.stdout + result.stderr)
        self.assertFalse(self.diagnosis_path.exists())
        self.assertFalse(self.method_path.exists())

    def test_method_note_references_actual_metrics(self) -> None:
        """方法说明引用快照里的实际指标：标题、字段路径与期间锚点。"""
        self.dirty_raw()
        self.assertEqual(self.build().returncode, 0)

        result = self.write_method()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = self.method_path.read_text(encoding="utf-8")
        self.assertIn("## 指标与结论字段", content)
        self.assertIn("### 整体技术人力成本", content)
        self.assertIn("### 整体折算人头", content)
        self.assertIn("### 营收业务（含共享平台）技术人力成本与折算人头（成本）", content)
        self.assertIn("### 收入与成本率（整体技术人力成本率）", content)
        self.assertIn("`metrics[0].数值`", content)
        self.assertIn("期间锚点：`2026-01实际`、`2026-05H1参考`、`2026-09实际`、`年末测算`", content)
        self.assertIn("已知缺口：2026-05H1参考 为 null", content)
        # 观察期间按任务书的期次标签写，不复述字典 repr。
        self.assertIn("## 观察期间\n\n2026-01实际、2026-05H1参考、2026-09实际、年末测算、年末目标", content)
        self.assertNotIn("'起始期次'", content)

    def test_method_note_lists_bad_rows_for_audit(self) -> None:
        """方法说明列出坏行，与快照的 warnings 一致，便于回原始 CSV 核对。"""
        self.dirty_raw()
        self.assertEqual(self.build().returncode, 0)
        data = json.loads(self.diagnosis_path.read_text(encoding="utf-8"))

        result = self.write_method()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = self.method_path.read_text(encoding="utf-8")
        self.assertIn("## 数据质量警告", content)
        for warning in data["warnings"]:
            self.assertIn(warning, content)

    def test_method_note_stays_numeric_free(self) -> None:
        """方法说明只说口径与缺口，不复述诊断数值。"""
        self.dirty_raw()
        self.assertEqual(self.build().returncode, 0)

        result = self.write_method()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = self.method_path.read_text(encoding="utf-8")
        self.assertNotIn("12.4", content)
        self.assertNotIn("9.9", content)
        self.assertNotIn("13.6", content)

    def test_pipeline_output_is_deterministic(self) -> None:
        """同一份任务书与脏数据两次跑出同一份快照与同一份方法说明。"""
        self.dirty_raw()
        self.assertEqual(self.build().returncode, 0)
        first_diagnosis = self.diagnosis_path.read_bytes()
        self.assertEqual(self.write_method().returncode, 0)
        first_method = self.method_path.read_bytes()

        self.assertEqual(self.build().returncode, 0)
        self.assertEqual(self.write_method().returncode, 0)

        self.assertEqual(self.diagnosis_path.read_bytes(), first_diagnosis)
        self.assertEqual(self.method_path.read_bytes(), first_method)


if __name__ == "__main__":
    unittest.main()
