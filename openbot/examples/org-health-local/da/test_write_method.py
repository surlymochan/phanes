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


if __name__ == "__main__":
    unittest.main()
