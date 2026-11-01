#!/usr/bin/env python3
"""Generate a numeric-free data-method note from a brief and diagnosis.

用法：
    python3 da/write_method.py --brief <brief.json> --data <diagnosis.json> --out <method.md> [--raw <dir>]

`--raw` 指向原始 CSV 目录：给出时方法说明里增加“数据来源”小节，逐个列出 CSV 文件名与数据行数
（不含表头），按文件名排序；不给时该小节写明未提供原始数据目录。

诊断快照带 `warnings` 时（脏 CSV 被跳过的坏行），方法说明里增加“数据质量警告”小节，
逐条列出问题，便于回原始 CSV 核对。

`--out` 的父目录不存在时自动创建。
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple


NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?")

DATA_SOURCE_HEADING = "## 数据来源"
WARNING_HEADING = "## 数据质量警告"


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
        return "；".join(f"{key}：{calibration_text(value)}" for key, value in method.items())
    return str(method)


def calibration_text(value: Any) -> str:
    """把任务书 `口径` 里的嵌套结构展开成一句可读文字。"""
    if isinstance(value, dict):
        return "；".join(f"{key}：{calibration_text(child)}" for key, child in value.items())
    if isinstance(value, list):
        return "；".join(calibration_text(item) for item in value)
    return str(value)


def brief_periods(brief: Any) -> List[str]:
    """列出任务书 `观察期间` 里的期次标签。

    支持三种写法：字符串、字符串数组，以及 Boss 的对象写法（`期次` 数组或
    `起始期次`/`参考期次`/`截止期次`）。返回顺序与任务书一致。
    """
    window = brief.get("观察期间") if isinstance(brief, dict) else None
    if isinstance(window, dict):
        labels: List[str] = []
        for name in ("起始期次", "参考期次", "截止期次"):
            value = window.get(name)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
        anchors = window.get("期次")
        if isinstance(anchors, list):
            labels.extend(
                item.strip() for item in anchors if isinstance(item, str) and item.strip()
            )
        return labels
    if isinstance(window, list):
        return [str(item).strip() for item in window if str(item).strip()]
    if isinstance(window, str) and window.strip():
        return [window.strip()]
    return []


def metric_calibration(brief: Any, title: str) -> Optional[str]:
    """按指标标题取任务书 `口径` 里对应的口径说明。

    任务书的 `口径` 按指标标题分组时（如 Boss 格式），每个指标只引用自己那一段；
    没有同名口径时返回 None，由调用方回退到任务书的整体口径。
    """
    method = brief.get("口径") if isinstance(brief, dict) else None
    if not isinstance(method, dict):
        return None
    matched = None
    for key in method:
        if isinstance(key, str) and title.startswith(key):
            if matched is None or len(key) > len(matched):
                matched = key
    if matched is None:
        return None
    return calibration_text(method[matched])


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
    periods = brief_periods(brief)
    period_text = "、".join(periods) if periods else "未提供"

    if sources is None:
        source_lines = ["未提供原始数据目录（`--raw`），无法列出数据来源文件。"]
    elif not sources:
        source_lines = ["原始数据目录里没有 CSV 文件。"]
    else:
        source_lines = [f"- `{name}`：{count} 行数据（不含表头）" for name, count in sources]

    warning_lines = [
        f"- {item}"
        for item in diagnosis.get("warnings", [])
        if isinstance(item, str) and item.strip()
    ]

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
    ]
    if warning_lines:
        lines.extend(
            [
                WARNING_HEADING,
                "",
                "原始数据有下列问题，相关行已跳过；同一批问题也写在诊断快照的 `warnings` 里，"
                "可回原始 CSV 逐条核对：",
                "",
                *warning_lines,
                "",
            ]
        )
    lines.extend(
        [
            "## 指标与结论字段",
            "",
        ]
    )
    if not sections:
        lines.append("未发现指标或结论字段。")
    for title, path, value, missing in sections:
        anchors = [str(key) for key in value] if isinstance(value, dict) else []
        anchor_line = (
            "- 期间锚点：" + "、".join("`" + key + "`" for key in anchors) if anchors else ""
        )
        calibration = metric_calibration(brief, title) or method
        lines.extend(
            [
                f"### {title}",
                "",
                f"- 数据来源（字段路径）：`{path}`",
                *([anchor_line] if anchor_line else []),
                f"- 计算口径：引用任务书“口径”：{calibration}",
                f"- 期间：{period_text}",
                "- 已知缺口："
                + ("；".join(f"{period} 为 null" for period in missing) if missing else "未发现明确的 null 期次"),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def strip_sections(text: str, headings: Iterable[str]) -> str:
    """去掉给定的小节。

    这些小节里的数字来自原始 CSV（行数、坏行行号），不是诊断数值，自检时不参与泄露比对。
    """
    for heading in headings:
        pattern = re.compile(
            r"^" + re.escape(heading) + r"\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL
        )
        text = pattern.sub("", text)
    return text


def strip_data_source(text: str) -> str:
    """去掉“数据来源”小节。

    该小节里的行数是原始 CSV 的行数，不是诊断数值，自检时不参与泄露比对。
    """
    return strip_sections(text, (DATA_SOURCE_HEADING,))


def metric_values(diagnosis: dict) -> List[Any]:
    """列出快照里存放结论数值的子树：`metrics[].数值`。

    泄露比对只看这里：`scope`、`口径说明`、`insights` 等散文型字段里的年份与单位约定
    不是“诊断数值”，方法说明引用任务书口径时不应被误判。
    """
    values: List[Any] = []
    metrics = diagnosis.get("metrics") if isinstance(diagnosis, dict) else None
    for metric in metrics or []:
        if isinstance(metric, dict) and "数值" in metric:
            values.append(metric["数值"])
    return values


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
        sources = None
        if args.raw is not None:
            sources = data_sources(args.raw)
    except MethodError as cause:
        print(str(cause), file=sys.stderr)
        return 1
    output = render(brief, diagnosis, sources)
    # Period labels and array indexes are deliberately present in source paths;
    # exclude those structural strings from the value-only leak check.
    # 数据来源与数据质量警告小节里的数字来自原始 CSV（行数、坏行行号），
    # 不是诊断数值，同样排除。
    check_output = re.sub(
        r"`[^`]*`", "", strip_sections(output, (DATA_SOURCE_HEADING, WARNING_HEADING))
    )
    for period in brief_periods(brief):
        check_output = check_output.replace(period, "")
    # 只比对指标与结论的数值：口径说明、洞察、风险等段落里的散文数字不是“诊断数值”，
    # 方法说明引用任务书口径时不应被误判为泄露。
    values = metric_values(diagnosis)
    leaked = sorted(
        set(
            token
            for token in numeric_literals(values)
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
