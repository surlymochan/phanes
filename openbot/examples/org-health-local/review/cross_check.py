#!/usr/bin/env python3
"""独立交叉审查：核对任务书、诊断快照与站点产物三方一致性。

用法：
    python3 review/cross_check.py --brief <brief.json> --data <diagnosis.json> [--dist <dist>]
                                    [--out <report.md>]
                                    [--json-out <results.json>]

逐条输出 PASS/FAIL，任一 FAIL 时退出码非 0；输入不可读或不是 JSON 对象时退出码为 2。
只使用 Python 3.9+ 标准库；除 `--out`/`--json-out` 指定的报告文件外不写任何文件，
两个输出路径的父目录不存在时自动创建。

检查项：

1. 期间一致：任务书 `观察期间` 与快照 `scope.观察月份`/`scope.数据截止` 一致。
2. 版本一致：任务书 `schema版本` 与快照 `scope.schema版本` 一致，且为 4。
3. 必答问题覆盖：任务书 `必答问题` 每条在快照的 insights/recommendations 中至少被引用一次。
4. 站点数据一致：给了 `--dist` 时 `dist/data/diagnosis.json` 与 `--data` 字节相同。
5. 隐私边界：快照不含个人/薪酬/编制粒度的字段名。

【期间一致规则】
    任务书 `观察期间` 可为对象、字符串或字符串数组：对象里的期次按 `YYYY-MM` 提取，
    字符串与数组按其中出现的 `YYYY-MM` 提取，得到期次集合。
    - 快照 `scope.观察月份` 必须落在该集合内；
    - 任务书若显式给出 `观察月份`（Boss 格式），必须等于 `scope.观察月份`；
    - 任务书若显式给出 `截止期次`，必须等于 `scope.观察月份`；
    - 任务书若显式给出 `数据截止`，必须等于 `scope.数据截止`。

【必答问题引用规则】
    快照 insights 与 recommendations 的每个条目分别作为答案单元（键名与值均可检索）。
    每个答案只归属一个问题，按下列顺序判定，命中其一即算被引用：
    1. id 命中：问题对象带 id/编号（至少 2 个字符）时，该 id 出现在可搜索文本里；
    2. 原文命中：忽略大小写、空白和标点后，问题原文出现在答案里；
    3. 关键词命中：问题原文按下列规则提取的关键词，且只允许唯一关键词；共享关键词形成冲突。
    关键词提取：先取出 `YYYY-MM`/`YYYY-MM-DD` 片段；其余按空白与标点切段，取片段里第一个疑问词
    标记（怎么、如何、什么、哪里、哪些、哪个、是否、多少、有没有、吗、呢）之前的部分，去掉首尾助词
    （的、在、与、和、对、为、有、是、了）；之前的部分太短时改取之后的部分；再用“的”切成更短的片段；
    长度不足 3 的片段丢弃；按长度降序最多留 3 个关键词（日期片段始终保留）。

【隐私边界规则】
    快照所有键名（含嵌套）都不含黑名单词条时通过；黑名单见 FORBIDDEN_FIELD_TOKENS。
    本项只检查字段名，不检查字段值。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

SCHEMA_VERSION = 4
FINDING_SEVERITIES = frozenset({"error", "warning", "info"})

MONTH_PATTERN = re.compile(r"\d{4}-\d{2}")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")
SEGMENT_PATTERN = re.compile(r"[0-9A-Za-z一-鿿]+")

# 任务书 `观察期间` 里表示截止期次与数据截止的键名。
END_KEYS = ("截止期次", "截止", "截止月份", "end")
AS_OF_KEYS = ("数据截止", "数据截止日期", "asOf")

# 任务书 `观察期间` 里表示观察月份（Boss 格式）的键名。
OBSERVED_KEYS = ("观察月份",)

# 任务书 `必答问题` 条目里表示 id 与问题正文的键名。
QUESTION_ID_KEYS = ("id", "ID", "编号", "题号", "问题编号", "序号")
QUESTION_TEXT_KEYS = ("问题", "标题", "内容", "文本", "question", "text")

# 段尾疑问词与助词；提取关键词时逐个剥离。
QUESTION_SUFFIXES = (
    "是多少",
    "是什么",
    "怎么样",
    "为什么",
    "有没有",
    "如何",
    "怎样",
    "哪些",
    "哪个",
    "是否",
    "多少",
    "吗",
    "呢",
)
PARTICLE_SUFFIXES = ("的", "在", "与", "和", "对", "为", "有", "是", "了")
PARTICLE_CHARS = "".join(PARTICLE_SUFFIXES)

# 问题里的疑问词标记；关键词取标记之前的部分。
QUESTION_MARKERS = (
    "为什么",
    "怎么样",
    "有没有",
    "怎么",
    "如何",
    "什么",
    "哪里",
    "哪些",
    "哪个",
    "是否",
    "多少",
    "吗",
    "呢",
)

# 关键词最短长度：太短的片段（如“口径”）不做匹配，避免误判为已引用。
MIN_KEYWORD_LENGTH = 3

# 隐私边界黑名单：命中即视为出现个人/薪酬/编制粒度字段名。
FORBIDDEN_FIELD_TOKENS = (
    "姓名",
    "个人",
    "员工",
    "职工",
    "薪酬",
    "薪资",
    "工资",
    "年薪",
    "月薪",
    "奖金",
    "个税",
    "编制",
    "职级",
    "职等",
    "绩效",
    "考核",
    "考勤",
    "工号",
    "身份证",
    "手机号",
    "邮箱",
    "住址",
    "入职",
    "离职",
    "上级",
    "下属",
    "岗位",
    "花名册",
)


class CheckError(Exception):
    """输入不可读或格式不对。"""


@dataclass
class Result:
    """一条检查结果。"""

    ok: bool
    name: str
    detail: str
    finding: dict[str, object] | None = None


def first_text(source: dict, *names: str) -> str | None:
    """按顺序取第一个非空字符串字段。"""
    for name in names:
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_json(path: Path) -> dict:
    """读取 JSON 对象文件。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as cause:
        raise CheckError(f"无法读取 {path}：{cause}") from cause
    try:
        data = json.loads(text)
    except json.JSONDecodeError as cause:
        raise CheckError(
            f"{path} 不是合法 JSON：第 {cause.lineno} 行第 {cause.colno} 列 {cause.msg}"
        ) from cause
    if not isinstance(data, dict):
        raise CheckError(f"{path} 必须是 JSON 对象")
    return data


