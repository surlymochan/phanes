#!/usr/bin/env python3
"""make_brief.py 的单元测试。

覆盖：生成结果的结构与字段齐全、schema版本 为 4、口径与合成示例一致、
禁止事项含必需条目，以及 --goal/--month 覆盖与命令行的退出码。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


def load_json(path: Path):
    """读取 JSON 文件。"""
    return json.loads(path.read_text(encoding="utf-8"))


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


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


if __name__ == "__main__":
    unittest.main()
