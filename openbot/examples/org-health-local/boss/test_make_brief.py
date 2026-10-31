#!/usr/bin/env python3
"""make_brief.py 的单元测试。

覆盖：生成结果的结构与字段齐全、schema版本 为 4、口径与合成示例一致、
禁止事项含必需条目，以及 --goal/--month 覆盖与命令行的退出码。

观察期间的期次顺序、重复期次与起止倒置按当前行为断言：已支持的情形直接断言，
已知缺口用 `unittest.expectedFailure` 标注并写明原因，不改实现。
"""
from __future__ import annotations

import json
import os
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

BRIEF_KEYS = {"目标", "观察期间", "口径", "必答问题", "数据需求", "禁止事项", "schema版本"}
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
        """期次锚点与合成示例的数值序列键名一致。"""
        keys = set(self.brief["观察期间"]["期次"])
        for metric in self.example["metrics"]:
            for series in metric["数值"].values():
                if not isinstance(series, dict):
                    continue
                for key in series:
                    # 全口径成本率的键带“整体”前缀，其余直接是期次锚点。
                    self.assertIn(key.removeprefix("整体"), keys, f"{metric['标题']} 的键名不在期次里")
        self.assertIn("年末目标", keys)

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
        for month in ("2026-13", "2026-1", "26-01", ""):
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
        """非法观察月份时退出码非 0，且不写文件。"""
        with temporary_directory() as folder:
            path = Path(folder) / "brief.json"
            code = make_brief.main(["--out", str(path), "--month", "2026-13"])
            self.assertNotEqual(code, 0)
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

    @unittest.expectedFailure
    def test_重复期次_应报出错误(self) -> None:
        """期次锚点重复时应报出重复错误。

        已知 bug：结构定义只要求期次至少 4 个（boss/brief.schema.json 的 `期次`），
        validate_brief 也不查重复；重复锚点会让同一期次被取两次数或被静默覆盖。
        实现补上去重校验后，删掉 expectedFailure 标记。
        """
        errors = self.errors(brief_with_period(DUPLICATED_PERIODS))
        self.assertTrue(
            any("重复" in message and "期次" in message for message in errors),
            "重复期次时未报出指向重复期次的错误",
        )

    @unittest.expectedFailure
    def test_命令行拒绝重复期次且不写出任务书(self) -> None:
        """生成结果含重复期次时命令行应报错，且不写出有效任务书。

        已知 bug：main 当前只校验 schema 与锚点归属，重复期次仍会通过校验并写出文件。
        实现补上重复校验后，删掉 expectedFailure 标记。
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


if __name__ == "__main__":
    unittest.main()
