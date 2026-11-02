#!/usr/bin/env python3
"""把诊断快照构建成可预览的静态站点目录。

用法：
    python3 EX/builder/build_site.py --data <diagnosis.json> --out <dist> [--bundle <app.bundle.js>] [--source-commit SHA]

先用示例目录的 validate.py 校验数据；数据不合格时退出码非 0，并且不生成 dist 目录。
构建产物：
    <dist>/index.html
    <dist>/app.bundle.js
    <dist>/styles.css
    <dist>/data/diagnosis.json
    <dist>/site-summary.json
    <dist>/build-report.json

site-summary.json 是 Builder 交给 Reviewer 的站点内容摘要，带角色元数据
（producer 与 periods），期次沿用诊断快照声明的 periods；快照没声明 periods 时
按快照 `scope.观察月份` 与指标期次键回落推导。

不带 --bundle 时，用与 serve.py 的 build_bundle() 相同的 bun 命令打包，输出直接写到 dist。
只使用 Python 3.9+ 标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

EX_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PATH = EX_ROOT / "validate.py"
SCHEMA_PATH = EX_ROOT / "schema" / "diagnosis.schema.json"

# dist 内相对 dist 根的固定路径；顺序即 build-report.json 里的文件清单顺序。
SITE_FILES = ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json", "site-summary.json")
STATIC_FILES = ("index.html", "styles.css")
BUNDLE_NAME = "app.bundle.js"
DATA_RELATIVE = Path("data") / "diagnosis.json"
REPORT_NAME = "build-report.json"
# 站点摘要交接文件名：Builder 交给 Reviewer 的站点内容摘要。
SUMMARY_NAME = "site-summary.json"
# 交接文件里的角色元数据：产出方写进站点摘要，供 Reviewer 核对。
PRODUCER = "builder"
# 期次键的月份前缀：形如 `2026-01实际` 的键取 `2026-01`。
MONTH_PATTERN = re.compile(r"\A([0-9]{4}-(0[1-9]|1[0-2]))")

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_BUILD = 3
DIAGNOSTIC_DIR_NAME = ".build-diagnostics"
FAILURE_REPORT_NAME = "build-failure.json"


def load_module(name: str, path: Path) -> ModuleType:
    """按文件路径加载模块，避免与同名的标准库或仓库内模块冲突。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_of(path: Path) -> str:
    """返回文件的 sha256 十六进制摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bun_command(outfile: Path) -> list[str]:
    """与 serve.py 的 build_bundle() 相同的 bun 命令，输出文件改写到目标路径。"""
    return ["bun", "build", "app.js", "--target=browser", f"--outfile={outfile}"]


class BundleBuildError(RuntimeError):
    """bundle 命令无法产出可用文件时的结构化错误。"""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def build_bundle(outfile: Path) -> None:
    """调用 bun 打包浏览器端脚本，产物直接写入 outfile。"""
    command = bun_command(outfile.resolve())
    try:
        subprocess.run(command, check=True, cwd=str(EX_ROOT))
    except FileNotFoundError as cause:
        raise BundleBuildError(f"构建命令不存在：{command[0]}") from cause
    except subprocess.CalledProcessError as cause:
        raise BundleBuildError(f"构建命令返回非零退出码：{cause.returncode}", cause.returncode) from cause
    if not outfile.is_file():
        raise BundleBuildError(f"构建命令输出不是文件：{outfile}")


def copy_static_files(out_dir: Path) -> None:
    """复制页面骨架与样式到 dist。"""
    for name in STATIC_FILES:
        shutil.copyfile(EX_ROOT / name, out_dir / name)


def report_relative_path(path: Path, out_dir: Path) -> str:
    """返回相对于构建工作目录（dist 的父目录）的稳定 POSIX 路径。"""
    return Path(os.path.relpath(path.resolve(), out_dir.parent.resolve())).as_posix()


def load_diagnosis(path: Path) -> dict:
    """读取诊断快照内容，用于整理站点摘要交接文件。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as cause:
        raise ValueError(f"无法读取诊断快照 {path}：{cause}") from cause
    if not isinstance(data, dict):
        raise ValueError(f"诊断快照 {path} 必须是 JSON 对象")
    return data


def site_periods(data: dict) -> list[str]:
    """站点摘要的期次元数据：沿用快照声明的 periods，快照没声明时按期次锚点回落。

    回落口径：快照 `scope.观察月份` 与 `metrics[*].数值` 里的期次键月份（YYYY-MM），
    保证站点摘要总有 periods，不因快照漏写元数据而缺字段。
    """
    declared = data.get("periods")
    if isinstance(declared, list):
        cleaned = [str(item).strip() for item in declared if str(item).strip()]
        if cleaned:
            return cleaned
    return snapshot_period_months(data)


