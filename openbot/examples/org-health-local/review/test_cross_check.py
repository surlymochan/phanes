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


def run_script(brief: Path, data: Path, dist: Path | None = None) -> subprocess.CompletedProcess:
    """调用命令行脚本，返回进程结果。"""
    argv = [sys.executable, str(SCRIPT), "--brief", str(brief), "--data", str(data)]
    if dist is not None:
        argv.extend(["--dist", str(dist)])
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


if __name__ == "__main__":
    unittest.main()
