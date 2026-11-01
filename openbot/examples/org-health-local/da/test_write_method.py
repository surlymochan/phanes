import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "write_method.py"
sys.path.insert(0, str(HERE))
import write_method

BRIEF = {
    "目标": "检查趋势",
    "观察期间": ["一月", "二月"],
    "口径": ["按任务书定义计算", "仅使用聚合字段"],
}
DIAGNOSIS = {
    "metrics": [
        {
            "标题": "活跃度",
            "数值": {"月度": {"一月": 120, "二月": None}},
        }
    ],
    "conclusion": {"判断": "需补证据", "趋势": {"一月": 8, "二月": None}},
}


def run_write_method(brief, diagnosis, raw_files=None, raw_dir=None):
    """跑一次 write_method.py，返回 (退出码, 标准错误, 输出内容)。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        brief_path = temp / "brief.json"
        data_path = temp / "diagnosis.json"
        out_path = temp / "method.md"
        brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        data_path.write_text(json.dumps(diagnosis, ensure_ascii=False), encoding="utf-8")
        command = [
            sys.executable,
            str(SCRIPT),
            "--brief",
            str(brief_path),
            "--data",
            str(data_path),
            "--out",
            str(out_path),
        ]
        if raw_files is not None:
            raw = temp / "raw"
            raw.mkdir()
            for name, text in raw_files.items():
                (raw / name).write_text(text, encoding="utf-8")
            command.extend(["--raw", str(raw)])
        elif raw_dir is not None:
            command.extend(["--raw", str(raw_dir)])
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        content = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        return result.returncode, result.stderr, content


class WriteMethodTest(unittest.TestCase):
    def test_preserves_missing_metric_period_as_gap(self):
        diagnosis = {
            "metrics": [
                {"标题": "活跃度", "数值": {"月度": {"一月": 120, "二月": None}}}
            ]
        }

        code, stderr, content = run_write_method(BRIEF, diagnosis)

        self.assertEqual(code, 0, stderr)
        self.assertIn("已知缺口：二月 为 null", content)
        self.assertNotIn("二月 为 0", content)
        self.assertNotIn("估算", content)

    def test_writes_complete_numeric_free_method(self):
        code, stderr, content = run_write_method(BRIEF, DIAGNOSIS)

        self.assertEqual(code, 0, stderr)
        self.assertIn("活跃度", content)
        self.assertIn("判断", content)
        self.assertIn("数据来源（字段路径）", content)
        self.assertIn("计算口径：引用任务书“口径”", content)
        self.assertIn("二月 为 null", content)
        self.assertNotIn("120", content)
        self.assertNotIn("8", content)

    def test_creates_missing_output_parent_directories(self):
        """--out 的父目录不存在时自动创建多层目录，并正常写出方法说明。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            brief_path = temp / "brief.json"
            data_path = temp / "diagnosis.json"
            out_path = temp / "reports" / "2026" / "09" / "method.md"
            brief_path.write_text(json.dumps(BRIEF, ensure_ascii=False), encoding="utf-8")
            data_path.write_text(json.dumps(DIAGNOSIS, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--brief",
                    str(brief_path),
                    "--data",
                    str(data_path),
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.parent.is_dir())
            self.assertTrue(out_path.is_file())
            content = out_path.read_text(encoding="utf-8")

        self.assertIn("# 数据口径说明", content)
        self.assertIn("活跃度", content)

    def test_brief_method_numeric_leak_fails_without_writing(self):
        brief = {**BRIEF, "口径": ["按任务书定义计算", "12 万元/月"]}
        diagnosis = {
            "metrics": [{"标题": "成本", "数值": {"月度": {"一月": 12}}}]
        }

        code, stderr, content = run_write_method(brief, diagnosis)

        self.assertEqual(code, 1)
        self.assertIn("生成文档包含诊断数值：12", stderr)
        self.assertEqual(content, "")

    def test_lists_raw_csv_files_and_row_counts(self):
        raw_files = {
            "heads.csv": "月份,整体折算人头\n2026-01,10\n2026-02,11\n2026-03,12\n",
            "cost.csv": "月份,整体技术人力成本万元\n2026-01,1.5\n",
            "fx.csv": "人民币每美元\n7.2\n",
            "notes.txt": "不是 CSV，不列入数据来源。\n",
        }

        code, stderr, content = run_write_method(BRIEF, DIAGNOSIS, raw_files=raw_files)

        self.assertEqual(code, 0, stderr)
        self.assertIn("## 数据来源", content)
        self.assertIn("- `cost.csv`：1 行数据（不含表头）", content)
        self.assertIn("- `fx.csv`：1 行数据（不含表头）", content)
        self.assertIn("- `heads.csv`：3 行数据（不含表头）", content)
        self.assertNotIn("notes.txt", content)
        # 按文件名排序，且该小节夹在观察期间与指标字段之间。
        self.assertLess(content.index("cost.csv"), content.index("fx.csv"))
        self.assertLess(content.index("fx.csv"), content.index("heads.csv"))
        self.assertLess(content.index("## 观察期间"), content.index("## 数据来源"))
        self.assertLess(content.index("## 数据来源"), content.index("## 指标与结论字段"))

    def test_mentions_missing_raw_dir(self):
        code, stderr, content = run_write_method(BRIEF, DIAGNOSIS)

        self.assertEqual(code, 0, stderr)
        self.assertIn("## 数据来源", content)
        self.assertIn("未提供原始数据目录", content)

    def test_missing_raw_dir_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "not-exist"
            code, stderr, content = run_write_method(BRIEF, DIAGNOSIS, raw_dir=missing)

        self.assertNotEqual(code, 0)
        self.assertIn("原始数据目录不存在", stderr)
        self.assertEqual(content, "")

    def test_row_counts_do_not_count_as_value_leak(self):
        raw_files = {"cost.csv": "月份,整体技术人力成本万元\n" + "".join(f"2026-0{index},1\n" for index in range(1, 9))}

        code, stderr, content = run_write_method(BRIEF, DIAGNOSIS, raw_files=raw_files)

        self.assertEqual(code, 0, stderr)
        self.assertIn("- `cost.csv`：8 行数据（不含表头）", content)

    def test_missing_brief_file_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            missing_brief = temp / "missing_brief.json"
            data_path = temp / "diagnosis.json"
            out_path = temp / "method.md"
            data_path.write_text(json.dumps(DIAGNOSIS, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--brief",
                    str(missing_brief),
                    "--data",
                    str(data_path),
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(missing_brief.name, result.stderr)
            self.assertNotIn("Traceback", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(out_path.exists())

    def test_corrupted_data_file_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            brief_path = temp / "brief.json"
            corrupted_data = temp / "corrupted_diagnosis.json"
            out_path = temp / "method.md"
            brief_path.write_text(json.dumps(BRIEF, ensure_ascii=False), encoding="utf-8")
            corrupted_data.write_text("{broken json", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--brief",
                    str(brief_path),
                    "--data",
                    str(corrupted_data),
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(corrupted_data.name, result.stderr)
            self.assertNotIn("Traceback", result.stdout)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(out_path.exists())

    def test_load_json_missing_file_raises_method_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "nonexistent.json"
            with self.assertRaises(write_method.MethodError) as ctx:
                write_method.load_json(missing)
            self.assertIn(str(missing), str(ctx.exception))

    def test_load_json_corrupted_json_raises_method_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            corrupted = Path(temp_dir) / "corrupted.json"
            corrupted.write_text("invalid json content", encoding="utf-8")
            with self.assertRaises(write_method.MethodError) as ctx:
                write_method.load_json(corrupted)
            self.assertIn(str(corrupted), str(ctx.exception))


class WarningsSectionTest(unittest.TestCase):
    """诊断快照带 warnings（脏 CSV 被跳过的坏行）时的方法说明。"""

    DIAGNOSIS_WITH_WARNINGS = {
        "metrics": [
            {"标题": "整体技术人力成本", "数值": {"2026-01实际": 10.4, "2026-05H1参考": None}}
        ],
        "conclusion": "整体技术人力成本由 10.4 万元/月变为 12.4 万元/月。",
        "warnings": [
            "原始数据 cost.csv 第 4 行的 整体技术人力成本万元 不是数值：待补（已跳过该行）",
            "原始数据 heads.csv 第 8 行的 整体折算人头 必须是非负数：-1（已跳过该行）",
        ],
    }

    def test_warnings_section_lists_every_warning(self):
        code, stderr, content = run_write_method(BRIEF, self.DIAGNOSIS_WITH_WARNINGS)

        self.assertEqual(code, 0, stderr)
        self.assertIn("## 数据质量警告", content)
        for warning in self.DIAGNOSIS_WITH_WARNINGS["warnings"]:
            self.assertIn(warning, content)
        self.assertIn("cost.csv", content)
        self.assertIn("heads.csv", content)

    def test_no_warnings_section_without_warnings(self):
        code, stderr, content = run_write_method(BRIEF, DIAGNOSIS)

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("## 数据质量警告", content)

    def test_warning_line_numbers_are_not_treated_as_leak(self):
        """坏行行号来自原始 CSV，不是诊断数值，不参与泄露比对。"""
        diagnosis = {
            "metrics": [{"标题": "整体技术人力成本", "数值": {"2026-01实际": 12}}],
            "warnings": ["原始数据 cost.csv 第 12 行的 整体技术人力成本万元 不是数值：待补（已跳过该行）"],
        }

        code, stderr, content = run_write_method(BRIEF, diagnosis)

        self.assertEqual(code, 0, stderr)
        self.assertIn("第 12 行", content)


class BriefPeriodFormTest(unittest.TestCase):
    """任务书 `观察期间` 的三种写法都能写进方法说明。"""

    BOSS_BRIEF = {
        "目标": "检查趋势",
        "观察期间": {
            "观察月份": "2026-09",
            "数据截止": "2026-09-30",
            "期次": ["2026-01实际", "2026-05H1参考", "2026-09实际", "年末测算", "年末目标"],
        },
        "口径": {"技术人力成本": "按月汇总，单位万元/月。", "汇率": 7.2},
    }

    def test_object_form_periods_are_listed(self):
        code, stderr, content = run_write_method(self.BOSS_BRIEF, DIAGNOSIS)

        self.assertEqual(code, 0, stderr)
        self.assertIn(
            "## 观察期间\n\n2026-01实际、2026-05H1参考、2026-09实际、年末测算、年末目标",
            content,
        )
        self.assertNotIn("'期次'", content)

    def test_object_form_periods_are_not_treated_as_leak(self):
        """期次标签是结构信息，不是诊断数值，不参与泄露比对。"""
        diagnosis = {
            "metrics": [{"标题": "整体技术人力成本", "数值": {"2026-01实际": 10.4}}],
            "conclusion": "整体技术人力成本由 10.4 万元/月变为 12.4 万元/月。",
        }

        code, stderr, content = run_write_method(self.BOSS_BRIEF, diagnosis)

        self.assertEqual(code, 0, stderr)
        self.assertIn("2026-01实际", content)
        self.assertNotIn("10.4", content)

    def test_calibration_rate_is_not_treated_as_leak(self):
        """任务书口径里的汇率不是诊断数值：口径照引，不判泄露。"""
        brief = {**BRIEF, "口径": ["按任务书定义计算", "汇率 7.2"]}
        diagnosis = {
            "metrics": [{"标题": "整体技术人力成本", "数值": {"一月": 10.4}}],
            "conclusion": "整体技术人力成本由 10.4 万元/月变为 12.4 万元/月。",
        }

        code, stderr, content = run_write_method(brief, diagnosis)

        self.assertEqual(code, 0, stderr)
        self.assertIn("汇率 7.2", content)


class MetricAnchorsTest(unittest.TestCase):
    """方法说明引用快照里的实际指标：标题、字段路径与期间锚点。"""

    DIAGNOSIS_WITH_SERIES = {
        "metrics": [
            {
                "标题": "整体技术人力成本",
                "数值": {
                    "2026-01实际": 10.4,
                    "2026-05H1参考": None,
                    "2026-09实际": 12.4,
                    "年末测算": 13.4,
                },
            },
            {
                "标题": "收入与成本率",
                "数值": {
                    "收入万美元每月": {
                        "2026-01实际": 11.2,
                        "2026-09实际": 13.6,
                        "年末目标": 15.2,
                    }
                },
            },
        ],
    }

    def test_period_anchors_are_listed(self):
        code, stderr, content = run_write_method(BRIEF, self.DIAGNOSIS_WITH_SERIES)

        self.assertEqual(code, 0, stderr)
        self.assertIn("期间锚点：`2026-01实际`、`2026-05H1参考`、`2026-09实际`、`年末测算`", content)
        self.assertIn("期间锚点：`2026-01实际`、`2026-09实际`、`年末目标`", content)

    def test_metric_titles_and_paths_are_listed(self):
        code, stderr, content = run_write_method(BRIEF, self.DIAGNOSIS_WITH_SERIES)

        self.assertEqual(code, 0, stderr)
        self.assertIn("### 整体技术人力成本", content)
        self.assertIn("### 收入与成本率（收入万美元每月）", content)
        self.assertIn("`metrics[0].数值`", content)
        self.assertIn("`metrics[1].数值.收入万美元每月`", content)

    def test_each_metric_cites_its_own_calibration(self):
        """任务书口径按指标标题分组时，每个指标只引用自己那段口径。"""
        brief = {
            "目标": "检查趋势",
            "观察期间": ["一月"],
            "口径": {
                "整体技术人力成本": {"单位": "万元/月", "定义": "技术人力成本按月汇总。"},
                "整体折算人头": {"单位": "人（当量）", "定义": "折算人头按当量折算。"},
            },
        }

        code, stderr, content = run_write_method(brief, self.DIAGNOSIS_WITH_SERIES)

        self.assertEqual(code, 0, stderr)
        cost_section = content[content.index("### 整体技术人力成本") : content.index("### 收入与成本率")]
        self.assertIn("单位：万元/月；定义：技术人力成本按月汇总。", cost_section)
        self.assertNotIn("人（当量）", cost_section)


if __name__ == "__main__":
    unittest.main()
