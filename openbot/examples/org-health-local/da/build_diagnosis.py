#!/usr/bin/env python3
"""按任务书口径把原始数据归并成 schema v4 诊断快照。

用法：
    python3 da/build_diagnosis.py --brief <brief.json> --raw <dir> --out <diagnosis.json>

输入：
    任务书 JSON：`目标`、`观察期间`、`口径`、`必答问题`、`数据需求`、`禁止事项`、`schema版本`。
    `口径.折算` 声明需要折算的币种（`["USD", "EUR"]`、`{"币种": [...]}` 或文字说明）。
    原始数据目录：四张 CSV，表头固定（见 REQUIRED_COLUMNS）：
        cost.csv     月度技术人力成本（万元/月）与运维设施成本
        heads.csv    月度折算人头
        revenue.csv  月度收入（万美元/月），可带 `币种` 列声明该行金额的币种
        forecast.csv 年末测算与年末目标（`期次` 列为 `年末测算` / `年末目标`），可带 `币种` 列
    汇率文件 `fx.csv` 两种写法：
        逐期逐币种汇率表 `期次,币种,人民币每单位`（或 `期次,人民币每美元`）
        单一汇率表 `人民币每美元`（一列汇率对所有期次生效）

输出：schema v4 诊断快照，结构见 `schema/diagnosis.schema.json`。
只使用 Python 3.9+ 标准库。输出确定性：同一份任务书与原始数据得到同一份 JSON，键序固定。
缺数据的期次写 null，不填估算值；整体口径的参考期次按 schema v4 固定为 null。
需要折算时每个期次每个币种都要有对应汇率：缺期次或缺币种直接失败，不写 null，
也不沿用其他期次的汇率；快照里保留汇率来源。
"""
from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import math
import re
import sys
from collections import Counter
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

BASE_CURRENCY = "CNY"
DEFAULT_CURRENCY = "USD"
CURRENCY_COLUMN = "币种"
FX_KEY_COLUMNS = ("期次", "月份")
FX_CURRENCY_COLUMN = "币种"
FX_RATE_COLUMN = "人民币每单位"
FX_RATE_PREFIX = "人民币每"
CURRENCY_NAMES = {"CNY": "人民币", "USD": "美元", "EUR": "欧元"}
CURRENCY_CODES = {"人民币": "CNY", "美元": "USD", "欧元": "EUR", "英镑": "GBP", "日元": "JPY"}

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
CURRENCY_PATTERN = re.compile(r"^[A-Z]{2,8}$")


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


def currency_code(value: object) -> str:
    """把币种写法规范成大写代码；支持 ISO 代码与登记过的中文名。"""
    if not isinstance(value, str):
        raise ValueError(f"币种必须是字符串：{value!r}")
    text = value.strip()
    if text in CURRENCY_CODES:
        return CURRENCY_CODES[text]
    code = text.upper()
    if not CURRENCY_PATTERN.match(code):
        raise ValueError(f"币种代码不合法：{value}")
    return code


def currency_name(code: str) -> str:
    """币种代码的中文名；没有登记的名字时原样返回代码。"""
    return CURRENCY_NAMES.get(code, code)


def parse_conversion(calibration: dict) -> dict:
    """解析任务书 `口径.折算` 声明：是否需要折算、声明的币种集合。

    支持三种写法：`"折算": ["USD", "EUR"]`、`"折算": {"币种": ["USD", "EUR"]}`
    与 `"折算": "需要折算"`（币种由原始数据的 `币种` 列给出）。
    """
    declared = calibration.get("折算")
    if declared is None:
        return {"declared": False, "currencies": None}
    if isinstance(declared, dict):
        raw_currencies = declared.get("币种")
        if raw_currencies is None:
            currencies = None
        elif isinstance(raw_currencies, list):
            currencies = brief_currencies(raw_currencies)
        else:
            raise BriefError("任务书 `口径.折算.币种` 必须是币种数组")
    elif isinstance(declared, list):
        currencies = brief_currencies(declared)
    elif isinstance(declared, str):
        currencies = None
    else:
        raise BriefError("任务书 `口径.折算` 必须是币种数组、对象或文字说明")
    if currencies is not None and not currencies:
        raise BriefError("任务书 `口径.折算.币种` 不能是空数组")
    return {"declared": True, "currencies": currencies}


