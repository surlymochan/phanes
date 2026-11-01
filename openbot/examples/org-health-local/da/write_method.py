#!/usr/bin/env python3
"""Generate a numeric-free data-method note from a brief and diagnosis.

用法：
    python3 da/write_method.py --brief <brief.json> --data <diagnosis.json> --out <method.md> [--raw <dir>]

`--raw` 指向原始 CSV 目录：给出时方法说明里增加“数据来源”小节，逐个列出 CSV 文件名与数据行数
（不含表头），按文件名排序；不给时该小节写明未提供原始数据目录。

`--out` 的父目录不存在时自动创建。
"""

import argparse
import csv
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple


NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?")
PERIOD_SUFFIXES = ("实际", "参考", "测算", "目标")

DATA_SOURCE_HEADING = "## 数据来源"


class MethodError(Exception):
    """输入数据不可用。"""


def load_json(path: Path) -> Any:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as cause:
        raise MethodError(f"无法读取 JSON 文件 {path}：{cause}") from cause


def count_data_rows(path: Path) -> int:
    """统计 CSV 的数据行数（不含表头）；空行不计入。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as cause:
        raise MethodError(f"无法读取原始数据 {path}：{cause}") from cause
    rows = [row for row in csv.reader(text.splitlines()) if row and any(cell.strip() for cell in row)]
    return max(len(rows) - 1, 0)


def data_sources(raw_dir: Path) -> List[Tuple[str, int]]:
    """列出原始 CSV 的文件名与数据行数（不含表头），按文件名排序。"""
    if not raw_dir.is_dir():
        raise MethodError(f"原始数据目录不存在：{raw_dir}")
    return [(path.name, count_data_rows(path)) for path in sorted(raw_dir.glob("*.csv"), key=lambda item: item.name)]


def brief_method(brief: Any) -> str:
    """Return the brief's method section without inventing a new definition."""
    method = brief.get("口径", "未提供") if isinstance(brief, dict) else "未提供"
    if isinstance(method, str):
        return method
    if isinstance(method, list):
        return "；".join(str(item) for item in method)
    if isinstance(method, dict):
        return "；".join(f"{key}：{value}" for key, value in method.items())
    return str(method)


