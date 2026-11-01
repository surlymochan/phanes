#!/usr/bin/env python3
"""生成组织健康诊断任务书 brief.json。

用法：
    python3 make_brief.py --out brief.json [--goal 文本] [--month YYYY-MM] [--diagnosis diagnosis.json]
    python3 make_brief.py --check brief.json

任务书只写口径、问题与字段映射，不含真实组织数据或真实数字。
默认口径与合成示例 data/diagnosis.json 一致：观察月份 2026-09、数据截止
2026-09-30、指标标题与顺序同站点的 metrics。--goal 覆盖“目标”，--month 覆盖
观察月份（数据截止与期次锚点随之推算）。结构定义见同目录 brief.schema.json，
校验复用上一级目录的 validate.py；只使用 Python 标准库。

写出任务书时先写同目录临时文件再替换目标文件：替换成功前旧任务书保持原样，
写出失败不会留下半个任务书。输出路径的父目录必须已存在，缺失或不可写时报错。
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EX = ROOT.parent
if str(EX) not in sys.path:
    sys.path.insert(0, str(EX))

from validate import Validator, load_schema  # noqa: E402

SCHEMA_PATH = ROOT / "brief.schema.json"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

SCHEMA_VERSION = 4
DEFAULT_MONTH = "2026-09"
# 观察月份：ASCII 数字的 YYYY-MM，月份在 01-12 之间。
# 用 \A/\Z 而不是 ^/$：$ 允许结尾换行，"2026-01\n" 会被误判成合法月份；
# 用 [0-9] 而不是 \d：\d 匹配 Unicode 数字，全角“２０２６-09”会被误判成合法月份。
MONTH_PATTERN = re.compile(r"\A[0-9]{4}-(0[1-9]|1[0-2])\Z")

# 站点期望的四个指标标题，顺序与 data/diagnosis.json 的 metrics 一致。
METRIC_TITLES = (
    "整体技术人力成本",
    "整体折算人头",
    "营收业务（含共享平台）技术人力成本与折算人头",
    "收入与成本率",
)

DEFAULT_GOALS = [
    "复核观察期间内整体技术人力成本与折算人头的水平与变化幅度。",
    "说明营收业务（含共享平台）口径的成本、折算人头与收入的匹配情况。",
    "给出整体、营收业务与全口径三个成本率的水平，并解释差异来源。",
    "列明缺少同口径数据的期次与领域，说明缺口对上述结论的影响。",
]

DEFAULT_QUESTIONS = [
    "观察期间内整体技术人力成本是多少？相对期初实际增减多少（金额与百分比）？",
    "整体折算人头的变化与整体技术人力成本的变化是否同向？折算人头口径是否与成本口径一致？",
    "营收业务（含共享平台）的技术人力成本与折算人头分别是多少？占整体口径的比例是多少？",
    "观察期间的收入是多少（万美元/月）？整体技术人力成本率与营收业务技术人力成本率各是多少？",
    "全口径技术成本率（含运维与设施）在哪些期次有同口径值？缺同口径值的期次如何呈现？",
    "哪些结论只能由聚合口径支撑，哪些领域因缺少证据而无法回答？",
]

# 字段路径是逻辑路径：数据提供方的原始表与列名由 DA 按实际来源映射。
DEFAULT_DATA_NEEDS = [
    {
        "字段路径": "月度成本/技术人力成本",
        "单位": "万元/月",
        "口径": "计入技术人力的成本合计，不含运维与设施；需可按期次与业务口径拆分。",
        "对应指标": "整体技术人力成本",
    },
    {
        "字段路径": "月度人力/折算人头",
        "单位": "人（当量）",
        "口径": "按投入当量折算的人数，非自然人数；与成本口径同源同范围。",
        "对应指标": "整体折算人头",
    },
    {
        "字段路径": "月度成本/营收业务技术人力成本",
        "单位": "万元/月",
        "口径": "只含营收业务（含共享平台）的技术人力成本，与整体口径同定义、不同范围。",
        "对应指标": "营收业务（含共享平台）技术人力成本与折算人头",
    },
    {
        "字段路径": "月度人力/营收业务折算人头",
        "单位": "人（当量）",
        "口径": "营收业务（含共享平台）口径的折算人头，范围与上一条一致。",
        "对应指标": "营收业务（含共享平台）技术人力成本与折算人头",
    },
    {
        "字段路径": "月度收入/营收业务收入",
        "单位": "万美元/月",
        "口径": "营收业务口径月度收入；成本率按同期收入折算为同一币种后计算。",
        "对应指标": "收入与成本率",
    },
    {
        "字段路径": "月度成本/运维与设施成本",
        "单位": "万元/月",
        "口径": "运维与设施成本，用于全口径技术成本率；缺失时该口径留空，不用整体成本代替。",
        "对应指标": "收入与成本率",
    },
]

DEFAULT_FORBIDS = [
    "不得写入真实组织数据：任务书只写口径、问题与字段映射，真实数字与明细只留在仓库外的工作目录。",
    "不作个人/薪酬/编制判断：只输出聚合口径，不含个人级字段、薪酬明细与编制明细。",
    "不得编造数字：没有取数支撑的值一律写 null，并在 risks/recommendations 的“缺失证据”里写明缺口。",
    "不得改动指标标题与顺序：metrics 的四个标题与顺序与站点期望一致，改动会被页面判为结构错误。",
    "不得把推断写成事实：洞察按“事实/推断/边界”标注，推断必须写明依据与核实方式。",
    "不得把仓库外的真实数据复制进仓库：诊断 JSON 写在仓库外，交接只给路径与校验值。",
]


def period_keys(month: str) -> list[str]:
    """按观察月份给出期次锚点，顺序与站点期望一致。

    2026-09 的期次为 2026-01实际、2026-05H1参考、2026-09实际、年末测算、年末目标，
    与合成示例 data/diagnosis.json 的数值序列键名一致。
    """
    year = month[:4]
    keys = [f"{year}-01实际", f"{year}-05H1参考", f"{month}实际", "年末测算", "年末目标"]
    ordered: list[str] = []
    for key in keys:
        if key not in ordered:
            ordered.append(key)
    return ordered


def extract_diagnosis_periods(diagnosis: dict) -> set[str]:
    """从 diagnosis 的 metrics 数值序列中提取所有期次键名（去除「整体」前缀）。"""
    periods: set[str] = set()
    for metric in diagnosis.get("metrics", []):
        values = metric.get("数值", {})
        if not isinstance(values, dict):
            continue
        for key, val in values.items():
            if isinstance(val, dict):
                for sub_key in val:
                    periods.add(sub_key.removeprefix("整体"))
            else:
                periods.add(key.removeprefix("整体"))
    return periods


def check_periods_alignment(brief: dict, diagnosis: dict) -> list[str]:
    """核对任务书期次锚点与诊断快照的期次是否对齐。错期时返回可读错误列表。"""
    errors: list[str] = []
    period_info = brief.get("观察期间", {})
    if not isinstance(period_info, dict):
        return ["任务书缺少合法的 `观察期间` 对象"]
    raw_keys = period_info.get("期次")
    if not isinstance(raw_keys, list) or not raw_keys:
        return ["任务书 `观察期间.期次` 缺失或为空"]
    brief_keys = set(raw_keys)
    diag_keys = extract_diagnosis_periods(diagnosis)
    if not diag_keys:
        return ["诊断快照 `metrics` 的 diagnosis periods 为空，不能生成任务书"]

    missing_in_diag = [k for k in raw_keys if k not in diag_keys]
    if missing_in_diag:
        errors.append(f"任务书期次 {missing_in_diag} 未在诊断期次 ({sorted(diag_keys)}) 中找到")
    missing_in_brief = [k for k in sorted(diag_keys) if k not in brief_keys]
    if missing_in_brief:
        errors.append(f"诊断期次 {missing_in_brief} 未在任务书期次 ({raw_keys}) 中找到")
    return errors


def month_end(month: str) -> str:
    """返回观察月份的最后一天，格式 YYYY-MM-DD。"""
    year, number = int(month[:4]), int(month[5:7])
    return f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def period_line(keys: list[str], month: str) -> str:
    """写出期间口径：哪些是已发生实际、参考基准与前瞻估算。"""
    actual = f"{month}实际"
    reference = f"{month[:4]}-05H1参考"
    return (
        f"按观察期间期次取值：{'、'.join(keys)}；"
        f"其中 {keys[0]} 与 {actual} 为已发生实际期次，{reference} 为 H1 冻结对照基准，"
        "年末测算与年末目标为前瞻预算与目标估算，不代表已发生实际。"
    )


def build_definitions(keys: list[str], month: str) -> dict:
    """按指标标题给出口径定义：单位、定义、期间与缺失处理。"""
    period = period_line(keys, month)
    reference = f"{month[:4]}-05H1参考"
    missing = "该期次没有同口径取数时写 null，界面显示为未提供，不以 0 或推测值替代。"
    return {
        "整体技术人力成本": {
            "单位": "万元/月",
            "定义": "技术人力成本为当期计入的技术人力相关成本合计，不含运维与设施。",
            "期间": period,
            "缺失处理": f"{missing}（例如 {reference} 未单独拆分整体成本与人头）。",
        },
        "整体折算人头": {
            "单位": "人（当量）",
            "定义": "折算人头为按投入当量折算的人数，非自然人数；与成本口径同源同范围。",
            "期间": period,
            "缺失处理": f"{missing}（例如 {reference} 未单独拆分整体成本与人头）。",
        },
        "营收业务（含共享平台）技术人力成本与折算人头": {
            "单位": "成本：万元/月；折算人头：人（当量）",
            "定义": "只统计营收业务（含共享平台）口径的技术人力成本与折算人头，与整体口径同定义、不同范围。",
            "期间": period,
            "缺失处理": "营收业务口径缺任一分项时该分项写 null，不用整体口径回填。",
        },
        "收入与成本率": {
            "单位": "收入：万美元/月；成本率：百分比（按原文显示，如 约2%）",
            "定义": (
                "收入为营收业务口径月度收入；成本率 = 技术人力成本 ÷ 同期收入，"
                "成本与收入折算为同一币种后计算；全口径技术成本率额外包含运维与设施成本。"
            ),
            "期间": period,
            "缺失处理": "收入或成本缺同口径值时成本率写 null，不用其他期次或其他口径替代。",
        },
    }


def check_month(month: str) -> str:
    """校验观察月份并原样返回；不满足 YYYY-MM 时抛出 ValueError。

    月份越界（00、13）与格式非法都走这一处检查，命令行据此以状态码 2 退出，
    错误信息同时点明“观察月份”与 YYYY-MM 约束。
    """
    if not isinstance(month, str) or not MONTH_PATTERN.match(month):
        raise ValueError(f"观察月份必须是 YYYY-MM（月份在 01-12 之间）：{month}")
    return month


def build_brief(
    goals: list[str] | None = None, month: str | None = None, fx: float | None = None
) -> dict:
    """生成任务书。goals 覆盖“目标”，month 覆盖观察月份。"""
    if goals is not None and not isinstance(goals, list):
        raise ValueError("目标必须是字符串数组，不能是字符串或其他类型")
    if goals is not None and any(not isinstance(goal, str) or not goal.strip() for goal in goals):
        raise ValueError("目标必须是非空字符串数组")
    observed = DEFAULT_MONTH if month is None else check_month(month)
    keys = period_keys(observed)
    definitions = build_definitions(keys, observed)
    if fx is not None:
        if isinstance(fx, bool) or not isinstance(fx, (int, float)) or fx <= 0:
            raise ValueError("汇率必须是正数（人民币/美元）")
        definitions["汇率"] = fx
    return {
        "目标": list(goals) if goals else list(DEFAULT_GOALS),
        "观察期间": {
            "观察月份": observed,
            "数据截止": month_end(observed),
            "期次": keys,
            "说明": (
                f"观察月份 {observed}，数据截止 {month_end(observed)}；"
                f"期次锚点 {'、'.join(keys)}，与站点数值序列的键名一致。"
            ),
        },
        "口径": definitions,
        "必答问题": list(DEFAULT_QUESTIONS),
        "数据需求": [dict(item) for item in DEFAULT_DATA_NEEDS],
        "禁止事项": list(DEFAULT_FORBIDS),
        "schema版本": SCHEMA_VERSION,
    }


def period_sort_key(value: object, keys: list[str]) -> tuple[str, int]:
    """返回期次用于先后比较的键值（YYYY-MM 前缀与期次列表索引）。"""
    prefix = value[:7] if isinstance(value, str) and MONTH_PATTERN.match(value[:7]) else ""
    idx = keys.index(value) if isinstance(value, str) and value in keys else 0
    return (prefix, idx)


def validate_brief(brief: object) -> list[str]:
    """按 brief.schema.json 校验任务书，返回错误说明列表。

    除结构校验外，另查观察期间的期次：重复项、期次顺序与起止/参考期次的先后。

    顶层不是 JSON 对象（数组、字符串等）时只报类型错误，不去取字段，
    避免调用 brief.get() 时抛出 AttributeError。
    """
    if not isinstance(brief, dict):
        return ["任务书必须是 JSON 对象"]
    errors = Validator(load_schema(SCHEMA_PATH)).errors(brief)
    period = brief.get("观察期间", {})
    raw_keys = period.get("期次") if isinstance(period, dict) else None
    keys = raw_keys if isinstance(raw_keys, list) else []
    if isinstance(period, dict):
        for name in ("起始期次", "参考期次", "截止期次"):
            if name in period and period[name] not in keys:
                errors.append(f"/观察期间/{name}: 必须是期次列表中的值")

        if isinstance(raw_keys, list):
            # 重复期次会让同一期次被取两次数或被静默覆盖，必须报出。
            try:
                has_duplicate = len(keys) != len(set(keys))
            except TypeError:
                # 不可哈希的元素（非字符串）由结构校验报错，这里不重复提示。
                has_duplicate = False
            if has_duplicate:
                errors.append("/观察期间/期次: 包含重复期次")

            dated = [
                item[:7]
                for item in keys
                if isinstance(item, str) and MONTH_PATTERN.match(item[:7])
            ]
            is_valid_order = dated == sorted(dated)
            if not is_valid_order:
                observed = period.get("观察月份")
                if isinstance(observed, str) and keys == period_keys(observed):
                    is_valid_order = True
            if not is_valid_order:
                errors.append("/观察期间/期次: 必须按时间升序排列")

        start = period.get("起始期次")
        ref = period.get("参考期次")
        end = period.get("截止期次")
        if start is not None and ref is not None and end is not None:
            if start in keys and ref in keys and end in keys:
                if not (
                    period_sort_key(start, keys)
                    <= period_sort_key(ref, keys)
                    <= period_sort_key(end, keys)
                ):
                    errors.append(
                        "/观察期间/起始期次: 必须满足起始期次 ≤ 参考期次 ≤ 截止期次"
                    )
        else:
            if start is not None and ref is not None and start in keys and ref in keys:
                if period_sort_key(start, keys) > period_sort_key(ref, keys):
                    errors.append("/观察期间/起始期次: 起始期次必须早于或等于参考期次")
            if ref is not None and end is not None and ref in keys and end in keys:
                if period_sort_key(ref, keys) > period_sort_key(end, keys):
                    errors.append("/观察期间/参考期次: 参考期次必须早于或等于截止期次")
            if start is not None and end is not None and start in keys and end in keys:
                if period_sort_key(start, keys) > period_sort_key(end, keys):
                    errors.append("/观察期间/起始期次: 起始期次必须早于或等于截止期次")
    return errors


def serialize_brief(brief: dict) -> str:
    """把任务书序列化成 JSON 文本。键序固定，同样输入的字节稳定。"""
    return json.dumps(brief, ensure_ascii=False, indent=2) + "\n"


def brief_file_mode(path: Path) -> int:
    """返回写出任务书时的文件权限：已有文件沿用原权限，新文件按当前 umask 取默认值。"""
    try:
        info = path.stat()
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_brief(brief: dict, path: Path) -> None:
    """把任务书写成 JSON 文件：先写同目录临时文件，再替换目标文件。

    替换成功前目标文件保持原样：写出失败时旧任务书不会被截断或覆盖，
    临时文件也会被清理，不留下半个任务书。父目录必须已存在。
    """
    path = Path(path)
    text = serialize_brief(brief)
    parent = path.parent
    if not parent.is_dir():
        raise NotADirectoryError(f"输出目录不存在：{parent}")
    mode = brief_file_mode(path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        # 任何一步失败都要清掉临时文件：目标位置要么是旧任务书，要么什么都没有。
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def load_brief(path: Path) -> object:
    """读取已有任务书 JSON。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as cause:
        raise ValueError(f"无法读取任务书 {path}：{cause}") from cause
    try:
        return json.loads(text)
    except json.JSONDecodeError as cause:
        raise ValueError(
            f"任务书 {path} 不是合法 JSON：第 {cause.lineno} 行第 {cause.colno} 列 {cause.msg}"
        ) from cause