def brief_currencies(items: list) -> list[str]:
    """把任务书里的币种数组规范成大写代码列表；不合法时报 BriefError。"""
    currencies: list[str] = []
    for item in items:
        try:
            currencies.append(currency_code(item))
        except ValueError as cause:
            raise BriefError(f"任务书 `口径.折算` 的币种不合法：{cause}") from cause
    return currencies


def parse_calibration(brief: dict) -> dict:
    """从任务书 `口径` 解析折算声明与口径说明文字。"""
    calibration = brief.get("口径")
    if not isinstance(calibration, dict):
        raise BriefError("任务书缺少 `口径` 对象")
    conversion = parse_conversion(calibration)
    definitions: list[str] = []
    for key, value in calibration.items():
        if key in ("汇率", "exchange_rate", "折算"):
            continue
        if isinstance(value, str) and value.strip():
            definitions.append(f"{key}：{value.strip()}")
    return {"conversion": conversion, "definitions": definitions}


def parse_brief_rate(calibration: dict) -> float | None:
    """读取任务书 `口径.汇率`；没有该字段时返回 None，由 fx.csv 提供汇率。"""
    rate = calibration.get("汇率", calibration.get("exchange_rate"))
    if rate is None:
        return None
    if isinstance(rate, dict):
        rate = first_text(rate, "人民币每美元", "人民币兑美元", "人民币/美元")
    if isinstance(rate, str):
        try:
            rate = float(rate)
        except ValueError as cause:
            raise BriefError(f"任务书 `口径.汇率` 不是数值：{rate}") from cause
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
        raise BriefError("任务书 `口径.汇率` 缺失或不是正数，无法计算成本率")
    return float(rate)


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
    calibration = parse_calibration(brief)
    fx = resolve_fx_source(
        parse_brief_rate(brief["口径"]), load_fx(raw_dir), calibration["conversion"]
    )
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
        "rate": fx["rate"],
        "fx": fx,
        "conversion": calibration["conversion"],
        "definitions": calibration["definitions"],
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
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    missing = [name for name in required if name not in fieldnames]
    if missing:
        raise RawDataError(f"原始数据 {path.name} 缺少必需列：{'、'.join(missing)}")
    duplicated = [name for name, count in Counter(fieldnames).items() if count > 1 and name]
    if duplicated:
        raise RawDataError(f"原始数据 {path.name} 表头出现重复列：{'、'.join(duplicated)}")
    rows: list[dict[str, str]] = []
    for number, row in enumerate(reader, start=2):
        if None in row:
            raise RawDataError(f"原始数据 {path.name} 第 {number} 行的字段数多于表头")
        # 字段数少于表头时 DictReader 把缺的取值写成 None：该列在这行里是空的。
        empty = [name for name in required if row.get(name) is None]
        if empty:
            raise RawDataError(
                f"原始数据 {path.name} 第 {number} 行缺少 {'、'.join(empty)} 的取值（字段数少于表头）"
            )
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
    except (TypeError, ValueError, OverflowError) as cause:
        raise RawDataError(f"原始数据 {path.name} 第 {number} 行的 {column} 不是数值：{text}") from cause
    if not math.isfinite(number_value):
        raise RawDataError(f"原始数据 {path.name} 第 {number} 行的 {column} 不是有限数值：{text}")
    return round(number_value, 4)


def load_table(
    path: Path,
    key_column: str,
    columns: tuple[str, ...],
    non_negative: bool = False,
    non_negative_columns: tuple[str, ...] = (),
    observation_range: tuple[str, str] | None = None,
) -> dict[str, dict[str, float | None]]:
    """读取一张 CSV，按期次索引各列数值。

    non_negative 为真时各列数值都必须是非负数；non_negative_columns 列出额外要求非负的列。
    负数按原始数据错误报出。
    """
    rows = read_table(path, (key_column,) + columns)
    table: dict[str, dict[str, float | None]] = {}
    row_numbers: dict[str, int] = {}
    for number, row in enumerate(rows, start=2):
        key = (row.get(key_column) or "").strip()
        if not key:
            raise RawDataError(f"原始数据 {path.name} 第 {number} 行缺少 {key_column}")
        if key in table:
            raise RawDataError(
                f"原始数据 {path.name} 中 {key_column} {key} 重复：第 {number} 行"
                f"（首次出现于第 {row_numbers[key]} 行）"
            )
        if observation_range is not None:
            start, end = observation_range
            if not MONTH_PATTERN.match(key) or not start <= key <= end:
                raise RawDataError(
                    f"原始数据 {path.name} 第 {number} 行的 {key_column} {key} 超出任务书观察范围"
                    f"（{start} 至 {end}，应为 YYYY-MM）"
                )
        values: dict[str, float | None] = {}
        for name in columns:
            value = parse_number(row.get(name), path, name, number)
            if value is not None and value < 0 and (non_negative or name in non_negative_columns):
                raise RawDataError(f"原始数据 {path.name} 第 {number} 行的 {name} 必须是非负数：{value}")
            values[name] = value
        table[key] = values
        row_numbers[key] = number
    return table