def brief_period(brief: dict) -> tuple[set[str], str | None, str | None, str | None]:
    """返回任务书观察期间的期次集合、显式截止期次、显式数据截止与显式观察月份。"""
    window = brief.get("观察期间")
    months: set[str] = set()
    end: str | None = None
    as_of: str | None = None
    observed: str | None = None
    if isinstance(window, dict):
        for value in window.values():
            if isinstance(value, str):
                months.update(MONTH_PATTERN.findall(value))
        end = first_text(window, *END_KEYS)
        as_of = first_text(window, *AS_OF_KEYS)
        observed = first_text(window, *OBSERVED_KEYS)
    elif isinstance(window, str):
        months.update(MONTH_PATTERN.findall(window))
    elif isinstance(window, list):
        for item in window:
            if isinstance(item, str):
                months.update(MONTH_PATTERN.findall(item))
    return months, end, as_of, observed


def check_period(brief: dict, diagnosis: dict) -> Result:
    """核对任务书观察期间与快照 scope 是否一致。"""
    name = "期间一致"
    scope = diagnosis.get("scope")
    if not isinstance(scope, dict):
        return Result(False, name, "诊断快照缺少 `scope` 对象")
    observed = scope.get("观察月份")
    if not isinstance(observed, str) or not observed.strip():
        return Result(False, name, "诊断快照缺少 `scope.观察月份`")
    months, end, as_of, brief_observed = brief_period(brief)
    if not months:
        return Result(False, name, "任务书缺少 `观察期间`，无法核对期间")
    listed = "/".join(sorted(months))
    problems: list[str] = []
    if observed not in months:
        problems.append(f"`scope.观察月份`={observed} 不在任务书观察期间（{listed}）内")
    if brief_observed and brief_observed != observed:
        problems.append(
            f"任务书 `观察期间.观察月份`={brief_observed} 与 `scope.观察月份`={observed} 不一致"
        )
    if end and end != observed:
        problems.append(f"任务书 `观察期间.截止期次`={end} 与 `scope.观察月份`={observed} 不一致")
    cutoff = scope.get("数据截止")
    if as_of and isinstance(cutoff, str) and as_of != cutoff:
        problems.append(f"任务书 `观察期间.数据截止`={as_of} 与 `scope.数据截止`={cutoff} 不一致")
    if problems:
        return Result(False, name, "；".join(problems))
    return Result(True, name, f"`scope.观察月份`={observed} 在任务书观察期间（{listed}）内")


