#!/usr/bin/env python3
"""Check the org-health example for accidental real-data traces."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLE_ROOT = ROOT / "openbot" / "examples" / "org-health-local"
SKIP_DIRECTORIES = {"node_modules", "build"}
PATTERNS = (
    (re.compile(r"/Users/"), "绝对用户路径"),
    (re.compile(r"/home/[A-Za-z0-9._-]+/"), "绝对用户路径"),
    (re.compile(r"C:\\Users\\"), "绝对用户路径"),
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"), "邮箱地址"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "中国手机号"),
    (re.compile(r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"), "身份证号"),
)


def is_skipped(path: Path, root: Path) -> bool:
    # The checked-in demo bundle is a generated build artifact, like build/.
    return bool(set(path.relative_to(root).parts) & SKIP_DIRECTORIES) or path.name.endswith(".bundle.js")


def iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or is_skipped(path, root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield path, text


def check(root: Path) -> list[str]:
    findings: list[str] = []
    for path, text in iter_text_files(root):
        for line_number, line in enumerate(text.splitlines(), 1):
            for pattern, label in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path}:{line_number}: {label}")
        if path.suffix.lower() == ".json" and "data" in path.relative_to(root).parts:
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                findings.append(f"{path}:{exc.lineno}: JSON 无法解析，无法确认 synthetic 标记")
            else:
                if not isinstance(value, dict) or value.get("synthetic") is not True:
                    first_line = next((i for i, line in enumerate(text.splitlines(), 1) if line.strip()), 1)
                    findings.append(f"{path}:{first_line}: data/ JSON 缺少顶层 \"synthetic\": true")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查组织健康示例中的隐私数据痕迹")
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_EXAMPLE_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"{root}: 扫描目录不存在", file=sys.stderr)
        return 2
    findings = check(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"隐私检查失败：{len(findings)} 个问题", file=sys.stderr)
        return 1
    print(f"隐私检查通过：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