def load_fx(raw_dir: Path) -> dict:
    """读取原始数据目录的 fx.csv。

    支持两种写法：逐期逐币种汇率表（`期次,币种,人民币每单位` 或 `期次,人民币每美元`）
    与单一汇率表（`人民币每美元` 一列，对所有期次生效）。
    返回 {"table": {(期次, 币种): 汇率}, "global": {币种: 汇率}, "source": 来源说明}。
    """
    path = raw_dir / FX_FILE
    if not path.is_file():
        return {"table": {}, "global": {}, "source": ""}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as cause:
        raise RawDataError(f"无法读取原始数据 {path}：{cause}") from cause
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    key_column = next((name for name in FX_KEY_COLUMNS if name in fieldnames), None)
    if key_column is None:
        return load_global_fx(path, fieldnames)
    if FX_CURRENCY_COLUMN in fieldnames:
        rate_column = next(
            (name for name in (FX_RATE_COLUMN, "汇率") if name in fieldnames), None
        )
        if rate_column is None:
            raise RawDataError(f"原始数据 {path.name} 缺少必需列：{FX_RATE_COLUMN}")
        return load_period_fx(path, key_column, (rate_column,), FX_CURRENCY_COLUMN)
    rate_columns = tuple(name for name in fieldnames if name.startswith(FX_RATE_PREFIX))
    if not rate_columns:
        raise RawDataError(
            f"原始数据 {path.name} 缺少必需列：{FX_CURRENCY_COLUMN} 或 {FX_RATE_PREFIX}<币种>"
        )
    return load_period_fx(path, key_column, rate_columns, None)


def load_global_fx(path: Path, fieldnames: list[str]) -> dict:
    """读取单一汇率表：表头形如 `人民币每美元`，一列汇率对所有期次生效。"""
    rate_columns = [name for name in fieldnames if name.startswith(FX_RATE_PREFIX)]
    if len(rate_columns) != 1:
        found = "、".join(rate_columns) if rate_columns else "无"
        raise RawDataError(
            f"原始数据 {path.name} 的单一汇率表需要一列 {FX_RATE_PREFIX}<币种>，当前为{found}；"
            f"逐期汇率表请写成 {FX_KEY_COLUMNS[0]},{FX_CURRENCY_COLUMN},{FX_RATE_COLUMN}"
        )
    column = rate_columns[0]
    try:
        code = currency_code(column[len(FX_RATE_PREFIX):])
    except ValueError as cause:
        raise RawDataError(f"原始数据 {path.name} 的汇率列名不合法：{cause}") from cause
    rows = read_table(path, (column,))
    if not rows:
        raise RawDataError(f"原始数据 {path.name} 缺少汇率取值")
    value = parse_number(rows[0].get(column), path, column, 2)
    if value is None or value <= 0:
        raise RawDataError(f"原始数据 {path.name} 第 2 行的 {column} 必须是正数汇率")
    return {"table": {}, "global": {code: value}, "source": f"{path.name} {column}"}


