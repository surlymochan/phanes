#!/usr/bin/env python3
"""make_brief.py 的单元测试。

覆盖：生成结果的结构与字段齐全、schema版本 为 4、口径与合成示例一致、
禁止事项含必需条目，以及 --goal/--month 覆盖与命令行的退出码。
另含任务书缺少必填字段（如“目标”）时的错误提示：退出码非 0、错误信息
指出字段名且不打印 traceback。

观察期间的期次顺序、重复期次与起止倒置按当前行为断言：已支持的情形直接断言。
另含观察月份的命令行回归：00 月、13 月与非法日期格式都应退出码非 0、
stderr 指出观察月份或 YYYY-MM 约束，且失败时不留下伪造任务书。

非空诊断样本违约（缺少必填区块、错误类型字段）时 make_brief 非零退出并指出
JSON pointer 与字段名；合法样本仍写出通过 schema 的 brief，禁止只测空文件。

另含输出文件一致性的命令行回归：--out 先写同目录临时文件再替换目标文件，
父目录不存在/不可写、目标是目录时返回稳定错误且不损坏已有任务书；
同样输入多次生成字节稳定；--check 的错误路径仍逐项指出 JSON 路径。
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
EX = ROOT.parent
if str(EX) not in sys.path:
    sys.path.insert(0, str(EX))

from boss import make_brief  # noqa: E402
from validate import Validator, load_schema  # noqa: E402

EXAMPLE_PATH = EX / "data" / "diagnosis.json"
SCHEMA_PATH = ROOT / "brief.schema.json"

BRIEF_KEYS = {"period", "summary", "目标", "观察期间", "口径", "必答问题", "数据需求", "禁止事项", "schema版本"}
ORDERED_BRIEF_KEYS = ["period", "summary", "目标", "观察期间", "口径", "必答问题", "数据需求", "禁止事项", "schema版本"]
REQUIRED_FORBIDS = ("不得写入真实组织数据", "不作个人/薪酬/编制判断")

# 期次锚点的期望顺序：2026-01实际 → 2026-05H1参考 → 2026-09实际 → 年末测算 → 年末目标。
ORDERED_PERIODS = ["2026-01实际", "2026-05H1参考", "2026-09实际", "年末测算", "年末目标"]
# 期次乱序：参考基准排到最前，实际期次随后。
SCRAMBLED_PERIODS = ["2026-05H1参考", "2026-01实际", "2026-09实际", "年末测算", "年末目标"]
# 重复期次：2026-01实际 出现两次。
DUPLICATED_PERIODS = ["2026-01实际", "2026-05H1参考", "2026-01实际", "年末测算", "年末目标"]


def load_json(path: Path):
    """读取 JSON 文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


def brief_with_period(keys: list[str], anchors: dict | None = None) -> dict:
    """生成任务书并改写期次与起止锚点。

    说明与口径里的期次表述同步改写，保证任务书里只剩下待检的那一处缺陷。
    """
    brief = make_brief.build_brief()
    period = brief["观察期间"]
    month = period["观察月份"]
    period["期次"] = list(keys)
    period["说明"] = (
        f"观察月份 {month}，数据截止 {period['数据截止']}；"
        f"期次锚点 {'、'.join(keys)}，与站点数值序列的键名一致。"
    )
    brief["口径"] = make_brief.build_definitions(list(keys), month)
    if anchors:
        period.update(anchors)
    return brief


