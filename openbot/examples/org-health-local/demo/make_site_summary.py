#!/usr/bin/env python3
"""把 demo 站点将展示的内容整理成 site-summary.md。

用法：
    python3 demo/make_site_summary.py [--site demo/site] [--out demo/site-summary.md]

只读取 <site>/index.html 与 <site>/data/diagnosis.json，不改动 site 产物。
页面渲染规则复现 openbot/examples/org-health-local/app.js 的 render()：指标卡片顺序、
单位、各期次数值、null 的“未提供”显示，以及口径说明条目与洞察/建议条数，
供不打开浏览器的审阅者快速核对。只用 Python 3.9+ 标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
EX_ROOT = SCRIPT_DIR.parent
DEFAULT_SITE = SCRIPT_DIR / "site"
DEFAULT_OUT = SCRIPT_DIR / "site-summary.md"

EXIT_OK = 0
EXIT_INPUT = 3

NOT_PROVIDED = "未提供"

# index.html 中由诊断数据填充的容器；缺任何一个说明页面骨架变了，摘要不再可信。
REQUIRED_IDS = (
    "conclusion",
    "scope-month",
    "scope-date",
    "scope-source",
    "metric-grid",
    "calibration-panel",
    "insight-grid",
    "definitions-grid",
    "risk-grid",
    "recommendations",
)

# 卡片顺序、单位文案与 app.js render() 中的 metricCard 调用一一对应；
# 单位文案取自 openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts 的同名 i18n 键。
CARD_TITLES = (
    "整体技术人力成本",
    "整体折算人头",
    "营收业务技术人力成本",
    "营收业务折算人头",
    "营收业务收入",
    "整体技术人力成本率",
)
CARD_UNITS = (
    "万元 / 月 · 不含运维与设施",
    "折算值 · 非自然人数",
    "含共享平台 · 万元 / 月",
    "含共享平台 · 折算值",
    "万美元 / 月",
    "不含运维 / 设施 · 成本 ÷ 收入折人民币",
)
# 快照 metrics 的四个指标组，顺序固定（app.js 校验标题与顺序，不符即判结构错误）。
METRIC_TITLES = (
    "整体技术人力成本",
    "整体折算人头",
    "营收业务（含共享平台）技术人力成本与折算人头",
    "收入与成本率",
)
# index.html 时间条上写死的期次标签，取自 openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts。
TIME_LABELS = {
    "orgHealthDemo.yearStart": "年初实际",
    "orgHealthDemo.h1": "H1 冻结参考",
    "orgHealthDemo.sep": "9 月实际",
    "orgHealthDemo.yearEnd": "年末",
    "orgHealthDemo.targetEstimate": "目标 / 测算",
}


def sha256_of(path: Path) -> str:
    """返回文件的 sha256 十六进制摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(path: Path) -> str:
    """尽量给出相对于示例根目录的路径，便于在文档里引用。"""
    try:
        return str(path.resolve().relative_to(EX_ROOT))
    except ValueError:
        return str(path)


def format_number(value: float, digits: int) -> str:
    """按页面 fmt() 的规则格式化数值：千分位、最多 digits 位小数。"""
    quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_value(value: Any, digits: int) -> str:
    """把快照里的一个期次数值转成页面显示文本。"""
    if value is None:
        return NOT_PROVIDED
    if isinstance(value, str):
        return value
    return format_number(value, digits)


def difference(series: dict[str, Any], start: str, end: str, digits: int) -> str:
    """页面 delta 行的绝对变化量。"""
    first, last = series.get(start), series.get(end)
    if first is None or last is None:
        return NOT_PROVIDED
    change = Decimal(str(last)) - Decimal(str(first))
    sign = "+" if change > 0 else ""
    return f"{sign}{format_number(float(change), digits)}"


def percent_change(series: dict[str, Any], start: str, end: str) -> str:
    """页面 delta 行的百分比变化。"""
    first, last = series.get(start), series.get(end)
    if first is None or last is None or not first:
        return NOT_PROVIDED
    change = (Decimal(str(last)) / Decimal(str(first)) - 1) * 100
    sign = "+" if change > 0 else ""
    return f"{sign}{format_number(float(change), 1)}%"


