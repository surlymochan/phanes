#!/usr/bin/env python3
"""校验 diagnosis.json 是否符合 schema/diagnosis.schema.json。

用法：
    python3 validate.py [文件 ...]

默认校验 data/diagnosis.json。只使用标准库。校验失败时退出码非 0，
并逐条打印出错路径，例如 /scope/schema版本。

字段处理规则全部来自结构定义，不在代码里写死字段名：
- 定义里 required 列出的字段缺省时报错（_check_object）；
- 定义里 properties 声明的可选字段（例如 口径说明）只在数据里出现时才校验，
  缺省不报错，出现时按定义校验类型与元素约束（_check、_check_array）；
- 定义里 additionalProperties 为 false 时，未声明的字段按额外字段报错。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema" / "diagnosis.schema.json"
DATA_PATH = ROOT / "data" / "diagnosis.json"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

# 结构定义里允许出现的关键字。其他关键字直接报错，避免规则被静默忽略。
SUPPORTED_KEYWORDS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "description",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "oneOf",
        "pattern",
        "prefixItems",
        "properties",
        "required",
        "title",
        "type",
    }
)


def escape_pointer(segment: str) -> str:
    """把一个路径片段转义成 JSON Pointer 片段。"""
    return segment.replace("~", "~0").replace("/", "~1")


def pointer(path: str) -> str:
    """把内部路径转成可读的 JSON Pointer。"""
    return path or "/"


def type_name(value: object) -> str:
    """返回值的 JSON 类型名，用于错误信息。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def matches_type(value: object, expected: str) -> bool:
    """判断值是否符合一个 JSON Schema 类型名。"""
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def show(value: object) -> str:
    """把值转成简短的可读形式，用于错误信息。"""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return type_name(value)