def check_version(brief: dict, diagnosis: dict) -> Result:
    """核对任务书与快照的 schema 版本是否一致。"""
    name = "版本一致"
    scope = diagnosis.get("scope")
    if not isinstance(scope, dict):
        return Result(False, name, "诊断快照缺少 `scope` 对象")
    brief_version = brief.get("schema版本")
    snapshot_version = scope.get("schema版本")
    if brief_version is None:
        return Result(False, name, "任务书缺少 `schema版本`")
    if snapshot_version is None:
        return Result(False, name, "诊断快照缺少 `scope.schema版本`")
    if brief_version != snapshot_version:
        return Result(
            False,
            name,
            f"任务书 `schema版本`={brief_version} 与 `scope.schema版本`={snapshot_version} 不一致",
        )
    if brief_version != SCHEMA_VERSION:
        return Result(False, name, f"`schema版本`={brief_version}，不是支持的 v{SCHEMA_VERSION}")
    return Result(True, name, f"任务书与快照的 `schema版本` 均为 {brief_version}")


def strip_suffixes(value: str) -> str:
    """去掉段尾的疑问词与助词。"""
    changed = True
    while changed:
        changed = False
        for suffix in QUESTION_SUFFIXES + PARTICLE_SUFFIXES:
            if len(value) > len(suffix) and value.endswith(suffix):
                value = value[: -len(suffix)]
                changed = True
    return value


def head_before_marker(segment: str) -> tuple[str, str]:
    """按第一个疑问词标记把片段切成前后两段；后段不含标记本身。"""
    index = len(segment)
    marker = ""
    for candidate in QUESTION_MARKERS:
        found = segment.find(candidate)
        if found >= 0 and found < index:
            index = found
            marker = candidate
    return segment[:index], segment[index + len(marker) :]


def extract_keywords(text: str) -> list[str]:
    """按文档里的规则从问题原文提取关键词。"""
    if not text:
        return []
    dates = DATE_PATTERN.findall(text)
    segments = SEGMENT_PATTERN.findall(DATE_PATTERN.sub(" ", text))
    keywords: list[str] = list(dict.fromkeys(dates))
    plain: list[str] = []
    for segment in segments:
        head, tail = head_before_marker(segment)
        head = strip_suffixes(head.strip(PARTICLE_CHARS))
        candidates = [head]
        if len(head) < MIN_KEYWORD_LENGTH:
            candidates.append(strip_suffixes(tail.strip(PARTICLE_CHARS)))
        if "的" in head:
            candidates.extend(part.strip(PARTICLE_CHARS) for part in head.split("的"))
        for candidate in candidates:
            if len(candidate) >= MIN_KEYWORD_LENGTH and candidate not in plain:
                plain.append(candidate)
    plain.sort(key=len, reverse=True)
    for value in plain:
        if value not in keywords:
            keywords.append(value)
        if len([item for item in keywords if item not in dates]) >= 3:
            break
    return keywords


def collect_strings(node: object) -> list[str]:
    """收集节点里的全部字符串（键名与值）。"""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            found.append(str(key))
            found.extend(collect_strings(value))
        return found
    if isinstance(node, list):
        found = []
        for item in node:
            found.extend(collect_strings(item))
        return found
    return []


def searchable_text(diagnosis: dict) -> str:
    """insights 与 recommendations 里的全部字符串，拼接成可搜索文本。"""
    parts: list[str] = []
    for name in ("insights", "recommendations"):
        parts.extend(collect_strings(diagnosis.get(name)))
    return "\n".join(parts)


