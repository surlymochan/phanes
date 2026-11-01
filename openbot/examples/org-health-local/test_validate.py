"""validate.py 的单元测试。

合成示例必须通过；缺字段、错误版本、错误类型各有一个失败用例。
口径说明 是可选字段：缺省、合法值都通过，类型不对时失败。
另有结构定义边界用例：缺 scope、schema版本 号形态错误、指标数值为字符串、
多余顶层字段（按结构定义是否声明该字段判定）。
"""
import copy
import json
import os
import re
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
BRIEF_PATH = ROOT / "demo" / "brief.json"


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
    """按结构定义校验合成示例与边界情况。"""

    def setUp(self) -> None:
        self.schema = load_json(SCHEMA_PATH)
        self.validator = validate.Validator(self.schema)
        self.data = load_json(EXAMPLE_PATH)

    def test_resolve_rejects_external_reference(self) -> None:
        """解析非文档内引用时抛出 ValueError。"""
        with self.assertRaisesRegex(ValueError, "只支持文档内引用"):
            self.validator._resolve("http://example.com/schema.json")

    def test_resolve_rejects_missing_internal_reference(self) -> None:
        """解析不存在的文档内路径时抛出 ValueError。"""
        with self.assertRaisesRegex(ValueError, "引用不存在"):
            self.validator._resolve("#/$defs/missing_type")

    def test_resolve_rejects_non_object_reference_target(self) -> None:
        """引用目标不是结构对象时抛出 ValueError。"""
        validator = validate.Validator({"$defs": {"string_type": "文本", "list_type": []}})

        with self.assertRaisesRegex(ValueError, "引用不是结构对象"):
            validator._resolve("#/$defs/string_type")
        with self.assertRaisesRegex(ValueError, "引用不是结构对象"):
            validator._resolve("#/$defs/list_type")

    def test_all_of_reports_each_failing_branch(self) -> None:
        """allOf 任一分支失败，并保留各分支说明对应的错误信息。"""
        validator = validate.Validator(
            {
                "allOf": [
                    {"type": "object", "required": ["基础信息"]},
                    {"type": "object", "required": ["诊断结论"]},
                ]
            }
        )

        errors = validator.errors({})

        self.assertIn("/基础信息: 缺少必需字段", errors)
        self.assertIn("/诊断结论: 缺少必需字段", errors)

    def test_any_of_reports_no_matching_candidate(self) -> None:
        """anyOf 所有候选分支都不匹配时报告候选结构错误。"""
        validator = validate.Validator(
            {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        )

        errors = validator.errors(True)

        self.assertTrue(any("不满足任何一个候选结构" in error for error in errors))

    def test_one_of_reports_zero_matching_candidates(self) -> None:
        """oneOf 没有匹配分支时报告匹配数量为 0。"""
        validator = validate.Validator(
            {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        )

        errors = validator.errors(True)

        self.assertTrue(any("满足的候选结构数量为 0，期望 1 个" in error for error in errors))

    def test_one_of_reports_two_matching_candidates(self) -> None:
        """oneOf 有两个匹配分支时报告匹配数量为 2。"""
        validator = validate.Validator(
            {"oneOf": [{"type": "number"}, {"type": "integer"}]}
        )

        errors = validator.errors(1)

        self.assertTrue(any("满足的候选结构数量为 2，期望 1 个" in error for error in errors))

    def test_prefix_items_then_items_validate_array_positions(self) -> None:
        """prefixItems 按位置校验前缀，剩余元素按 items 校验。"""
        validator = validate.Validator(
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": {"type": "boolean"},
            }
        )

        errors = validator.errors(["名称", 1, False, "不是布尔值"])

        self.assertEqual(len(errors), 1)
        self.assertIn("/3: 期望类型 boolean，实际为 string", errors)

    def test_items_true_does_not_validate_array_elements(self) -> None:
        """items 为 True 时不校验数组元素内容。"""
        validator = validate.Validator({"type": "array", "items": True})

        self.assertEqual(validator.errors(["任意内容", {}, None]), [])

    def test_items_schema_validates_each_remaining_array_element(self) -> None:
        """items 为子结构时校验每个数组元素。"""
        validator = validate.Validator(
            {"type": "array", "items": {"type": "integer"}}
        )

        errors = validator.errors([1, "不是整数", 3])

        self.assertEqual(len(errors), 1)
        self.assertIn("/1: 期望类型 integer，实际为 string", errors)

    def errors(self, data) -> list[str]:
        """返回校验错误列表。"""
        return self.validator.errors(data)

    def test_numeric_constraints_report_boundaries_and_ignore_boolean_values(self) -> None:
        """数值边界关键字报错并带路径，布尔值不参与数值比较。"""
        schema = {
            "type": "object",
            "properties": {
                "below": {"minimum": 10},
                "above": {"maximum": 20},
                "exclusive": {"exclusiveMinimum": 10},
                "boolean": {"minimum": 10, "maximum": 20, "exclusiveMinimum": 10},
            },
        }
        instance = {"below": 9, "above": 21, "exclusive": 10, "boolean": True}

        errors = validate.Validator(schema).errors(instance)

        self.assertTrue(any("/below" in message for message in errors), errors)
        self.assertTrue(any("/above" in message for message in errors), errors)
        self.assertTrue(any("/exclusive" in message for message in errors), errors)
        self.assertFalse(any("/boolean" in message for message in errors), errors)

    def test_string_constraints_report_length_and_pattern_errors(self) -> None:
        """字符串长度与正则约束不满足时报告对应路径。"""
        schema = {
            "type": "object",
            "properties": {
                "too_short": {"minLength": 3},
                "too_long": {"maxLength": 3},
                "wrong_pattern": {"pattern": r"^OK-[0-9]+$"},
            },
        }
        instance = {"too_short": "ab", "too_long": "abcd", "wrong_pattern": "nope"}

        errors = validate.Validator(schema).errors(instance)

        self.assertTrue(any("/too_short" in message for message in errors), errors)
        self.assertTrue(any("/too_long" in message for message in errors), errors)
        self.assertTrue(any("/wrong_pattern" in message for message in errors), errors)

    def test_enum_and_const_report_invalid_values(self) -> None:
        """enum 和 const 不满足时报告对应路径。"""
        schema = {
            "type": "object",
            "properties": {
                "choice": {"enum": ["red", "blue"]},
                "version": {"const": 4},
            },
        }
        instance = {"choice": "green", "version": 3}

        errors = validate.Validator(schema).errors(instance)

        self.assertTrue(any("/choice" in message for message in errors), errors)
        self.assertTrue(any("/version" in message for message in errors), errors)

    def test_object_property_count_constraints_report_errors(self) -> None:
        """对象字段数低于或高于边界时报告对象路径。"""
        schema = {
            "type": "object",
            "properties": {
                "too_few": {"type": "object", "minProperties": 2},
                "too_many": {"type": "object", "maxProperties": 1},
            },
        }
        instance = {"too_few": {"a": 1}, "too_many": {"a": 1, "b": 2}}

        errors = validate.Validator(schema).errors(instance)

        self.assertTrue(any("/too_few" in message for message in errors), errors)
        self.assertTrue(any("/too_many" in message for message in errors), errors)

    def test_additional_properties_schema_validates_undeclared_fields(self) -> None:
        """additionalProperties 为子结构时，未声明字段按子结构校验并带路径。"""
        schema = {
            "type": "object",
            "properties": {"known": {"type": "string"}},
            "additionalProperties": {"type": "number", "minimum": 0},
        }
        instance = {"known": "ok", "extra": -1}

        errors = validate.Validator(schema).errors(instance)

        self.assertTrue(any("/extra" in message for message in errors), errors)

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

    def test_command_line_help_lists_options(self) -> None:
        """--help 列出命令行选项、默认数据文件与退出码，与 README 的用法一致。"""
        result = subprocess.run(
            [sys.executable, str(ROOT / "validate.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("data/diagnosis.json", result.stdout)
        self.assertIn("--schema", result.stdout)
        self.assertIn("--json", result.stdout)
        self.assertIn("退出码", result.stdout)

    def test_command_line_schema_missing_reports_exit_code_two_and_path(self) -> None:
        """--schema 指向不存在文件时退出码为 2，并提示结构定义路径。"""
        with temporary_directory() as folder:
            missing = Path(folder) / "missing-schema.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "validate.py"),
                    "--schema",
                    str(missing),
                    str(EXAMPLE_PATH),
                ],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, validate.EXIT_USAGE)
        self.assertIn("无法读取结构定义", result.stderr)
        self.assertIn(str(missing), result.stderr)
        self.assertNotIn("Traceback", output)

    def test_command_line_schema_corrupted_json_reports_exit_code_two(self) -> None:
        """--schema 文件是损坏 JSON 时退出码为 2，并提示不是合法 JSON。"""
        with temporary_directory() as folder:
            broken = Path(folder) / "corrupted-schema.json"
            broken.write_text("{不是合法 JSON", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "validate.py"),
                    "--schema",
                    str(broken),
                    str(EXAMPLE_PATH),
                ],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, validate.EXIT_USAGE)
        self.assertIn("不是合法 JSON", result.stderr)
        self.assertNotIn("Traceback", output)

    def test_command_line_schema_non_object_reports_exit_code_two(self) -> None:
        """--schema 顶层不是对象时退出码为 2，并提示结构定义必须是对象。"""
        with temporary_directory() as folder:
            non_object = Path(folder) / "array-schema.json"
            non_object.write_text("[]", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "validate.py"),
                    "--schema",
                    str(non_object),
                    str(EXAMPLE_PATH),
                ],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, validate.EXIT_USAGE)
        self.assertIn("结构定义", result.stderr)
        self.assertIn("必须是对象", result.stderr)
        self.assertNotIn("Traceback", output)

    def test_command_line_schema_unsupported_keyword_reports_exit_code_two(self) -> None:
        """--schema 含不支持的关键字时退出码为 2，stderr 指出关键字名且不打印 traceback。"""
        with temporary_directory() as folder:
            unsupported = Path(folder) / "unsupported-schema.json"
            unsupported.write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "unevaluatedProperties": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "validate.py"),
                    "--schema",
                    str(unsupported),
                    str(EXAMPLE_PATH),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, validate.EXIT_USAGE)
        self.assertIn("不支持的关键字", result.stderr)
        self.assertIn("unevaluatedProperties", result.stderr)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_command_line_json_accepts_example(self) -> None:
        """JSON 输出包含通过状态及 schema 版本。"""
        result = subprocess.run(
            [sys.executable, str(ROOT / "validate.py"), "--json", str(EXAMPLE_PATH)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output, {"ok": True, "errors": [], "schema_version": 4})

    def test_command_line_json_reports_failure(self) -> None:
        """JSON 输出失败状态与字段路径。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 3
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--json", str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 1)
        output = json.loads(result.stdout)
        self.assertFalse(output["ok"])
        self.assertTrue(any("/scope/schema版本" in error["path"] for error in output["errors"]))
        self.assertEqual(output["schema_version"], 3)

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

    def test_missing_scope_fails(self) -> None:
        """顶层缺少 scope 时失败，并指出路径 /scope。"""
        broken = copy.deepcopy(self.data)
        del broken["scope"]
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope: 缺少必需字段" in message for message in errors),
            f"错误信息未指出缺少 scope：{errors}",
        )

    def test_scope_not_an_object_fails(self) -> None:
        """scope 不是对象时失败，并指出路径 /scope。"""
        broken = copy.deepcopy(self.data)
        broken["scope"] = "2026-09"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope: 期望类型 object" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_schema_version_missing_fails(self) -> None:
        """scope 里缺少 schema版本 时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        del broken["scope"]["schema版本"]
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/schema版本: 缺少必需字段" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_schema_version_wrong_type_fails(self) -> None:
        """schema版本 是字符串 "4" 时失败：版本号必须是数值 4。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = "4"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/schema版本" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_schema_version_null_fails(self) -> None:
        """schema版本 为 null 时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = None
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/schema版本" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_schema_version_boolean_fails(self) -> None:
        """schema版本 为 true 时失败：布尔值不是版本号 4。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = True
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/schema版本" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_schema_version_future_value_fails(self) -> None:
        """schema版本 为 5 时失败：页面与结构定义只接受 4。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 5
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/schema版本" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_schema_version_float_matches_number_value(self) -> None:
        """schema版本 为 4.0 时通过：JSON 数值相等按数学值比较。

        结构定义用 const: 4 固定版本号；draft 2020-12 的 JSON 相等规则（第 4.2.3 节）
        规定两个数值按数学值比较，所以 4.0 与 4 相等。页面 app.js 用 `!== 4` 判断，
        JavaScript 里 4.0 === 4，两侧口径一致。
        """
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 4.0
        self.assertEqual(self.errors(broken), [])

    def test_metric_value_as_string_fails(self) -> None:
        """指标数值整体写成字符串时失败，并指出路径 /metrics/0/数值。"""
        broken = copy.deepcopy(self.data)
        broken["metrics"][0]["数值"] = "十二"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/metrics/0/数值" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_metric_name_field_missing_fails(self) -> None:
        """metrics 数组元素缺少名称字段（标题）时失败，并指出缺哪个字段。"""
        broken = copy.deepcopy(self.data)
        del broken["metrics"][0]["标题"]
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/metrics/0/标题: 缺少必需字段" in message for message in errors),
            f"错误信息未指出缺少名称字段：{errors}",
        )

    def test_metric_item_written_with_name_key_fails(self) -> None:
        """metrics 数组元素用 name 字段代替名称字段（标题）时失败，错误信息含字段名。"""
        broken = copy.deepcopy(self.data)
        metric = broken["metrics"][0]
        metric["name"] = metric.pop("标题")
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/metrics/0/标题: 缺少必需字段" in message for message in errors),
            f"错误信息未指出缺少名称字段：{errors}",
        )
        self.assertTrue(
            any("/metrics/0/name" in message for message in errors),
            f"错误信息未指出 name 字段：{errors}",
        )

    def test_numeric_series_string_value_fails(self) -> None:
        """成本数值序列里出现字符串时失败，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["metrics"][2]["数值"]["成本"]["2026-01实际"] = "十"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/metrics/2/数值/成本/2026-01实际" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_empty_metrics_array_fails(self) -> None:
        """metrics 为空数组时失败，并指出路径 /metrics。"""
        broken = copy.deepcopy(self.data)
        broken["metrics"] = []
        errors = self.errors(broken)
        self.assertTrue(errors, "metrics 为空数组时未报出错误")
        paths = [message.split(":", 1)[0] for message in errors]
        self.assertIn("/metrics", paths, f"错误信息未指出路径 /metrics：{errors}")

    def test_command_line_reports_empty_metrics_array(self) -> None:
        """命令行校验 metrics 为空数组的数据时退出码非 0，并打印出错路径 /metrics。"""
        broken = copy.deepcopy(self.data)
        broken["metrics"] = []
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, validate.EXIT_INVALID)
        self.assertIn("/metrics", result.stdout + result.stderr)

    def test_string_value_allowed_only_in_ratio_series(self) -> None:
        """同一个字符串值放在比率序列通过、放在数值序列失败（按结构定义判定）。"""
        allowed = copy.deepcopy(self.data)
        allowed["metrics"][3]["数值"]["整体技术人力成本率"]["2026-01实际"] = "约1%"
        self.assertEqual(self.errors(allowed), [])

        rejected = copy.deepcopy(self.data)
        rejected["metrics"][0]["数值"]["2026-01实际"] = "约1%"
        errors = self.errors(rejected)
        self.assertTrue(errors)
        self.assertTrue(
            any("/metrics/0/数值/2026-01实际" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_top_level_schema_forbids_extra_fields(self) -> None:
        """顶层结构定义声明 additionalProperties 为 false，多余字段因此被拒绝。"""
        self.assertIs(self.schema["additionalProperties"], False)

    def test_extra_top_level_field_fails(self) -> None:
        """顶层多余字段被拒绝，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["备注"] = "多余字段"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/备注: 不允许的额外字段" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_declared_top_level_field_passes(self) -> None:
        """结构定义里声明的可选顶层字段（stage）出现时通过校验。"""
        broken = copy.deepcopy(self.data)
        broken["stage"] = {
            "标题": "规模分档",
            "来源口径": "合成示例口径",
            "指标": [{"分档": "A", "例证": "示例"}],
        }
        self.assertEqual(self.errors(broken), [])

    def test_extra_field_inside_scope_fails(self) -> None:
        """scope 里多余字段同样被拒绝，并指出路径。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["备注"] = "多余字段"
        errors = self.errors(broken)
        self.assertTrue(errors)
        self.assertTrue(
            any("/scope/备注: 不允许的额外字段" in message for message in errors),
            f"错误信息未指出路径：{errors}",
        )

    def test_command_line_reports_missing_scope(self) -> None:
        """命令行校验缺少 scope 的数据时退出码非 0，并打印出错路径。"""
        broken = copy.deepcopy(self.data)
        del broken["scope"]
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/scope", result.stdout + result.stderr)

    def test_command_line_reports_extra_top_level_field(self) -> None:
        """命令行校验含多余顶层字段的数据时退出码非 0，并打印出错路径。"""
        broken = copy.deepcopy(self.data)
        broken["备注"] = "多余字段"
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/备注", result.stdout + result.stderr)

    def test_command_line_reports_string_metric_value(self) -> None:
        """命令行校验指标数值为字符串的数据时退出码非 0，并打印出错路径。"""
        broken = copy.deepcopy(self.data)
        broken["metrics"][0]["数值"] = "十二"
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/metrics/0/数值", result.stdout + result.stderr)

    def test_command_line_reports_metric_name_missing(self) -> None:
        """命令行校验缺少名称字段的 metrics 元素时退出码非 0，并打印出错字段名。"""
        broken = copy.deepcopy(self.data)
        del broken["metrics"][0]["标题"]
        with temporary_directory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, validate.EXIT_INVALID)
        self.assertIn("/metrics/0/标题", result.stdout + result.stderr)

    def test_command_line_reports_top_level_array(self) -> None:
        """命令行校验顶层数组时退出码非 0，并报告顶层类型错误而非 traceback。"""
        with temporary_directory() as folder:
            path = Path(folder) / "array.json"
            path.write_text(json.dumps([self.data], ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/: 期望类型 object，实际为 array", output)
        self.assertNotIn("Traceback", output)

    def test_command_line_reports_top_level_string(self) -> None:
        """命令行校验顶层字符串时退出码非 0，并报告顶层类型错误而非 traceback。"""
        with temporary_directory() as folder:
            path = Path(folder) / "string.json"
            path.write_text(json.dumps("合成诊断"), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path)],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/: 期望类型 object，实际为 string", output)
        self.assertNotIn("Traceback", output)

    def test_command_line_nonexistent_file_reports_exit_code_two_and_path(self) -> None:
        """待校验文件不存在时退出码为 2（输入不可读），stderr 提示里含该路径。"""
        with temporary_directory() as folder:
            missing = Path(folder) / "does_not_exist.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(missing)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(missing), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_command_line_corrupted_json_reports_exit_code_two_and_line_col(self) -> None:
        """待校验文件是损坏 JSON 时退出码为 2，错误消息含「第 N 行第 M 列」。"""
        with temporary_directory() as folder:
            broken = Path(folder) / "corrupted.json"
            broken.write_text("{\n  \"field\": \n}", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(broken)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertRegex(result.stderr, r"第 \d+ 行第 \d+ 列")
        self.assertIn(str(broken), result.stderr)
        self.assertIn("不是合法 JSON", result.stderr)
        self.assertIn("解析失败", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_command_line_json_nonexistent_file_reports_exit_code_two_and_json_errors(self) -> None:
        """待校验文件不存在加 --json 时退出码为 2，stdout 为单个合法 JSON 对象且 ok 为 false、含 path 与 message。"""
        with temporary_directory() as folder:
            missing = Path(folder) / "does_not_exist.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--json", str(missing)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        raw_stdout = result.stdout.strip()
        output = json.loads(result.stdout)
        self.assertIsInstance(output, dict)
        self.assertEqual(raw_stdout, json.dumps(output, ensure_ascii=False))
        self.assertFalse(output["ok"])
        self.assertIsInstance(output["errors"], list)
        self.assertEqual(len(output["errors"]), 1)
        self.assertEqual(output["errors"][0]["path"], str(missing))
        self.assertIn(str(missing), output["errors"][0]["message"])
        self.assertEqual(result.stderr, "")

    def test_command_line_json_corrupted_file_reports_exit_code_two_and_json_errors(self) -> None:
        """待校验文件是损坏 JSON 加 --json 时退出码为 2，stdout 为单个合法 JSON 对象且 ok 为 false、含 path 与 message。"""
        with temporary_directory() as folder:
            broken = Path(folder) / "corrupted.json"
            broken.write_text("{\n  \"field\": \n}", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--json", str(broken)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        raw_stdout = result.stdout.strip()
        output = json.loads(result.stdout)
        self.assertIsInstance(output, dict)
        self.assertEqual(raw_stdout, json.dumps(output, ensure_ascii=False))
        self.assertFalse(output["ok"])
        self.assertIsInstance(output["errors"], list)
        self.assertEqual(len(output["errors"]), 1)
        self.assertEqual(output["errors"][0]["path"], str(broken))
        self.assertRegex(output["errors"][0]["message"], r"第 \d+ 行第 \d+ 列")
        self.assertIn("不是合法 JSON", output["errors"][0]["message"])
        self.assertIn("解析失败", output["errors"][0]["message"])
        self.assertEqual(result.stderr, "")

    def test_command_line_multiple_valid_files_reports_exit_code_zero_and_passed(self) -> None:
        """传入两个合格数据文件时退出码为 0，逐个打印校验通过。"""
        with temporary_directory() as folder:
            path1 = Path(folder) / "valid1.json"
            path2 = Path(folder) / "valid2.json"
            content = json.dumps(self.data, ensure_ascii=False)
            path1.write_text(content, encoding="utf-8")
            path2.write_text(content, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(path1), str(path2)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{path1}: 校验通过", result.stdout)
        self.assertIn(f"{path2}: 校验通过", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_command_line_mixed_files_reports_exit_code_one_and_details(self) -> None:
        """传入一个合格文件与一个不合规文件时退出码为 1，合格文件输出通过、不合规文件输出具体错误路径。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 3
        with temporary_directory() as folder:
            valid_path = Path(folder) / "valid.json"
            broken_path = Path(folder) / "broken.json"
            valid_path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            broken_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), str(valid_path), str(broken_path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{valid_path}: 校验通过", result.stdout)
        self.assertIn(f"{broken_path}: 校验失败", result.stdout)
        self.assertIn("/scope/schema版本", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_command_line_json_mixed_files_reports_summary_and_errors(self) -> None:
        """传入合格与不合规文件并加 --json 参数时退出码为 1，stdout 输出包含全部文件在内的汇总状态与错误列表。"""
        broken = copy.deepcopy(self.data)
        broken["scope"]["schema版本"] = 3
        with temporary_directory() as folder:
            valid_path = Path(folder) / "valid.json"
            broken_path = Path(folder) / "broken.json"
            valid_path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            broken_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--json", str(valid_path), str(broken_path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 1)
        raw_stdout = result.stdout.strip()
        output = json.loads(result.stdout)
        self.assertEqual(raw_stdout, json.dumps(output, ensure_ascii=False))
        self.assertFalse(output["ok"])
        self.assertIsInstance(output["errors"], list)
        self.assertEqual(len(output["errors"]), 1)
        self.assertEqual(output["errors"][0]["path"], f"{broken_path}/scope/schema版本")
        self.assertIn("/scope/schema版本", output["errors"][0]["message"])
        self.assertEqual(result.stderr, "")

    def test_command_line_json_multiple_valid_files_reports_summary(self) -> None:
        """传入两个合格数据文件并加 --json 参数时退出码为 0，stdout 输出汇总状态为通过且错误列表为空。"""
        with temporary_directory() as folder:
            path1 = Path(folder) / "valid1.json"
            path2 = Path(folder) / "valid2.json"
            content = json.dumps(self.data, ensure_ascii=False)
            path1.write_text(content, encoding="utf-8")
            path2.write_text(content, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--json", str(path1), str(path2)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        raw_stdout = result.stdout.strip()
        output = json.loads(result.stdout)
        self.assertEqual(raw_stdout, json.dumps(output, ensure_ascii=False))
        self.assertTrue(output["ok"])
        self.assertEqual(output["errors"], [])
        self.assertEqual(output["schema_version"], 4)
        self.assertEqual(result.stderr, "")

    def test_command_line_root_aggregates_schema_errors_across_outputs(self) -> None:
        """临时 root 中 brief、diagnosis 与 site 的多处 schema 错误全部出现在错误列表。"""
        brief = load_json(BRIEF_PATH)
        diagnosis = copy.deepcopy(self.data)
        site_diagnosis = copy.deepcopy(self.data)
        diagnosis["scope"]["schema版本"] = 3
        del brief["目标"]
        site_diagnosis["metrics"] = []
        with temporary_directory() as folder:
            root = Path(folder)
            (root / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            (root / "diagnosis.json").write_text(json.dumps(diagnosis, ensure_ascii=False), encoding="utf-8")
            (root / "site" / "data").mkdir(parents=True)
            (root / "site" / "data" / "diagnosis.json").write_text(
                json.dumps(site_diagnosis, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--root", str(root), "--json"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, validate.EXIT_INVALID, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["ok"])
        error_paths = {error["path"] for error in output["errors"]}
        self.assertIn(f"{root / 'brief.json'}/目标", error_paths)
        self.assertIn(f"{root / 'diagnosis.json'}/scope/schema版本", error_paths)
        self.assertIn(f"{root / 'site' / 'data' / 'diagnosis.json'}/metrics", error_paths)

    def test_command_line_root_accepts_valid_outputs(self) -> None:
        """临时 root 中三个真实 JSON 文件均合法时退出码为 0。"""
        with temporary_directory() as folder:
            root = Path(folder)
            (root / "brief.json").write_text(BRIEF_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            content = json.dumps(self.data, ensure_ascii=False)
            (root / "diagnosis.json").write_text(content, encoding="utf-8")
            (root / "site" / "data").mkdir(parents=True)
            (root / "site" / "data" / "diagnosis.json").write_text(content, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate.py"), "--root", str(root)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, validate.EXIT_OK, result.stdout + result.stderr)
        self.assertIn("校验通过", result.stdout)

    def test_supported_keywords_contains_exclusive_maximum(self) -> None:
        """SUPPORTED_KEYWORDS 常量集合中包含 exclusiveMaximum。"""
        self.assertIn("exclusiveMaximum", validate.SUPPORTED_KEYWORDS)

    def test_exclusive_maximum_passes_when_value_is_less(self) -> None:
        """数值严格小于 exclusiveMaximum 时校验通过。"""
        v = validate.Validator({"type": "number", "exclusiveMaximum": 100})
        self.assertEqual(v.errors(50), [])
        self.assertEqual(v.errors(99.9), [])

    def test_exclusive_maximum_fails_when_value_is_equal_or_greater(self) -> None:
        """数值大于等于 exclusiveMaximum 时校验失败，并按指定格式输出错误信息。"""
        v = validate.Validator({"type": "number", "exclusiveMaximum": 100})
        self.assertEqual(v.errors(100), ["/: 数值必须小于 100"])
        self.assertEqual(v.errors(100.5), ["/: 数值必须小于 100"])
        self.assertEqual(v.errors(150), ["/: 数值必须小于 100"])

    def test_exclusive_maximum_nested_property_and_type_exclusion(self) -> None:
        """对象嵌套属性及布尔值排除：报错带 JSON Pointer 路径，布尔值不触发数值比较。"""
        v = validate.Validator(
            {"type": "object", "properties": {"threshold": {"exclusiveMaximum": 1.0}}}
        )
        self.assertEqual(v.errors({"threshold": 0.99}), [])
        self.assertEqual(v.errors({"threshold": 1.0}), ["/threshold: 数值必须小于 1.0"])
        self.assertEqual(v.errors({"threshold": 1.5}), ["/threshold: 数值必须小于 1.0"])

        # 布尔值虽然是 int 子类，但不应触发数值比较
        bool_v = validate.Validator({"exclusiveMaximum": 0})
        self.assertEqual(bool_v.errors(True), [])



if __name__ == "__main__":
    unittest.main()