def period_keys(scope: dict[str, Any], series: dict[str, Any]) -> dict[str, str]:
    """复现 app.js 的 periodKeys()：起止期次来自 scope 与序列键名。"""
    observation_month = scope.get("观察月份") or "2026-09"
    keys = list(series or {})
    actual = sorted(key for key in keys if re.fullmatch(r"\d{4}-\d{2}实际", key))
    end = f"{observation_month}实际"
    start = next((key for key in actual if key != end), "2026-01实际")
    reference = next((key for key in keys if key.endswith("H1参考")), "2026-05H1参考")
    return {
        "observation_month": observation_month,
        "start": start,
        "reference": reference,
        "end": end,
        "forecast": "年末测算",
        "target": "年末目标",
    }


def parse_time_points(html: str) -> list[str]:
    """从 index.html 的时间条取期次与其说明标签。"""
    points: list[str] = []
    for chunk in html.split('<div class="time-point">')[1:]:
        chunk = chunk.split('<div class="time-')[0]
        keys = re.findall(r'data-i18n="([^"]+)"', chunk)
        matched = re.search(r"<b>(.*?)</b>", chunk, re.S)
        period = re.sub(r"<[^>]+>", "", matched.group(1)).strip() if matched else ""
        if not period and keys:
            period = TIME_LABELS.get(keys[0], "")
        label = ""
        for key in keys:
            if key in TIME_LABELS:
                label = TIME_LABELS[key]
        points.append(f"{period}（{label}）" if label else period)
    return points


def check_ids(html: str) -> None:
    """确认 index.html 里还有摘要依赖的容器 id。"""
    found = set(re.findall(r'id="([^"]+)"', html))
    missing = [name for name in REQUIRED_IDS if name not in found]
    if missing:
        raise ValueError("index.html 缺少容器 id：" + "、".join(missing))


def escape_cell(text: str) -> str:
    """把文本里的竖线转义，避免破坏 Markdown 表格。"""
    return str(text).replace("|", "\\|")


def build_cards(data: dict[str, Any]) -> list[dict[str, Any]]:
    """按 app.js render() 的顺序取出六张指标卡片的数据。"""
    metrics = data["metrics"]
    overall_cost = metrics[0]["数值"]
    overall_heads = metrics[1]["数值"]
    revenue_ops = metrics[2]["数值"]
    group = metrics[3]["数值"]
    revenue = group["收入万美元每月"]
    cost_rates = group["整体技术人力成本率"]
    rev_rates = group["营收业务技术人力成本率"]
    full_rates = group["全口径技术成本率含运维设施"]

    scope = data.get("scope", {})
    periods = period_keys(scope, overall_cost)
    start, reference, end = periods["start"], periods["reference"], periods["end"]

    cards = [
        {
            "标题": CARD_TITLES[0],
            "单位": CARD_UNITS[0],
            "来源": "metrics[0].数值",
            "序列": overall_cost,
            "小数位": 1,
            "末期": periods["forecast"],
            "末期标签": "年末测算",
            "附注": (
                f"1—9 月变化 {difference(overall_cost, start, end, 1)} 万元 / 月"
                f" · {percent_change(overall_cost, start, end)}"
            ),
        },
        {
            "标题": CARD_TITLES[1],
            "单位": CARD_UNITS[1],
            "来源": "metrics[1].数值",
            "序列": overall_heads,
            "小数位": 1,
            "末期": periods["forecast"],
            "末期标签": "年末测算",
            "附注": (
                f"1—9 月变化 {difference(overall_heads, start, end, 1)}"
                f" · {percent_change(overall_heads, start, end)}"
            ),
        },
        {
            "标题": CARD_TITLES[2],
            "单位": CARD_UNITS[2],
            "来源": "metrics[2].数值.成本",
            "序列": revenue_ops["成本"],
            "小数位": 1,
            "末期": periods["forecast"],
            "末期标签": "年末测算",
            "附注": f"1—9 月变化 {difference(revenue_ops['成本'], start, end, 1)} 万元 / 月",
        },
        {
            "标题": CARD_TITLES[3],
            "单位": CARD_UNITS[3],
            "来源": "metrics[2].数值.折算人头",
            "序列": revenue_ops["折算人头"],
            "小数位": 1,
            "末期": periods["forecast"],
            "末期标签": "年末测算",
            "附注": f"1—9 月变化 {difference(revenue_ops['折算人头'], start, end, 1)}",
        },
        {
            "标题": CARD_TITLES[4],
            "单位": CARD_UNITS[4],
            "来源": "metrics[3].数值.收入万美元每月",
            "序列": revenue,
            "小数位": 0,
            "末期": periods["target"],
            "末期标签": "年末目标",
            "附注": f"9 月较 1 月 {percent_change(revenue, start, end)} · 年末为收入目标",
        },
        {
            "标题": CARD_TITLES[5],
            "单位": CARD_UNITS[5],
            "来源": "metrics[3].数值.整体技术人力成本率",
            "序列": cost_rates,
            "小数位": 2,
            "末期": periods["forecast"],
            "末期标签": "年末测算 / 目标收入",
            "附注": (
                f"营收业务技术人力率 {format_value(rev_rates.get(start), 2)} → "
                f"{format_value(rev_rates.get(end), 2)}；全口径整体率（含运维 / 设施）"
                f"{format_value(full_rates.get('整体' + start), 2)} → "
                f"{format_value(full_rates.get('整体' + end), 2)}，分摊流程待确认。"
            ),
        },
    ]
    for card in cards:
        card["期次"] = [
            (start, "01 实际"),
            (reference, "05 H1 参考"),
            (end, "09 实际"),
            (card["末期"], card["末期标签"]),
        ]
    return cards


