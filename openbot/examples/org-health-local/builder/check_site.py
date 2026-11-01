#!/usr/bin/env python3
"""检查一个已构建的组织诊断静态站点。"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import posixpath
import re
import sys
from urllib.parse import unquote, urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = (
    "index.html",
    "app.bundle.js",
    "styles.css",
    "data/diagnosis.json",
    "build-report.json",
)
DIAGNOSIS_CORE_SECTIONS = (
    "metrics",
    "insights",
    "definitions",
    "risks",
    "recommendations",
)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
EXTERNAL_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class SectionChecker(HTMLParser):
    """收集 HTML 中内容为空的 section 元素。"""

    def __init__(self) -> None:
        super().__init__()
        self.empty_sections: list[str] = []
        self._section_stack: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section":
            attrs_dict = dict(attrs)
            if "id" in attrs_dict and attrs_dict["id"]:
                desc = f"<section id='{attrs_dict['id']}'>"
            elif "class" in attrs_dict and attrs_dict["class"]:
                desc = f"<section class='{attrs_dict['class']}'>"
            else:
                desc = "<section>"
            self._section_stack.append({"desc": desc, "has_content": False})
        elif self._section_stack:
            for item in self._section_stack:
                item["has_content"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "section" and self._section_stack:
            item = self._section_stack.pop()
            if not item["has_content"]:
                self.empty_sections.append(str(item["desc"]))

    def handle_data(self, data: str) -> None:
        if self._section_stack and data.strip():
            for item in self._section_stack:
                item["has_content"] = True


class LocalReferences(HTMLParser):
    """收集 HTML 中会加载本地资源的引用。"""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = {"script": "src", "link": "href", "img": "src"}.get(tag)
        if attribute:
            value = dict(attrs).get(attribute)
            if value:
                self.references.append(value)


class AnchorIds(HTMLParser):
    """收集页面中可作为页内锚点的 id/name。"""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        for attribute in ("id", "name"):
            value = attrs_dict.get(attribute)
            if value:
                self.ids.add(value)


class InPageLinks(HTMLParser):
    """收集链接元素中的页内引用，避免把资源 URL 的片段当作锚点。"""

    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            value = dict(attrs).get("href")
            if value:
                self.references.append(value)


def sha256(path: Path) -> str:
    """返回文件的 SHA-256 摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_report_path(relative: str) -> str:
    """规范化报告中的相对路径，并拒绝越出 dist 根目录的路径。"""
    candidate = posixpath.normpath(relative.replace("\\", "/"))
    if candidate in ("", ".") or candidate.startswith("/") or candidate == ".." or candidate.startswith("../"):
        raise ValueError(f"文件路径必须是 dist 内的相对路径：{relative}")
    return candidate


def report_hashes(report: object) -> dict[str, str]:
    """读取报告中的文件摘要，兼容 build_site.py 的 文件清单 列表、files 或顶层 sha256 两种表示。"""
    if not isinstance(report, dict):
        raise ValueError("build-report.json 必须是对象")
    manifest = report.get("文件清单")
    if isinstance(manifest, list):
        hashes: dict[str, str] = {}
        for item in manifest:
            if not isinstance(item, dict) or not isinstance(item.get("路径"), str) or not isinstance(item.get("sha256"), str):
                raise ValueError("文件清单 每项必须包含 路径 与 sha256 字符串")
            relative = normalize_report_path(item["路径"])
            if relative in hashes:
                raise ValueError(f"文件清单存在重复路径或规范化后指向同一文件：{item['路径']}（{relative}）")
            hashes[relative] = item["sha256"]
        return hashes
    source = report.get("files", report.get("sha256"))
    if not isinstance(source, dict):
        raise ValueError("build-report.json 缺少 files 或 sha256 对象")
    hashes: dict[str, str] = {}
    for name, value in source.items():
        if isinstance(value, dict):
            value = value.get("sha256")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("文件摘要必须是 文件名 -> sha256 字符串")
        relative = normalize_report_path(name)
        if relative in hashes:
            raise ValueError(f"文件摘要存在重复路径或规范化后指向同一文件：{name}（{relative}）")
        hashes[relative] = value
    return hashes