def load_period_fx(
    path: Path, key_column: str, rate_columns: tuple[str, ...], currency_column: str | None
) -> dict:
    """读取逐期汇率表：长表按 `币种` 列取币种，宽表按 `人民币每<币种>` 列名取币种。"""
    required = (key_column,) + rate_columns + ((currency_column,) if currency_column else ())
    rows = read_table(path, required)
    table: dict[tuple[str, str], float] = {}
    for number, row in enumerate(rows, start=2):
        period = (row.get(key_column) or "").strip()
        if not period:
            raise RawDataError(f"原始数据 {path.name} 第 {number} 行缺少 {key_column}")
        for column in rate_columns:
            if currency_column is None:
                raw_currency = column[len(FX_RATE_PREFIX):]
            else:
                raw_currency = (row.get(currency_column) or "").strip()
            try:
                code = currency_code(raw_currency)
            except ValueError as cause:
                raise RawDataError(
                    f"原始数据 {path.name} 第 {number} 行的币种不合法：{cause}"
                ) from cause
            value = parse_number(row.get(column), path, column, number)
            if value is None:
                continue
            if value <= 0:
                raise RawDataError(f"原始数据 {path.name} 第 {number} 行的 {column} 必须是正数汇率：{value}")
            key = (period, code)
            if key in table:
                raise RawDataError(
                    f"原始数据 {path.name} 中 {key_column} {period} 与币种 {code} 重复：第 {number} 行"
                )
            table[key] = value
    source = f"{path.name} 逐期汇率表（{key_column}，{'、'.join(rate_columns)}）"
    return {"table": table, "global": {}, "source": source}


def resolve_fx_source(brief_rate: float | None, fx: dict, conversion: dict) -> dict:
    """决定折算用的汇率来源。

    逐期汇率表是唯一权威来源：缺某个期次或币种就报错，不回落任务书汇率，也不沿用上一期。
    没有逐期汇率表时用任务书 `口径.汇率`，再退到 fx.csv 的单一汇率。
    """
    if fx["table"]:
        return {
            "mode": "per_period",
            "table": fx["table"],
            "global": {},
            "rate": None,
            "source": fx["source"],
        }
    if brief_rate is not None:
        currencies = conversion["currencies"]
        code = currencies[0] if currencies and len(currencies) == 1 else DEFAULT_CURRENCY
        return {
            "mode": "global",
            "table": {},
            "global": {code: brief_rate},
            "rate": brief_rate,
            "source": "任务书 `口径.汇率`",
        }
    if fx["global"]:
        code = sorted(fx["global"])[0]
        return {
            "mode": "global",
            "table": {},
            "global": dict(fx["global"]),
            "rate": fx["global"][code],
            "source": fx["source"],
        }
    raise BriefError(
        "任务书 `口径.汇率` 缺失或不是正数，且原始数据目录没有 fx.csv，无法计算成本率"
    )


def load_currency_map(path: Path, key_column: str) -> dict[str, str]:
    """读取可选的 `币种` 列，返回每个期次的币种代码；没有该列时返回空字典。"""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as cause:
        raise RawDataError(f"无法读取原始数据 {path}：{cause}") from cause
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames or []
    if CURRENCY_COLUMN not in fieldnames:
        return {}
    mapping: dict[str, str] = {}
    for number, row in enumerate(reader, start=2):
        key = (row.get(key_column) or "").strip()
        if not key:
            continue
        try:
            mapping[key] = currency_code((row.get(CURRENCY_COLUMN) or "").strip())
        except ValueError as cause:
            raise RawDataError(
                f"原始数据 {path.name} 第 {number} 行的 {CURRENCY_COLUMN} 不合法：{cause}"
            ) from cause
    return mapping


def load_raw(
    raw_dir: Path, observation_range: tuple[str, str] | None = None
) -> dict[str, dict[str, dict[str, float | None]]]:
    """读取四张原始数据 CSV，并记下各表 `币种` 列声明的币种。"""
    if not raw_dir.is_dir():
        raise RawDataError(f"原始数据目录不存在：{raw_dir}")
    cost_columns = (OVERALL_COST, REVOPS_COST, OPS_COST)
    heads_columns = (OVERALL_HEADS, REVOPS_HEADS)
    forecast_columns = cost_columns + heads_columns + (REVENUE,)
    return {
        "cost": load_table(
            raw_dir / COST_FILE, "月份", cost_columns, non_negative=True, observation_range=observation_range
        ),
        "heads": load_table(
            raw_dir / HEADS_FILE, "月份", heads_columns, non_negative=True, observation_range=observation_range
        ),
        "revenue": load_table(
            raw_dir / REVENUE_FILE, "月份", (REVENUE,), non_negative=True, observation_range=observation_range
        ),
        "forecast": load_table(raw_dir / FORECAST_FILE, "期次", forecast_columns, non_negative=True),
        "currency": {
            "revenue": load_currency_map(raw_dir / REVENUE_FILE, "月份"),
            "forecast": load_currency_map(raw_dir / FORECAST_FILE, "期次"),
        },
    }