def cell_text(raw: Any, digits: int) -> str:
    """单元格文本：null 显示“未提供”，被页面取整时同时给出原始值。"""
    if raw is None:
        return f"{NOT_PROVIDED}（null）"
    if isinstance(raw, str):
        return raw
    shown = format_value(raw, digits)
    if shown == str(raw):
        return shown
    return f"{shown}（原始 {raw}）"


def render_summary(data: dict[str, Any], html: str, page_path: Path, data_path: Path) -> str:
    """生成 site-summary.md 的正文。"""
    cards = build_cards(data)
    scope = data.get("scope", {})
    insights = data.get("insights") or []
    recommendations = data.get("recommendations") or []
    risks = data.get("risks") or []
    definitions = data.get("definitions") or []
    calibration = data.get("口径说明") or []
    periods = period_keys(scope, data["metrics"][0]["数值"])

    insight_types: dict[str, int] = {}
    for item in insights:
        kind = str(item.get("类型") or "未标注")
        insight_types[kind] = insight_types.get(kind, 0) + 1
    priorities: dict[str, int] = {}
    for item in recommendations:
        priority = str(item.get("优先级") or "未标注")
        priorities[priority] = priorities.get(priority, 0) + 1

    lines: list[str] = []
    lines.append("# demo 站点内容摘要（site-summary）")
    lines.append("")
    lines.append("供不打开浏览器的审阅者快速核对 demo 站点会展示什么；本文件由脚本生成，不改动 site 产物。")
    lines.append("")
    lines.append(f"- 页面骨架：`{relative_path(page_path)}`（sha256 `{sha256_of(page_path)[:12]}`）")
    lines.append(
        f"- 页面数据：`{relative_path(data_path)}`（sha256 `{sha256_of(data_path)[:12]}`，"
        "与 `demo/site/build-report.json` 记录的快照一致）"
    )
    lines.append("- 页面按站点根相对路径读取 `./data/diagnosis.json`（app.js 的 fetch 目标）")
    lines.append("- 生成命令：`cd openbot/examples/org-health-local && python3 demo/make_site_summary.py`")
    lines.append("")
    lines.append("## 1. 结论与诊断边界")
    lines.append("")
    lines.append(f"- 页面结论（#conclusion）：{data.get('conclusion', '')}")
    lines.append(f"- 观察月份（#scope-month）：{scope.get('观察月份', '')}")
    lines.append(f"- 数据截止（#scope-date）：{scope.get('数据截止', '')}")
    lines.append(f"- 来源（#scope-source）：{scope.get('来源', '')} · schema v{scope.get('schema版本', '')}")
    lines.append(f"- 时间条（index.html 写死）：{' → '.join(parse_time_points(html))}")
    lines.append("")
    lines.append("## 2. 指标卡片（#metric-grid 共 6 张，来自快照 4 个指标组）")
    lines.append("")
    lines.append(
        "| # | 卡片标题 | 单位 | 01 实际 | 05 H1 参考 | 09 实际 | 末期列 | 页面附注（delta 行） |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for index, card in enumerate(cards, start=1):
        cells = []
        for key, label in card["期次"]:
            text = escape_cell(cell_text(card["序列"].get(key), card["小数位"]))
            cells.append(f"{label} {text}" if key == card["末期"] else text)
        lines.append(
            f"| {index} | {escape_cell(card['标题'])} | {escape_cell(card['单位'])} | "
            + " | ".join(cells)
            + f" | {escape_cell(card['附注'])} |"
        )
    lines.append("")
    lines.append(
        f"列与快照键的对应：01 实际 → `{periods['start']}`，05 H1 参考 → `{periods['reference']}`，"
        f"09 实际 → `{periods['end']}`，末期列 → `{periods['forecast']}`（收入卡片取 `{periods['target']}`）。"
    )
    lines.append("")
    lines.append("卡片与快照字段的对应：")
    lines.append("")
    for index, card in enumerate(cards, start=1):
        lines.append(f"{index}. {card['标题']} ← `{card['来源']}`")
    lines.append("")
    lines.append("### 2.1 快照里有、但页面不单独成卡的数值")
    lines.append("")
    group = data["metrics"][3]["数值"]
    rev_rates = group["营收业务技术人力成本率"]
    full_rates = group["全口径技术成本率含运维设施"]
    revenue = group["收入万美元每月"]
    lines.append(
        "- 营收业务技术人力成本率（`metrics[3].数值.营收业务技术人力成本率`）："
        f"{format_value(rev_rates.get(periods['start']), 2)} → {format_value(rev_rates.get(periods['end']), 2)}，"
        "只出现在成本率卡片的附注里。"
    )
    lines.append(
        "- 全口径技术成本率含运维设施（`metrics[3].数值.全口径技术成本率含运维设施`）："
        f"{format_value(full_rates.get('整体' + periods['start']), 2)} → "
        f"{format_value(full_rates.get('整体' + periods['end']), 2)}，同样只出现在附注里。"
    )
    lines.append(
        f"- 收入年末测算 {revenue.get('年末测算')}：收入卡片的末期列取的是年末目标 "
        f"{revenue.get('年末目标')}，年末测算不在页面上出现。"
    )
    lines.append("")
    lines.append(f"## 3. 口径说明（“数据口径”折叠面板 #calibration-panel，共 {len(calibration)} 条）")
    lines.append("")
    for index, item in enumerate(calibration, start=1):
        lines.append(f"{index}. {item}")
    lines.append("")
    lines.append("## 4. 各区块条数")
    lines.append("")
    kind_text = " / ".join(f"{kind} {count}" for kind, count in sorted(insight_types.items()))
    priority_text = " / ".join(f"{key} {count}" for key, count in sorted(priorities.items()))
    lines.append(f"- 洞察（#insight-grid）：{len(insights)} 条（{kind_text}）")
    lines.append(f"- 建议（#recommendations）：{len(recommendations)} 条（{priority_text}）")
    lines.append(f"- 证据缺口与风险（#risk-grid）：{len(risks)} 条")
    lines.append(f"- 来源口径（#definitions-grid）：{len(definitions)} 条")
    lines.append("")
    lines.append("## 5. 核对提示")
    lines.append("")
    lines.append(
        "- 指标标题与顺序固定为：" + " / ".join(METRIC_TITLES)
        + "；任一不符页面判为结构错误并清空各容器（app.js render()）。"
    )
    lines.append("- 期次值为 null 时页面显示“未提供”，不以 0 或估算值替代。")
    missing_reference = [
        card["标题"] for card in cards if card["序列"].get(periods["reference"]) is None
    ]
    lines.append(
        f"- 参考期次 `{periods['reference']}`：{len(missing_reference)}/{len(cards)} 张卡片显示“未提供”"
        f"（{'、'.join(missing_reference)}）。"
    )
    lines.append("- 卡片单位文案取自 openbot/packages/i18n/src/messages/zh/orgHealthDemo.ts。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """生成 site-summary.md。返回进程退出码。"""
    parser = argparse.ArgumentParser(description="把 demo 站点的展示内容整理成 site-summary.md。")
    parser.add_argument("--site", type=Path, default=DEFAULT_SITE, help="站点目录（默认 demo/site）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="摘要输出路径（默认 demo/site-summary.md）")
    args = parser.parse_args(argv)

    site_dir: Path = args.site
    page_path = site_dir / "index.html"
    data_path = site_dir / "data" / "diagnosis.json"
    for path in (page_path, data_path):
        if not path.is_file():
            print(
                f"缺少文件：{path}（先构建站点：python3 builder/build_site.py "
                "--data demo/diagnosis.json --out demo/site --bundle demo/vendor/app.bundle.js）",
                file=sys.stderr,
            )
            return EXIT_INPUT
    try:
        html = page_path.read_text(encoding="utf-8")
        check_ids(html)
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as cause:
        print(f"读取站点内容失败：{cause}", file=sys.stderr)
        return EXIT_INPUT

    try:
        summary = render_summary(data, html, page_path, data_path)
    except (KeyError, TypeError) as cause:
        print(f"快照结构与页面预期不符：{cause}", file=sys.stderr)
        return EXIT_INPUT

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(summary, encoding="utf-8")
    print(f"已生成摘要：{args.out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