class MakeBriefTest(unittest.TestCase):
    """任务书生成的结构与字段检查。"""

    def setUp(self) -> None:
        self.schema = load_json(SCHEMA_PATH)
        self.validator = Validator(self.schema)
        self.example = load_json(EXAMPLE_PATH)
        self.brief = make_brief.build_brief()

    def errors(self, brief: dict) -> list[str]:
        """返回结构错误列表。"""
        return make_brief.validate_brief(brief)

    def test_schema_uses_draft_2020_12(self) -> None:
        """结构定义声明 JSON Schema draft 2020-12。"""
        self.assertEqual(self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_brief_has_all_fields(self) -> None:
        """任务书字段齐全，且没有多余字段。"""
        self.assertEqual(set(self.brief), BRIEF_KEYS)
        self.assertRegex(self.brief["period"], r"^[0-9]{4}-(0[1-9]|1[0-2])$")
        self.assertIsInstance(self.brief["summary"], str)
        self.assertTrue(self.brief["summary"].strip())

    def test_schema_requires_period_and_summary(self) -> None:
        """schema 明确要求 period 与 summary。"""
        self.assertIn("period", self.schema["required"])
        self.assertIn("summary", self.schema["required"])

    def test_顶层字段序列化顺序稳定(self) -> None:
        """任务书 JSON 序列化为文本后顶层字段按 schema/实现稳定顺序输出且无漂移。"""
        # 使用仓库内示例输入（scope.观察月份）生成任务书
        observed_month = self.example["scope"]["观察月份"]
        expected_keys = list(self.schema["properties"].keys())
        self.assertEqual(expected_keys, ORDERED_BRIEF_KEYS)

        brief = make_brief.build_brief(month=observed_month)
        self.assertEqual(list(brief.keys()), expected_keys)

        # 序列化为 JSON 文本并断言顶层字段按稳定顺序输出
        serialized = json.dumps(brief, ensure_ascii=False, indent=2)
        top_level_matches = re.findall(r'^[ ]{2}"([^"]+)":', serialized, re.MULTILINE)
        self.assertEqual(top_level_matches, expected_keys)

        parsed_pairs = json.loads(serialized, object_pairs_hook=list)
        self.assertEqual([key for key, _ in parsed_pairs], expected_keys)

        positions = [serialized.index(f'"{k}"') for k in expected_keys]
        self.assertEqual(positions, sorted(positions))

        # 通过 write_brief 写出文件后验证文本字段顺序与多次重复无漂移
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            make_brief.write_brief(brief, path)
            written_text = path.read_text(encoding="utf-8")
            written_keys = re.findall(r'^[ ]{2}"([^"]+)":', written_text, re.MULTILINE)
            self.assertEqual(written_keys, expected_keys)

            for _ in range(5):
                repeated = make_brief.build_brief(month=observed_month)
                self.assertEqual(
                    json.dumps(repeated, ensure_ascii=False, indent=2) + "\n",
                    written_text,
                )

    def test_schema_version_is_four(self) -> None:
        """schema版本 固定为 4。"""
        self.assertEqual(self.brief["schema版本"], 4)
        self.assertEqual(self.brief["schema版本"], self.example["scope"]["schema版本"])

    def test_brief_passes_schema(self) -> None:
        """生成结果符合 brief.schema.json。"""
        self.assertEqual(self.errors(self.brief), [])

    def test_观察期间_matches_example(self) -> None:
        """默认观察期间与合成示例的 scope 对齐。"""
        scope = self.example["scope"]
        period = self.brief["观察期间"]
        self.assertEqual(period["观察月份"], scope["观察月份"])
        self.assertEqual(period["数据截止"], scope["数据截止"])
        self.assertEqual(period["观察月份"], "2026-09")
        self.assertEqual(period["数据截止"], "2026-09-30")

    def test_期次_matches_example_series(self) -> None:
        """期次锚点与合成示例的数值序列键名一致，且 boss/make_brief.py 产出的期次都能在 diagnosis periods 中找到。"""
        brief_periods = self.brief["观察期间"]["期次"]
        diagnosis_periods = make_brief.extract_diagnosis_periods(self.example)
        for period in brief_periods:
            self.assertIn(
                period,
                diagnosis_periods,
                f"boss/make_brief.py 产出的期次字段 {period} 必须能在 diagnosis periods 中找到",
            )
        for period in diagnosis_periods:
            self.assertIn(
                period,
                brief_periods,
                f"diagnosis period {period} 必须在任务书期次中",
            )
        self.assertEqual(set(brief_periods), diagnosis_periods)

    def test_口径_covers_metric_titles(self) -> None:
        """口径与示例 metrics 的标题、顺序一一对应。"""
        titles = [metric["标题"] for metric in self.example["metrics"]]
        self.assertEqual([key for key in self.brief["口径"] if key != "汇率"], titles)
        self.assertEqual(list(self.brief["口径"]), list(make_brief.METRIC_TITLES))

    def test_口径_states_units_and_period(self) -> None:
        """每个口径都写明单位与期间定义。"""
        for title, item in self.brief["口径"].items():
            with self.subTest(title=title):
                self.assertTrue(item["单位"].strip())
                self.assertTrue(item["定义"].strip())
                self.assertTrue(item["期间"].strip())
                self.assertTrue(item["缺失处理"].strip())
        units = {title: item["单位"] for title, item in self.brief["口径"].items()}
        self.assertIn("万元/月", units["整体技术人力成本"])
        self.assertIn("万美元/月", units["收入与成本率"])

    def test_禁止事项_contains_required_items(self) -> None:
        """禁止事项至少包含不得写入真实组织数据与不作个人/薪酬/编制判断。"""
        items = self.brief["禁止事项"]
        self.assertGreaterEqual(len(items), 2)
        joined = "\n".join(items)
        for phrase in REQUIRED_FORBIDS:
            self.assertIn(phrase, joined)

    def test_必答问题_and_数据需求_not_empty(self) -> None:
        """必答问题与数据需求都有内容，且数据需求指向已知指标。"""
        self.assertTrue(self.brief["必答问题"])
        self.assertTrue(self.brief["数据需求"])
        titles = set(make_brief.METRIC_TITLES)
        for need in self.brief["数据需求"]:
            self.assertTrue(need["字段路径"].strip())
            self.assertTrue(need["单位"].strip())
            self.assertTrue(need["口径"].strip())
            self.assertIn(need["对应指标"], titles)

    def test_goal_override(self) -> None:
        """--goal 覆盖“目标”。"""
        brief = make_brief.build_brief(goals=["只回答成本与人头"])
        self.assertEqual(brief["目标"], ["只回答成本与人头"])
        self.assertEqual(self.errors(brief), [])

    def test_goal_string_is_rejected_without_character_split(self) -> None:
        """目标字符串不能被 list() 拆成字符数组。"""
        with self.assertRaisesRegex(ValueError, "目标必须是字符串数组"):
            make_brief.build_brief(goals="只回答成本与人头")  # type: ignore[arg-type]

    def test_month_override(self) -> None:
        """--month 覆盖观察期间，并推算数据截止与期次锚点。"""
        brief = make_brief.build_brief(month="2026-12")
        period = brief["观察期间"]
        self.assertEqual(period["观察月份"], "2026-12")
        self.assertEqual(period["数据截止"], "2026-12-31")
        self.assertIn("2026-12实际", period["期次"])
        self.assertIn("2026-01实际", period["期次"])
        self.assertEqual(self.errors(brief), [])

    def test_leap_year_month_end(self) -> None:
        """闰年二月的数据截止日为 29 日，截止期次锚点与观察月份一致。"""
        brief = make_brief.build_brief(month="2024-02")
        period = brief["观察期间"]
        self.assertEqual(period["观察月份"], "2024-02")
        self.assertEqual(period["数据截止"], "2024-02-29")
        self.assertIn(f"{period['观察月份']}实际", period["期次"])
        self.assertEqual(self.errors(brief), [])

    def test_optional_fx_and_period_anchors(self) -> None:
        """可选汇率和期间锚点符合 DA 契约。"""
        brief = make_brief.build_brief(fx=7.2)
        self.assertEqual(brief["口径"]["汇率"], 7.2)
        self.assertEqual(self.errors(brief), [])
        period = brief["观察期间"]
        period.update({"起始期次": period["期次"][0], "参考期次": period["期次"][1], "截止期次": period["期次"][2]})
        self.assertEqual(self.errors(brief), [])
        period["参考期次"] = "不存在"
        self.assertTrue(any("参考期次" in error for error in self.errors(brief)))

    def test_invalid_fx_rejected(self) -> None:
        """汇率必须是正数。"""
        for fx in (0, -1):
            with self.subTest(fx=fx), self.assertRaises(ValueError):
                make_brief.build_brief(fx=fx)

    def test_invalid_month_rejected(self) -> None:
        """非法观察月份被拒绝。"""
        for month in ("2026-00", "2026-13", "2026-1", "26-01", ""):
            with self.subTest(month=month):
                with self.assertRaises(ValueError):
                    make_brief.build_brief(month=month)

    def test_command_line_writes_brief(self) -> None:
        """命令行生成任务书并写出文件。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            code = make_brief.main(
                ["--out", str(path), "--goal", "复核成本率", "--month", "2026-11", "--fx", "7.2"]
            )
            self.assertEqual(code, 0)
            written = load_json(path)
        self.assertEqual(written["目标"], ["复核成本率"])
        self.assertEqual(written["观察期间"]["观察月份"], "2026-11")
        self.assertEqual(written["口径"]["汇率"], 7.2)
        self.assertEqual(self.errors(written), [])

    def test_command_line_rejects_invalid_month(self) -> None:
        """非法观察月份时退出码非 0，stderr 指出观察月份约束，且不写文件。"""
        for month in ("2026-00", "2026-13"):
            with self.subTest(month=month), temporary_directory() as folder:
                path = Path(folder) / "brief.json"
                error = StringIO()
                with redirect_stderr(error):
                    code = make_brief.main(["--out", str(path), "--month", month])
                self.assertNotEqual(code, 0)
                self.assertIn("观察月份", error.getvalue())
                self.assertIn("YYYY-MM", error.getvalue())
                self.assertFalse(path.exists())

    def test_command_line_requires_out(self) -> None:
        """缺少 --out 时命令行报错退出。"""
        with self.assertRaises(SystemExit):
            make_brief.main([])

    def test_script_runs_as_module_path(self) -> None:
        """脚本可以直接运行，产物符合结构定义。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--out", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            written = load_json(path)
        self.assertEqual(self.errors(written), [])

    def test_cli_help_describes_options(self) -> None:
        """--help 成功退出，并完整展示 CLI 选项及其说明。"""
        result = subprocess.run(
            [sys.executable, str(ROOT / "make_brief.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--out", "--check", "--goal", "--month", "--fx"):
            with self.subTest(option=option):
                self.assertIn(option, result.stdout)
        for description in (
            "任务书输出路径。",
            "校验已有任务书路径。",
            "覆盖“目标”",
            "格式 YYYY-MM",
            "汇率，人民币/美元",
        ):
            with self.subTest(description=description):
                self.assertIn(description, result.stdout)

    def test_cli_requires_out_or_check(self) -> None:
        """未指定 --out 或 --check 时进程失败并提示必填参数。"""
        result = subprocess.run(
            [sys.executable, str(ROOT / "make_brief.py")],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("必须指定 --out 或 --check", result.stdout + result.stderr)

    def test_cli_rejects_invalid_month_format(self) -> None:
        """--month 非法格式时以状态码 2 退出并提示 YYYY-MM。"""
        for month in ("2026-9", "2026-13"):
            with self.subTest(month=month), temporary_directory() as folder:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "make_brief.py"),
                        "--out",
                        str(Path(folder) / "brief.json"),
                        "--month",
                        month,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("必须是 YYYY-MM", result.stdout + result.stderr)

    def test_check_accepts_existing_brief(self) -> None:
        """--check 校验符合 schema 的已有任务书。"""
        output = StringIO()
        with redirect_stdout(output):
            code = make_brief.main(["--check", str(EX / "demo" / "brief.json")])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "任务书校验通过\n")

    def test_check_reports_required_field_error(self) -> None:
        """--check 逐条报告缺少必需字段。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            brief = make_brief.build_brief()
            del brief["目标"]
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = make_brief.main(["--check", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("缺少必需字段", output.getvalue())

    def test_check_reports_type_error(self) -> None:
        """--check 逐条报告字段类型错误。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            brief = make_brief.build_brief()
            brief["schema版本"] = "4"
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = make_brief.main(["--check", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("期望值 4", output.getvalue())

    def test_check_rejects_non_object_top_level(self) -> None:
        """--check 校验顶层不是 JSON 对象的任务书时报错，不抛 AttributeError。"""
        cases = {"数组": "[]", "字符串": '"只是字符串"'}
        for name, content in cases.items():
            with self.subTest(顶层=name), temporary_directory() as folder:
                path = Path(folder) / "brief.json"
                path.write_text(content, encoding="utf-8")
                output, error = StringIO(), StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    code = make_brief.main(["--check", str(path)])
                self.assertEqual(code, 1)
                self.assertIn("任务书必须是 JSON 对象", output.getvalue() + error.getvalue())
                self.assertNotIn("Traceback", output.getvalue())
                self.assertNotIn("Traceback", error.getvalue())

    def test_check_non_object_top_level_exits_one_without_traceback(self) -> None:
        """命令行 --check 顶层非对象时退出码为 1，且不打印 traceback。"""
        cases = {"数组": "[]", "字符串": '"只是字符串"'}
        for name, content in cases.items():
            with self.subTest(顶层=name), temporary_directory() as folder:
                path = Path(folder) / "brief.json"
                path.write_text(content, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("任务书必须是 JSON 对象", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_check_missing_file_exits_one_without_traceback(self) -> None:
        """命令行 --check 文件不存在时退出码为 1，且不打印 traceback。"""
        with temporary_directory() as folder:
            path = Path(folder) / "missing-brief.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("无法读取任务书", output)
        self.assertIn(str(path), output)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class MonthBoundaryCommandLineTest(unittest.TestCase):
    """观察月份的命令行回归：期次边界越界与日期格式非法。

    要求：退出码非 0、stderr 指出观察月份或 YYYY-MM 约束，
    并且失败时不留下伪造任务书；合法月份与闰年二月的既有行为保持不变。
    """

    # 月份越界：00 月与 13 月都不在 01-12 之间。
    OUT_OF_RANGE_MONTHS = ("2026-00", "2026-13")
    # 格式非法：位数不对、分隔符不对、多带日期、全角数字、结尾带换行。
    INVALID_MONTH_FORMATS = (
        "2026-1",
        "26-01",
        "2026/09",
        "2026-09-01",
        "202609",
        "2026-1a",
        "",
        "2026-01\n",
        "２０２６-09",
    )

    def run_cli(self, month: str, folder: str) -> tuple[subprocess.CompletedProcess, Path]:
        """在指定目录里跑一次带 --month 的命令行，返回进程结果与输出路径。"""
        path = Path(folder) / "brief.json"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "make_brief.py"),
                "--out",
                str(path),
                "--month",
                month,
            ],
            capture_output=True,
            text=True,
        )
        return result, path

    def assert_rejected(self, month: str, folder: str) -> None:
        """断言一次非法月份的命令行调用被拒绝，且没有留下伪造任务书。"""
        result, path = self.run_cli(month, folder)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("观察月份", result.stderr)
        self.assertIn("YYYY-MM", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(list(Path(folder).iterdir()), [], "失败时留下了伪造任务书")

    def test_cli_rejects_out_of_range_month(self) -> None:
        """00 月与 13 月越界时退出码非 0，stderr 指出观察月份与 YYYY-MM 约束。"""
        for month in self.OUT_OF_RANGE_MONTHS:
            with self.subTest(month=month), temporary_directory() as folder:
                self.assert_rejected(month, folder)

    def test_cli_rejects_invalid_month_format(self) -> None:
        """非法日期格式时退出码非 0，stderr 指出观察月份与 YYYY-MM 约束。"""
        for month in self.INVALID_MONTH_FORMATS:
            with self.subTest(month=month), temporary_directory() as folder:
                self.assert_rejected(month, folder)

    def test_failed_run_leaves_no_brief(self) -> None:
        """失败时不留下伪造任务书：输出目录里不产生任何文件。"""
        for month in self.OUT_OF_RANGE_MONTHS + self.INVALID_MONTH_FORMATS:
            with self.subTest(month=month), temporary_directory() as folder:
                result, path = self.run_cli(month, folder)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(path.exists())
                self.assertEqual(list(Path(folder).iterdir()), [], "失败时留下了伪造任务书")

    def test_valid_month_and_leap_year_still_write_brief(self) -> None:
        """合法月份与闰年二月的既有行为不变：退出码 0 且写出符合结构的任务书。"""
        cases = {"2026-01": "2026-01-31", "2026-09": "2026-09-30", "2024-02": "2024-02-29"}
        for month, last_day in cases.items():
            with self.subTest(month=month), temporary_directory() as folder:
                result, path = self.run_cli(month, folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(path.exists())
                written = load_json(path)
                self.assertEqual(written["观察期间"]["观察月份"], month)
                self.assertEqual(written["观察期间"]["数据截止"], last_day)
                self.assertEqual(make_brief.validate_brief(written), [])


class MissingRequiredFieldTest(unittest.TestCase):
    """任务书缺少必填字段（如“目标”）时的错误提示。"""

    def brief_without_goal(self) -> dict:
        """返回缺“目标”的任务书：其余字段保持生成器的默认结果。"""
        brief = make_brief.build_brief()
        del brief["目标"]
        return brief

    def test_check_missing_goal_exits_nonzero_and_names_field(self) -> None:
        """--check 校验缺“目标”的任务书时退出码非 0，错误信息指出字段名且无 traceback。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            path.write_text(
                json.dumps(self.brief_without_goal(), ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                capture_output=True,
                text=True,
            )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("目标", output)
        self.assertIn("缺少必需字段", output)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_check_missing_goal_names_field_on_stderr(self) -> None:
        """--check 缺“目标”时错误信息写在 stderr 上，指出字段名且不打印 traceback。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            path.write_text(
                json.dumps(self.brief_without_goal(), ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("目标", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_out_missing_goal_reports_field_on_stderr(self) -> None:
        """生成的任务书缺“目标”时退出码为 1，stderr 指出字段名且无 traceback。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            output = StringIO()
            with patch.object(
                make_brief, "build_brief", return_value=self.brief_without_goal()
            ), redirect_stderr(output):
                code = make_brief.main(["--out", str(path)])
        self.assertEqual(code, make_brief.EXIT_INVALID)
        self.assertIn("目标", output.getvalue())
        self.assertIn("缺少必需字段", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertFalse(path.exists())


class PeriodOrderTest(unittest.TestCase):
    """观察期间的期次顺序、重复期次与起止倒置。"""

    def errors(self, brief: dict) -> list[str]:
        """返回结构错误列表。"""
        return make_brief.validate_brief(brief)

    def test_生成期次按顺序且不重复(self) -> None:
        """生成侧：期次锚点按时间顺序且不重复。"""
        brief = make_brief.build_brief()
        keys = brief["观察期间"]["期次"]
        self.assertEqual(keys, ORDERED_PERIODS)
        self.assertEqual(len(set(keys)), len(keys))
        # 观察月份与首个锚点同月时（2026-01），period_keys 去重后只剩四个锚点。
        self.assertEqual(
            make_brief.period_keys("2026-01"),
            ["2026-01实际", "2026-05H1参考", "年末测算", "年末目标"],
        )

    def test_生成任务书不写起止锚点(self) -> None:
        """生成侧：任务书不写起始/参考/截止期次，这三个字段只出现在人工任务书里。"""
        period = make_brief.build_brief()["观察期间"]
        for name in ("起始期次", "参考期次", "截止期次"):
            with self.subTest(field=name):
                self.assertNotIn(name, period)

    def test_当前不校验期次顺序重复与起止倒置(self) -> None:
        """期次乱序与起始晚于截止场景必须报出错误。"""
        cases = {
            "期次乱序": brief_with_period(SCRAMBLED_PERIODS),
            "重复期次": brief_with_period(DUPLICATED_PERIODS),
            "起始晚于截止": brief_with_period(
                ORDERED_PERIODS,
                {"起始期次": "2026-09实际", "参考期次": "2026-05H1参考", "截止期次": "2026-01实际"},
            ),
        }
        for name, brief in cases.items():
            with self.subTest(scenario=name):
                errors = self.errors(brief)
                self.assertNotEqual(errors, [])
                self.assertTrue(any("期次" in message for message in errors))

    def test_期次乱序_应报出错误(self) -> None:
        """期次锚点乱序时应报出顺序错误。

        已知 bug：validate_brief 只做 schema 校验与起止/参考期次的归属校验
        （boss/make_brief.py:214-223），不校验期次顺序；乱序任务书照样校验通过，
        DA 会按乱掉的顺序取数。实现补上顺序校验后，删掉 expectedFailure 标记。
        """
        errors = self.errors(brief_with_period(SCRAMBLED_PERIODS))
        self.assertTrue(any("期次" in message for message in errors), "期次乱序时未报出错误")

    def test_重复期次_应报出错误(self) -> None:
        """期次锚点重复时应报出重复错误。

        重复锚点会让同一期次被取两次数或被静默覆盖，validate_brief 报出
        /观察期间/期次: 包含重复期次。
        """
        errors = self.errors(brief_with_period(DUPLICATED_PERIODS))
        self.assertTrue(
            any("重复" in message and "期次" in message for message in errors),
            "重复期次时未报出指向重复期次的错误",
        )

    def test_命令行拒绝重复期次且不写出任务书(self) -> None:
        """生成结果含重复期次时命令行应报错，且不写出有效任务书。

        main 只在校验通过时写出任务书，重复期次报出错误后不写文件。
        """
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            duplicate = brief_with_period(DUPLICATED_PERIODS)
            output = StringIO()
            with patch.object(make_brief, "build_brief", return_value=duplicate), redirect_stderr(output):
                code = make_brief.main(["--out", str(path)])
        self.assertNotEqual(code, 0)
        self.assertFalse(path.exists())
        self.assertIn("重复", output.getvalue())
        self.assertIn("期次", output.getvalue())

    def test_起始晚于截止_应报出错误(self) -> None:
        """起始期次晚于截止期次时应报出错误。

        已知 bug：validate_brief 只检查起止/参考期次是否在期次列表里
        （boss/make_brief.py:220-222），不比较三者的先后；起止倒置的任务书照样通过，
        DA 会把观察窗口算反。实现补上先后比较后，删掉 expectedFailure 标记。
        """
        errors = self.errors(
            brief_with_period(
                ORDERED_PERIODS,
                {"起始期次": "2026-09实际", "参考期次": "2026-05H1参考", "截止期次": "2026-01实际"},
            )
        )
        self.assertTrue(any("期次" in message for message in errors), "起始晚于截止时未报出错误")

    def test_check_期次乱序_退出码非零(self) -> None:
        """--check 校验乱序期次的任务书时退出码应非 0。

        已知 bug：与 test_期次乱序_应报出错误 同源，--check 只看校验错误列表，
        乱序期次当前仍打印“任务书校验通过”并返回 0。修复后删掉 expectedFailure 标记。
        """
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            brief = brief_with_period(SCRAMBLED_PERIODS)
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = make_brief.main(["--check", str(path)])
        self.assertEqual(code, 1)

    def test_期次为空数组_应报出错误(self) -> None:
        """期次列表为空数组时校验应失败并指出期次错误。"""
        brief = make_brief.build_brief()
        brief["观察期间"]["期次"] = []
        errors = self.errors(brief)
        self.assertTrue(errors, "期次为空数组时未报出错误")
        self.assertTrue(
            any("期次" in message for message in errors),
            "期次为空数组时未报出指向期次的错误",
        )

    def test_命令行拒绝期次为空数组且stderr指出期次(self) -> None:
        """生成结果期次列表为空数组时命令行应报错，stderr 指出期次且不写出任务书。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            empty = make_brief.build_brief()
            empty["观察期间"]["期次"] = []
            output = StringIO()
            with patch.object(make_brief, "build_brief", return_value=empty), redirect_stderr(output):
                code = make_brief.main(["--out", str(path)])
        self.assertNotEqual(code, 0)
        self.assertFalse(path.exists())
        self.assertIn("期次", output.getvalue())

    def test_check_期次为空数组_退出码非零(self) -> None:
        """--check 校验期次为空数组的任务书时退出码应非 0。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            brief = make_brief.build_brief()
            brief["观察期间"]["期次"] = []
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                code = make_brief.main(["--check", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("期次", output.getvalue())


class NonEmptyBriefSchemaViolationTest(unittest.TestCase):
    """非空诊断样本违约（缺少必填区块、错误类型字段）时的校验与命令行回归。

    覆盖：
    1. 缺少必填区块：顶层或嵌套缺少必填区块时，make_brief 非零退出（EXIT_INVALID=1），
       并在错误输出中明确指出 JSON pointer 与字段名；
    2. 错误类型字段：字段取值类型不符合 brief.schema.json 约束时，make_brief 非零退出，
       并指出 JSON pointer 与字段名；
    3. 合法样本验证：合法非空样本仍能通过 schema 校验，且 --out 与 --check 正常输出/验证；
    4. 禁止只测空文件：所有样本均采用完整业务结构的非空诊断任务书。
    """

    def valid_sample(self) -> dict:
        """返回一个非空且结构完整的组织诊断任务书样本。"""
        return make_brief.build_brief(
            goals=["评估整体技术人力成本与人头变化", "复核营收业务成本匹配情况"],
            month="2026-09",
            fx=7.2,
        )

    def test_缺少必填区块_非零退出且指出路径与字段名(self) -> None:
        """非空样本缺少必填区块时，make_brief 以非 0 退出并指出 JSON pointer 与字段名。"""
        cases = [
            ("缺少 period", lambda b: b.pop("period"), "/period", "period"),
            ("缺少 summary", lambda b: b.pop("summary"), "/summary", "summary"),
            ("缺少顶层口径区块", lambda b: b.pop("口径"), "/口径", "口径"),
            ("缺少顶层观察期间区块", lambda b: b.pop("观察期间"), "/观察期间", "观察期间"),
            ("缺少顶层数据需求区块", lambda b: b.pop("数据需求"), "/数据需求", "数据需求"),
            ("缺少顶层必答问题区块", lambda b: b.pop("必答问题"), "/必答问题", "必答问题"),
            ("缺少顶层禁止事项区块", lambda b: b.pop("禁止事项"), "/禁止事项", "禁止事项"),
            (
                "缺少观察期间观察月份",
                lambda b: b["观察期间"].pop("观察月份"),
                "/观察期间/观察月份",
                "观察月份",
            ),
            (
                "缺少观察期间期次",
                lambda b: b["观察期间"].pop("期次"),
                "/观察期间/期次",
                "期次",
            ),
            (
                "缺少口径整体技术人力成本区块",
                lambda b: b["口径"].pop("整体技术人力成本"),
                "/口径/整体技术人力成本",
                "整体技术人力成本",
            ),
            (
                "缺少口径项单位字段",
                lambda b: b["口径"]["整体技术人力成本"].pop("单位"),
                "/口径/整体技术人力成本/单位",
                "单位",
            ),
            (
                "缺少数据需求字段路径",
                lambda b: b["数据需求"][0].pop("字段路径"),
                "/数据需求/0/字段路径",
                "字段路径",
            ),
        ]

        for desc, mutator, pointer, field_name in cases:
            with self.subTest(scenario=desc), temporary_directory() as folder:
                sample = copy.deepcopy(self.valid_sample())
                mutator(sample)
                # 禁止只测空文件：断言样本是非空的业务对象
                self.assertGreater(len(sample), 0)
                self.assertGreater(len(json.dumps(sample, ensure_ascii=False)), 200)

                # 1. 结构校验层面断言指出 JSON pointer 与字段名
                errors = make_brief.validate_brief(sample)
                self.assertTrue(errors, f"{desc} 未报出校验错误")
                self.assertTrue(
                    any(pointer in err and "缺少必需字段" in err for err in errors),
                    f"{desc} 校验错误中未包含预期的 JSON pointer {pointer}: {errors}",
                )

                # 2. main(["--check", path]) 函数入口返回 EXIT_INVALID
                path = Path(folder) / "brief.json"
                path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
                output = StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    code = make_brief.main(["--check", str(path)])
                self.assertEqual(code, make_brief.EXIT_INVALID)
                self.assertIn(pointer, output.getvalue())
                self.assertIn(field_name, output.getvalue())
                self.assertIn("缺少必需字段", output.getvalue())

                # 3. 命令行进程入口非零退出且无 traceback
                result = subprocess.run(
                    [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                    capture_output=True,
                    text=True,
                )
                output_text = result.stdout + result.stderr
                self.assertEqual(result.returncode, make_brief.EXIT_INVALID, output_text)
                self.assertIn(pointer, output_text)
                self.assertIn(field_name, output_text)
                self.assertIn("缺少必需字段", output_text)
                self.assertNotIn("Traceback", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_period与summary内容非法时非零退出(self) -> None:
        """非空样本的 period 必须为 YYYY-MM，summary 必须为非空字符串。"""
        cases = [
            ("period 为空", "period", "", "/period"),
            ("period 月份非法", "period", "2026-13", "/period"),
            ("summary 为空", "summary", "", "/summary"),
        ]
        for desc, field, value, pointer in cases:
            with self.subTest(scenario=desc):
                sample = copy.deepcopy(self.valid_sample())
                sample[field] = value
                self.assertGreater(len(json.dumps(sample, ensure_ascii=False)), 200)
                errors = make_brief.validate_brief(sample)
                self.assertTrue(errors, f"{desc} 未报出校验错误")
                self.assertTrue(any(pointer in error for error in errors), errors)

    def test_错误类型字段_非零退出且指出路径与字段名(self) -> None:
        """非空样本包含错误类型字段时，make_brief 以非 0 退出并指出 JSON pointer 与字段名。"""
        cases = [
            (
                "schema版本为字符串",
                lambda b: b.update({"schema版本": "4"}),
                "/schema版本",
                "schema版本",
                "期望值 4",
            ),
            (
                "schema版本为错误数字",
                lambda b: b.update({"schema版本": 99}),
                "/schema版本",
                "schema版本",
                "期望值 4",
            ),
            (
                "观察期间观察月份为整数类型",
                lambda b: b["观察期间"].update({"观察月份": 202609}),
                "/观察期间/观察月份",
                "观察月份",
                "期望类型 string",
            ),
            (
                "目标为字符串而非数组",
                lambda b: b.update({"目标": "整体技术人力成本评估目标"}),
                "/目标",
                "目标",
                "期望类型 array",
            ),
            (
                "期次为整数而非数组",
                lambda b: b["观察期间"].update({"期次": 12345}),
                "/观察期间/期次",
                "期次",
                "期望类型 array",
            ),
            (
                "观察期间为字符串而非对象",
                lambda b: b.update({"观察期间": "2026-09"}),
                "/观察期间",
                "观察期间",
                "期望类型 object",
            ),
            (
                "必答问题为字符串而非数组",
                lambda b: b.update({"必答问题": "只回答成本"}),
                "/必答问题",
                "必答问题",
                "期望类型 array",
            ),
            (
                "口径项单位为整数类型",
                lambda b: b["口径"]["整体技术人力成本"].update({"单位": 1000}),
                "/口径/整体技术人力成本/单位",
                "单位",
                "期望类型 string",
            ),
            (
                "汇率为字符串类型",
                lambda b: b["口径"].update({"汇率": "7.2"}),
                "/口径/汇率",
                "汇率",
                "期望类型 number",
            ),
            (
                "数据需求对应指标不在枚举中",
                lambda b: b["数据需求"][0].update({"对应指标": "未定义的指标"}),
                "/数据需求/0/对应指标",
                "对应指标",
                "取值不在允许的列表内",
            ),
            (
                "禁止事项少于2条",
                lambda b: b.update({"禁止事项": ["只有一条禁止事项"]}),
                "/禁止事项",
                "禁止事项",
                "元素数量不足 2",
            ),
        ]

        for desc, mutator, pointer, field_name, expected_hint in cases:
            with self.subTest(scenario=desc), temporary_directory() as folder:
                sample = copy.deepcopy(self.valid_sample())
                mutator(sample)
                # 禁止只测空文件：断言样本非空
                self.assertGreater(len(sample), 0)
                self.assertGreater(len(json.dumps(sample, ensure_ascii=False)), 200)

                # 1. 结构校验层面断言指出 JSON pointer 与预期提示
                errors = make_brief.validate_brief(sample)
                self.assertTrue(errors, f"{desc} 未报出校验错误")
                self.assertTrue(
                    any(pointer in err and expected_hint in err for err in errors),
                    f"{desc} 校验错误中未包含预期 pointer {pointer} 或提示 {expected_hint}: {errors}",
                )

                # 2. main(["--check", path]) 返回 EXIT_INVALID
                path = Path(folder) / "brief.json"
                path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
                output = StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    code = make_brief.main(["--check", str(path)])
                self.assertEqual(code, make_brief.EXIT_INVALID)
                self.assertIn(pointer, output.getvalue())
                self.assertIn(expected_hint, output.getvalue())

                # 3. 命令行进程非 0 退出且无 traceback
                result = subprocess.run(
                    [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                    capture_output=True,
                    text=True,
                )
                output_text = result.stdout + result.stderr
                self.assertEqual(result.returncode, make_brief.EXIT_INVALID, output_text)
                self.assertIn(pointer, output_text)
                self.assertIn(expected_hint, output_text)
                self.assertNotIn("Traceback", result.stdout)
                self.assertNotIn("Traceback", result.stderr)

    def test_合法非空样本通过schema并写出brief(self) -> None:
        """合法非空样本顺利通过 schema 校验，且 --out 与 --check 正常输出与验证。"""
        sample = self.valid_sample()
        # 禁止只测空文件
        self.assertGreater(len(sample), 0)
        self.assertGreater(len(json.dumps(sample, ensure_ascii=False)), 500)
        self.assertEqual(make_brief.validate_brief(sample), [])

        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")

            # 校验合法样本退出码为 0
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("任务书校验通过", result.stdout)

            # 使用 --out 重新写出合法样本
            out_path = Path(folder) / "generated_brief.json"
            gen_result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "make_brief.py"),
                    "--out",
                    str(out_path),
                    "--goal",
                    "评估整体技术人力成本",
                    "--month",
                    "2026-09",
                    "--fx",
                    "7.2",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(gen_result.returncode, 0, gen_result.stdout + gen_result.stderr)
            self.assertTrue(out_path.exists())
            written = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertGreater(len(written), 0)
            self.assertEqual(make_brief.validate_brief(written), [])

    def test_禁止只测空文件(self) -> None:
        """空文件不是合法诊断任务书，会被拒绝；单测必须使用非空结构化样本。"""
        with temporary_directory() as folder:
            empty_path = Path(folder) / "empty.json"
            empty_path.write_text("", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--check", str(empty_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, make_brief.EXIT_INVALID)
            self.assertIn("不是合法 JSON", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)


class DiagnosisPeriodsAlignmentTest(unittest.TestCase):
    """期次锚点与 diagnosis periods 对齐及错期时的可读失败回归（含临时目录回归）。"""

    def setUp(self) -> None:
        self.example = load_json(EXAMPLE_PATH)

    def test_generated_brief_periods_align_with_diagnosis(self) -> None:
        """boss/make_brief.py 产出的期次字段全部能在 diagnosis periods 中找到。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--out", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            brief = load_json(path)

            brief_periods = brief["观察期间"]["期次"]
            diag_periods = make_brief.extract_diagnosis_periods(self.example)
            for period in brief_periods:
                self.assertIn(
                    period,
                    diag_periods,
                    f"boss/make_brief.py 产出的期次字段 {period} 必须能在 diagnosis periods 中找到",
                )
            errors = make_brief.check_periods_alignment(brief, self.example)
            self.assertEqual(errors, [])

    def test_错期时失败可读_任务书错期(self) -> None:
        """任务书期次与 diagnosis periods 错期时，报错信息清晰指出错期项及预期期次。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            # 制造任务书错期（如 2026-08实际 代替 2026-09实际）
            mismatched_brief = brief_with_period(
                ["2026-01实际", "2026-05H1参考", "2026-08实际", "年末测算", "年末目标"]
            )
            path.write_text(json.dumps(mismatched_brief, ensure_ascii=False), encoding="utf-8")
            loaded = load_json(path)

            errors = make_brief.check_periods_alignment(loaded, self.example)
            self.assertTrue(errors)
            error_message = "；".join(errors)
            self.assertIn("2026-08实际", error_message)
            self.assertIn("未在诊断期次", error_message)
            self.assertIn("2026-09实际", error_message)

    def test_错期时失败可读_诊断快照错期(self) -> None:
        """诊断快照期次错期时，报错信息清晰指出未在任务书期次中找到的项。"""
        with temporary_directory() as folder:
            brief = make_brief.build_brief()
            mismatched_diagnosis = copy.deepcopy(self.example)
            # 修改诊断快照的一个指标期次造成错期
            mismatched_diagnosis["metrics"][0]["数值"]["2026-99实际"] = 99
            del mismatched_diagnosis["metrics"][0]["数值"]["2026-09实际"]

            diag_path = Path(folder) / "diagnosis_mismatched.json"
            diag_path.write_text(json.dumps(mismatched_diagnosis, ensure_ascii=False), encoding="utf-8")
            loaded_diag = load_json(diag_path)

            errors = make_brief.check_periods_alignment(brief, loaded_diag)
            self.assertTrue(errors)
            error_message = "；".join(errors)
            self.assertIn("2026-99实际", error_message)
            self.assertIn("2026-09实际", error_message)

    def test_错期断言失败信息可读(self) -> None:
        """直接对期次列表断言时，若产生错期，assertIn 的错误信息指出具体期次字段。"""
        diag_periods = make_brief.extract_diagnosis_periods(self.example)
        mismatched_period = "2026-08实际"
        with self.assertRaises(AssertionError) as ctx:
            self.assertIn(
                mismatched_period,
                diag_periods,
                f"boss/make_brief.py 产出的期次字段 {mismatched_period} 必须能在 diagnosis periods 中找到",
            )
        self.assertIn(f"期次字段 {mismatched_period} 必须能在 diagnosis periods 中找到", str(ctx.exception))

    def test_cli_非空诊断夹具写出任务书(self) -> None:
        """非空 diagnosis periods 与默认任务书对齐时允许写出 brief。"""
        with temporary_directory() as folder:
            brief_path = Path(folder) / "brief.json"
            diagnosis_path = Path(folder) / "diagnosis.json"
            diagnosis_path.write_text(
                json.dumps(self.example, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "make_brief.py"),
                    "--out",
                    str(brief_path),
                    "--diagnosis",
                    str(diagnosis_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreater(brief_path.stat().st_size, 0)

    def test_cli_空诊断夹具非零且不写出任务书(self) -> None:
        """diagnosis metrics 为空时指出 periods 字段并拒绝写出空 brief。"""
        with temporary_directory() as folder:
            brief_path = Path(folder) / "brief.json"
            diagnosis_path = Path(folder) / "diagnosis-empty.json"
            empty_diagnosis = copy.deepcopy(self.example)
            # 保留非空 metrics，明确覆盖“诊断期次为空”的路径，而不是空文件/空 metrics。
            empty_diagnosis["metrics"] = [{"数值": {}}]
            diagnosis_path.write_text(
                json.dumps(empty_diagnosis, ensure_ascii=False), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "make_brief.py"),
                    "--out",
                    str(brief_path),
                    "--diagnosis",
                    str(diagnosis_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("periods", result.stderr)
            self.assertFalse(brief_path.exists())


class OutputFileConsistencyTest(unittest.TestCase):
    """--out 的输出文件一致性：先写临时文件再替换，失败时给出稳定错误。

    用临时目录执行真实 CLI（子进程），覆盖四条路径：
    1. 成功：写出合法非空任务书，同样输入多次生成字节稳定；
    2. 已有文件替换失败保护：父目录不存在/不可写、目标是目录时退出码非 0，
       已有任务书保持原样，且不残留临时文件；
    3. 检查通过：--check 校验刚生成的任务书退出码为 0；
    4. 非法非空 brief：--check 逐项指出 JSON 路径。
    """

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        """在真实进程里执行 make_brief.py，返回进程结果。"""
        return subprocess.run(
            [sys.executable, str(ROOT / "make_brief.py"), *args],
            capture_output=True,
            text=True,
        )

    def listdir(self, folder: Path) -> list[str]:
        """返回目录下的文件名（含隐藏文件），用于断言没有残留临时文件。"""
        return sorted(os.listdir(folder))

    def test_cli_成功路径_写出合法非空任务书(self) -> None:
        """--out 成功时写出合法非空任务书，目录里不残留临时文件。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            result = self.run_cli("--out", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("已写出任务书", result.stdout)
            self.assertTrue(path.exists())
            written = load_json(path)
            self.assertGreater(len(written), 0)
            self.assertEqual(make_brief.validate_brief(written), [])
            self.assertEqual(self.listdir(Path(folder)), ["brief.json"])

    def test_cli_同样输入多次生成字节稳定(self) -> None:
        """同样输入重复生成时字节一致：覆盖已有文件与另一个目录两种情形。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            first = self.run_cli("--out", str(path))
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            original = path.read_bytes()
            self.assertGreater(len(original), 0)

            second = self.run_cli("--out", str(path))
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(path.read_bytes(), original, "重复生成同一路径时字节漂移")

            copy_dir = Path(folder) / "copy"
            copy_dir.mkdir()
            copy_path = copy_dir / "brief.json"
            third = self.run_cli("--out", str(copy_path))
            self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
            self.assertEqual(copy_path.read_bytes(), original, "同样输入写到别的路径时字节漂移")

            # 输出里没有时间戳或随机名：与进程内序列化结果一致。
            self.assertEqual(
                original,
                make_brief.serialize_brief(make_brief.build_brief()).encode("utf-8"),
            )

    def test_cli_父目录不存在_稳定错误且不留下文件(self) -> None:
        """--out 的父目录不存在时退出码非 0、错误稳定，且不创建目录也不写文件。"""
        with temporary_directory() as folder:
            missing = Path(folder) / "missing"
            path = missing / "brief.json"
            result = self.run_cli("--out", str(path))
            self.assertEqual(
                result.returncode, make_brief.EXIT_USAGE, result.stdout + result.stderr
            )
            self.assertIn("无法写出", result.stderr)
            self.assertIn(str(path), result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(missing.exists(), "父目录不存在时不应被自动创建")
            self.assertEqual(self.listdir(Path(folder)), [])

    def test_cli_父目录不可写_稳定错误且不损坏已有任务书(self) -> None:
        """父目录不可写时退出码非 0，已有任务书保持原样且不残留临时文件。"""
        if os.geteuid() == 0:
            self.skipTest("root 会忽略目录权限，无法模拟不可写目录")
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            generated = self.run_cli("--out", str(path))
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            original = path.read_bytes()

            os.chmod(folder, 0o555)
            try:
                result = self.run_cli("--out", str(path))
            finally:
                os.chmod(folder, 0o755)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("无法写出", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(path.read_bytes(), original, "写失败时旧任务书被改动")
            self.assertEqual(self.listdir(Path(folder)), ["brief.json"])

    def test_cli_目标是目录_稳定错误且不损坏目录内容(self) -> None:
        """--out 指向已存在的目录时退出码非 0，目录及其内容保持原样。"""
        with temporary_directory() as folder:
            target = Path(folder) / "brief.json"
            target.mkdir()
            (target / "keep.txt").write_text("保留内容", encoding="utf-8")
            result = self.run_cli("--out", str(target))
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("无法写出", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertTrue(target.is_dir())
            self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "保留内容")
            self.assertEqual(self.listdir(Path(folder)), ["brief.json"])

    def test_替换失败时已有任务书保持原样(self) -> None:
        """替换失败时旧任务书不被截断或覆盖，临时文件也被清理。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            path.write_text("旧任务书\n", encoding="utf-8")
            original = path.read_bytes()
            error = StringIO()
            with patch("boss.make_brief.os.replace", side_effect=OSError("替换失败")), redirect_stderr(
                error
            ):
                code = make_brief.main(["--out", str(path)])
            self.assertEqual(code, make_brief.EXIT_USAGE)
            self.assertIn("无法写出", error.getvalue())
            self.assertEqual(path.read_bytes(), original, "替换失败时旧任务书被改动")
            self.assertEqual(self.listdir(Path(folder)), ["brief.json"])

    def test_写出先写同目录临时文件再替换(self) -> None:
        """写出走“同目录临时文件 + 替换”：替换发生在临时文件写完之后。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            recorded: dict = {}
            real_replace = os.replace

            def fake_replace(src, dst):
                recorded["src"] = Path(src)
                recorded["dst"] = Path(dst)
                recorded["temp_bytes"] = Path(src).read_bytes()
                return real_replace(src, dst)

            with patch("boss.make_brief.os.replace", side_effect=fake_replace):
                make_brief.write_brief(make_brief.build_brief(), path)

            self.assertEqual(recorded["dst"], path)
            self.assertNotEqual(recorded["src"], path, "替换源不是临时文件")
            self.assertEqual(recorded["src"].parent, path.parent, "临时文件必须与目标同目录")
            self.assertEqual(recorded["temp_bytes"], path.read_bytes())
            self.assertEqual(self.listdir(Path(folder)), ["brief.json"])

    def test_cli_检查通过_刚生成的任务书校验通过(self) -> None:
        """--check 校验刚生成的任务书时退出码为 0 并提示校验通过。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            generated = self.run_cli("--out", str(path))
            self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
            checked = self.run_cli("--check", str(path))
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("任务书校验通过", checked.stdout)

    def test_cli_非法非空任务书_逐项指出JSON路径(self) -> None:
        """--check 校验非法非空任务书时逐项指出 JSON 路径，退出码非 0。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            sample = make_brief.build_brief()
            del sample["口径"]
            sample["观察期间"]["期次"] = []
            sample["schema版本"] = "4"
            path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")

            result = self.run_cli("--check", str(path))
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, make_brief.EXIT_INVALID, output)
            self.assertIn("/口径", output)
            self.assertIn("/观察期间/期次", output)
            self.assertIn("/schema版本", output)
            self.assertNotIn("Traceback", result.stderr)


class PeriodsAlignmentTest(unittest.TestCase):
    """期次锚点与 diagnosis periods 对齐、多期次输入及缺期次的可读失败回归。"""

    def setUp(self) -> None:
        self.example = load_json(EXAMPLE_PATH)
        # 多期次输入：三个实际期次 + 一个 H1 参考基准 + 两个年末前瞻口径。
        self.periods = [
            "2026-01实际",
            "2026-03实际",
            "2026-05H1参考",
            "2026-09实际",
            "年末测算",
            "年末目标",
        ]

    def diagnosis_with_periods(self) -> dict:
        """在合成示例快照里补上 2026-03实际 期次，得到与多期次任务书对齐的快照。"""
        diagnosis = copy.deepcopy(self.example)
        for metric in diagnosis["metrics"][:2]:
            values = metric.get("数值")
            if isinstance(values, dict) and "2026-01实际" in values:
                values["2026-03实际"] = values["2026-01实际"]
        return diagnosis

    def test_multi_period_input_aligns_all_anchors_and_calibers(self) -> None:
        """多期次输入生成包含全部锚点的 brief，明确列出多个口径，且可被 da 和 review 消费。"""
        brief = make_brief.build_brief(periods=self.periods)
        period_obj = brief["观察期间"]

        # 包含全部锚点
        self.assertEqual(period_obj["期次"], self.periods)
        for anchor in self.periods:
            self.assertIn(anchor, period_obj["说明"])

        # 明确列出多个口径分类：已发生实际期次、参考基准、前瞻估算
        period_def = brief["口径"]["整体技术人力成本"]["期间"]
        self.assertIn("2026-01实际、2026-03实际、2026-09实际 为已发生实际期次", period_def)
        self.assertIn("2026-05H1参考 为 H1 冻结对照基准", period_def)
        self.assertIn("年末测算与年末目标为前瞻预算与目标估算", period_def)

        # 结构校验通过
        self.assertEqual(make_brief.validate_brief(brief), [])

        # 能被 da 正常消费解析
        from da import build_diagnosis  # noqa: E402

        parsed = build_diagnosis.parse_periods(brief)
        self.assertEqual(parsed["start"], "2026-01")
        self.assertEqual(parsed["reference"], "2026-05")
        self.assertEqual(parsed["end"], "2026-09")
        self.assertEqual(parsed["as_of"], "2026-09-30")

        # 能被 review 正常核对期间
        from review import cross_check  # noqa: E402

        diagnosis = self.diagnosis_with_periods()
        res = cross_check.check_period(brief, diagnosis)
        self.assertTrue(res.ok, res.detail)

        # 任务书期次与 diagnosis periods 逐项对齐
        self.assertEqual(make_brief.check_periods_alignment(brief, diagnosis), [])

    def test_cli_multi_period_options_write_brief(self) -> None:
        """命令行通过 --period 或 --periods 传入多期次可生成包含全部锚点的任务书。"""
        periods = [
            "2026-01实际",
            "2026-05H1参考",
            "2026-09实际",
            "年末测算",
            "年末目标",
        ]
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            code = make_brief.main(["--out", str(path), "--periods", ",".join(periods)])
            self.assertEqual(code, 0)
            written = load_json(path)
            self.assertEqual(written["观察期间"]["期次"], periods)
            self.assertEqual(make_brief.validate_brief(written), [])

        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            cli_args = ["--out", str(path)]
            for p in periods:
                cli_args.extend(["--period", p])
            code = make_brief.main(cli_args)
            self.assertEqual(code, 0)
            written = load_json(path)
            self.assertEqual(written["观察期间"]["期次"], periods)
            self.assertEqual(make_brief.validate_brief(written), [])

    def test_cli_periods_align_with_diagnosis(self) -> None:
        """命令行给出的期次与诊断快照的 periods 对齐时才写出任务书。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            diagnosis_path = Path(folder) / "diagnosis.json"
            diagnosis_path.write_text(
                json.dumps(self.diagnosis_with_periods(), ensure_ascii=False), encoding="utf-8"
            )
            code = make_brief.main(
                [
                    "--out",
                    str(path),
                    "--periods",
                    ",".join(self.periods),
                    "--diagnosis",
                    str(diagnosis_path),
                ]
            )
            self.assertEqual(code, 0)
            written = load_json(path)
            self.assertEqual(written["观察期间"]["期次"], self.periods)

    def test_missing_periods_raises_readable_error(self) -> None:
        """缺少 periods 输入时抛出可读的 ValueError。"""
        with self.assertRaisesRegex(ValueError, "缺少期次锚点"):
            make_brief.build_brief(periods=[])

        with self.assertRaisesRegex(ValueError, "非空字符串"):
            make_brief.build_brief(periods=[""])

        with self.assertRaisesRegex(ValueError, "缺少期次锚点"):
            make_brief.period_keys("2026-09", periods=[])

    def test_cli_empty_periods_fails_with_readable_error(self) -> None:
        """命令行传入空期次时以状态码 2 退出，stderr 输出可读错误且不留任务书。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            error = StringIO()
            with redirect_stderr(error):
                code = make_brief.main(["--out", str(path), "--periods", ""])
            self.assertEqual(code, 2)
            self.assertIn("期次", error.getvalue())
            self.assertFalse(path.exists())

        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            error = StringIO()
            with redirect_stderr(error):
                code = make_brief.main(["--out", str(path), "--period", ""])
            self.assertEqual(code, 2)
            self.assertIn("期次", error.getvalue())
            self.assertFalse(path.exists())

    def test_check_missing_period_field_fails_readably(self) -> None:
        """校验任务书缺少期次字段时给出可读失败且退出码为 1。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            brief = make_brief.build_brief()
            del brief["观察期间"]["期次"]
            path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")

            # validate_brief 纯函数报错
            errors = make_brief.validate_brief(brief)
            self.assertTrue(any("期次" in e for e in errors))

            # CLI --check 退出码为 1 且提示可读错误无 traceback
            result = subprocess.run(
                [sys.executable, str(ROOT / "make_brief.py"), "--check", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("期次", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stdout)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
