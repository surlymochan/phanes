#!/usr/bin/env python3
"""按任务书口径把原始数据归并成 schema v4 诊断快照。

用法：
    python3 da/build_diagnosis.py --brief <brief.json> --raw <dir> --out <diagnosis.json>

输入：
    任务书 JSON：`目标`、`观察期间`、`口径`、`必答问题`、`数据需求`、`禁止事项`、`schema版本`。
    原始数据目录：四张 CSV，表头固定（见 REQUIRED_COLUMNS）：
        cost.csv     月度技术人力成本（万元/月）与运维设施成本
        heads.csv    月度折算人头
        revenue.csv  月度收入（万美元/月）
        forecast.csv 年末测算与年末目标（`期次` 列为 `年末测算` / `年末目标`）

输出：schema v4 诊断快照，结构见 `schema/diagnosis.schema.json`。
只使用 Python 3.9+ 标准库。输出确定性：同一份任务书与原始数据得到同一份 JSON，键序固定。
缺数据的期次写 null，不填估算值；整体口径的参考期次按 schema v4 固定为 null。
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import validate  # noqa: E402

SCHEMA_PATH = ROOT / "schema" / "diagnosis.schema.json"
SAMPLE_RAW_DIR = Path(__file__).resolve().parent / "sample-raw"
SCHEMA_VERSION = 4

EXIT_OK = 0
EXIT_DATA = 1

ACTUAL_SUFFIX = "实际"
REFERENCE_SUFFIX = "H1参考"
FORECAST_LABEL = "年末测算"
TARGET_LABEL = "年末目标"
FULL_RATE_PREFIX = "整体"

COST_FILE = "cost.csv"
HEADS_FILE = "heads.csv"
REVENUE_FILE = "revenue.csv"
FORECAST_FILE = "forecast.csv"
FX_FILE = "fx.csv"

REQUIRED_COLUMNS = {
    COST_FILE: ("月份", "整体技术人力成本万元", "营收业务技术人力成本万元", "运维设施成本万元"),
    HEADS_FILE: ("月份", "整体折算人头", "营收业务折算人头"),
    REVENUE_FILE: ("月份", "收入万美元"),
    FORECAST_FILE: (
        "期次",
        "整体技术人力成本万元",
        "营收业务技术人力成本万元",
        "整体折算人头",
        "营收业务折算人头",
        "运维设施成本万元",
        "收入万美元",
    ),
}

OVERALL_COST = "整体技术人力成本万元"
REVOPS_COST = "营收业务技术人力成本万元"
OPS_COST = "运维设施成本万元"
OVERALL_HEADS = "整体折算人头"
REVOPS_HEADS = "营收业务折算人头"
REVENUE = "收入万美元"

COLUMN_NAMES = {
    OVERALL_COST: "整体技术人力成本",
    REVOPS_COST: "营收业务技术人力成本",
    OPS_COST: "运维设施成本",
    OVERALL_HEADS: "整体折算人头",
    REVOPS_HEADS: "营收业务折算人头",
    REVENUE: "收入",
}

METRIC_TITLES = (
    "整体技术人力成本",
    "整体折算人头",
    "营收业务（含共享平台）技术人力成本与折算人头",
    "收入与成本率",
)

MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_TOKEN = re.compile(r"\d{4}-\d{2}")


class BriefError(Exception):
    """任务书内容不合法。"""


class RawDataError(Exception):
    """原始数据不合法。"""


class SnapshotError(Exception):
    """生成的快照不满足结构定义。"""


def load_brief(path: Path) -> dict:
    """读取任务书 JSON。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as cause:
        raise BriefError(f"无法读取任务书 {path}：{cause}") from cause
    try:
        brief = json.loads(text)
    except json.JSONDecodeError as cause:
        raise BriefError(f"任务书 {path} 不是合法 JSON：{cause}") from cause
    if not isinstance(brief, dict):
        raise BriefError("任务书必须是 JSON 对象")
    return brief