def cell(table: dict[str, dict[str, float | None]], key: str, column: str) -> float | None:
    """取某个期次某列的数值；缺该期次或该列为空时返回 None。"""
    row = table.get(key)
    if row is None:
        return None
    return row.get(column)


def rate_of(cost: float | None, revenue: float | None, rate: float | None) -> float | None:
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


def change_text(start: float | None, end: float | None, unit: str) -> str:
    """期初到期末的增减金额与增减百分比文本；缺任一端时写明缺数据。"""
    if start is None or end is None:
        return "增减金额与百分比未提供（期初或期末缺同口径数据）"
    diff = end - start
    if diff > 0:
        direction = "增加"
    elif diff < 0:
        direction = "减少"
    else:
        direction = "持平"
    pct = growth(start, end)
    if pct is None:
        return f"{direction} {fmt_number(abs(diff))} {unit}（百分比未提供：期初为 0）"
    ratio = "增幅" if pct > 0 else ("降幅" if pct < 0 else "变动")
    return f"{direction} {fmt_number(abs(diff))} {unit}（{ratio} {abs(pct):.1f}%）"


def default_currency(plan: dict) -> str:
    """没有 `币种` 列时使用的默认币种：声明了单一币种就用它，否则用美元。"""
    currencies = plan["conversion"]["currencies"]
    if currencies and len(currencies) == 1:
        return currencies[0]
    return DEFAULT_CURRENCY


def period_currency(plan: dict, raw: dict, period: str) -> str:
    """取某个期次金额的币种：优先用原始数据的 `币种` 列，没有则用默认币种。"""
    mapping = raw.get("currency", {})
    if period in (FORECAST_LABEL, TARGET_LABEL):
        table = mapping.get("forecast", {})
    else:
        table = mapping.get("revenue", {})
    return table.get(period, default_currency(plan))


def income_value(raw: dict, period: str) -> float | None:
    """取某个期次的收入金额；没有该期次或金额为空时返回 None。"""
    if period in (FORECAST_LABEL, TARGET_LABEL):
        return cell(raw["forecast"], period, REVENUE)
    return cell(raw["revenue"], period, REVENUE)


def amount_periods(plan: dict, raw: dict) -> list[str]:
    """列出需要折算的期次：观察期间内有收入金额的月份，以及年末测算行。"""
    keys = [month for month in observation_months(plan) if month in raw["revenue"]]
    if FORECAST_LABEL in raw["forecast"]:
        keys.append(FORECAST_LABEL)
    return keys


def required_fx_pairs(plan: dict, raw: dict) -> list[tuple[str, str]]:
    """列出需要折算的期次与币种组合：金额非空且币种不是基准币种。"""
    pairs: list[tuple[str, str]] = []
    for period in amount_periods(plan, raw):
        if income_value(raw, period) is None:
            continue
        currency = period_currency(plan, raw, period)
        if currency == BASE_CURRENCY:
            continue
        pairs.append((period, currency))
    return pairs


def missing_fx_pairs(plan: dict, raw: dict) -> list[tuple[str, str]]:
    """列出缺汇率的期次与币种组合；只按精确匹配判定，不沿用其他期次的汇率。"""
    fx = plan["fx"]
    missing: list[tuple[str, str]] = []
    for period, currency in required_fx_pairs(plan, raw):
        if fx["mode"] == "per_period":
            if (period, currency) not in fx["table"]:
                missing.append((period, currency))
        elif currency not in fx["global"]:
            missing.append((period, currency))
    return missing


def check_fx_coverage(plan: dict, raw: dict) -> None:
    """逐期逐币种汇率必须齐全；缺任一项都失败，不写 null 也不沿用上一期汇率。"""
    missing = missing_fx_pairs(plan, raw)
    if not missing:
        return
    detail = "；".join(f"{period} 的 {currency}" for period, currency in missing)
    fx = plan["fx"]
    if fx["mode"] == "per_period":
        hint = (
            f"请在 {FX_FILE} 补齐这些期次与币种的汇率（{FX_RATE_COLUMN}），"
            "不沿用其他期次的汇率。"
        )
    else:
        available = "、".join(sorted(fx["global"])) or "无"
        hint = (
            f"汇率来源 {fx['source']} 只提供 {available} 的汇率；"
            f"请在 {FX_FILE} 补齐这些币种的汇率，或把金额改成同一币种。"
        )
    raise RawDataError(
        f"原始数据 {FX_FILE} 缺少汇率：{detail}。"
        f"任务书口径需要逐期折算为 {BASE_CURRENCY}，缺任一期次或币种都不能折算；{hint}"
    )


