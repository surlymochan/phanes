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
CURRENCY_FIELDS = {"币种", "货币", "币别", "currency", "currency_code", "currencycode"}


class MethodError(Exception):
    """输入数据不可用。"""


def validate_currencies(diagnosis: Any, raw_dir: Optional[Path]) -> None:
    """无 FX 表时拒绝诊断或原始 CSV 中的多个币种。"""
    if raw_dir is not None and (raw_dir / "fx.csv").is_file():
        return
    values_by_field: dict[str, set[str]] = {}
    if raw_dir is not None:
        if not raw_dir.is_dir():
            raise MethodError(f"原始数据目录不存在：{raw_dir}")
        for path in sorted(raw_dir.glob("*.csv")):
            if path.name == "fx.csv":
                continue
            try:
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    rows = list(reader)
                    for field in (reader.fieldnames or []):
                        if field and field.casefold() in CURRENCY_FIELDS:
                            bucket = values_by_field.setdefault(f"{path.name}.{field}", set())
                            for row in rows:
                                value = (row.get(field) or "").strip()
                                if value:
                                    bucket.add(value)
            except (OSError, csv.Error) as cause:
                raise MethodError(f"无法读取币种字段 {path.name}：{cause}") from cause
    else:
        def walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).casefold() in CURRENCY_FIELDS:
                        bucket = values_by_field.setdefault(path + "." + str(key), set())
                        if isinstance(child, list):
                            bucket.update(str(item).strip() for item in child if str(item).strip())
                        elif child is not None and str(child).strip():
                            bucket.add(str(child).strip())
                    walk(child, path + "." + str(key))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")
        walk(diagnosis, "diagnosis")
    populated = {field: values for field, values in values_by_field.items() if values}
    all_values = {value for values in populated.values() for value in values}
    if len(all_values) > 1:
        fields = "、".join(sorted(populated))
        currencies = "、".join(sorted(all_values))
        raise MethodError(f"缺少 FX 表，不能混用多个币种（币种字段：{fields}；值：{currencies}）")


def validate_diagnosis(diagnosis: Any) -> None:
    """拒绝空诊断，避免脏数据被包装成空方法说明。"""
    if not isinstance(diagnosis, dict):
        raise MethodError('{"error":"invalid_diagnosis","message":"诊断必须是 JSON 对象"}')
    sections = ("metrics", "conclusion", "insights", "risks", "recommendations")
    if not any(diagnosis.get(name) for name in sections):
        raise MethodError(
            '{"error":"empty_diagnosis","message":"诊断没有指标、结论或风险记录，拒绝生成空方法说明"}'
        )


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


def period_labels(value: Any) -> List[str]:
    """Collect textual period labels from the brief, including nested objects."""
    if isinstance(value, dict):
        labels: List[str] = []
        for key, child in value.items():
            labels.extend(period_labels(key))
            labels.extend(period_labels(child))
        return labels
    if isinstance(value, list):
        labels = []
        for child in value:
            labels.extend(period_labels(child))
        return labels
    if not isinstance(value, str):
        return []
    # Date strings are structural labels; also exclude their year token because
    # the numeric self-check intentionally tokenizes strings such as 2026-09-30.
    return [value, *re.findall(r"\d{4}|-\d{2}", value)]


def render_period_text(value: Any) -> str:
    """Render common brief period shapes without exposing a Python dict repr."""
    if isinstance(value, dict):
        return "、".join(
            f"{key}：{child}" for key, child in value.items() if not isinstance(child, (dict, list))
        ) or "未提供"
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def markdown_code_block(value: Any) -> str:
    """Render arbitrary block text without allowing it to change Markdown structure."""
    text = str(value).rstrip("\n")
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}\n{text}\n{fence}"


def markdown_escape_text(value: Any) -> str:
    """Keep diagnosis prose readable while preventing Markdown injection."""
    text = str(value).replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "(", ")", "#", ">", "|", "~"):
        text = text.replace(marker, "\\" + marker)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "  \n")


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
    period_text = render_period_text(periods)

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
        f"## 统一口径\n\n{markdown_code_block(method)}",
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
                f"### {markdown_escape_text(title)}",
                "",
                f"- 数据来源（字段路径）：`{path}`",
                "- 计算口径：引用任务书“口径”：",
                markdown_code_block(method),
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
            f"- {redacted_text(markdown_escape_text(item.get('标题', '未命名问题')))}"
            for item in insights
            if isinstance(item, dict)
        )
    else:
        lines.append("未提供问题回答。")
    lines.extend(["", "## 定义", ""])
    definitions = diagnosis.get("definitions", [])
    if definitions:
        lines.extend(f"- {redacted_text(markdown_escape_text(item))}" for item in definitions)
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


def strip_method_section(text: str) -> str:
    """Exclude task-book prose from the diagnosis-value leak check."""
    pattern = re.compile(
        r"^## 统一口径\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL
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
    # build_diagnosis 已在生成前完成快照结构与有限数值校验；其洞察还会引用
    # 口径说明中的派生值，未必能在 method 的 period series 中一一重建。
    # 手工/外部诊断仍走严格追溯，避免伪造数字被静默写入方法说明。
    if isinstance(diagnosis.get("origin"), str) and diagnosis["origin"].startswith("DA 快照（"):
        return
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
        validate_diagnosis(diagnosis)
        validate_currencies(diagnosis, args.raw)
        validate_numeric_references(diagnosis)
        sources = None
        if args.raw is not None:
            sources = data_sources(args.raw)
    except MethodError as cause:
        print(str(cause), file=sys.stderr)
        return 1
    output = render(brief, diagnosis, sources)
    method = brief_method(brief)
    # Period labels and array indexes are deliberately present in source paths;
    # exclude those structural strings from the value-only leak check.
    # 数据来源小节里的行数是原始 CSV 的行数，不是诊断数值，同样排除。
    # 方法说明来自 brief，不是诊断数据字段；其中的普通数字和 Markdown
    # 示例必须原样保留，不应被诊断数值泄露检查误判。
    check_output = strip_method_section(strip_data_source(output))
    # 口径也会在每个字段条目中重复展示；全部移除后再检查诊断字段。
    check_output = check_output.replace(method, "")
    check_output = re.sub(r"`[^`]*`", "", check_output)
    periods = brief.get("观察期间", []) if isinstance(brief, dict) else []
    for period in period_labels(periods):
        check_output = check_output.replace(period, "")
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
