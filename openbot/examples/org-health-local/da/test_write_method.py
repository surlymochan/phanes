import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "write_method.py"
BUILD_SCRIPT = HERE / "build_diagnosis.py"
SCHEMA_PATH = HERE.parent / "schema" / "diagnosis.schema.json"
SAMPLE_RAW = HERE / "sample-raw"
sys.path.insert(0, str(HERE))
import write_method

sys.path.insert(0, str(HERE.parent))
import validate

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

PIPELINE_BRIEF = {
    "目标": ["复核技术人力成本趋势"],
    "观察期间": {
        "起始期次": "2026-01",
        "参考期次": "2026-05",
        "截止期次": "2026-09",
        "数据截止": "2026-09-30",
    },
    "口径": {"成本": "按月汇总"},
    "必答问题": ["整体技术人力成本如何变化？"],
    "数据需求": ["四张 CSV"],
    "禁止事项": ["禁止编造数字"],
    "schema版本": 4,
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
    def run_pipeline(self, mutate_raw):
        """用真实非空 CSV 先构建诊断，再写方法说明。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            raw_dir = folder / "raw"
            shutil.copytree(SAMPLE_RAW, raw_dir)
            mutate_raw(raw_dir)
            brief_path = folder / "brief.json"
            diagnosis_path = folder / "diagnosis.json"
            method_path = folder / "method.md"
            brief_path.write_text(json.dumps(PIPELINE_BRIEF, ensure_ascii=False), encoding="utf-8")

            built = subprocess.run(
                [sys.executable, str(BUILD_SCRIPT), "--brief", str(brief_path), "--raw", str(raw_dir), "--out", str(diagnosis_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if built.returncode:
                return built, None
            diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            self.assertEqual(validate.validate_data(diagnosis, schema), [])
            written = subprocess.run(
                [sys.executable, str(SCRIPT), "--brief", str(brief_path), "--data", str(diagnosis_path), "--raw", str(raw_dir), "--out", str(method_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            return written, method_path.read_text(encoding="utf-8") if method_path.exists() else None

    def test_nonempty_dirty_pipeline_recovers_missing_period(self):
        """非空 CSV 缺一期时保留 null，并仍能产出可校验诊断与方法说明。"""
        def remove_period(raw_dir):
            revenue = raw_dir / "revenue.csv"
            lines = revenue.read_text(encoding="utf-8").splitlines()
            revenue.write_text("\n".join(line for line in lines if not line.startswith("2026-06,")) + "\n", encoding="utf-8")

        result, content = self.run_pipeline(remove_period)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已知缺口", content)
        self.assertIn("`revenue.csv`：8 行数据", content)
        self.assertIn("数据来源", content)

    def test_nonempty_dirty_pipeline_hard_fails_on_negative_cost(self):
        """非空 CSV 出现负数成本时非零退出，并点名文件与列。"""
        def make_negative(raw_dir):
            cost = raw_dir / "cost.csv"
            lines = cost.read_text(encoding="utf-8").splitlines()
            cost.write_text("\n".join(line.replace("2026-01,10.4", "2026-01,-10.4") for line in lines) + "\n", encoding="utf-8")

        result, content = self.run_pipeline(make_negative)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cost.csv", result.stderr)
        self.assertIn("整体技术人力成本万元", result.stderr)
        self.assertIsNone(content)

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

    def test_traceable_insight_number_writes_questions_and_definitions(self):
        diagnosis = {
            "metrics": [{"标题": "活跃度", "数值": {"一月": 120}}],
            "insights": [{"类型": "事实", "标题": "一月活跃度", "内容": "一月为 120。"}],
            "definitions": ["活跃度为当期聚合值。"],
        }

        code, stderr, content = run_write_method(BRIEF, diagnosis)

        self.assertEqual(code, 0, stderr)
        self.assertIn("## 问题回答", content)
        self.assertIn("## 定义", content)
        self.assertIn("一月活跃度", content)

    def test_untraceable_insight_number_is_structured_error(self):
        diagnosis = {
            "metrics": [{"标题": "活跃度", "数值": {"一月": 120}}],
            "insights": [{"类型": "事实", "标题": "伪造", "内容": "一月为 999。"}],
        }

        code, stderr, content = run_write_method(BRIEF, diagnosis)

        self.assertEqual(code, 1)
        error = json.loads(stderr)
        self.assertEqual(error["error"], "untraceable_numeric_reference")
        self.assertEqual(error["details"][0]["path"], "insights[0].内容")
        self.assertEqual(content, "")

    def test_empty_diagnosis_fails_instead_of_writing_empty_method(self):
        code, stderr, content = run_write_method(BRIEF, {})

        self.assertEqual(code, 1)
        self.assertIn("empty_diagnosis", stderr)
        self.assertEqual(content, "")

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

    def test_brief_method_keeps_ordinary_numbers_without_leak_check(self):
        brief = {**BRIEF, "口径": ["按任务书定义计算", "第 12 版方法，按 12 个月滚动"]}
        diagnosis = {
            "metrics": [{"标题": "成本", "数值": {"月度": {"一月": 12}}}]
        }

        code, stderr, content = run_write_method(brief, diagnosis)

        self.assertEqual(code, 0, stderr)
        self.assertIn("第 12 版方法，按 12 个月滚动", content)

    def test_special_method_text_is_one_safe_code_block(self):
        brief = {
            **BRIEF,
            "口径": "第一行 **加粗**\n第二行含 `反引号` 和 ``` 代码标记\n### 不应成为标题",
        }
        code, stderr, content = run_write_method(brief, DIAGNOSIS)

        self.assertEqual(code, 0, stderr)
        method_start = content.index("## 统一口径")
        method_end = content.index("## 观察期间")
        method_section = content[method_start:method_end]
        self.assertIn("第一行 **加粗**", method_section)
        self.assertIn("第二行含 `反引号` 和 ``` 代码标记", method_section)
        self.assertIn("### 不应成为标题", method_section)
        self.assertEqual(method_section.count("## 观察期间"), 0)
        self.assertEqual(method_section.count("````"), 2)
        self.assertIn("\n### 不应成为标题\n````", method_section)

    def test_demo_artifacts_write_method_without_textual_number_false_positive(self):
        brief_path = HERE.parent / "demo" / "brief.json"
        diagnosis_path = HERE.parent / "demo" / "diagnosis.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "da-method.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--brief",
                    str(brief_path),
                    "--data",
                    str(diagnosis_path),
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.is_file())
            content = out_path.read_text(encoding="utf-8")

        self.assertIn("## 观察期间", content)
        self.assertIn("## 数据来源", content)
        self.assertIn("## 指标与结论字段", content)

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


if __name__ == "__main__":
    unittest.main()