def validate_report_metadata(report: object) -> list[str]:
    """校验新格式报告的输入摘要、bundle 摘要与复制清单。"""
    if not isinstance(report, dict):
        return ["build-report.json 必须是对象"]
    failures: list[str] = []
    summary = report.get("输入摘要")
    if summary is not None:
        if not isinstance(summary, dict):
            failures.append("失败：build-report.json 的输入摘要必须是对象")
        else:
            for name in ("brief", "diagnosis"):
                item = summary.get(name)
                if item is None and name == "brief":
                    continue
                if not isinstance(item, dict) or not isinstance(item.get("路径"), str):
                    failures.append(f"失败：build-report.json 缺少输入摘要 {name} 的相对路径")
                elif not isinstance(item.get("sha256"), str) or not HASH_RE.fullmatch(item["sha256"]):
                    failures.append(f"失败：build-report.json 的输入摘要 {name} 缺少有效 sha256")
                else:
                    input_path = item["路径"].replace("\\", "/")
                    if not input_path or input_path.startswith("/") or "\x00" in input_path:
                        failures.append(f"失败：build-report.json 的输入摘要 {name} 路径必须是相对路径")
    bundle = report.get("bundle")
    if bundle is not None:
        if not isinstance(bundle, dict) or bundle.get("路径") != "app.bundle.js":
            failures.append("失败：build-report.json 的 bundle 路径必须是 app.bundle.js")
        elif not isinstance(bundle.get("sha256"), str) or not HASH_RE.fullmatch(bundle["sha256"]):
            failures.append("失败：build-report.json 的 bundle 缺少有效 sha256")
    copied = report.get("复制文件")
    if copied is not None and (
        not isinstance(copied, list) or any(not isinstance(item, str) for item in copied)
    ):
        failures.append("失败：build-report.json 的复制文件必须是路径字符串清单")
    return failures


