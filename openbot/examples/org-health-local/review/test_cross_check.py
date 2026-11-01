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


def drop_period_key(node: dict, key: str) -> None:
    """把字典里的期次键删掉（就地修改，嵌套字典一并处理）。"""
    for name in list(node.keys()):
        if name == key:
            node.pop(name)
        elif isinstance(node[name], dict):
            drop_period_key(node[name], key)


def run_script(
    brief: Path,
    data: Path,
    dist: Path | None = None,
    out: Path | None = None,
    json_out: Path | None = None,
) -> subprocess.CompletedProcess:
    """调用命令行脚本，返回进程结果。"""
    argv = [sys.executable, str(SCRIPT), "--brief", str(brief), "--data", str(data)]
    if dist is not None:
        argv.extend(["--dist", str(dist)])
    if out is not None:
        argv.extend(["--out", str(out)])
    if json_out is not None:
        argv.extend(["--json-out", str(json_out)])
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

    def test_brief_period_keys_mismatch_fails(self) -> None:
        """快照数值序列的期次键与任务书期次锚点不一致时必须报出结构化冲突。"""
        self.brief["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        for metric in self.diagnosis["metrics"]:
            rename_period_key(metric["数值"], "2026-09实际", "2026-08实际")
        failures = self.failing(self.check())
        self.assertTrue(failures, "快照期次键与任务书期次不一致时未报出")
        period = failures[0]
        self.assertEqual(period.name, "期间一致")
        self.assertEqual(period.severity, "error")
        self.assertEqual(period.finding["field"], "期次")
        self.assertEqual(period.finding["brief_path"], "/观察期间/期次")
        self.assertIn("2026-08实际", period.finding["actual"])
        finding = cross_check.findings_report([period])[0]
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["path"], period.finding["diagnosis_path"])
        self.assertEqual(finding["context"]["field"], "期次")

    def test_brief_period_keys_aligned_passes(self) -> None:
        """任务书期次锚点与快照数值序列对齐时通过。"""
        self.brief["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        self.assertEqual(self.failing(self.check()), [])

    def test_brief_period_keys_missing_fails(self) -> None:
        """快照数值序列缺少任务书期次锚点时必须报出缺哪些键。"""
        self.brief["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        for metric in self.diagnosis["metrics"]:
            drop_period_key(metric["数值"], "年末目标")
        failures = self.failing(self.check())
        self.assertTrue(failures, "快照期次键缺任务书锚点时未报出")
        period = failures[0]
        self.assertEqual(period.name, "期间一致")
        self.assertIn("缺少", period.detail)
        self.assertIn("年末目标", period.detail)
        self.assertEqual(period.finding["missing"], ["年末目标"])
        self.assertEqual(period.finding["actual"], [])

    def test_brief_anchor_period_keys_mismatch_fails(self) -> None:
        """任务书只给起止/参考期次时，快照期次月份不一致必须报出缺哪些、多哪些。"""
        for metric in self.diagnosis["metrics"]:
            drop_period_key(metric["数值"], "2026-05H1参考")
        self.diagnosis["metrics"][0]["数值"]["2026-08实际"] = 1
        failures = self.failing(self.check())
        self.assertTrue(failures, "快照期次月份与任务书锚点不一致时未报出")
        period = failures[0]
        self.assertEqual(period.name, "期间一致")
        self.assertIn("缺少", period.detail)
        self.assertIn("2026-05", period.detail)
        self.assertIn("多出", period.detail)
        self.assertIn("2026-08", period.detail)
        self.assertEqual(period.finding["expected"], ["2026-01", "2026-05", "2026-09"])

    def test_period_key_diff_compares_brief_anchors_and_snapshot_keys(self) -> None:
        """期次键比对纯函数：全口径的「整体」前缀去掉后与任务书期次锚点比对。"""
        keys = cross_check.diagnosis_period_keys(self.diagnosis)
        self.assertIn("2026-01实际", keys)
        self.assertIn("2026-09实际", keys)
        self.assertNotIn("整体2026-09实际", keys)

        diff = cross_check.period_key_diff(self.brief, self.diagnosis)
        self.assertEqual(diff.missing, [])
        self.assertEqual(diff.extra, [])
        self.assertEqual(diff.expected, ["2026-01", "2026-05", "2026-09"])

        boss = copy.deepcopy(self.brief)
        boss["观察期间"] = copy.deepcopy(BOSS_PERIOD)
        diff = cross_check.period_key_diff(boss, self.diagnosis)
        self.assertEqual(diff.missing, [])
        self.assertEqual(diff.extra, [])
        self.assertEqual(diff.brief_path, "/观察期间/期次")
        self.assertIn("2026-09实际", diff.expected)

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

    def test_temp_files_full_text_cover_all_questions(self) -> None:
        """真实 brief/diagnosis 临时文件中的全文变体可覆盖全部问题。"""
        brief_data = copy.deepcopy(self.brief)
        brief_data["必答问题"] = ["收入与成本率的口径是什么？", "整体技术人力成本在 2026-09 的实际值是多少？"]
        diagnosis_data = copy.deepcopy(self.diagnosis)
        diagnosis_data["insights"] = [
            {"标题": "收入与成本率的口径是什么？", "内容": "按部门统计。"},
            {"标题": "整体技术人力成本在2026-09的实际值是多少?", "内容": "12 万元/月。"},
        ]
        diagnosis_data["recommendations"] = []
        with temporary_directory() as folder:
            base = Path(folder)
            result = run_script(
                write_json(base / "brief.json", brief_data),
                write_json(base / "diagnosis.json", diagnosis_data),
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_temp_files_exact_id_match_passes(self) -> None:
        """真实临时文件中 id 精确命中时通过，且不把 Q1 当成 Q10。"""
        brief_data = copy.deepcopy(self.brief)
        brief_data["必答问题"] = [{"id": "Q1", "问题": "成本走势如何？"}]
        diagnosis_data = copy.deepcopy(self.diagnosis)
        diagnosis_data["insights"] = [{"标题": "Q1", "内容": "见成本趋势。"}]
        diagnosis_data["recommendations"] = []
        with temporary_directory() as folder:
            base = Path(folder)
            result = run_script(
                write_json(base / "brief.json", brief_data),
                write_json(base / "diagnosis.json", diagnosis_data),
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_temp_files_shared_keyword_reports_structured_conflict(self) -> None:
        """真实临时文件中共享关键词不能让一个答案覆盖两个问题。"""
        brief_data = copy.deepcopy(self.brief)
        brief_data["必答问题"] = ["整体成本如何？", "整体成本是多少？"]
        diagnosis_data = copy.deepcopy(self.diagnosis)
        diagnosis_data["insights"] = [{"标题": "整体成本分析", "内容": "等待补充证据。"}]
        diagnosis_data["recommendations"] = []
        with temporary_directory() as folder:
            base = Path(folder)
            result = run_script(
                write_json(base / "brief.json", brief_data),
                write_json(base / "diagnosis.json", diagnosis_data),
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = self.failing(cross_check.run_checks(brief_data, diagnosis_data))
        question_findings = [item for item in findings if item.name == "必答问题覆盖"]
        self.assertEqual(len(question_findings), 2)
        self.assertTrue(all(item.finding and item.finding["conflicts"] for item in question_findings))
        self.assertIn("整体成本", result.stdout)

    def test_missing_recommendations_section_passes(self) -> None:
        """诊断快照缺少可选 recommendations 段时交叉审查仍能通过并输出稳定结果。"""
        self.diagnosis["insights"].append(
            {
                "类型": "事实",
                "标题": "收入与成本率的口径是什么？",
                "内容": "收入与成本率的口径按部门统计。",
            }
        )
        del self.diagnosis["recommendations"]
        results = self.check()
        self.assertEqual(self.failing(results), [])

    def test_null_recommendations_section_passes(self) -> None:
        """诊断快照 recommendations 为 None 时交叉审查仍能通过。"""
        self.diagnosis["insights"].append(
            {
                "类型": "事实",
                "标题": "收入与成本率的口径是什么？",
                "内容": "收入与成本率的口径按部门统计。",
            }
        )
        self.diagnosis["recommendations"] = None
        results = self.check()
        self.assertEqual(self.failing(results), [])

    def test_missing_recommendations_with_unreferenced_question_fails_cleanly(self) -> None:
        """缺少 recommendations 且必答问题未在 insights 覆盖时稳定报出失败而不崩溃。"""
        del self.diagnosis["recommendations"]
        failures = self.failing(self.check())
        self.assertTrue(failures)
        self.assertEqual(failures[0].name, "必答问题覆盖")
        self.assertIn("未在 insights/recommendations 中被引用", failures[0].detail)

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

    def test_nested_forbidden_compound_field_names_fail_with_paths(self) -> None:
        """深层指标序列与建议对象中的复合黑名单键必须输出完整路径。"""
        cases = (
            ("岗位", "metrics", "岗位明细", "/metrics/0/数值/按组织/岗位明细"),
            ("工号", "metrics", "人员工号", "/metrics/0/数值/按组织/人员工号"),
            ("绩效", "metrics", "绩效结果", "/metrics/0/数值/按组织/绩效结果"),
            ("职级", "recommendations", "负责人职级", "/recommendations/0/行动/负责人职级"),
            ("薪酬", "recommendations", "薪酬调整", "/recommendations/0/行动/薪酬调整"),
            ("编制", "recommendations", "编制规划", "/recommendations/0/行动/编制规划"),
        )
        for token, container, field_name, expected_path in cases:
            with self.subTest(token=token, container=container):
                diagnosis = copy.deepcopy(self.diagnosis)
                if container == "metrics":
                    diagnosis["metrics"][0]["数值"]["按组织"] = {field_name: 1}
                else:
                    diagnosis["recommendations"][0]["行动"] = {field_name: "仅用于测试"}

                results = self.check(diagnosis=diagnosis)
                failures = self.failing(results)
                self.assertTrue(failures)
                privacy = next(result for result in failures if result.name == "隐私边界")
                self.assertIn(expected_path, privacy.detail)
                report = cross_check.markdown_report(results)
                self.assertIn("FAIL 隐私边界", report)
                self.assertIn(expected_path, report)

    def test_aggregate_field_names_pass(self) -> None:
        """聚合口径字段名（折算人头、成本）不算越界。"""
        self.assertEqual(self.failing(self.check()), [])

    def test_nested_aggregate_compound_field_names_pass(self) -> None:
        """深层合法聚合指标名不应因复合命名被误报。"""
        diagnosis = copy.deepcopy(self.diagnosis)
        diagnosis["metrics"].append(
            {
                "标题": "营收业务技术人力成本",
                "数值": {"整体折算人头": {"2026-09实际": 12}},
            }
        )
        diagnosis["recommendations"][0]["行动"] = {
            "整体折算人头": "保持聚合口径",
            "营收业务技术人力成本": "保持聚合口径",
        }
        self.assertEqual(self.failing(self.check(diagnosis=diagnosis)), [])

    def test_nested_multiline_forbidden_field_fails_with_stable_path(self) -> None:
        """嵌套数组与对象中含禁止字段名且文本跨行时，失败报告输出稳定路径且不被换行截断。"""
        diagnosis = copy.deepcopy(self.diagnosis)
        diagnosis["recommendations"][0]["行动清单"] = [
            {
                "方案说明": "跨行行动建议第一行\n跨行行动建议第二行\n跨行行动建议第三行",
                "执行细节": {
                    "岗位明细": "第一行梳理关键岗位明细与分工职责\n第二行按季度评估编制并优化配置",
                    "整体折算人头": 14,
                    "营收业务技术人力成本": "约2%",
                },
            }
        ]

        results = self.check(diagnosis=diagnosis)
        failures = self.failing(results)
        self.assertEqual(len(failures), 1)
        privacy = failures[0]
        self.assertEqual(privacy.name, "隐私边界")

        expected_path = "/recommendations/0/行动清单/0/执行细节/岗位明细"
        self.assertIn(expected_path, privacy.detail)
        self.assertIn("岗位明细", privacy.detail)
        self.assertIn("（岗位）", privacy.detail)

        # 失败报告单行稳定，不被跨行文本污染或截断
        self.assertNotIn("\n", privacy.detail)
        self.assertNotIn("\r", privacy.detail)

        # 同级合法 aggregate 聚合字段不被误判为违规
        self.assertNotIn("整体折算人头", privacy.detail)
        self.assertNotIn("营收业务技术人力成本", privacy.detail)

        # 现有报告格式及 PASS/FAIL 汇总稳定不变
        report = cross_check.markdown_report(results)
        self.assertIn("**汇总：共 5 项检查，4 项通过，1 项失败。**", report)
        self.assertIn(
            f"- **FAIL 隐私边界**：出现个人/薪酬/编制粒度字段名：{expected_path}（岗位）",
            report,
        )
        self.assertIn("- **PASS 期间一致**", report)
        self.assertIn("- **PASS 版本一致**", report)
        self.assertIn("- **PASS 必答问题覆盖**", report)

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

    def test_command_line_help_lists_arguments(self) -> None:
        """--help 成功退出并列出全部命令行参数。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for argument in ("--brief", "--data", "--dist", "--out", "--json-out"):
            self.assertIn(argument, result.stdout)

    def test_command_line_missing_required_arguments_exits_two(self) -> None:
        """缺少 --brief 或 --data 时 argparse 以退出码 2 结束。"""
        missing_brief = subprocess.run(
            [sys.executable, str(SCRIPT), "--data", "missing.json"],
            capture_output=True,
            text=True,
        )
        missing_data = subprocess.run(
            [sys.executable, str(SCRIPT), "--brief", "missing.json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing_brief.returncode, 2, missing_brief.stderr)
        self.assertEqual(missing_data.returncode, 2, missing_data.stderr)

    def test_command_line_writes_successful_markdown_report(self) -> None:
        """合法输入带 --out 时成功写出含汇总与逐条 PASS/FAIL 列表的报告。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            report = base / "report.md"
            result = run_script(brief, data, out=report)
            content = report.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("**汇总：共 5 项检查，5 项通过，0 项失败。**", content)
        self.assertIn("- **PASS 期间一致**", content)
        self.assertIn("- **PASS 隐私边界**", content)
        self.assertNotIn("- **FAIL ", content)

    def test_command_line_writes_successful_json_report(self) -> None:
        """合法输入带 --json-out 时写出稳定的逐条 JSON 结果。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            json_out = base / "review.json"
            result = run_script(brief, data, json_out=json_out)
            payload = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, cross_check.EXIT_OK, result.stdout + result.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"], {"total": 5, "passed": 5, "failed": 0})
        self.assertTrue(payload["checks"])
        self.assertTrue(
            all(set(check) == {"name", "ok", "detail"} for check in payload["checks"])
        )
        self.assertEqual(payload["findings"], [])

    def test_json_findings_use_severity_path_message_contract(self) -> None:
        """冲突样本输出非空 findings，且每条都有统一契约字段。"""
        brief_data = copy.deepcopy(self.brief)
        brief_data["必答问题"] = ["整体成本如何？", "整体成本是多少？"]
        diagnosis_data = copy.deepcopy(self.diagnosis)
        diagnosis_data["insights"] = [{"标题": "整体成本分析", "内容": "等待补充证据。"}]
        diagnosis_data["recommendations"] = []
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", brief_data)
            data = write_json(base / "diagnosis.json", diagnosis_data)
            json_out = base / "review.json"
            result = run_script(brief, data, json_out=json_out)
            payload = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stdout + result.stderr)
        self.assertTrue(payload["findings"])
        for finding in payload["findings"]:
            self.assertIn(finding["severity"], cross_check.FINDING_SEVERITIES)
            self.assertTrue(finding["path"])
            self.assertTrue(finding["message"])
        self.assertTrue(any(finding["severity"] == "error" for finding in payload["findings"]))

    def test_finding_without_severity_uses_default_and_path(self) -> None:
        """缺少 severity 的非空失败 finding 使用默认枚举并保留告警路径。"""
        result = cross_check.Result(False, "版本一致", "schema 版本不一致")

        findings = cross_check.findings_report([result])

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], cross_check.DEFAULT_FINDING_SEVERITY)
        self.assertEqual(findings[0]["path"], "/schema版本")

    def test_finding_with_legal_severity_is_preserved(self) -> None:
        """合法 severity 枚举在非空 finding 中保留且路径可定位。"""
        result = cross_check.Result(False, "隐私边界", "发现敏感字段：/diagnosis/岗位", severity="warning")

        findings = cross_check.findings_report([result])

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "warning")
        self.assertEqual(findings[0]["path"], "/diagnosis/岗位")

    def test_command_line_missing_recommendations_passes_and_writes_markdown_report(self) -> None:
        """快照缺少可选 recommendations 段时命令行正常退出并写出完整 Markdown 报告。"""
        self.diagnosis["insights"].append(
            {
                "类型": "事实",
                "标题": "收入与成本率的口径是什么？",
                "内容": "收入与成本率的口径按部门统计。",
            }
        )
        del self.diagnosis["recommendations"]
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            report = base / "report.md"
            result = run_script(brief, data, out=report)
            content = report.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("**汇总：共 5 项检查，5 项通过，0 项失败。**", content)
        self.assertIn("- **PASS 必答问题覆盖**", content)
        self.assertNotIn("- **FAIL ", content)

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

    def test_command_line_writes_failed_json_report(self) -> None:
        """失败检查带 --json-out 时写出失败 JSON 且退出码为 1。"""
        self.brief["schema版本"] = 3
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            json_out = base / "review.json"
            result = run_script(brief, data, json_out=json_out)
            payload = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stdout + result.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["summary"]["failed"], 1)
        self.assertTrue(any(not check["ok"] for check in payload["checks"]))

    def test_command_line_empty_questions_fails_with_report_summary(self) -> None:
        """任务书必答问题为空时命令行失败并在报告汇总中计数。"""
        self.brief["必答问题"] = []
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            report = base / "report.md"
            result = run_script(brief, data, out=report)
            content = report.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stdout + result.stderr)
        self.assertIn("FAIL 必答问题覆盖", result.stdout)
        self.assertIn("**汇总：共 4 项检查，3 项通过，1 项失败。**", content)

    def test_command_line_nested_multiline_forbidden_field_writes_report(self) -> None:
        """命令行遇到嵌套数组/对象中含禁止字段名且文本跨行时，生成完整且未被换行截断的报告。"""
        diagnosis = copy.deepcopy(self.diagnosis)
        diagnosis["recommendations"][0]["行动清单"] = [
            {
                "方案说明": "跨行行动建议第一行\n跨行行动建议第二行",
                "执行细节": {
                    "岗位明细": "第一行按团队梳理岗位明细\n第二行编制与预算对齐",
                    "整体折算人头": 14,
                    "营收业务技术人力成本": "约2%",
                },
            }
        ]
        expected_path = "/recommendations/0/行动清单/0/执行细节/岗位明细"
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", diagnosis)
            report = base / "report.md"
            result = run_script(brief, data, out=report)
            content = report.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, cross_check.EXIT_FAIL, result.stdout + result.stderr)
        self.assertIn(
            f"FAIL 隐私边界：出现个人/薪酬/编制粒度字段名：{expected_path}（岗位）",
            result.stdout,
        )
        self.assertIn("**汇总：共 5 项检查，4 项通过，1 项失败。**", content)
        self.assertIn(
            f"- **FAIL 隐私边界**：出现个人/薪酬/编制粒度字段名：{expected_path}（岗位）",
            content,
        )
        self.assertNotIn("整体折算人头", content)
        self.assertNotIn("营收业务技术人力成本", content)

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

    def test_dist_missing_diagnosis_artifact_reports_specific_path(self) -> None:
        """dist 目录存在但缺少 data/diagnosis.json 时报出具体产物并以退出码 1 结束。"""
        for make_data_dir in (False, True):
            with self.subTest(data_dir=make_data_dir):
                with temporary_directory() as folder:
                    base = Path(folder)
                    brief = write_json(base / "brief.json", self.brief)
                    data = write_json(base / "diagnosis.json", self.diagnosis)
                    dist = base / "dist"
                    if make_data_dir:
                        (dist / "data").mkdir(parents=True)
                    else:
                        dist.mkdir()
                    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
                    report = base / "report.md"
                    result = run_script(brief, data, dist, out=report)
                    content = report.read_text(encoding="utf-8")

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL 站点数据一致", result.stdout)
                # 报告明确指出缺失的具体产物：dist/data/diagnosis.json
                self.assertIn("dist/data/diagnosis.json", result.stdout)
                self.assertIn(str(dist / "data" / "diagnosis.json"), result.stdout)
                self.assertIn("- **FAIL 站点数据一致**", content)
                self.assertIn(str(dist / "data" / "diagnosis.json"), content)

                # 产物缺失不得误报成隐私或期间错误
                self.assertNotIn("FAIL 隐私边界", result.stdout)
                self.assertNotIn("FAIL 期间一致", result.stdout)
                self.assertIn("PASS 期间一致", result.stdout)
                self.assertIn("PASS 隐私边界", result.stdout)

    def test_dist_omitted_keeps_contract(self) -> None:
        """省略 --dist 时不做站点核对，其余检查与退出码不变。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            result = run_script(brief, data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("站点数据一致", result.stdout)
        self.assertIn("共 5 项检查：5 项通过，0 项失败", result.stdout)

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

    def test_report_parent_missing_is_created(self) -> None:
        """--out 的父目录不存在时自动创建多层目录，并正常写出报告。"""
        with temporary_directory() as folder:
            base = Path(folder)
            brief = write_json(base / "brief.json", self.brief)
            data = write_json(base / "diagnosis.json", self.diagnosis)
            report = base / "reports" / "2026" / "09" / "review" / "report.md"
            result = run_script(brief, data, out=report)
            content = report.read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# 组织诊断交叉审查报告", content)
        self.assertIn("**汇总：共 5 项检查，5 项通过，0 项失败。**", content)
        self.assertIn("- **PASS 期间一致**", content)
        self.assertNotIn("- **FAIL ", content)


if __name__ == "__main__":
    unittest.main()