def snapshot_period_months(data: dict) -> list[str]:
    """从快照里取期次月份：观察月份加上指标数值里的期次键月份，按时间升序去重。"""
    months: list[str] = []
    scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    observed = scope.get("观察月份")
    if isinstance(observed, str):
        match = MONTH_PATTERN.match(observed.strip())
        if match:
            months.append(match.group(1))
    metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else []
    for metric in metrics:
        if isinstance(metric, dict):
            collect_period_months(metric.get("数值"), months)
    return sorted(set(months))


def collect_period_months(node: object, months: list[str]) -> None:
    """递归收集数值节点里的期次键月份（期次键形如 `2026-01实际`）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            match = MONTH_PATTERN.match(key.strip()) if isinstance(key, str) else None
            if match:
                if match.group(1) not in months:
                    months.append(match.group(1))
                continue
            collect_period_months(value, months)
    elif isinstance(node, list):
        for item in node:
            collect_period_months(item, months)


def build_site_summary(data: dict) -> dict:
    """按诊断快照整理站点内容摘要：角色元数据加页面展示的条目清单。"""
    scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    metrics = data.get("metrics") if isinstance(data.get("metrics"), list) else []
    insights = data.get("insights") if isinstance(data.get("insights"), list) else []
    recommendations = data.get("recommendations") if isinstance(data.get("recommendations"), list) else []
    titles = [
        metric.get("标题")
        for metric in metrics
        if isinstance(metric, dict) and isinstance(metric.get("标题"), str)
    ]
    observed = scope.get("观察月份") if isinstance(scope.get("观察月份"), str) else ""
    summary: dict[str, object] = {"producer": PRODUCER}
    periods = site_periods(data)
    if periods:
        summary["periods"] = periods
    summary["观察月份"] = observed
    summary["指标标题"] = titles
    summary["洞察条数"] = len(insights)
    summary["建议条数"] = len(recommendations)
    summary["摘要"] = (
        f"站点已生成：{len(titles)} 个指标、{len(insights)} 条洞察、{len(recommendations)} 条建议"
        + (f"；观察月份 {observed}。" if observed else "。")
    )
    return summary


def write_site_summary(path: Path, data: dict) -> dict:
    """写出站点内容摘要交接文件，返回摘要内容。"""
    summary = build_site_summary(data)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def write_report(
    out_dir: Path,
    data_path: Path,
    source_commit: str | None = None,
    brief_path: Path | None = None,
) -> dict:
    """写 build-report.json：输入摘要、bundle 摘要、复制文件清单与生成时间。"""
    files = []
    for relative in SITE_FILES:
        path = out_dir / relative
        files.append(
            {
                "路径": relative,
                "字节数": path.stat().st_size,
                "sha256": sha256_of(path),
            }
        )
    input_path = str(data_path.resolve())
    input_sha256 = sha256_of(data_path)
    input_summary = {
        "diagnosis": {
            "路径": report_relative_path(data_path, out_dir),
            "sha256": input_sha256,
        }
    }
    if brief_path is not None:
        input_summary["brief"] = {
            "路径": report_relative_path(brief_path, out_dir),
            "sha256": sha256_of(brief_path),
        }
    bundle_entry = next(entry for entry in files if entry["路径"] == BUNDLE_NAME)
    previous_time = None
    report_path = out_dir / REPORT_NAME
    if report_path.is_file():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            previous = None
        if (
            isinstance(previous, dict)
            and previous.get("输入路径") == input_path
            and previous.get("sha256") == input_sha256
            and previous.get("文件清单") == files
            and previous.get("输入摘要") == input_summary
            and previous.get("bundle") == bundle_entry
            and previous.get("复制文件") == list(SITE_FILES)
            and previous.get("来源提交") == source_commit
            and isinstance(previous.get("生成时间"), str)
        ):
            previous_time = previous["生成时间"]
    report = {
        "输入路径": input_path,
        "sha256": input_sha256,
        "输入摘要": input_summary,
        "bundle": bundle_entry,
        "复制文件": list(SITE_FILES),
        "文件清单": files,
        "生成时间": previous_time
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if source_commit is not None:
        report["来源提交"] = source_commit
    (out_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def write_failure_report(
    out_dir: Path,
    stage: str,
    command: list[str],
    exit_code: int,
    command_exit_code: int | None,
    error: str,
) -> Path:
    """将失败信息写到 dist 外，避免诊断文件伪装成可用站点报告。"""
    diagnostic_dir = out_dir.parent / DIAGNOSTIC_DIR_NAME
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "状态": "失败",
        "阶段": stage,
        "命令": command,
        "退出码": exit_code,
        "命令退出码": command_exit_code,
        "错误": error,
        "生成时间": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    report_path = diagnostic_dir / FAILURE_REPORT_NAME
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path


def build_site(
    data_path: Path,
    out_dir: Path,
    bundle_path: Path | None = None,
    source_commit: str | None = None,
    brief_path: Path | None = None,
    data: dict | None = None,
) -> dict:
    """把已校验的数据构建成 dist 目录，返回 build-report 的内容。

    data_path 是诊断快照；bundle_path 是已编译好的浏览器包，给 None 时调用 bun 打包。
    data 给出已读入的快照内容，给 None 时从 data_path 读。
    """
    if bundle_path is not None and not bundle_path.is_file():
        raise FileNotFoundError(f"bundle 文件不存在或不是有效文件：{bundle_path}")
    if brief_path is None:
        candidate = data_path.parent / "brief.json"
        brief_path = candidate if candidate.is_file() else None
    elif not brief_path.is_file():
        raise FileNotFoundError(f"brief 文件不存在或不是有效文件：{brief_path}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.build-", dir=str(out_dir.parent)))
    try:
        (staging / "data").mkdir(parents=True, exist_ok=True)
        copy_static_files(staging)
        shutil.copyfile(data_path, staging / DATA_RELATIVE)
        # 站点摘要交接文件：角色元数据与页面条目清单，供 Reviewer 核对。
        write_site_summary(staging / SUMMARY_NAME, data if data is not None else load_diagnosis(data_path))
        if bundle_path is None:
            build_bundle(staging / BUNDLE_NAME)
        else:
            shutil.copyfile(bundle_path, staging / BUNDLE_NAME)
        report = write_report(staging, data_path, source_commit, brief_path)

        out_dir.mkdir(parents=True, exist_ok=True)
        for relative in (*SITE_FILES, REPORT_NAME):
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staging / relative, target)
        return report
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="把诊断快照构建成可预览的静态站点目录。")
    parser.add_argument("--data", type=Path, required=True, help="诊断快照路径（schema v4）")
    parser.add_argument("--brief", type=Path, default=None, help="任务书路径（可选，用于构建报告输入摘要）")
    parser.add_argument("--out", type=Path, required=True, help="输出目录 dist")
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="已编译好的浏览器包；不给则调用 bun 打包",
    )
    parser.add_argument("--source-commit", default=None, help="来源 git 提交 SHA")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """校验数据并构建站点。返回进程退出码。"""
    args = parse_args(argv)
    data_path: Path = args.data
    brief_path: Path | None = args.brief
    out_dir: Path = args.out
    bundle_path: Path | None = args.bundle
    source_commit: str | None = args.source_commit

    if not data_path.is_file():
        print(f"数据文件不存在：{data_path}", file=sys.stderr)
        return EXIT_USAGE
    if bundle_path is not None and not bundle_path.is_file():
        print(
            f"bundle 文件不存在或不是有效文件：{bundle_path}；请先生成 demo/vendor/app.bundle.js，或指定有效的 --bundle 文件。",
            file=sys.stderr,
        )
        return EXIT_INVALID
    if brief_path is not None and not brief_path.is_file():
        print(f"brief 文件不存在或不是有效文件：{brief_path}", file=sys.stderr)
        return EXIT_USAGE

    validate = load_module("org_health_validate", VALIDATE_PATH)
    try:
        schema = validate.load_schema(SCHEMA_PATH)
    except ValueError as cause:
        print(f"结构定义不可用：{cause}", file=sys.stderr)
        return EXIT_USAGE
    try:
        data = validate.load_data(data_path)
    except ValueError as cause:
        print(f"数据文件不可用：{cause}", file=sys.stderr)
        return EXIT_INVALID
    errors = validate.Validator(schema).errors(data)
    if errors:
        # 数据不合格：不生成 dist，退出码非 0。
        print(f"{data_path}: 校验失败（{len(errors)} 处问题）", file=sys.stderr)
        for message in errors:
            print(f"  {message}", file=sys.stderr)
        return EXIT_INVALID

    build_command = bun_command((out_dir / BUNDLE_NAME).resolve()) if bundle_path is None else []
    try:
        report = build_site(data_path, out_dir, bundle_path, source_commit, brief_path, data)
    except BundleBuildError as cause:
        diagnostic_path = write_failure_report(
            out_dir,
            "bundle",
            build_command,
            EXIT_BUILD,
            cause.returncode,
            str(cause),
        )
        print(f"构建失败：{cause}；诊断报告：{diagnostic_path}", file=sys.stderr)
        return EXIT_BUILD
    except (OSError, subprocess.CalledProcessError) as cause:
        print(f"构建失败：{cause}", file=sys.stderr)
        return EXIT_BUILD

    print(f"已生成站点：{out_dir}")
    for entry in report["文件清单"]:
        print(f"  {entry['路径']}（{entry['字节数']} 字节，sha256 {entry['sha256'][:12]}…）")
    print(f"  {REPORT_NAME}（输入 {report['输入路径']}）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
