import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "write_method.py"


class WriteMethodTest(unittest.TestCase):
    def test_writes_complete_numeric_free_method(self):
        brief = {
            "目标": "检查趋势",
            "观察期间": ["一月", "二月"],
            "口径": ["按任务书定义计算", "仅使用聚合字段"],
        }
        diagnosis = {
            "metrics": [
                {
                    "标题": "活跃度",
                    "数值": {"月度": {"一月": 120, "二月": None}},
                }
            ],
            "conclusion": {"判断": "需补证据", "趋势": {"一月": 8, "二月": None}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            brief_path = temp / "brief.json"
            data_path = temp / "diagnosis.json"
            out_path = temp / "method.md"
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            data_path.write_text(json.dumps(diagnosis, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--brief", str(brief_path), "--data", str(data_path), "--out", str(out_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            content = out_path.read_text(encoding="utf-8")

        self.assertIn("活跃度", content)
        self.assertIn("判断", content)
        self.assertIn("数据来源（字段路径）", content)
        self.assertIn("计算口径：引用任务书“口径”", content)
        self.assertIn("二月 为 null", content)
        self.assertNotIn("120", content)
        self.assertNotIn("8", content)


if __name__ == "__main__":
    unittest.main()