class Validator:
    """JSON Schema draft 2020-12 的子集校验器。

    只实现结构定义用到的关键字。遇到不支持的关键字时抛出 ValueError，
    这样结构定义写错时不会静默通过校验。
    """

    def __init__(self, schema: dict) -> None:
        self.schema = schema

    def errors(self, instance: object) -> list[str]:
        """返回错误的说明列表。空列表表示通过校验。"""
        found: list[str] = []
        self._check(instance, self.schema, "", found)
        return found

    def _resolve(self, ref: str) -> dict:
        """解析文档内引用，例如 #/$defs/scope。"""
        if not ref.startswith("#/"):
            raise ValueError(f"只支持文档内引用：{ref}")
        target: object = self.schema
        for segment in ref[2:].split("/"):
            key = segment.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or key not in target:
                raise ValueError(f"引用不存在：{ref}")
            target = target[key]
        if not isinstance(target, dict):
            raise ValueError(f"引用不是结构对象：{ref}")
        return target

    def _check(self, instance: object, schema: dict, path: str, errors: list[str]) -> None:
        """按结构定义检查一个值，把错误追加到 errors。"""
        if not isinstance(schema, dict):
            raise ValueError("结构定义节点必须是对象")
        unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
        if unknown:
            raise ValueError(f"结构定义使用了不支持的关键字：{', '.join(unknown)}")
        if "$ref" in schema:
            self._check(instance, self._resolve(schema["$ref"]), path, errors)
        if "type" in schema:
            expected = schema["type"]
            options = expected if isinstance(expected, list) else [expected]
            if not any(matches_type(instance, option) for option in options):
                errors.append(
                    f"{pointer(path)}: 期望类型 {' 或 '.join(options)}，实际为 {type_name(instance)}"
                )
                return
        if "const" in schema and instance != schema["const"]:
            errors.append(f"{pointer(path)}: 期望值 {show(schema['const'])}，实际为 {show(instance)}")
        if "enum" in schema and instance not in schema["enum"]:
            errors.append(f"{pointer(path)}: 取值不在允许的列表内，实际为 {show(instance)}")
        if "pattern" in schema and isinstance(instance, str):
            if re.search(schema["pattern"], instance) is None:
                errors.append(f"{pointer(path)}: 不匹配格式 {schema['pattern']}")
        if "minLength" in schema and isinstance(instance, str):
            if len(instance) < schema["minLength"]:
                errors.append(f"{pointer(path)}: 字符串长度不足 {schema['minLength']}")
        if "maxLength" in schema and isinstance(instance, str):
            if len(instance) > schema["maxLength"]:
                errors.append(f"{pointer(path)}: 字符串长度超过 {schema['maxLength']}")
        if "minimum" in schema and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if instance < schema["minimum"]:
                errors.append(f"{pointer(path)}: 数值小于允许的最小值 {schema['minimum']}")
        if "maximum" in schema and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            if instance > schema["maximum"]:
                errors.append(f"{pointer(path)}: 数值大于允许的最大值 {schema['maximum']}")
        if isinstance(instance, dict):
            self._check_object(instance, schema, path, errors)
        if isinstance(instance, list):
            self._check_array(instance, schema, path, errors)
        if "allOf" in schema:
            for branch in schema["allOf"]:
                self._check(instance, branch, path, errors)
        if "anyOf" in schema:
            self._check_choice(instance, schema["anyOf"], path, errors, "anyOf")
        if "oneOf" in schema:
            self._check_choice(instance, schema["oneOf"], path, errors, "oneOf")

    def _check_object(self, instance: dict, schema: dict, path: str, errors: list[str]) -> None:
        """检查对象的必需字段、字段模式和额外字段。"""
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{pointer(path + '/' + escape_pointer(name))}: 缺少必需字段")
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append(f"{pointer(path)}: 字段数量不足 {schema['minProperties']}")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append(f"{pointer(path)}: 字段数量超过 {schema['maxProperties']}")
        properties = schema.get("properties", {})
        for name, sub in properties.items():
            if name in instance:
                self._check(instance[name], sub, path + "/" + escape_pointer(name), errors)
        if "additionalProperties" in schema:
            extra = schema["additionalProperties"]
            for name, value in instance.items():
                if name in properties:
                    continue
                if extra is False:
                    errors.append(f"{pointer(path + '/' + escape_pointer(name))}: 不允许的额外字段")
                elif extra is not True:
                    self._check(value, extra, path + "/" + escape_pointer(name), errors)

    def _check_array(self, instance: list, schema: dict, path: str, errors: list[str]) -> None:
        """检查数组的元素数量和元素结构。"""
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{pointer(path)}: 元素数量不足 {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{pointer(path)}: 元素数量超过 {schema['maxItems']}")
        prefix = schema.get("prefixItems", [])
        for index, value in enumerate(instance):
            if index < len(prefix):
                self._check(value, prefix[index], f"{path}/{index}", errors)
            elif "items" in schema and schema["items"] is not True:
                self._check(value, schema["items"], f"{path}/{index}", errors)

    def _check_choice(
        self, instance: object, branches: list, path: str, errors: list[str], keyword: str
    ) -> None:
        """检查 anyOf 和 oneOf。"""
        matched = 0
        best: list[str] = []
        for branch in branches:
            local: list[str] = []
            self._check(instance, branch, path, local)
            if not local:
                matched += 1
                continue
            if not best or len(local) < len(best):
                best = local
        if keyword == "anyOf" and matched == 0:
            errors.append(f"{pointer(path)}: 不满足任何一个候选结构")
            errors.extend(best)
        if keyword == "oneOf" and matched != 1:
            errors.append(f"{pointer(path)}: 满足的候选结构数量为 {matched}，期望 1 个")
            errors.extend(best)


def load_schema(path: Path) -> dict:
    """读取结构定义文件。"""
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as cause:
        raise ValueError(f"无法读取结构定义 {path}：{cause}") from cause
    except json.JSONDecodeError as cause:
        raise ValueError(f"结构定义 {path} 不是合法 JSON：{cause}") from cause
    if not isinstance(schema, dict):
        raise ValueError(f"结构定义 {path} 必须是对象")
    return schema


def load_data(path: Path) -> object:
    """读取待校验的数据文件。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as cause:
        raise ValueError(f"无法读取数据文件 {path}：{cause}") from cause
    try:
        return json.loads(text)
    except json.JSONDecodeError as cause:
        raise ValueError(
            f"数据文件 {path} 不是合法 JSON：第 {cause.lineno} 行第 {cause.colno} 列 {cause.msg}"
        ) from cause


def validate_data(data: object, schema: dict) -> list[str]:
    """按结构定义校验数据，返回错误的说明列表。"""
    return Validator(schema).errors(data)


def main(argv: list[str] | None = None) -> int:
    """校验命令行给出的文件。返回进程退出码。"""
    parser = argparse.ArgumentParser(
        description="校验 diagnosis.json 是否符合 schema/diagnosis.schema.json。"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="待校验的数据文件。默认校验 data/diagnosis.json。",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=SCHEMA_PATH,
        help="结构定义文件。默认 schema/diagnosis.schema.json。",
    )
    args = parser.parse_args(argv)
    paths = args.paths or [DATA_PATH]
    try:
        validator = Validator(load_schema(args.schema))
    except ValueError as cause:
        print(str(cause), file=sys.stderr)
        return EXIT_USAGE
    failed = 0
    unreadable = 0
    for path in paths:
        try:
            data = load_data(path)
        except ValueError as cause:
            print(str(cause), file=sys.stderr)
            unreadable += 1
            continue
        errors = validator.errors(data)
        if errors:
            failed += 1
            print(f"{path}: 校验失败（{len(errors)} 处问题）")
            for message in errors:
                print(f"  {message}")
        else:
            print(f"{path}: 校验通过")
    if unreadable:
        return EXIT_USAGE
    if failed:
        return EXIT_INVALID
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
