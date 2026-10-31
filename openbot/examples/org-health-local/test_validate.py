"""validate.py 的单元测试。

合成示例必须通过；缺字段、错误版本、错误类型各有一个失败用例。
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

    def test_optional_calibration_can_be_omitted(self) -> None:
        """口径说明为可选字段，字段缺失时仍可通过校验（不改变旧数据接受范围）。"""
        data = copy.deepcopy(self.data)
        data.pop("口径说明", None)
        self.assertEqual(self.errors(data), [])

    def test_invalid_calibration_fails(self) -> None:
        """口径说明包含空字符串或非法格式时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["口径说明"] = [""]
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/口径说明/0" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

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