def answer_units(diagnosis: dict) -> list[str]:
    """按 insight/recommendation 条目返回可归属的答案文本，去掉纯重复项。"""
    units: list[str] = []
    seen: set[str] = set()
    for name in ("insights", "recommendations"):
        section = diagnosis.get(name)
        values = section if isinstance(section, list) else [section]
        for value in values:
            if value is None:
                continue
            text = "\n".join(collect_strings(value))
            key = normalize_match_text(text)
            if text and key and key not in seen:
                seen.add(key)
                units.append(text)
    return units


def normalize_match_text(value: str) -> str:
    """统一大小写、空白及常见标点，支持问题文本的格式变体。"""
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def exact_id_in_text(identifier: str, text: str) -> bool:
    """匹配完整 id，避免 Q1 误命中 Q10。"""
    if not identifier or len(identifier) < 2:
        return False
    if re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])", text) is not None
    return identifier in text


def question_entries(brief: dict) -> list[tuple[str, str | None]]:
    """从 `必答问题` 取出 (问题原文, id) 列表。"""
    raw = brief.get("必答问题")
    entries: list[tuple[str, str | None]] = []
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if isinstance(item, str):
            entries.append((item.strip(), None))
        elif isinstance(item, dict):
            text = first_text(item, *QUESTION_TEXT_KEYS) or ""
            identifier = first_text(item, *QUESTION_ID_KEYS)
            entries.append((text, identifier))
        else:
            entries.append((json.dumps(item, ensure_ascii=False), None))
    return entries


def check_question(question: str, identifier: str | None, text: str) -> Result:
    """核对一条必答问题是否在快照里被引用。"""
    name = "必答问题覆盖"
    label = identifier or question
    if len(label) > 24:
        label = label[:24] + "…"
    if identifier and exact_id_in_text(identifier, text):
        return Result(True, name, f"必答问题「{label}」按 id 被引用")
    if question and normalize_match_text(question) in normalize_match_text(text):
        return Result(True, name, f"必答问题「{label}」按原文被引用")
    keywords = extract_keywords(question)
    for keyword in keywords:
        if normalize_match_text(keyword) in normalize_match_text(text):
            return Result(True, name, f"必答问题「{label}」按关键词「{keyword}」被引用")
    detail = f"必答问题「{label}」未在 insights/recommendations 中被引用"
    if keywords:
        detail += f"（关键词：{'、'.join(keywords)}）"
    return Result(False, name, detail)