def load_diagnosis(path: Path) -> dict:
    """读取并确认诊断快照是 JSON 对象。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as cause:
        raise ValueError(f"无法读取诊断快照 {path}：{cause}") from cause
    try:
        diagnosis = json.loads(text)
    except json.JSONDecodeError as cause:
        raise ValueError(
            f"诊断快照 {path} 不是合法 JSON：第 {cause.lineno} 行第 {cause.colno} 列 {cause.msg}"
        ) from cause
    if not isinstance(diagnosis, dict):
        raise ValueError(f"诊断快照 {path} 必须是 JSON 对象")
    return diagnosis


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="生成组织健康诊断任务书 brief.json。")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--out", type=Path, help="任务书输出路径。")
    output.add_argument("--check", type=Path, help="校验已有任务书路径。")
    parser.add_argument(
        "--goal",
        action="append",
        default=None,
        help="覆盖“目标”。可重复给出；不给时用默认目标。",
    )
    parser.add_argument(
        "--month",
        default=None,
        help=f"覆盖观察月份，格式 YYYY-MM。默认 {DEFAULT_MONTH}。",
    )
    parser.add_argument("--fx", type=float, default=None, help="汇率，人民币/美元；默认不写入任务书。")
    parser.add_argument(
        "--diagnosis",
        type=Path,
        help="可选诊断快照；生成前核对其 metrics 期次，空 periods 时拒绝写出任务书。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """生成任务书并写出文件。返回进程退出码。"""
    args = parse_args(argv)
    if args.out is None and args.check is None:
        raise SystemExit("必须指定 --out 或 --check")
    if args.check is not None:
        try:
            brief = load_brief(args.check)
        except ValueError as cause:
            print(str(cause), file=sys.stderr)
            print(str(cause))
            return EXIT_INVALID
        errors = validate_brief(brief)
        if errors:
            print("任务书校验失败：", file=sys.stderr)
            for message in errors:
                print(f"  {message}", file=sys.stderr)
            print("任务书校验失败：")
            for message in errors:
                print(f"  {message}")
            if not isinstance(brief, dict):
                # 顶层不是对象属于输入问题，在 stderr 上补一句带路径的提示。
                print(f"任务书必须是 JSON 对象：{args.check}", file=sys.stderr)
            return EXIT_INVALID
        print("任务书校验通过")
        return EXIT_OK
    try:
        brief = build_brief(args.goal, args.month, args.fx)
    except ValueError as cause:
        print(str(cause), file=sys.stderr)
        return EXIT_USAGE
    errors = validate_brief(brief)
    if args.diagnosis is not None:
        try:
            diagnosis = load_diagnosis(args.diagnosis)
        except ValueError as cause:
            print(str(cause), file=sys.stderr)
            return EXIT_INVALID
        errors.extend(check_periods_alignment(brief, diagnosis))
    if errors:
        print(f"生成的任务书不符合 {SCHEMA_PATH.name}：", file=sys.stderr)
        for message in errors:
            print(f"  {message}", file=sys.stderr)
        return EXIT_INVALID
    try:
        write_brief(brief, args.out)
    except OSError as cause:
        # 写出失败时目标位置保持原样：已有任务书不会被截断或覆盖，也不会留下临时文件。
        print(f"无法写出 {args.out}：{cause}", file=sys.stderr)
        return EXIT_USAGE
    print(f"已写出任务书：{args.out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