def leaf_fields(value: Any, path: str) -> Iterable[Tuple[str, Any]]:
    """Yield every conclusion/value leaf, retaining a JSON-like source path."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaf_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from leaf_fields(child, f"{path}[{index}]")
    else:
        yield path, value


def periods_for(value: Any) -> List[str]:
    """Find period keys and the periods whose values are explicitly null."""
    if not isinstance(value, dict):
        return []
    return [str(key) for key, child in value.items() if child is None]


def metric_sections(diagnosis: dict) -> Iterable[Tuple[str, str, Any, List[str]]]:
    def walk(value: Any, path: str, title: str) -> Iterable[Tuple[str, str, Any, List[str]]]:
        if isinstance(value, dict) and value and all(
            not isinstance(item, (dict, list)) for item in value.values()
        ):
            yield title, path, value, periods_for(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                yield from walk(child, f"{path}.{key}", f"{title}（{key}）")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, f"{path}[{index}]", title)

    for index, metric in enumerate(diagnosis.get("metrics", [])):
        if not isinstance(metric, dict):
            continue
        title = str(metric.get("标题", f"指标 {index + 1}"))
        values = metric.get("数值")
        yield from walk(values, f"metrics[{index}].数值", title)


def conclusion_sections(diagnosis: dict) -> Iterable[Tuple[str, str, Any, List[str]]]:
    conclusion = diagnosis.get("conclusion")
    def walk(value: Any, path: str, title: str) -> Iterable[Tuple[str, str, Any, List[str]]]:
        if isinstance(value, dict) and value and all(
            not isinstance(item, (dict, list)) for item in value.values()
        ):
            yield title, path, value, periods_for(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if isinstance(child, (dict, list)):
                    yield from walk(child, child_path, str(key))
                else:
                    yield str(key), child_path, child, []
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from walk(child, f"{path}[{index}]", title)

    if isinstance(conclusion, (dict, list)):
        yield from walk(conclusion, "conclusion", "结论")
    elif conclusion is not None:
        yield "结论", "conclusion", conclusion, []


def render(brief: dict, diagnosis: dict, sources: Optional[List[Tuple[str, int]]] = None) -> str:
    method = brief_method(brief)
    periods = brief.get("观察期间", "未提供")
    if isinstance(periods, list):
        period_text = "、".join(str(period) for period in periods)
    else:
        period_text = str(periods)

    if sources is None:
        source_lines = ["未提供原始数据目录（`--raw`），无法列出数据来源文件。"]
    elif not sources:
        source_lines = ["原始数据目录里没有 CSV 文件。"]
    else:
        source_lines = [f"- `{name}`：{count} 行数据（不含表头）" for name, count in sources]

    sections = list(metric_sections(diagnosis)) + list(conclusion_sections(diagnosis))
    lines = [
        "# 数据口径说明",
        "",
        "本文只说明字段来源、计算口径、期间和缺口，不复述诊断数值。",
        "",
        f"## 统一口径\n\n{method}",
        "",
        f"## 观察期间\n\n{period_text}",
        "",
        DATA_SOURCE_HEADING,
        "",
        *source_lines,
        "",
        "## 指标与结论字段",
        "",
    ]
    if not sections:
        lines.append("未发现指标或结论字段。")
    for title, path, value, missing in sections:
        lines.extend(
            [
                f"### {title}",
                "",
                f"- 数据来源（字段路径）：`{path}`",
                f"- 计算口径：引用任务书“口径”：{method}",
                f"- 期间：{period_text}",
                "- 已知缺口："
                + ("；".join(f"{period} 为 null" for period in missing) if missing else "未发现明确的 null 期次"),
                "",
            ]
        )
    lines.extend(["## 问题回答", ""])
    insights = diagnosis.get("insights", [])
    if insights:
        lines.extend(
            f"- {redacted_text(item.get('标题', '未命名问题'))}"
            for item in insights
            if isinstance(item, dict)
        )
    else:
        lines.append("未提供问题回答。")
    lines.extend(["", "## 定义", ""])
    definitions = diagnosis.get("definitions", [])
    if definitions:
        lines.extend(f"- {redacted_text(item)}" for item in definitions)
    else:
        lines.append("未提供指标定义。")
    return "\n".join(lines).rstrip() + "\n"


def strip_data_source(text: str) -> str:
    """去掉“数据来源”小节。

    该小节里的行数是原始 CSV 的行数，不是诊断数值，自检时不参与泄露比对。
    """
    pattern = re.compile(
        r"^" + re.escape(DATA_SOURCE_HEADING) + r"\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL
    )
    return pattern.sub("", text)


def numeric_literals(value: Any) -> List[str]:
    """Collect numeric diagnosis values for the output self-check."""
    found: List[str] = []
    if isinstance(value, bool) or value is None:
        return found
    if isinstance(value, (int, float)):
        found.append(str(value))
    elif isinstance(value, str):
        found.extend(NUMBER_RE.findall(value))
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(numeric_literals(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(numeric_literals(child))
    return found


def _number_key(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).replace("%", "")
    match = NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _period_matches(text: str, period: str) -> bool:
    if period in text:
        return True
    return any(period.endswith(suffix) and period[:-len(suffix)] in text for suffix in PERIOD_SUFFIXES)


def snapshot_series(diagnosis: dict) -> List[Tuple[str, str, Any]]:
    """Return period-keyed numeric leaves from facts/series and metrics."""
    result: List[Tuple[str, str, Any]] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if value and all(not isinstance(child, (dict, list)) for child in value.values()):
                for period, child in value.items():
                    if child is not None and _number_key(child) is not None:
                        result.append((str(period), path, child))
            else:
                for key, child in value.items():
                    walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    for root in ("facts", "series", "metrics"):
        if root in diagnosis:
            walk(diagnosis[root], root)
    return result


def validate_numeric_references(diagnosis: dict) -> None:
    """Require every insight/recommendation number to cite a same-period series value."""
    sources = snapshot_series(diagnosis)
    periods = {period for period, _, _ in sources}

    def numeric_references(text: str) -> List[str]:
        masked = text
        for period in sorted(periods, key=len, reverse=True):
            masked = masked.replace(period, "")
            for suffix in PERIOD_SUFFIXES:
                if period.endswith(suffix):
                    masked = masked.replace(period[:-len(suffix)], "")
        return NUMBER_RE.findall(masked)

    errors = []
    for section in ("insights", "recommendations"):
        items = diagnosis.get(section)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for field, value in item.items():
                if section == "recommendations" and field == "优先级":
                    continue
                if not isinstance(value, str) or not value.strip():
                    continue
                for token in numeric_references(value):
                    number = _number_key(token)
                    matches = [
                        (period, path) for period, path, source_value in sources
                        if _number_key(source_value) == number and _period_matches(value, period)
                    ]
                    if not matches:
                        errors.append({
                            "path": f"{section}[{index}].{field}",
                            "number": token,
                            "message": "数字引用无法回溯到快照 facts/series 中的同值指标和期间",
                        })
    if errors:
        raise MethodError(json.dumps({"error": "untraceable_numeric_reference", "details": errors}, ensure_ascii=False))


def redacted_text(value: Any) -> str:
    """Keep chapter text without copying numeric diagnosis values."""
    return NUMBER_RE.sub("[数值]", str(value))


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--raw", type=Path, help="原始 CSV 目录；给出时列出数据来源文件与行数。")
    args = parser.parse_args(argv)

    try:
        brief = load_json(args.brief)
        diagnosis = load_json(args.data)
        validate_numeric_references(diagnosis)
        sources = None
        if args.raw is not None:
            sources = data_sources(args.raw)
    except MethodError as cause:
        print(str(cause), file=sys.stderr)
        return 1
    output = render(brief, diagnosis, sources)
    # Period labels and array indexes are deliberately present in source paths;
    # exclude those structural strings from the value-only leak check.
    # 数据来源小节里的行数是原始 CSV 的行数，不是诊断数值，同样排除。
    check_output = re.sub(r"`[^`]*`", "", strip_data_source(output))
    periods = brief.get("观察期间", []) if isinstance(brief, dict) else []
    if isinstance(periods, list):
        for period in periods:
            check_output = check_output.replace(str(period), "")
    elif periods:
        check_output = check_output.replace(str(periods), "")
    leaked = sorted(
        set(
            token
            for token in numeric_literals(diagnosis)
            if token and re.search(r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", check_output)
        )
    )
    if leaked:
        print("生成文档包含诊断数值：" + "、".join(leaked), file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
