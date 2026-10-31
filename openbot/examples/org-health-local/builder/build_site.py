#!/usr/bin/env python3
"""把诊断快照构建成可预览的静态站点目录。

用法：
    python3 EX/builder/build_site.py --data <diagnosis.json> --out <dist> [--bundle <app.bundle.js>]

先用示例目录的 validate.py 校验数据；数据不合格时退出码非 0，并且不生成 dist 目录。
构建产物：
    <dist>/index.html
    <dist>/app.bundle.js
    <dist>/styles.css
    <dist>/data/diagnosis.json
    <dist>/build-report.json

不带 --bundle 时，用与 serve.py 的 build_bundle() 相同的 bun 命令打包，输出直接写到 dist。
只使用 Python 3.9+ 标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

EX_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PATH = EX_ROOT / "validate.py"
SCHEMA_PATH = EX_ROOT / "schema" / "diagnosis.schema.json"

# dist 内相对 dist 根的固定路径；顺序即 build-report.json 里的文件清单顺序。
SITE_FILES = ("index.html", "app.bundle.js", "styles.css", "data/diagnosis.json")
STATIC_FILES = ("index.html", "styles.css")
BUNDLE_NAME = "app.bundle.js"
DATA_RELATIVE = Path("data") / "diagnosis.json"
REPORT_NAME = "build-report.json"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_BUILD = 3


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


def build_bundle(outfile: Path) -> None:
    """调用 bun 打包浏览器端脚本，产物直接写入 outfile。"""
    subprocess.run(bun_command(outfile.resolve()), check=True, cwd=str(EX_ROOT))


def copy_static_files(out_dir: Path) -> None:
    """复制页面骨架与样式到 dist。"""
    for name in STATIC_FILES:
        shutil.copyfile(EX_ROOT / name, out_dir / name)


def write_report(out_dir: Path, data_path: Path) -> dict:
    """写 build-report.json：输入路径、输入 sha256、文件清单、生成时间。"""
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
    report = {
        "输入路径": str(data_path.resolve()),
        "sha256": sha256_of(data_path),
        "文件清单": files,
        "生成时间": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def build_site(data_path: Path, out_dir: Path, bundle_path: Path | None = None) -> dict:
    """把已校验的数据构建成 dist 目录，返回 build-report 的内容。

    data_path 是诊断快照；bundle_path 是已编译好的浏览器包，给 None 时调用 bun 打包。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    copy_static_files(out_dir)
    shutil.copyfile(data_path, out_dir / DATA_RELATIVE)
    if bundle_path is None:
        build_bundle(out_dir / BUNDLE_NAME)
    else:
        shutil.copyfile(bundle_path, out_dir / BUNDLE_NAME)
    return write_report(out_dir, data_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="把诊断快照构建成可预览的静态站点目录。")
    parser.add_argument("--data", type=Path, required=True, help="诊断快照路径（schema v4）")
    parser.add_argument("--out", type=Path, required=True, help="输出目录 dist")
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="已编译好的浏览器包；不给则调用 bun 打包",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """校验数据并构建站点。返回进程退出码。"""
    args = parse_args(argv)
    data_path: Path = args.data
    out_dir: Path = args.out
    bundle_path: Path | None = args.bundle

    if not data_path.is_file():
        print(f"数据文件不存在：{data_path}", file=sys.stderr)
        return EXIT_USAGE
    if bundle_path is not None and not bundle_path.is_file():
        print(
            f"bundle 文件不存在：{bundle_path}；请先生成 demo/vendor/app.bundle.js，或指定有效的 --bundle 文件。",
            file=sys.stderr,
        )
        return EXIT_INVALID

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

    created = not out_dir.exists()
    try:
        report = build_site(data_path, out_dir, bundle_path)
    except (OSError, subprocess.CalledProcessError) as cause:
        print(f"构建失败：{cause}", file=sys.stderr)
        if created and out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        return EXIT_BUILD

    print(f"已生成站点：{out_dir}")
    for entry in report["文件清单"]:
        print(f"  {entry['路径']}（{entry['字节数']} 字节，sha256 {entry['sha256'][:12]}…）")
    print(f"  {REPORT_NAME}（输入 {report['输入路径']}）")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
