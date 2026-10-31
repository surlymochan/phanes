#!/usr/bin/env python3
"""Generate a numeric-free data-method note from a brief and diagnosis."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, List, Tuple


NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?%?")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


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


def render(brief: dict, diagnosis: dict) -> str:
    method = brief_method(brief)
    periods = brief.get("观察期间", "未提供")
    if isinstance(periods, list):
        period_text = "、".join(str(period) for period in periods)
    else:
        period_text = str(periods)

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
    return "\n".join(lines).rstrip() + "\n"


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
    args = parser.parse_args(argv)

    brief = load_json(args.brief)
    diagnosis = load_json(args.data)
    output = render(brief, diagnosis)
    # Period labels and array indexes are deliberately present in source paths;
    # exclude those structural strings from the value-only leak check.
    check_output = re.sub(r"`[^`]*`", "", output)
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