def check_questions(brief: dict, diagnosis: dict) -> list[Result]:
    """核对每条必答问题是否在快照里被引用。"""
    name = "必答问题覆盖"
    raw = brief.get("必答问题")
    if not isinstance(raw, list):
        if raw is None:
            return [Result(False, name, "任务书缺少 `必答问题` 数组")]
        return [Result(False, name, "任务书 `必答问题` 不是数组")]
    entries = question_entries(brief)
    if not entries:
        return [Result(False, name, "任务书 `必答问题` 为空，没有可核对的问题")]
    units = answer_units(diagnosis)
    normalized_units = [normalize_match_text(unit) for unit in units]
    keyword_sets = [extract_keywords(question) for question, _ in entries]
    # 每个答案只归属一个问题：id > 全文 > 唯一关键词；同级多候选则形成冲突。
    assignments: dict[int, tuple[str, int, str]] = {}
    conflicts: dict[int, list[tuple[int, str]]] = {}
    for answer_index, (unit, normalized) in enumerate(zip(units, normalized_units)):
        candidates: dict[str, set[int]] = {"id": set(), "全文": set(), "关键词": set()}
        evidence: dict[tuple[str, int], str] = {}
        for question_index, ((question, identifier), keywords) in enumerate(zip(entries, keyword_sets)):
            if identifier and exact_id_in_text(identifier, unit):
                candidates["id"].add(question_index)
                evidence[("id", question_index)] = identifier
            elif question and normalize_match_text(question) in normalized:
                candidates["全文"].add(question_index)
                evidence[("全文", question_index)] = question
            else:
                for keyword in keywords:
                    normalized_keyword = normalize_match_text(keyword)
                    if normalized_keyword in normalized:
                        candidates["关键词"].add(question_index)
                        evidence[("关键词", question_index)] = keyword
                        break
        for match_type in ("id", "全文", "关键词"):
            matched = candidates[match_type]
            if not matched:
                continue
            if len(matched) > 1:
                for question_index in matched:
                    conflicts.setdefault(question_index, []).extend(
                        (other, evidence[(match_type, other)])
                        for other in sorted(matched - {question_index})
                    )
            else:
                question_index = next(iter(matched))
                assignments.setdefault(question_index, (match_type, answer_index, evidence[(match_type, question_index)]))
            break

    findings: list[Result] = []
    for index, (question, identifier) in enumerate(entries):
        label = identifier or question
        if len(label) > 24:
            label = label[:24] + "…"
        conflict_items = conflicts.get(index, [])
        conflict_indexes = sorted({item[0] for item in conflict_items})
        if index in assignments:
            match_type, _, evidence = assignments[index]
            finding = {"question": {"id": identifier, "text": question}, "match": match_type, "evidence": evidence}
            findings.append(Result(True, name, f"必答问题「{label}」按{match_type}被引用；证据：{evidence}", finding))
            continue
        if conflict_indexes:
            conflict_labels = [entries[item][1] or entries[item][0] for item in conflict_indexes]
            finding = {
                "question": {"id": identifier, "text": question},
                "match": None,
                "evidence": sorted({item[1] for item in conflict_items}),
                "conflicts": [{"id": entries[item][1], "text": entries[item][0]} for item in conflict_indexes],
            }
            findings.append(Result(False, name, f"必答问题「{label}」命中冲突：{ '、'.join(conflict_labels) }；答案不能同时覆盖两个问题", finding))
            continue
        keywords = keyword_sets[index]
        detail = f"必答问题「{label}」未在 insights/recommendations 中被引用"
        if keywords:
            detail += f"（关键词：{'、'.join(keywords)}）"
        findings.append(Result(False, name, detail, {"question": {"id": identifier, "text": question}, "match": None, "evidence": None}))
    return findings


def digest(data: bytes) -> str:
    """返回内容的 sha256 前 12 位，用于交接记录。"""
    return hashlib.sha256(data).hexdigest()[:12]


def check_dist(data_path: Path, dist: Path) -> Result:
    """核对站点产物里的诊断数据与快照字节相同。"""
    name = "站点数据一致"
    site = Path(dist) / "data" / "diagnosis.json"
    if not site.is_file():
        return Result(False, name, f"站点目录缺少 `data/diagnosis.json`（缺失的站点产物：{site}）")
    try:
        site_bytes = site.read_bytes()
        data_bytes = Path(data_path).read_bytes()
    except OSError as cause:
        return Result(False, name, f"无法读取站点数据文件：{cause}")
    if site_bytes == data_bytes:
        return Result(True, name, f"`data/diagnosis.json` 与 `--data` 字节相同（sha256={digest(site_bytes)}）")
    return Result(
        False,
        name,
        f"`data/diagnosis.json` 与 `--data` 字节不同（sha256={digest(site_bytes)} vs {digest(data_bytes)}）",
    )


def forbidden_paths(node: object, path: str = "") -> list[str]:
    """返回命中隐私边界黑名单的键名路径。

    仅递归检查键名，嵌套数组与对象中的多行文本值不作为路径，避免输出被换行截断。
    """
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}/{key}"
            for token in FORBIDDEN_FIELD_TOKENS:
                if token in key:
                    hits.append(f"{child}（{token}）")
                    break
            hits.extend(forbidden_paths(value, child))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            hits.extend(forbidden_paths(item, f"{path}/{index}"))
    return hits


def check_forbidden_fields(diagnosis: dict) -> Result:
    """核对快照是否含个人/薪酬/编制粒度的字段名。"""
    name = "隐私边界"
    hits = forbidden_paths(diagnosis)
    if hits:
        return Result(False, name, "出现个人/薪酬/编制粒度字段名：" + "、".join(hits))
    return Result(True, name, "未出现个人/薪酬/编制粒度字段名")


