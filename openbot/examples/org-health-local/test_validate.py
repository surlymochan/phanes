"""validate.py 的单元测试。

合成示例必须通过；缺字段、错误版本、错误类型各有一个失败用例。
口径说明 是可选字段：缺省、合法值都通过，类型不对时失败。
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import validate  # noqa: E402

EXAMPLE_PATH = ROOT / "data" / "diagnosis.json"
SCHEMA_PATH = ROOT / "schema" / "diagnosis.schema.json"


def load_json(path: Path):
    """读取 JSON 文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


class ValidateTest(unittest.TestCase):
    """按结构定义校验合成示例。"""

    def setUp(self) -> None:
        self.schema = load_json(SCHEMA_PATH)
        self.validator = validate.Validator(self.schema)
        self.data = load_json(EXAMPLE_PATH)

    def errors(self, data) -> list[str]:
        """返回校验错误列表。"""
        return self.validator.errors(data)

    def test_schema_uses_draft_2020_12(self) -> None:
        """结构定义声明 JSON Schema draft 2020-12。"""
        self.assertEqual(self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_example_passes(self) -> None:
        """合成示例通过校验。"""
        self.assertEqual(self.errors(self.data), [])

    def test_missing_field_fails(self) -> None:
        """缺少必需字段时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        del broken["scope"]["来源"]
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/来源" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_wrong_version_fails(self) -> None:
        """schema版本 不是 4 时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 3
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/schema版本" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_wrong_type_fails(self) -> None:
        """数值序列出现字符串时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["metrics"][0]["数值"]["2026-01实际"] = "十二"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/metrics/0/数值/2026-01实际" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_calibration_note_is_optional(self) -> None:
        """口径说明 在定义里声明为可选字段：出现在 properties 里，不在 required 里。"""
        self.assertIn("口径说明", self.schema["properties"])
        self.assertNotIn("口径说明", self.schema["required"])

    def test_calibration_note_absent_passes(self) -> None:
        """缺省口径说明 时通过校验。"""
        without = copy.deepcopy(self.data)
        del without["口径说明"]
        self.assertEqual(self.errors(without), [])

    def test_calibration_note_valid_passes(self) -> None:
        """口径说明 为非空字符串数组时通过校验。"""
        valid = copy.deepcopy(self.data)
        valid["口径说明"] = ["期间：2026-01 与 2026-09 为实际期次。", "null 含义：未统计。"]
        self.assertEqual(self.errors(valid), [])

    def test_calibration_note_wrong_type_fails(self) -> None:
        """口径说明 类型不对时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["口径说明"] = "口径：2026-01 与 2026-09 为实际期次。"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/口径说明" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_calibration_note_empty_item_fails(self) -> None:
        """口径说明 里出现空字符串时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["口径说明"] = ["期间说明", ""]
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/口径说明/1" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_command_line_reports_calibration_note_type_error(self) -> None:
        """命令行校验口径说明 类型错误的数据时退出码非 0，并打印出错路径。"""
        broken = copy.deepcopy(self.data)
        broken["口径说明"] = 2026
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/口径说明", result.stdout + result.stderr)

    def test_command_line_accepts_example(self) -> None:
        """命令行校验合成示例时退出码为 0。"""
        result = subprocess.run(
            [sys.executable, str(ROOT / "validate.py"), str(EXAMPLE_PATH)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_command_line_reports_failure(self) -> None:
        """命令行校验失败数据时退出码非 0，并打印出错路径。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 3
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/scope/schema版本", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
