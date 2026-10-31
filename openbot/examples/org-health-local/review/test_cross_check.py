"""cross_check.py 的单元测试。

用测试内构造的最小任务书、诊断快照与站点目录覆盖通过与各类失败；
只依赖 review 包自身，不依赖其他角色的脚本是否存在。
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from review import cross_check  # noqa: E402  (需要先让示例目录可被导入)

SCRIPT = ROOT / "review" / "cross_check.py"

BRIEF = {
    "目标": ["说明整体成本与成本率的走势"],
    "观察期间": {
        "起始期次": "2026-01",
        "参考期次": "2026-05",
        "截止期次": "2026-09",
        "数据截止": "2026-09-30",
    },
    "口径": {"汇率": 7.1, "技术人力成本": "万元/月"},
    "必答问题": [
        "整体技术人力成本在 2026-09 的实际值是多少？",
        "收入与成本率的口径是什么？",
    ],
    "数据需求": ["cost.csv"],
    "禁止事项": ["禁止写入个人数据"],
    "schema版本": 4,
}

DIAGNOSIS = {
    "scope": {
        "数据截止": "2026-09-30",
        "观察月份": "2026-09",
        "来源": "合成示例",
        "schema版本": 4,
    },
    "conclusion": "合成示例，不用于任何真实判断。",
    "origin": "synthetic fixture",
    "metrics": [
        {
            "标题": "整体技术人力成本",
            "数值": {
                "2026-01实际": 10,
                "2026-05H1参考": None,
                "2026-09实际": 12,
                "年末测算": 14,
            },
        },
        {
            "标题": "整体折算人头",
            "数值": {
                "2026-01实际": 10,
                "2026-05H1参考": None,
                "2026-09实际": 12,
                "年末测算": 14,
            },
        },
        {
            "标题": "营收业务（含共享平台）技术人力成本与折算人头",
            "数值": {
                "成本": {
                    "2026-01实际": 10,
                    "2026-05H1参考": None,
                    "2026-09实际": 12,
                    "年末测算": 14,
                },
                "折算人头": {
                    "2026-01实际": 10,
                    "2026-05H1参考": None,
                    "2026-09实际": 12,
                    "年末测算": 14,
                },
            },
        },
        {
            "标题": "收入与成本率",
            "数值": {
                "收入万美元每月": {
                    "2026-01实际": 10,
                    "2026-05H1参考": None,
                    "2026-09实际": 12,
                    "年末测算": 14,
                    "年末目标": 15,
                },
                "整体技术人力成本率": {
                    "2026-01实际": "约1%",
                    "2026-09实际": "约2%",
                },
                "营收业务技术人力成本率": {
                    "2026-01实际": "约1%",
                    "2026-09实际": "约2%",
                },
                "全口径技术成本率含运维设施": {
                    "整体2026-01实际": "约1%",
                    "整体2026-09实际": "约2%",
                },
            },
        },
    ],
    "insights": [
        {
            "类型": "事实",
            "标题": "整体技术人力成本在 2026-09 的实际值是多少？",
            "内容": "2026-09 实际值为 12 万元/月。",
        }
    ],
    "definitions": ["折算人头为当量折算值，非自然人数。"],
    "risks": [{"标题": "证据不足", "内容": "5 月缺同口径值。"}],
    "recommendations": [
        {
            "优先级": "P1",
            "标题": "收入与成本率的口径是什么？",
            "依据": "口径说明",
            "缺失证据": "汇率与单位",
            "验证方式": "核对口径说明",
        }
    ],
}


# Boss 格式的观察期间（与 boss/make_brief.py 产出一致）：期次是字符串数组。
BOSS_PERIOD = {
    "观察月份": "2026-09",
    "数据截止": "2026-09-30",
    "期次": ["2026-01实际", "2026-05H1参考", "2026-09实际", "年末测算", "年末目标"],
    "说明": (
        "观察月份 2026-09，数据截止 2026-09-30；"
        "期次锚点 2026-01实际、2026-05H1参考、2026-09实际、年末测算、年末目标，"
        "与站点数值序列的键名一致。"
    ),
}


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


def write_json(path: Path, payload: object) -> Path:
    """把数据写成 JSON 文件。"""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def rename_period_key(node: dict, old: str, new: str) -> None:
    """把字典里的期次键改名（就地修改，嵌套字典一并处理）。"""
    for key in list(node.keys()):
        if key == old:
            node[new] = node.pop(key)
        elif isinstance(node[key], dict):
            rename_period_key(node[key], old, new)


def run_script(
    brief: Path, data: Path, dist: Path | None = None, out: Path | None = None
) -> subprocess.CompletedProcess:
    """调用命令行脚本，返回进程结果。"""
    argv = [sys.executable, str(SCRIPT), "--brief", str(brief), "--data", str(data)]
    if dist is not None:
        argv.extend(["--dist", str(dist)])
    if out is not None:
        argv.extend(["--out", str(out)])
    return subprocess.run(argv, capture_output=True, text=True)


class CrossCheckTest(unittest.TestCase):
    """三方一致性的逐条核对。"""

    def setUp(self) -> None:
        self.brief = copy.deepcopy(BRIEF)
        self.diagnosis = copy.deepcopy(DIAGNOSIS)

    def check(self, brief: dict | None = None, diagnosis: dict | None = None) -> list:
        """在内存里跑一遍检查，返回逐条结果。"""
        return cross_check.run_checks(
            brief if brief is not None else self.brief,
            diagnosis if diagnosis is not None else self.diagnosis,
        )

    def failing(self, results: list) -> list:
        """返回失败的结果列表。"""
        return [result for result in results if not result.ok]

    def test_consistent_inputs_pass(self) -> None:
        """三方一致时全部检查通过。"""
        results = self.check()
        self.assertEqual(self.failing(results), [])

    def test_period_not_in_brief_fails(self) -> None:
        """快照观察月份不在任务书观察期间内时失败。"""
        self.brief["观察期间"] = {
            "起始期次": "2026-01",
            "参考期次": "2026-05",
            "截止期次": "2026-05",
            "数据截止": "2026-05-31",
        }
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "期间一致")
        self.assertIn("观察月份", failures[0].detail)

    def test_period_cutoff_mismatch_fails(self) -> None:
        """任务书数据截止与快照不一致时失败。"""
        self.brief["观察期间"]["数据截止"] = "2026-09-29"
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "期间一致")
        self.assertIn("数据截止", failures[0].detail)

    def test_period_end_mismatch_fails(self) -> None:
        """任务书截止期次与快照观察月份不一致时失败。"""
        self.brief["观察期间"]["截止期次"] = "2026-12"
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "期间一致")

    def test_brief_period_mismatch_fails(self) -> None:
        """任务书期次与快照期次不一致时必须报出。"""
        self.brief["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        self.diagnosis["scope"]["观察月份"] = "2026-08"
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "期间一致")
        self.assertIn("观察月份", failures[0].detail)

    def test_brief_observed_month_mismatch_fails(self) -> None:
        """快照期次落在任务书提到的其他期次上时必须报出。

        Boss 格式任务书的 `说明` 会列出全部期次锚点，快照落到 2026-01 时期次集合内命中，
        需再比对任务书 `观察期间.观察月份` 与快照 `scope.观察月份`。
        """
        self.brief["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        self.diagnosis["scope"]["观察月份"] = "2026-01"
        failures = self.failing(self.check())
        self.assertTrue(failures, "快照期次与任务书观察月份不一致时未报出")
        self.assertEqual(failures[0].name, "期间一致")
        self.assertIn("观察月份", failures[0].detail)

    @unittest.expectedFailure
    def test_brief_period_keys_mismatch_fails(self) -> None:
        """快照数值序列的期次键与任务书 `观察期间.期次` 不一致时必须报出。

        当前实现只核对 `scope.观察月份`，没有比对快照各指标的期次键与任务书期次锚点，
        DA 用错期次锚点时漏报。实现补上该比较后，删掉 expectedFailure 标记。
        """
        self.brief["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        for metric in self.diagnosis["metrics"]:
            rename_period_key(metric["数值"], "2026-09实际", "2026-08实际")
        failures = self.failing(self.check())
        self.assertTrue(failures, "快照期次键与任务书期次不一致时未报出")

    def test_version_mismatch_fails(self) -> None:
        """任务书与快照的 schema版本 不一致时失败。"""
        self.brief["schema版本"] = 3
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "版本一致")

    def test_missing_brief_version_fails(self) -> None:
        """任务书缺少 schema版本 时失败。"""
        del self.brief["schema版本"]
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "版本一致")

    def test_unreferenced_question_fails(self) -> None:
        """必答问题在 insights/recommendations 里没有被引用时失败。"""
        self.diagnosis["insights"] = []
        self.diagnosis["recommendations"] = []
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "必答问题覆盖")
        self.assertEqual(len(failures), 2)

    def test_missing_questions_fails(self) -> None:
        """任务书缺少必答问题数组时失败。"""
        del self.brief["必答问题"]
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "必答问题覆盖")

    def test_questions_not_a_list_fails(self) -> None:
        """任务书的必答问题不是数组时失败。"""
        self.brief["必答问题"] = "整体成本是多少？"
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "必答问题覆盖")

    def test_empty_questions_fails(self) -> None:
        """任务书的必答问题为空时失败。"""
        self.brief["必答问题"] = []
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "必答问题覆盖")

    def test_question_matched_by_id_passes(self) -> None:
        """带 id 的必答问题按 id 命中即可算被引用。"""
        self.brief["必答问题"] = [{"id": "Q1", "问题": "整体成本走势如何？"}]
        self.diagnosis["insights"] = [
            {"类型": "推断", "标题": "Q1 的答案", "内容": "整体成本走势见下表。"}
        ]
        self.diagnosis["recommendations"] = []
        self.assertEqual(self.failing(self.check()), [])

    def test_question_matched_by_keyword_passes(self) -> None:
        """必答问题按关键词命中即可算被引用。"""
        self.brief["必答问题"] = ["折算人头是怎么算出来的？"]
        self.diagnosis["insights"] = [
            {"类型": "事实", "标题": "折算人头口径", "内容": "折算人头按当量折算。"}
        ]
        self.diagnosis["recommendations"] = []
        self.assertEqual(self.failing(self.check()), [])

    def test_forbidden_field_name_fails(self) -> None:
        """出现个人/薪酬/编制粒度字段名时失败。"""
        self.diagnosis["risks"][0]["个人薪酬"] = "张三 10 万"
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "隐私边界")
        self.assertIn("个人薪酬", failures[0].detail)

    def test_forbidden_field_name_in_metrics_fails(self) -> None:
        """指标里出现编制粒度字段名时失败。"""
        self.diagnosis["metrics"][0]["数值"]["编制人数"] = 12
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "隐私边界")
        self.assertIn("编制", failures[0].detail)

    def test_aggregate_field_names_pass(self) -> None:
        """聚合口径字段名（折算人头、成本）不算越界。"""
        self.assertEqual(self.failing(self.check()), [])

    def test_extract_keywords(self) -> None:
        """关键词提取保留日期片段并去掉疑问词。"""
        keywords = cross_check.extract_keywords("整体技术人力成本在 2026-09 的实际值是多少？")
        self.assertIn("2026-09", keywords)
        self.assertIn("整体技术人力成本", keywords)

    def test_command_line_passes(self) -> None:
        """命令行核对一致的三方时退出码为 0。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(brief, data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_command_line_reports_failure(self) -> None:
        """命令行核对失败时退出码非 0，并打印 FAIL 行。"""
        self.brief["schema版本"] = 3
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(brief, data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL", result.stdout)

    def test_command_line_exit_code_is_one_on_failure(self) -> None:
        """任一检查失败时退出码为 1。"""
        self.brief["schema版本"] = 3
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(brief, data)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_command_line_exit_code_is_two_on_unreadable_input(self) -> None:
        """输入不可读或不是 JSON 对象时退出码为 2。"""
        with temporary_directory() as folder:
            base = Path(folder)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            missing = run_script(base / "missing.json", data)
            broken = base / "broken.json"
            broken.write_text("{不是 JSON", encoding="utf-8")
            invalid = run_script(broken, data)
            array = base / "array.json"
            array.write_text("[]", encoding="utf-8")
            not_object = run_script(array, data)
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertEqual(invalid.returncode, 2, invalid.stdout + invalid.stderr)
        self.assertEqual(not_object.returncode, 2, not_object.stdout + not_object.stderr)

    def test_command_line_writes_markdown_report(self) -> None:
        """--out 写入含标题、汇总与逐条检查结果的 Markdown。"""
        self.brief["schema版本"] = 3
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            report = base / "report.md"
            result = run_script(brief, data, out=report)
            content = report.read_text(encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("# 组织诊断交叉审查报告", content)
        self.assertIn("**汇总：共 5 项检查，4 项通过，1 项失败。**", content)
        self.assertIn("- **FAIL 版本一致**", content)
        self.assertIn("- **PASS 期间一致**", content)

    def test_command_line_writes_report_on_site_mismatch(self) -> None:
        """站点数据与快照不一致时仍写出 Markdown 报告，报告里记 FAIL。"""
        stale = copy.deepcopy(self.diagnosis)
        stale["metrics"][0]["数值"]["2026-09实际"] = 99
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "dist"
            (dist / "data").mkdir(parents=True)
            write_json(dist / "data" / "diagnosis.json", stale)
            report = base / "report.md"
            result = run_script(brief, data, dist, out=report)
            content = report.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("- **FAIL 站点数据一致**", content)
        self.assertIn("**汇总：共 6 项检查，5 项通过，1 项失败。**", content)

    def test_command_line_prints_one_line_per_check(self) -> None:
        """命令行逐条输出检查结果并给出汇总。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(brief, data)
        lines = [line for line in result.stdout.splitlines() if line]
        self.assertEqual(len(lines), 6)
        self.assertTrue(lines[-1].startswith("共 5 项检查"))

    def test_dist_identical_bytes_passes(self) -> None:
        """站点数据与快照字节相同时通过。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "dist"
            (dist / "data").mkdir(parents=True)
            (dist / "data" / "diagnosis.json").write_bytes(data.read_bytes())
            result = run_script(brief, data, dist)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS 站点数据一致", result.stdout)

    def test_run_checks_site_check_requires_dist_and_data_paths(self) -> None:
        """只有同时提供 --dist 与数据路径时才生成站点数据一致检查。"""
        with temporary_directory() as folder:
            base = Path(folder)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "dist"
            (dist / "data").mkdir(parents=True)
            (dist / "data" / "diagnosis.json").write_bytes(data.read_bytes())

            without_dist = cross_check.run_checks(self.brief, self.diagnosis, data_path=data)
            with_dist = cross_check.run_checks(self.brief, self.diagnosis, data_path=data, dist=dist)

        self.assertNotIn("站点数据一致", [result.name for result in without_dist])
        self.assertIn("站点数据一致", [result.name for result in with_dist])

    def test_dist_different_bytes_fails(self) -> None:
        """站点数据与快照字节不同时失败。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "dist"
            (dist / "data").mkdir(parents=True)
            (dist / "data" / "diagnosis.json").write_text(
                json.dumps({"scope": {"观察月份": "2026-09"}}, ensure_ascii=False), encoding="utf-8"
            )
            result = run_script(brief, data, dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL 站点数据一致", result.stdout)

    def test_dist_numeric_value_mismatch_fails(self) -> None:
        """站点产物与工作目录快照的某个数值不一致时必须报出。"""
        stale = copy.deepcopy(self.diagnosis)
        stale["metrics"][0]["数值"]["2026-09实际"] = 99
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "dist"
            (dist / "data").mkdir(parents=True)
            write_json(dist / "data" / "diagnosis.json", stale)
            result = run_script(brief, data, dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL 站点数据一致", result.stdout)
        self.assertIn("字节不同", result.stdout)

    def test_dist_missing_data_fails(self) -> None:
        """站点目录缺少 data/diagnosis.json 时失败。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "dist"
            dist.mkdir()
            result = run_script(brief, data, dist)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL 站点数据一致", result.stdout)

    def test_unreadable_brief_exits_non_zero(self) -> None:
        """任务书不可读时退出码非 0。"""
        with temporary_directory() as folder:
            base = Path(folder)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(base / "missing.json", data)
        self.assertNotEqual(result.returncode, 0)

    def test_corrupt_brief_json_fails(self) -> None:
        """brief.json 损坏时命令行非零退出且 stderr 输出包含可读错误。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = base / "brief.json"
            brief.write_text("{不是合法 JSON", encoding="utf-8")
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(brief, data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不是合法 JSON", result.stderr)
        self.assertIn(str(brief), result.stderr)

    def test_diagnosis_json_not_object_fails(self) -> None:
        """diagnosis.json 顶层非对象时命令行非零退出且 stderr 输出包含可读错误。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = base / "diagnosis.json"
            data.write_text("[]", encoding="utf-8")
            result = run_script(brief, data)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("必须是 JSON 对象", result.stderr)
        self.assertIn(str(data), result.stderr)

    def test_dist_nonexistent_fails(self) -> None:
        """dist 不存在时命令行非零退出且输出包含可读错误。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            dist = base / "nonexistent_dist"
            result = run_script(brief, data, dist)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL 站点数据一致", result.stdout)
        self.assertIn("站点目录缺少 `data/diagnosis.json`", result.stdout)
        self.assertIn(str(dist), result.stdout)

    def test_report_parent_missing_fails(self) -> None:
        """--out 的父目录不存在时命令行非零退出且输出包含可读错误。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            report = base / "missing_parent" / "report.md"
            result = run_script(brief, data, out=report)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("无法写入报告", result.stderr)
        self.assertIn(str(report), result.stderr)


if __name__ == "__main__":
    unittest.main()