def check_site(dist: Path) -> list[str]:
    """返回逐条检查结果；空列表表示通过。"""
    failures: list[str] = []
    if not dist.is_dir():
        failures.append(f"失败：站点路径不存在或不是目录：{dist}")
        return failures

    paths: dict[str, Path] = {}
    for relative in REQUIRED_FILES:
        path = dist / relative
        paths[relative] = path
        if not path.is_file():
            failures.append(f"失败：{relative} 不存在")
        elif path.stat().st_size == 0:
            failures.append(f"失败：{relative} 为空")
        else:
            print(f"通过：{relative} 存在且非空")

    data_path = paths["data/diagnosis.json"]
    if data_path.is_file() and data_path.stat().st_size:
        try:
            sys.path.insert(0, str(ROOT))
            import validate

            data = validate.load_data(data_path)
            errors = validate.Validator(validate.load_schema(validate.SCHEMA_PATH)).errors(data)
            if errors:
                failures.append(f"失败：data/diagnosis.json 未通过 validate.py（{len(errors)} 处问题）")
            else:
                print("通过：data/diagnosis.json 通过 validate.py")

            if isinstance(data, dict):
                for section in DIAGNOSIS_CORE_SECTIONS:
                    value = data.get(section)
                    if isinstance(value, (list, dict)) and len(value) == 0:
                        failures.append(f"失败：data/diagnosis.json 存在空章节（{section} 为空）")
        except (OSError, ValueError) as cause:
            failures.append(f"失败：data/diagnosis.json 无法通过 validate.py（{cause}）")

    for relative in ("index.html", "styles.css"):
        path = paths[relative]
        if path.is_file() and path.stat().st_size:
            text = path.read_text(encoding="utf-8", errors="replace")
            if EXTERNAL_URL_RE.search(text):
                failures.append(f"失败：{relative} 引用了外部 http(s) 资源")
            else:
                print(f"通过：{relative} 未引用外部 http(s) 资源")

    index_path = paths["index.html"]
    if index_path.is_file() and index_path.stat().st_size:
        html_text = index_path.read_text(encoding="utf-8", errors="replace")
        parser = LocalReferences()
        parser.feed(html_text)
        anchors = AnchorIds()
        anchors.feed(html_text)
        in_page_links = InPageLinks()
        in_page_links.feed(html_text)
        for reference in in_page_links.references:
            parsed = urlsplit(reference)
            if not parsed.path and parsed.fragment and unquote(parsed.fragment) not in anchors.ids:
                failures.append(
                    f"失败：index.html 页内锚点 #{parsed.fragment} 不存在 [broken_anchor]"
                )
        for reference in parser.references:
            # urlsplit 剥离 query 与 fragment，分别校验本地文件和页内锚点。
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc or reference.startswith("/"):
                continue
            if not parsed.path:
                continue
            target = index_path.parent / unquote(parsed.path)
            if not target.is_file():
                failures.append(
                    f"失败：index.html 引用的本地文件 {reference} 不存在 [missing_file]"
                )
            else:
                print(f"通过：index.html 引用的本地文件 {parsed.path} 存在")

        section_checker = SectionChecker()
        section_checker.feed(html_text)
        for empty_section in section_checker.empty_sections:
            failures.append(f"失败：index.html 存在空章节（{empty_section} 为空）")

    report_path = paths["build-report.json"]
    if report_path.is_file() and report_path.stat().st_size:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            failures.extend(validate_report_metadata(report))
            hashes = report_hashes(report)
            copied = report.get("复制文件") if isinstance(report, dict) else None
            if isinstance(copied, list) and set(copied) != set(hashes):
                failures.append("失败：build-report.json 的复制文件清单与文件摘要清单不一致")
            registered = set(hashes)
            actual_files: set[str] = set()
            for path in sorted(dist.rglob("*")):
                if path.is_file() and path != report_path:
                    relative = path.relative_to(dist).as_posix()
                    actual_files.add(relative)
                    if path.stat().st_size and relative not in registered:
                        failures.append(
                            f"失败：{relative} 未在 build-report 中登记；"
                            "修复：将该非空站点产物加入文件清单并记录 sha256，或移除该文件"
                        )
            for relative in sorted(registered - actual_files):
                failures.append(
                    f"失败：{relative} 已在 build-report 中登记但文件不存在；"
                    "修复：重新构建站点并更新文件清单，或移除该登记"
                )
            for relative in REQUIRED_FILES[:-1]:
                expected = hashes.get(relative)
                if not isinstance(expected, str) or not HASH_RE.fullmatch(expected):
                    failures.append(f"失败：build-report.json 缺少 {relative} 的有效 sha256")
                elif paths[relative].is_file():
                    if expected != sha256(paths[relative]):
                        failures.append(f"失败：{relative} 的 sha256 与 build-report.json 不一致")
                    else:
                        print(f"通过：{relative} 的 sha256 一致")
            bundle = report.get("bundle") if isinstance(report, dict) else None
            if isinstance(bundle, dict) and paths["app.bundle.js"].is_file():
                if bundle.get("sha256") == sha256(paths["app.bundle.js"]):
                    print("通过：bundle 的 sha256 一致")
                else:
                    failures.append("失败：app.bundle.js 的 sha256 与 build-report.json 的 bundle 摘要不一致")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as cause:
            failures.append(f"失败：无法读取 build-report.json 中的 sha256（{cause}）")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查组织诊断静态站点产物")
    parser.add_argument("dist", type=Path, help="静态站点目录")
    args = parser.parse_args(argv)
    failures = check_site(args.dist)
    for failure in failures:
        print(failure)
    if failures:
        print(f"站点检查失败：{len(failures)} 项")
        return 1
    print("站点检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