def check_declared_currencies(plan: dict, raw: dict) -> None:
    """任务书声明的币种必须覆盖原始数据用到的币种，避免混用不同币种金额。"""
    declared = plan["conversion"]["currencies"]
    if not declared:
        return
    used: dict[str, list[str]] = {}
    for mapping in raw.get("currency", {}).values():
        for period, currency in mapping.items():
            used.setdefault(currency, []).append(period)
    unknown = sorted(code for code in used if code not in declared)
    if not unknown:
        return
    detail = "；".join(f"{code}（{'、'.join(sorted(used[code]))}）" for code in unknown)
    raise RawDataError(
        f"原始数据出现任务书未声明的币种：{detail}；"
        f"任务书 `口径.折算.币种` 只声明了 {'、'.join(declared)}，"
        "不同币种的金额不能直接混用，请改任务书口径或把金额改成同一币种。"
    )


def fx_rate(plan: dict, period: str, currency: str) -> float:
    """取某个期次某个币种的汇率；基准币种固定为 1，其余按精确匹配取。"""
    if currency == BASE_CURRENCY:
        return 1.0
    fx = plan["fx"]
    if fx["mode"] == "per_period":
        rate = fx["table"].get((period, currency))
    else:
        rate = fx["global"].get(currency)
    if rate is None:
        raise RawDataError(
            f"原始数据 {FX_FILE} 缺少 {period} 的 {currency} 汇率："
            f"该期次金额按 {currency} 计价，需折算为 {BASE_CURRENCY}；"
            f"请在 {FX_FILE} 补齐 {period} 行的 {currency} 汇率，不沿用其他期次的汇率。"
        )
    return rate


def income_rate(plan: dict, raw: dict, period: str, value: float | None) -> float | None:
    """取某个期次收入折算用的汇率；没有金额时返回 None。"""
    if value is None:
        return None
    return fx_rate(plan, period, period_currency(plan, raw, period))


def rate_phrase(plan: dict) -> str:
    """折算所用汇率的简短说法。"""
    if plan["fx"]["mode"] == "per_period":
        return "逐期逐币种汇率"
    return f"汇率 {plan['rate']}"