def run_checks(
    brief: dict,
    diagnosis: dict,
    data_path: Path | None = None,
    dist: Path | None = None,
) -> list[Result]:
    """执行全部检查并返回逐条结果。"""
    results = [check_period(brief, diagnosis), check_version(brief, diagnosis)]
    results.extend(check_questions(brief, diagnosis))
    if dist is not None and data_path is not None:
        results.append(check_dist(data_path, dist))
    results.append(check_forbidden_fields(diagnosis))
    return results


def markdown_report(results: list[Result]) -> str:
    """将检查结果格式化为 Markdown 报告。"""
    failed = sum(not result.ok for result in results)
    lines = [
        "# 组织诊断交叉审查报告",
        "",
        f"**汇总：共 {len(results)} 项检查，{len(results) - failed} 项通过，{failed} 项失败。**",
        "",
    ]
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        lines.append(f"- **{status} {result.name}**：{result.detail}")
    return "\n".join(lines) + "\n"


def finding_path(result: Result) -> str:
    """返回失败检查对应的稳定逻辑路径。"""
    paths = {
        "期间一致": "/观察期间",
        "版本一致": "/schema版本",
        "必答问题覆盖": "/必答问题",
        "站点数据一致": "/dist/data/diagnosis.json",
        "隐私边界": "/diagnosis",
    }
    if result.name == "隐私边界":
        match = re.search(r"(/[^（、]+)", result.detail)
        if match:
            return match.group(1)
    return paths.get(result.name, f"/{result.name}")


def findings_report(results: list[Result]) -> list[dict[str, object]]:
    """序列化失败项为统一的 finding 契约。"""
    findings: list[dict[str, object]] = []
    for result in results:
        if result.ok:
            continue
        finding: dict[str, object] = {
            "severity": "error",
            "path": finding_path(result),
            "message": result.detail,
        }
        if result.finding:
            finding["context"] = result.finding
        findings.append(finding)
    return findings


def json_report(results: list[Result]) -> str:
    """将检查结果格式化为稳定的 JSON 报告。"""
    failed = sum(not result.ok for result in results)
    payload = {
        "ok": failed == 0,
        "findings": findings_report(results),
        "checks": [
            {"name": result.name, "ok": result.ok, "detail": result.detail}
            for result in results
        ],
        "summary": {
            "total": len(results),
            "passed": len(results) - failed,
            "failed": failed,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    """核对三方一致性。返回进程退出码。"""
    parser = argparse.ArgumentParser(
        description="核对任务书、诊断快照与站点产物的三方一致性，逐条输出 PASS/FAIL。"
    )
    parser.add_argument("--brief", type=Path, required=True, help="任务书 JSON。")
    parser.add_argument("--data", type=Path, required=True, help="诊断快照 JSON。")
    parser.add_argument(
        "--dist",
        type=Path,
        default=None,
        help="站点产物目录；给出时核对 `data/diagnosis.json` 与 `--data` 是否字节相同。",
    )
    parser.add_argument("--out", type=Path, default=None, help="可选的 Markdown 报告输出路径。")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="可选的 JSON 结果输出路径。",
    )
    args = parser.parse_args(argv)
    try:
        brief = load_json(args.brief)
        diagnosis = load_json(args.data)
    except CheckError as cause:
        print(str(cause), file=sys.stderr)
        return EXIT_USAGE
    results = run_checks(brief, diagnosis, args.data, args.dist)
    if args.out is not None:
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(markdown_report(results), encoding="utf-8")
        except OSError as cause:
            print(f"无法写入报告 {args.out}：{cause}", file=sys.stderr)
            return EXIT_USAGE
    if args.json_out is not None:
        try:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json_report(results), encoding="utf-8")
        except OSError as cause:
            print(f"无法写入 JSON 结果 {args.json_out}：{cause}", file=sys.stderr)
            return EXIT_USAGE
    failed = 0
    for result in results:
        if result.ok:
            print(f"PASS {result.name}：{result.detail}")
        else:
            failed += 1
            print(f"FAIL {result.name}：{result.detail}")
    print(f"共 {len(results)} 项检查：{len(results) - failed} 项通过，{failed} 项失败")
    return EXIT_FAIL if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