def first_text(source: dict, *names: str) -> str | None:
    """按顺序取第一个非空字符串字段。"""
    for name in names:
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def last_day_of_month(month: str) -> str:
    """返回 YYYY-MM 对应月份的最后一天。"""
    year, number = (int(part) for part in month.split("-"))
    return f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def parse_periods(brief: dict) -> dict:
    """从任务书 `观察期间` 解析起始、参考、截止期次与数据截止日。"""
    window = brief.get("观察期间")
    start = reference = end = as_of = None
    if isinstance(window, dict):
        start = first_text(window, "起始期次", "起始", "start")
        reference = first_text(window, "参考期次", "参考", "reference")
        end = first_text(window, "截止期次", "截止", "end")
        as_of = first_text(window, "数据截止", "数据截止日期", "asOf")
        if not start and isinstance(window.get("期次"), list):
            anchors = [item.strip() for item in window["期次"] if isinstance(item, str) and item.strip()]
            # Boss 格式按期次顺序映射：首个“实际”为起始，含“参考”为参考，观察月份对应的“实际”为截止。
            actuals = [item for item in anchors if item.endswith(ACTUAL_SUFFIX)]
            references = [item for item in anchors if "参考" in item]
            observed = first_text(window, "观察月份")
            start = actuals[0][:-len(ACTUAL_SUFFIX)] if actuals else None
            end = next(
                (item[:-len(ACTUAL_SUFFIX)] for item in actuals if item == f"{observed}{ACTUAL_SUFFIX}"),
                None,
            )
            reference = references[0].replace(REFERENCE_SUFFIX, "").replace("参考", "") if references else None
    elif isinstance(window, str):
        months = sorted(set(MONTH_TOKEN.findall(window)))
        if len(months) == 1:
            start = end = months[0]
        elif len(months) == 2:
            start, end = months
        elif len(months) >= 3:
            start, reference, end = months[0], months[1], months[-1]
    else:
        raise BriefError("任务书缺少 `观察期间` 对象")
    if not start or not end:
        raise BriefError("任务书 `观察期间` 缺少起始期次或截止期次")
    if not reference:
        raise BriefError("任务书 `观察期间` 缺少参考期次（schema v4 的 H1 参考锚点）")
    for name, value in (("起始期次", start), ("参考期次", reference), ("截止期次", end)):
        if not MONTH_PATTERN.match(value):
            raise BriefError(f"任务书 `观察期间` 的{name}不是 YYYY-MM：{value}")
    if as_of and not DATE_PATTERN.match(as_of):
        raise BriefError(f"任务书 `观察期间` 的数据截止不是 YYYY-MM-DD：{as_of}")
    return {
        "start": start,
        "reference": reference,
        "end": end,
        "as_of": as_of or last_day_of_month(end),
    }


def parse_calibration(brief: dict, raw_dir: Path) -> tuple[float, list[str]]:
    """从任务书 `口径` 解析汇率与口径说明文字。"""
    calibration = brief.get("口径")
    if not isinstance(calibration, dict):
        raise BriefError("任务书缺少 `口径` 对象")
    rate = calibration.get("汇率", calibration.get("exchange_rate"))
    if rate is None:
        fx_path = raw_dir / FX_FILE
        if fx_path.is_file():
            rows = read_table(fx_path, ("人民币每美元",))
            rate = rows[0].get("人民币每美元") if rows else None
    if isinstance(rate, dict):
        rate = first_text(rate, "人民币每美元", "人民币兑美元", "人民币/美元")
    if isinstance(rate, str):
        try:
            rate = float(rate)
        except ValueError as cause:
            raise BriefError(f"任务书 `口径.汇率` 不是数值：{rate}") from cause
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
        raise BriefError("任务书 `口径.汇率` 缺失或不是正数，无法计算成本率")
    definitions: list[str] = []
    for key, value in calibration.items():
        if key in ("汇率", "exchange_rate"):
            continue
        if isinstance(value, str) and value.strip():
            definitions.append(f"{key}：{value.strip()}")
    return float(rate), definitions


def parse_text_list(brief: dict, name: str) -> list[str]:
    """读取任务书里的字符串数组字段。"""
    value = brief.get(name)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def detect_nature(brief: dict, raw_dir: Path) -> str:
    """确定原始数据性质：任务书显式声明优先，否则按是否使用仓库内合成样本判断。"""
    declared = first_text(brief, "数据性质")
    if declared:
        return declared
    try:
        if raw_dir.resolve() == SAMPLE_RAW_DIR:
            return "合成示例"
    except OSError:
        pass
    return "未标注"


def parse_plan(brief: dict, raw_dir: Path) -> dict:
    """把任务书解析成计算用的口径计划。"""
    version = brief.get("schema版本")
    if version is not None and version != SCHEMA_VERSION:
        raise BriefError(f"任务书 `schema版本` 为 {version}，与 schema v{SCHEMA_VERSION} 不一致")
    periods = parse_periods(brief)
    rate, definitions = parse_calibration(brief, raw_dir)
    labels = {
        "start": f"{periods['start']}{ACTUAL_SUFFIX}",
        "reference": f"{periods['reference']}{REFERENCE_SUFFIX}",
        "end": f"{periods['end']}{ACTUAL_SUFFIX}",
        "forecast": FORECAST_LABEL,
        "target": TARGET_LABEL,
    }
    return {
        "periods": periods,
        "labels": labels,
        "rate": rate,
        "definitions": definitions,
        "goals": parse_text_list(brief, "目标"),
        "questions": parse_text_list(brief, "必答问题"),
        "prohibitions": parse_text_list(brief, "禁止事项"),
        "nature": detect_nature(brief, raw_dir),
    }