def build_facts(plan: dict, raw: dict) -> dict:
    """按任务书期间取出各指标数值并算好成本率。"""
    periods = plan["periods"]
    labels = plan["labels"]
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
    check_declared_currencies(plan, raw)
    check_fx_coverage(plan, raw)
    rates = {
        "start": income_rate(plan, raw, periods["start"], income["start"]),
        "reference": income_rate(plan, raw, periods["reference"], income["reference"]),
        "end": income_rate(plan, raw, periods["end"], income["end"]),
        "forecast": income_rate(plan, raw, FORECAST_LABEL, income["forecast"]),
    }
    ratios = {
        "overall": {
            "start": rate_of(overall_cost["start"], income["start"], rates["start"]),
            "reference": rate_of(overall_cost["reference"], income["reference"], rates["reference"]),
            "end": rate_of(overall_cost["end"], income["end"], rates["end"]),
            "forecast": rate_of(overall_cost["forecast"], income["forecast"], rates["forecast"]),
        },
        "revops": {
            "start": rate_of(revops_cost["start"], income["start"], rates["start"]),
            "reference": rate_of(revops_cost["reference"], income["reference"], rates["reference"]),
            "end": rate_of(revops_cost["end"], income["end"], rates["end"]),
            "forecast": rate_of(revops_cost["forecast"], income["forecast"], rates["forecast"]),
        },
        "full": {
            "start": rate_of(
                None if overall_cost["start"] is None or ops_cost["start"] is None
                else overall_cost["start"] + ops_cost["start"],
                income["start"],
                rates["start"],
            ),
            "reference": rate_of(
                None if overall_cost["reference"] is None or ops_cost["reference"] is None
                else overall_cost["reference"] + ops_cost["reference"],
                income["reference"],
                rates["reference"],
            ),
            "end": rate_of(
                None if overall_cost["end"] is None or ops_cost["end"] is None
                else overall_cost["end"] + ops_cost["end"],
                income["end"],
                rates["end"],
            ),
            "forecast": rate_of(
                None if overall_cost["forecast"] is None or ops_cost["forecast"] is None
                else overall_cost["forecast"] + ops_cost["forecast"],
                income["forecast"],
                rates["forecast"],
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


def observation_months(plan: dict) -> list[str]:
    """按自然月份顺序列出观察期间的期次。"""
    periods = plan["periods"]
    months = []
    year, number = (int(part) for part in periods["start"].split("-"))
    while f"{year:04d}-{number:02d}" <= periods["end"]:
        months.append(f"{year:04d}-{number:02d}")
        number += 1
        if number > 12:
            year += 1
            number = 1
    return months


def missing_observation_notes(plan: dict, raw: dict) -> list[str]:
    """生成观察期间内原始指标缺失期次的口径说明。"""
    tables = (
        ("cost", (OVERALL_COST, REVOPS_COST, OPS_COST)),
        ("heads", (OVERALL_HEADS, REVOPS_HEADS)),
        ("revenue", (REVENUE,)),
    )
    notes = []
    for table_name, columns in tables:
        table = raw[table_name]
        for column in columns:
            missing = [
                month
                for month in observation_months(plan)
                if cell(table, month, column) is None
            ]
            if missing:
                notes.append(f"{COLUMN_NAMES[column]} 缺 {'、'.join(missing)} 数据，按 null 处理")
    return notes


def missing_file_period_notes(plan: dict, raw: dict) -> list[str]:
    """按业务 CSV 汇总缺口，便于继续使用快照时追溯来源文件。"""
    reference = plan["periods"]["reference"]
    expected = [month for month in observation_months(plan) if month != reference]
    notes = []
    for filename, table_name in ((COST_FILE, "cost"), (HEADS_FILE, "heads"), (REVENUE_FILE, "revenue")):
        missing = [month for month in expected if month not in raw[table_name]]
        if missing:
            notes.append(f"{filename} 缺 {'、'.join(missing)} 数据，相关指标按 null 处理")
    return notes


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
                f"变为 {fmt_number(income['end'])} 万美元/月，按{rate_phrase(plan)}折人民币计算。"
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
    """按关键词把任务书问题对应到已算出的证据；成本增减问题补出增减金额与百分比；成本率问题补出收入数值；没有对应口径时标为边界。"""
    cost = facts["overall_cost"]
    heads = facts["overall_heads"]
    revops_cost = facts["revops_cost"]
    revops_heads = facts["revops_heads"]
    income = facts["income"]
    ratios = facts["ratios"]
    if "营收业务" in question and ("占比" in question or "比例" in question):
        cost_ratio = (
            revops_cost["end"] / cost["end"]
            if cost["end"] not in (None, 0) and revops_cost["end"] is not None
            else None
        )
        heads_ratio = (
            revops_heads["end"] / heads["end"]
            if heads["end"] not in (None, 0) and revops_heads["end"] is not None
            else None
        )
        cost_ratio_str = f"{cost_ratio * 100:.1f}%" if cost_ratio is not None else "未提供"
        heads_ratio_str = f"{heads_ratio * 100:.1f}%" if heads_ratio is not None else "未提供"
        return (
            "事实",
            f"营收业务技术人力成本 {fmt_number(revops_cost['start'])} → {fmt_number(revops_cost['end'])} 万元/月；"
            f"营收业务折算人头 {fmt_number(revops_heads['start'])} → {fmt_number(revops_heads['end'])}；"
            f"成本占比 {cost_ratio_str}，人头占比 {heads_ratio_str}。",
        )
    if "率" in question:
        return (
            "事实",
            f"收入 {fmt_number(income['start'])} → {fmt_number(income['end'])} 万美元/月；"
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
        content = (
            f"整体技术人力成本 {fmt_number(cost['start'])} → {fmt_number(cost['end'])} 万元/月；"
            f"营收业务技术人力成本 {fmt_number(revops_cost['start'])} → {fmt_number(revops_cost['end'])} 万元/月"
        )
        if "增减" in question:
            content += f"。整体技术人力成本相对期初{change_text(cost['start'], cost['end'], '万元/月')}"
        return ("事实", content + "。")
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
        f"收入折人民币按{rate_phrase(plan)}计算（来源：{plan['fx']['source']}）。"
    )
    declared = plan["conversion"]["currencies"]
    if declared:
        lines.append(
            f"折算口径：任务书 `口径.折算` 声明的币种为 {'、'.join(declared)}，"
            f"按各期次金额的币种折算为 {BASE_CURRENCY}；汇率来源 {plan['fx']['source']}。"
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
            "依据": f"收入同时给出{FORECAST_LABEL} {fmt_number(facts['income']['forecast'])} 万美元/月 与"
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
    if plan["fx"]["mode"] == "per_period":
        notes.append(f"汇率：逐期逐币种；数据截止：{periods['as_of']}。")
    else:
        code = sorted(plan["fx"]["global"])[0]
        notes.append(
            f"汇率：{plan['rate']} 人民币/{currency_name(code)}；数据截止：{periods['as_of']}。"
        )
    notes.append(f"汇率来源：{plan['fx']['source']}。")
    pairs = required_fx_pairs(plan, raw)
    if len({code for _, code in pairs}) > 1:
        notes.append(
            "折算币种："
            + "；".join(f"{period} {currency_name(code)}" for period, code in pairs)
            + "。"
        )
    notes.append(
        "null 含义：表示该期次未统计、未提供或无同口径对比数据，界面显示为未提供，"
        "不以 0 或其他推测值替代。"
    )
    notes.extend(missing_observation_notes(plan, raw))
    notes.extend(missing_file_period_notes(plan, raw))
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


def ensure_finite_snapshot(value: object, path: str = "快照") -> None:
    """拒绝计算过程中产生的非有限数值，避免 JSON 写出 Infinity/NaN。"""
    if isinstance(value, bool):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise SnapshotError(f"{path} 含非有限数值；请检查原始数据 CSV 的来源文件与行号")
    if isinstance(value, dict):
        for key, child in value.items():
            ensure_finite_snapshot(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            ensure_finite_snapshot(child, f"{path}[{index}]")


def build_from_files(brief_path: Path, raw_dir: Path) -> dict:
    """按任务书与原始数据生成快照，并自检结构。"""
    brief = load_brief(brief_path)
    plan = parse_plan(brief, raw_dir)
    periods = plan["periods"]
    raw = load_raw(raw_dir, (periods["start"], periods["end"]))
    snapshot = build_snapshot(plan, raw)
    ensure_finite_snapshot(snapshot)
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
    parser.add_argument("--brief", type=Path, help="任务书 JSON 路径。")
    parser.add_argument("--raw", type=Path, help="原始数据目录（若干 CSV）。")
    parser.add_argument("--out", type=Path, help="输出诊断快照 JSON 路径。")
    parser.add_argument("--strict", action="store_true", help="任一指标缺少观察期次数据时失败。")
    args = parser.parse_args(argv)
    missing_args = [name for name in ("--brief", "--raw", "--out") if getattr(args, name[2:]) is None]
    if missing_args:
        parser.error("缺少必需参数：" + "、".join(missing_args))
    try:
        snapshot = build_from_files(args.brief, args.raw)
    except (BriefError, RawDataError, SnapshotError) as cause:
        # 失败时不得让旧文件伪装成这次运行成功生成的快照。
        if args.out.is_file():
            try:
                args.out.unlink()
            except OSError as cleanup_cause:
                print(f"无法清理失败的诊断快照 {args.out}：{cleanup_cause}", file=sys.stderr)
        print(str(cause), file=sys.stderr)
        return EXIT_DATA
    if args.strict:
        missing = [note for note in snapshot["口径说明"] if " 缺 " in note and note.endswith(" 数据，按 null 处理")]
        if missing:
            if args.out.is_file():
                args.out.unlink()
            print("严格模式失败：观察期间存在指标缺失期次：" + "；".join(missing), file=sys.stderr)
            return EXIT_DATA
    try:
        write_snapshot(args.out, snapshot)
    except OSError as cause:
        print(f"无法写出诊断快照 {args.out}：{cause}", file=sys.stderr)
        return EXIT_DATA
    print(f"{args.out}: 已生成诊断快照")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