def read_table(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    """读取 CSV 并检查必需列。缺列时抛出 RawDataError。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as cause:
        raise RawDataError(f"无法读取原始数据 {path}：{cause}") from cause
    reader = csv.DictReader(text.splitlines())
    fieldnames = reader.fieldnames or []
    missing = [name for name in required if name not in fieldnames]
    if missing:
        raise RawDataError(f"原始数据 {path.name} 缺少必需列：{'、'.join(missing)}")
    rows: list[dict[str, str]] = []
    for number, row in enumerate(reader, start=2):
        if None in row:
            raise RawDataError(f"原始数据 {path.name} 第 {number} 行的字段数多于表头")
        rows.append(row)
    return rows


def parse_number(value: str | None, path: Path, column: str, number: int) -> float | None:
    """把单元格解析为数值；空单元格表示该期次缺数据。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number_value = float(text)
    except ValueError as cause:
        raise RawDataError(f"原始数据 {path.name} 第 {number} 行的 {column} 不是数值：{text}") from cause
    if not math.isfinite(number_value):
        raise RawDataError(f"原始数据 {path.name} 第 {number} 行的 {column} 不是有限数值：{text}")
    return round(number_value, 4)


def load_table(path: Path, key_column: str, columns: tuple[str, ...]) -> dict[str, dict[str, float | None]]:
    """读取一张 CSV，按期次索引各列数值。"""
    rows = read_table(path, (key_column,) + columns)
    table: dict[str, dict[str, float | None]] = {}
    for number, row in enumerate(rows, start=2):
        key = (row.get(key_column) or "").strip()
        if not key:
            raise RawDataError(f"原始数据 {path.name} 第 {number} 行缺少 {key_column}")
        if key in table:
            raise RawDataError(f"原始数据 {path.name} 中 {key_column} {key} 重复")
        table[key] = {name: parse_number(row.get(name), path, name, number) for name in columns}
    return table


def load_raw(raw_dir: Path) -> dict[str, dict[str, dict[str, float | None]]]:
    """读取四张原始数据 CSV。"""
    if not raw_dir.is_dir():
        raise RawDataError(f"原始数据目录不存在：{raw_dir}")
    cost_columns = (OVERALL_COST, REVOPS_COST, OPS_COST)
    heads_columns = (OVERALL_HEADS, REVOPS_HEADS)
    forecast_columns = cost_columns + heads_columns + (REVENUE,)
    return {
        "cost": load_table(raw_dir / COST_FILE, "月份", cost_columns),
        "heads": load_table(raw_dir / HEADS_FILE, "月份", heads_columns),
        "revenue": load_table(raw_dir / REVENUE_FILE, "月份", (REVENUE,)),
        "forecast": load_table(raw_dir / FORECAST_FILE, "期次", forecast_columns),
    }


def cell(table: dict[str, dict[str, float | None]], key: str, column: str) -> float | None:
    """取某个期次某列的数值；缺该期次或该列为空时返回 None。"""
    row = table.get(key)
    if row is None:
        return None
    return row.get(column)


def rate_of(cost: float | None, revenue: float | None, rate: float) -> float | None:
    """成本率 = 成本 ÷（收入按汇率折人民币）。缺任一输入时返回 None。"""
    if cost is None or revenue is None or revenue == 0:
        return None
    return cost / (revenue * rate)


def fmt_number(value: float | None, digits: int = 1) -> str:
    """格式化数值；缺数据时写“未提供”。"""
    if value is None:
        return "未提供"
    return f"{value:,.{digits}f}"


def rate_text(value: float | None) -> str | None:
    """成本率文本；缺数据的期次返回 None。"""
    if value is None:
        return None
    return f"约{value * 100:.1f}%"


def fmt_rate(value: float | None) -> str:
    """格式化成本率文本；缺数据时写“未提供”。"""
    return rate_text(value) or "未提供"


def growth(start: float | None, end: float | None) -> float | None:
    """变化百分比；缺任一端或基数为 0 时返回 None。"""
    if start is None or end is None or start == 0:
        return None
    return (end / start - 1) * 100


def build_facts(plan: dict, raw: dict) -> dict:
    """按任务书期间取出各指标数值并算好成本率。"""
    periods = plan["periods"]
    labels = plan["labels"]
    rate = plan["rate"]
    cost = raw["cost"]
    heads = raw["heads"]
    revenue = raw["revenue"]
    forecast = raw["forecast"]

    overall_cost = {
        "start": cell(cost, periods["start"], OVERALL_COST),
        "reference": cell(cost, periods["reference"], OVERALL_COST),
        "end": cell(cost, periods["end"], OVERALL_COST),
        "forecast": cell(forecast, FORECAST_LABEL, OVERALL_COST),
    }
    overall_heads = {
        "start": cell(heads, periods["start"], OVERALL_HEADS),
        "reference": cell(heads, periods["reference"], OVERALL_HEADS),
        "end": cell(heads, periods["end"], OVERALL_HEADS),
        "forecast": cell(forecast, FORECAST_LABEL, OVERALL_HEADS),
    }
    revops_cost = {
        "start": cell(cost, periods["start"], REVOPS_COST),
        "reference": cell(cost, periods["reference"], REVOPS_COST),
        "end": cell(cost, periods["end"], REVOPS_COST),
        "forecast": cell(forecast, FORECAST_LABEL, REVOPS_COST),
    }
    revops_heads = {
        "start": cell(heads, periods["start"], REVOPS_HEADS),
        "reference": cell(heads, periods["reference"], REVOPS_HEADS),
        "end": cell(heads, periods["end"], REVOPS_HEADS),
        "forecast": cell(forecast, FORECAST_LABEL, REVOPS_HEADS),
    }
    ops_cost = {
        "start": cell(cost, periods["start"], OPS_COST),
        "reference": cell(cost, periods["reference"], OPS_COST),
        "end": cell(cost, periods["end"], OPS_COST),
        "forecast": cell(forecast, FORECAST_LABEL, OPS_COST),
    }
    income = {
        "start": cell(revenue, periods["start"], REVENUE),
        "reference": cell(revenue, periods["reference"], REVENUE),
        "end": cell(revenue, periods["end"], REVENUE),
        "forecast": cell(forecast, FORECAST_LABEL, REVENUE),
        "target": cell(forecast, TARGET_LABEL, REVENUE),
    }
    if overall_cost["reference"] is not None or overall_heads["reference"] is not None:
        raise SnapshotError(
            "schema v4 要求整体口径的参考期次固定为 null；原始数据含同口径数值，"
            "请调整任务书观察期间或原始数据。"
        )
    ratios = {
        "overall": {
            "start": rate_of(overall_cost["start"], income["start"], rate),
            "reference": rate_of(overall_cost["reference"], income["reference"], rate),
            "end": rate_of(overall_cost["end"], income["end"], rate),
            "forecast": rate_of(overall_cost["forecast"], income["forecast"], rate),
        },
        "revops": {
            "start": rate_of(revops_cost["start"], income["start"], rate),
            "reference": rate_of(revops_cost["reference"], income["reference"], rate),
            "end": rate_of(revops_cost["end"], income["end"], rate),
            "forecast": rate_of(revops_cost["forecast"], income["forecast"], rate),
        },
        "full": {
            "start": rate_of(
                None if overall_cost["start"] is None or ops_cost["start"] is None
                else overall_cost["start"] + ops_cost["start"],
                income["start"],
                rate,
            ),
            "reference": rate_of(
                None if overall_cost["reference"] is None or ops_cost["reference"] is None
                else overall_cost["reference"] + ops_cost["reference"],
                income["reference"],
                rate,
            ),
            "end": rate_of(
                None if overall_cost["end"] is None or ops_cost["end"] is None
                else overall_cost["end"] + ops_cost["end"],
                income["end"],
                rate,
            ),
            "forecast": rate_of(
                None if overall_cost["forecast"] is None or ops_cost["forecast"] is None
                else overall_cost["forecast"] + ops_cost["forecast"],
                income["forecast"],
                rate,
            ),
        },
    }
    return {
        "labels": labels,
        "overall_cost": overall_cost,
        "overall_heads": overall_heads,
        "revops_cost": revops_cost,
        "revops_heads": revops_heads,
        "ops_cost": ops_cost,
        "income": income,
        "ratios": ratios,
    }


def series(values: dict, labels: dict) -> dict:
    """把内部期次值转成 schema 的期次键序。"""
    return {
        labels["start"]: values["start"],
        labels["reference"]: values["reference"],
        labels["end"]: values["end"],
        labels["forecast"]: values["forecast"],
    }


def missing_at_reference(plan: dict, raw: dict) -> list[str]:
    """列出参考期次缺同口径数据的字段。"""
    periods = plan["periods"]
    found: list[str] = []
    for table_name, columns in (
        ("cost", (OVERALL_COST, REVOPS_COST, OPS_COST)),
        ("heads", (OVERALL_HEADS, REVOPS_HEADS)),
    ):
        table = raw[table_name]
        for column in columns:
            if cell(table, periods["reference"], column) is None:
                found.append(COLUMN_NAMES[column])
    return found


def covered_months(plan: dict, raw: dict) -> list[str]:
    """列出原始数据实际覆盖的月份。"""
    periods = plan["periods"]
    months = sorted(set(raw["cost"]) | set(raw["heads"]) | set(raw["revenue"]))
    return [month for month in months if periods["start"] <= month <= periods["end"]]


def coverage_note(plan: dict, raw: dict) -> str:
    """说明各表在观察期间内的缺口月份。"""
    periods = plan["periods"]
    months = []
    year, number = (int(part) for part in periods["start"].split("-"))
    while f"{year:04d}-{number:02d}" <= periods["end"]:
        months.append(f"{year:04d}-{number:02d}")
        number += 1
        if number > 12:
            year += 1
            number = 1
    notes = []
    for name, table in (("成本", raw["cost"]), ("折算人头", raw["heads"]), ("收入", raw["revenue"])):
        missing = [month for month in months if month not in table]
        notes.append(f"{name}缺 {'、'.join(missing)}" if missing else f"{name}无缺口")
    return "；".join(notes)


def build_insights(plan: dict, facts: dict, raw: dict) -> list[dict]:
    """生成洞察：事实、推断与边界分别标注。"""
    periods = plan["periods"]
    labels = facts["labels"]
    cost = facts["overall_cost"]
    heads = facts["overall_heads"]
    revops_cost = facts["revops_cost"]
    revops_heads = facts["revops_heads"]
    income = facts["income"]
    ratios = facts["ratios"]
    insights: list[dict] = []

    insights.append(
        {
            "类型": "事实",
            "标题": f"整体技术人力成本 {periods['start']} → {periods['end']}",
            "内容": (
                f"整体技术人力成本由 {fmt_number(cost['start'])} 万元/月（{periods['start']}）"
                f"变为 {fmt_number(cost['end'])} 万元/月（{periods['end']}）；"
                f"年末测算 {fmt_number(cost['forecast'])} 万元/月（测算值，非已发生实际）。"
            ),
        }
    )
    insights.append(
        {
            "类型": "事实",
            "标题": f"整体折算人头 {periods['start']} → {periods['end']}",
            "内容": (
                f"整体折算人头由 {fmt_number(heads['start'])}（{periods['start']}）"
                f"变为 {fmt_number(heads['end'])}（{periods['end']}）；"
                f"年末测算 {fmt_number(heads['forecast'])}（当量折算，非自然人数）。"
            ),
        }
    )
    insights.append(
        {
            "类型": "事实",
            "标题": f"整体技术人力成本率 {fmt_rate(ratios['overall']['start'])} → {fmt_rate(ratios['overall']['end'])}",
            "内容": (
                f"整体技术人力成本率由{fmt_rate(ratios['overall']['start'])}变为"
                f"{fmt_rate(ratios['overall']['end'])}；同期收入由 {fmt_number(income['start'])} 万美元/月"
                f"变为 {fmt_number(income['end'])} 万美元/月，按汇率 {plan['rate']} 折人民币计算。"
            ),
        }
    )
    insights.append(
        {
            "类型": "事实",
            "标题": f"营收业务成本与折算人头 {periods['start']} → {periods['end']}",
            "内容": (
                f"营收业务（含共享平台）技术人力成本由 {fmt_number(revops_cost['start'])} 万元/月变为 "
                f"{fmt_number(revops_cost['end'])} 万元/月；折算人头由 {fmt_number(revops_heads['start'])} 变为 "
                f"{fmt_number(revops_heads['end'])}；营收业务技术人力成本率"
                f"{fmt_rate(ratios['revops']['start'])} → {fmt_rate(ratios['revops']['end'])}。"
            ),
        }
    )

    cost_growth = growth(cost["start"], cost["end"])
    income_growth = growth(income["start"], income["end"])
    if cost_growth is None or income_growth is None:
        insights.append(
            {
                "类型": "边界",
                "标题": "成本与收入增速关系暂不可判",
                "内容": "期初或期末的成本或收入缺同口径数据，无法比较增速，列为证据缺口。",
            }
        )
    elif cost_growth > income_growth:
        insights.append(
            {
                "类型": "推断",
                "标题": "成本增速高于收入增速",
                "内容": (
                    f"整体技术人力成本增幅 {cost_growth:.1f}% 高于收入增幅 {income_growth:.1f}%，"
                    "在收入不变的前提下成本率存在上行压力；该推断基于任务书口径，需真实台账复核。"
                ),
            }
        )
    else:
        insights.append(
            {
                "类型": "推断",
                "标题": "成本增速不高于收入增速",
                "内容": (
                    f"整体技术人力成本增幅 {cost_growth:.1f}%，收入增幅 {income_growth:.1f}%，"
                    "成本率未出现上行压力；该推断基于任务书口径，需真实台账复核。"
                ),
            }
        )

    share_start = None if cost["start"] in (None, 0) or revops_cost["start"] is None else revops_cost["start"] / cost["start"]
    share_end = None if cost["end"] in (None, 0) or revops_cost["end"] is None else revops_cost["end"] / cost["end"]
    if share_start is None or share_end is None:
        insights.append(
            {
                "类型": "边界",
                "标题": "成本结构暂不可判",
                "内容": "期初或期末缺整体或营收业务成本，无法计算成本结构占比。",
            }
        )
    else:
        insights.append(
            {
                "类型": "推断",
                "标题": f"营收业务成本占比 {share_start * 100:.1f}% → {share_end * 100:.1f}%",
                "内容": (
                    f"营收业务成本占整体技术人力成本的 {share_start * 100:.1f}% → {share_end * 100:.1f}%，"
                    f"{'未见明显结构迁移' if abs(share_end - share_start) < 0.005 else '成本结构出现迁移'}；"
                    "该推断基于任务书口径，需真实台账复核。"
                ),
            }
        )

    gaps = missing_at_reference(plan, raw)
    if gaps:
        insights.append(
            {
                "类型": "边界",
                "标题": f"参考期次 {periods['reference']} 缺同口径数据",
                "内容": (
                    f"参考期次 {periods['reference']} 缺少同口径的{'、'.join(gaps)}，"
                    f"快照按口径约定记为 null（{labels['reference']}），未用其他期次或估算值替代。"
                ),
            }
        )
    insights.append(
        {
            "类型": "边界",
            "标题": "年末为测算与目标值",
            "内容": (
                f"{labels['forecast']}与{labels['target']}是前瞻预算与目标估算，不代表已发生实际；"
                "与实际期次比较时须注明口径差异。"
            ),
        }
    )
    if plan["nature"] == "合成示例":
        insights.append(
            {
                "类型": "边界",
                "标题": "合成样本边界",
                "内容": "本快照由 da/sample-raw 下的合成原始数据生成，只用于流程演示，不能用于判断任何真实组织。",
            }
        )
    for question in plan["questions"]:
        kind, content = answer_question(question, facts)
        insights.append({"类型": kind, "标题": question, "内容": content})
    return insights


def answer_question(question: str, facts: dict) -> tuple[str, str]:
    """按关键词把任务书问题对应到已算出的证据；没有对应口径时标为边界。"""
    cost = facts["overall_cost"]
    heads = facts["overall_heads"]
    revops_cost = facts["revops_cost"]
    revops_heads = facts["revops_heads"]
    income = facts["income"]
    ratios = facts["ratios"]
    if "率" in question:
        return (
            "事实",
            f"整体技术人力成本率 {fmt_rate(ratios['overall']['start'])} → {fmt_rate(ratios['overall']['end'])}；"
            f"营收业务技术人力成本率 {fmt_rate(ratios['revops']['start'])} → {fmt_rate(ratios['revops']['end'])}；"
            f"全口径技术成本率（含运维设施）{fmt_rate(ratios['full']['start'])} → {fmt_rate(ratios['full']['end'])}。",
        )
    if "人头" in question:
        return (
            "事实",
            f"整体折算人头 {fmt_number(heads['start'])} → {fmt_number(heads['end'])}；"
            f"营收业务折算人头 {fmt_number(revops_heads['start'])} → {fmt_number(revops_heads['end'])}。",
        )
    if "成本" in question:
        return (
            "事实",
            f"整体技术人力成本 {fmt_number(cost['start'])} → {fmt_number(cost['end'])} 万元/月；"
            f"营收业务技术人力成本 {fmt_number(revops_cost['start'])} → {fmt_number(revops_cost['end'])} 万元/月。",
        )
    if "收入" in question:
        return (
            "事实",
            f"收入 {fmt_number(income['start'])} → {fmt_number(income['end'])} 万美元/月；"
            f"年末测算 {fmt_number(income['forecast'])}、年末目标 {fmt_number(income['target'])} 万美元/月。",
        )
    return (
        "边界",
        "任务书问题在本次原始数据中没有对应口径，已列为缺失证据；补齐数据需求后再回答。",
    )


def build_definitions(plan: dict) -> list[str]:
    """生成来源口径说明。"""
    periods = plan["periods"]
    lines = list(plan["definitions"])
    lines.append(
        f"期间：{periods['start']} 与 {periods['end']} 为已发生实际期次；"
        f"{periods['reference']} 为 H1 参考期次；{FORECAST_LABEL}与{TARGET_LABEL}为测算或目标值。"
    )
    lines.append(
        f"数据截止：{periods['as_of']}；观察月份：{periods['end']}；"
        f"收入折人民币按汇率 {plan['rate']} 计算。"
    )
    lines.append(
        "null 含义：表示该期次未统计、未提供或无同口径对比数据，界面显示为未提供，"
        "不以 0 或其他推测值替代。"
    )
    if plan["prohibitions"]:
        lines.append("任务书禁止事项：" + "；".join(plan["prohibitions"]) + "。")
    return lines


def build_risks(plan: dict, facts: dict, raw: dict) -> list[dict]:
    """生成证据缺口与风险。"""
    periods = plan["periods"]
    gaps = missing_at_reference(plan, raw)
    risks: list[dict] = []
    if gaps:
        risks.append(
            {
                "标题": f"参考期次 {periods['reference']} 数据缺口",
                "内容": (
                    f"参考期次 {periods['reference']} 缺少同口径的{'、'.join(gaps)}，"
                    "快照以 null 表示；页面显示为未提供，不以其他期次或估算值替代。"
                ),
            }
        )
    if facts["ops_cost"]["forecast"] is None:
        risks.append(
            {
                "标题": "全口径年末测算缺口",
                "内容": (
                    f"{FORECAST_LABEL}缺少运维设施成本口径，全口径技术成本率年末测算为 null；"
                    "年末实际值须在年末后按实际台账重算。"
                ),
            }
        )
    if facts["income"]["forecast"] is not None and facts["income"]["target"] is not None:
        risks.append(
            {
                "标题": "年末测算与年末目标并存",
                "内容": (
                    f"收入同时给出{FORECAST_LABEL} {fmt_number(facts['income']['forecast'])} 万美元/月与"
                    f"{TARGET_LABEL} {fmt_number(facts['income']['target'])} 万美元/月，两者口径不同，"
                    "页面分别展示，不做相互替代。"
                ),
            }
        )
    months = covered_months(plan, raw)
    risks.append(
        {
            "标题": "观察期间覆盖不完整",
            "内容": (
                f"原始数据在 {periods['start']} 至 {periods['end']} 期间覆盖 {len(months)} 个月份"
                f"（{'、'.join(months)}），但各表覆盖不一致（{coverage_note(plan, raw)}），"
                "成本变化可能受未覆盖月份影响。"
            ),
        }
    )
    if plan["nature"] == "合成示例":
        risks.append(
            {
                "标题": "合成数据边界",
                "内容": "原始数据为人工构造的合成样本，不含任何真实组织信息，也不构成人员、薪酬或编制结论。",
            }
        )
    return risks


def build_recommendations(plan: dict, facts: dict, raw: dict) -> list[dict]:
    """生成建议：每条带依据、缺失证据与验证方式。"""
    periods = plan["periods"]
    gaps = missing_at_reference(plan, raw)
    recommendations: list[dict] = []
    if gaps:
        recommendations.append(
            {
                "优先级": "P1",
                "标题": f"补齐参考期次 {periods['reference']} 的同口径取数",
                "依据": f"参考期次 {periods['reference']} 缺少{'、'.join(gaps)}，快照只能以 null 表示，"
                "无法给出同口径对照。",
                "缺失证据": f"{periods['reference']} 的整体技术人力成本、折算人头与营收业务技术人力成本明细。",
                "验证方式": f"与财务台账核对 {periods['reference']} 的整体技术人力成本与折算人头，"
                "回填原始数据后重跑 build_diagnosis.py。",
            }
        )
    if facts["ops_cost"]["forecast"] is None:
        recommendations.append(
            {
                "优先级": "P2",
                "标题": "补齐年末运维设施成本口径",
                "依据": f"{FORECAST_LABEL}缺运维设施成本，导致全口径技术成本率年末测算为 null。",
                "缺失证据": "年末运维与设施成本的预算或测算口径说明。",
                "验证方式": "与运维负责人确认年末运维设施成本口径后回填 forecast.csv 并重算。",
            }
        )
    recommendations.append(
        {
            "优先级": "P2",
            "标题": "核对年末测算与年末目标的口径差异",
            "依据": f"收入同时给出{FORECAST_LABEL} {fmt_number(facts['income']['forecast'])} 与"
            f"{TARGET_LABEL} {fmt_number(facts['income']['target'])} 万美元/月，口径不同却同屏展示。",
            "缺失证据": "目标制定依据、测算假设与两者的换算说明。",
            "验证方式": "与业务负责人核对目标口径与测算假设，并在方法说明中记录差异。",
        }
    )
    recommendations.append(
        {
            "优先级": "P2",
            "标题": "补齐逐月原始数据并复核覆盖月份",
            "依据": f"观察期间内各表覆盖不一致（{coverage_note(plan, raw)}），成本变化可能受未覆盖月份影响。",
            "缺失证据": "观察期间逐月的技术人力成本、折算人头与收入明细。",
            "验证方式": "补齐逐月数据后重跑 build_diagnosis.py，并逐月与台账核对差异。",
        }
    )
    return recommendations


def build_conclusion(plan: dict, facts: dict) -> str:
    """生成结论摘要。"""
    periods = plan["periods"]
    cost = facts["overall_cost"]
    income = facts["income"]
    ratios = facts["ratios"]
    return (
        f"{periods['start']} 至 {periods['end']}，整体技术人力成本由 {fmt_number(cost['start'])} 万元/月"
        f"变为 {fmt_number(cost['end'])} 万元/月，整体技术人力成本率由{fmt_rate(ratios['overall']['start'])}"
        f"变为{fmt_rate(ratios['overall']['end'])}，收入由 {fmt_number(income['start'])} 万美元/月变为"
        f" {fmt_number(income['end'])} 万美元/月；参考期次与年末测算的缺口见 risks。"
    )


def build_snapshot(plan: dict, raw: dict) -> dict:
    """生成 schema v4 诊断快照。"""
    periods = plan["periods"]
    facts = build_facts(plan, raw)
    labels = facts["labels"]
    metrics = [
        {"标题": METRIC_TITLES[0], "数值": series(facts["overall_cost"], labels)},
        {"标题": METRIC_TITLES[1], "数值": series(facts["overall_heads"], labels)},
        {
            "标题": METRIC_TITLES[2],
            "数值": {
                "成本": series(facts["revops_cost"], labels),
                "折算人头": series(facts["revops_heads"], labels),
            },
        },
        {
            "标题": METRIC_TITLES[3],
            "数值": {
                "收入万美元每月": {
                    labels["start"]: facts["income"]["start"],
                    labels["reference"]: facts["income"]["reference"],
                    labels["end"]: facts["income"]["end"],
                    labels["forecast"]: facts["income"]["forecast"],
                    labels["target"]: facts["income"]["target"],
                },
                "整体技术人力成本率": ratio_series(facts["ratios"]["overall"], labels),
                "营收业务技术人力成本率": ratio_series(facts["ratios"]["revops"], labels),
                "全口径技术成本率含运维设施": full_rate_series(facts["ratios"]["full"], labels),
            },
        },
    ]
    notes = []
    if plan["goals"]:
        notes.append("目标：" + "；".join(plan["goals"]) + "。")
    notes.append(
        f"期间：{periods['start']} 与 {periods['end']} 为已发生实际期次；{periods['reference']} 为 H1 参考期次；"
        f"{FORECAST_LABEL}与{TARGET_LABEL}为测算或目标值。"
    )
    notes.append(
        "单位：技术人力成本为万元/月（不含运维与设施）；折算人头为当量折算值，非自然人数；"
        "收入为万美元/月；成本率为成本占同期收入按汇率折人民币的比例。"
    )
    notes.append(f"汇率：{plan['rate']} 人民币/美元；数据截止：{periods['as_of']}。")
    notes.append(
        "null 含义：表示该期次未统计、未提供或无同口径对比数据，界面显示为未提供，"
        "不以 0 或其他推测值替代。"
    )
    snapshot = {
        "口径说明": notes,
        "scope": {
            "数据截止": periods["as_of"],
            "观察月份": periods["end"],
            "来源": f"{plan['nature']}数据",
            "schema版本": SCHEMA_VERSION,
        },
        "conclusion": build_conclusion(plan, facts),
        "origin": f"DA 快照（{plan['nature']}原始数据，按任务书口径计算）",
        "metrics": metrics,
        "insights": build_insights(plan, facts, raw),
        "definitions": build_definitions(plan),
        "risks": build_risks(plan, facts, raw),
        "recommendations": build_recommendations(plan, facts, raw),
    }
    return snapshot


def ratio_series(values: dict, labels: dict) -> dict:
    """把成本率转成 schema 的期次键序。"""
    return {
        labels["start"]: rate_text(values["start"]),
        labels["reference"]: rate_text(values["reference"]),
        labels["end"]: rate_text(values["end"]),
        labels["forecast"]: rate_text(values["forecast"]),
    }


def full_rate_series(values: dict, labels: dict) -> dict:
    """把全口径成本率转成带整体前缀的期次键序。"""
    return {
        FULL_RATE_PREFIX + labels["start"]: rate_text(values["start"]),
        FULL_RATE_PREFIX + labels["reference"]: rate_text(values["reference"]),
        FULL_RATE_PREFIX + labels["end"]: rate_text(values["end"]),
        FULL_RATE_PREFIX + labels["forecast"]: rate_text(values["forecast"]),
    }


def validate_snapshot(snapshot: dict) -> list[str]:
    """用 validate.py 的校验函数检查快照。"""
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as cause:
        raise SnapshotError(f"无法读取结构定义 {SCHEMA_PATH}：{cause}") from cause
    return validate.validate_data(snapshot, schema)


def build_from_files(brief_path: Path, raw_dir: Path) -> dict:
    """按任务书与原始数据生成快照，并自检结构。"""
    brief = load_brief(brief_path)
    plan = parse_plan(brief, raw_dir)
    raw = load_raw(raw_dir)
    snapshot = build_snapshot(plan, raw)
    errors = validate_snapshot(snapshot)
    if errors:
        raise SnapshotError("生成的快照未通过结构校验：\n  " + "\n  ".join(errors))
    return snapshot


def write_snapshot(path: Path, snapshot: dict) -> None:
    """写出快照 JSON，键序固定。"""
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回进程退出码。"""
    parser = argparse.ArgumentParser(description="按任务书口径生成 schema v4 诊断快照。")
    parser.add_argument("--brief", type=Path, required=True, help="任务书 JSON 路径。")
    parser.add_argument("--raw", type=Path, required=True, help="原始数据目录（若干 CSV）。")
    parser.add_argument("--out", type=Path, required=True, help="输出诊断快照 JSON 路径。")
    args = parser.parse_args(argv)
    try:
        snapshot = build_from_files(args.brief, args.raw)
    except (BriefError, RawDataError, SnapshotError) as cause:
        print(str(cause), file=sys.stderr)
        return EXIT_DATA
    write_snapshot(args.out, snapshot)
    print(f"{args.out}: 已生成诊断快照")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
