"""build_diagnosis.py 的单元测试。

用 `da/sample-raw/` 下的合成原始数据加一个内嵌的最小任务书生成快照，
再用 `validate.py` 的校验函数断言零错误；缺列时命令行退出码非 0。

异常原始数据（缺期次、负数、重复行、CSV 多余列）按当前行为断言：已支持的情形断言
退出码与错误信息，已知 bug 用 `unittest.expectedFailure` 标注并写明原因，不改实现。

输入错误（必需数字列为空、CSV 出现重复列名、缺少必需列）另用最小 raw fixture 覆盖：
命令行退出码非 0，错误信息指出文件与列名。

列顺序变化（表头名称不变、只调换列的位置）另用临时 CSV 夹具覆盖：仍按列名取数，
生成的快照与原顺序一致；缺少必需列时仍失败并指出缺列与来源文件。
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = PACKAGE_DIR.parent
for entry in (str(EXAMPLE_DIR), str(PACKAGE_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import build_diagnosis  # noqa: E402
import validate  # noqa: E402

SCRIPT = PACKAGE_DIR / "build_diagnosis.py"
MAKE_BRIEF = EXAMPLE_DIR / "boss" / "make_brief.py"
SAMPLE_RAW = PACKAGE_DIR / "sample-raw"
SCHEMA_PATH = EXAMPLE_DIR / "schema" / "diagnosis.schema.json"

BRIEF = {
    "目标": ["复核技术人力成本与成本率的变化"],
    "观察期间": {
        "起始期次": "2026-01",
        "参考期次": "2026-05",
        "截止期次": "2026-09",
        "数据截止": "2026-09-30",
    },
    "口径": {
        "技术人力成本": "技术人力成本=固定薪酬+社保公积金+外包人力，按月汇总，单位万元/月，不含运维与设施。",
        "折算人头": "折算人头=当月投入技术交付的人力当量，1 人月=1 个当量，非自然人数。",
        "收入": "收入=当月确认的营收业务收入（含共享平台分摊），单位万美元/月。",
        "成本率": "成本率=技术人力成本÷同期收入按汇率折人民币；全口径另计运维与设施成本。",
        "汇率": 7.2,
    },
    "必答问题": ["整体技术人力成本从期初到期末如何变化？", "技术人力成本率是否改善？"],
    "数据需求": ["da/sample-raw 下的四张 CSV"],
    "禁止事项": ["禁止编造数字", "不得把真实组织数据写进仓库"],
    "schema版本": 4,
}


def temporary_directory() -> tempfile.TemporaryDirectory:
    """在 TMPDIR 下建临时目录；TMPDIR 不可用时用系统默认目录。"""
    parent = os.environ.get("TMPDIR")
    if parent and Path(parent).is_dir():
        return tempfile.TemporaryDirectory(dir=parent)
    return tempfile.TemporaryDirectory()


def run_cli(brief_path: Path, raw_dir: Path, out_path: Path, strict: bool = False) -> subprocess.CompletedProcess:
    """调用 build_diagnosis.py 命令行。"""
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--brief",
            str(brief_path),
            "--raw",
            str(raw_dir),
            "--out",
            str(out_path),
        ] + (["--strict"] if strict else []),
        capture_output=True,
        text=True,
    )


def write_brief(folder: Path, brief: dict) -> Path:
    """把任务书写到临时目录。"""
    path = folder / "brief.json"
    path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def copy_raw(folder: Path, name: str) -> Path:
    """把合成原始数据复制到临时目录，返回副本目录路径。"""
    target = folder / name
    shutil.copytree(SAMPLE_RAW, target)
    return target


def csv_lines(path: Path) -> list[str]:
    """读出 CSV 的文本行。"""
    return path.read_text(encoding="utf-8").splitlines()


def write_csv_lines(path: Path, lines: list[str]) -> None:
    """写回 CSV 的文本行。"""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def drop_csv_column(text: str, column: str) -> str:
    """从 CSV 文本里删掉一列：表头与每行的取值一起去掉。"""
    lines = text.splitlines()
    index = lines[0].split(",").index(column)
    kept = []
    for line in lines:
        cells = line.split(",")
        kept.append(",".join(cells[:index] + cells[index + 1:]))
    return "\n".join(kept) + "\n"


def reorder_csv_columns(text: str, order: list[str]) -> str:
    """按 `order` 重排 CSV 的列：表头与每行取值一起移动，列名与取值本身不变。"""
    rows = list(csv.reader(io.StringIO(text, newline="")))
    positions = {name: index for index, name in enumerate(rows[0])}
    missing = [name for name in order if name not in positions]
    if missing:
        raise ValueError(f"CSV 里没有这些列：{'、'.join(missing)}")
    picked = [positions[name] for name in order]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow([row[index] for index in picked])
    return buffer.getvalue()


# 最小 raw fixture：只保留观察期间的起止两个月与年末两行，表头按必需列写全。
MINIMAL_COST_CSV = (
    "月份,整体技术人力成本万元,营收业务技术人力成本万元,运维设施成本万元\n"
    "2026-01,10.4,8.3,2.1\n"
    "2026-09,12.4,9.9,2.5\n"
)
MINIMAL_HEADS_CSV = (
    "月份,整体折算人头,营收业务折算人头\n"
    "2026-01,10.4,8.3\n"
    "2026-09,12.4,9.9\n"
)
MINIMAL_REVENUE_CSV = (
    "月份,收入万美元\n"
    "2026-01,11.2\n"
    "2026-09,13.6\n"
)
MINIMAL_FORECAST_CSV = (
    "期次,整体技术人力成本万元,营收业务技术人力成本万元,整体折算人头,营收业务折算人头,运维设施成本万元,收入万美元\n"
    "年末测算,13.4,10.7,13.4,10.7,,14.8\n"
    "年末目标,,,,,,15.2\n"
)

# 最小 raw fixture 的四张 CSV，按文件名索引。
MINIMAL_RAW_FILES = {
    "cost.csv": MINIMAL_COST_CSV,
    "heads.csv": MINIMAL_HEADS_CSV,
    "revenue.csv": MINIMAL_REVENUE_CSV,
    "forecast.csv": MINIMAL_FORECAST_CSV,
}

# 列顺序变化夹具：列名与取值都不变，只把各列在表头里的位置换掉（键列挪到末位）。
REORDERED_COLUMNS = {
    "cost.csv": ("运维设施成本万元", "营收业务技术人力成本万元", "整体技术人力成本万元", "月份"),
    "heads.csv": ("营收业务折算人头", "整体折算人头", "月份"),
    "revenue.csv": ("收入万美元", "月份"),
    "forecast.csv": (
        "收入万美元",
        "运维设施成本万元",
        "营收业务折算人头",
        "整体折算人头",
        "营收业务技术人力成本万元",
        "整体技术人力成本万元",
        "期次",
    ),
}


class ParsePeriodsTest(unittest.TestCase):
    """直接测试任务书观察期间的解析，不启动命令行子进程。"""

    def test_explicit_periods_keep_as_of_or_infer_month_end(self) -> None:
        """显式锚点原样返回，缺数据截止时推算月末。"""
        brief = {
            "观察期间": {
                "起始期次": "2026-01",
                "参考期次": "2026-05",
                "截止期次": "2026-09",
                "数据截止": "2026-09-28",
            }
        }
        self.assertEqual(
            build_diagnosis.parse_periods(brief),
            {"start": "2026-01", "reference": "2026-05", "end": "2026-09", "as_of": "2026-09-28"},
        )

        brief["观察期间"].pop("数据截止")
        self.assertEqual(build_diagnosis.parse_periods(brief)["as_of"], "2026-09-30")

    def test_boss_period_list_uses_actual_reference_and_observed_month(self) -> None:
        """Boss 格式期次列表按实际、参考和观察月份映射锚点。"""
        brief = {
            "观察期间": {
                "观察月份": "2026-09",
                "期次": ["2026-01实际", "2026-05H1参考", "2026-09实际"],
            }
        }
        self.assertEqual(
            build_diagnosis.parse_periods(brief),
            {"start": "2026-01", "reference": "2026-05", "end": "2026-09", "as_of": "2026-09-30"},
        )

    def test_string_periods_use_first_middle_and_last_month(self) -> None:
        """字符串中的三个期次依次作为起始、参考和截止。"""
        self.assertEqual(
            build_diagnosis.parse_periods({"观察期间": "从 2026-01 到 2026-05，截止 2026-09"}),
            {"start": "2026-01", "reference": "2026-05", "end": "2026-09", "as_of": "2026-09-30"},
        )

    def test_string_periods_with_one_or_two_months_require_reference(self) -> None:
        """字符串只有一或两个期次时因缺少参考期次报错。"""
        for value in ("2026-09", "2026-01 至 2026-09"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(build_diagnosis.BriefError, "参考期次"):
                    build_diagnosis.parse_periods({"观察期间": value})

    def test_parse_periods_reports_invalid_or_missing_fields(self) -> None:
        """缺字段及格式错误均抛 BriefError，并指出对应字段名。"""
        cases = (
            ({}, "观察期间"),
            ({"观察期间": {"参考期次": "2026-05", "截止期次": "2026-09"}}, "起始期次"),
            ({"观察期间": {"起始期次": "2026-01", "参考期次": "2026-05"}}, "截止期次"),
            ({"观察期间": {"起始期次": "2026-01", "截止期次": "2026-09"}}, "参考期次"),
            (
                {"观察期间": {"起始期次": "2026-1", "参考期次": "2026-05", "截止期次": "2026-09"}},
                "起始期次",
            ),
            (
                {
                    "观察期间": {
                        "起始期次": "2026-01",
                        "参考期次": "2026-05",
                        "截止期次": "2026-09",
                        "数据截止": "2026-09-9",
                    }
                },
                "数据截止",
            ),
        )
        for brief, field in cases:
            with self.subTest(field=field):
                with self.assertRaisesRegex(build_diagnosis.BriefError, field):
                    build_diagnosis.parse_periods(brief)


class BuildDiagnosisTest(unittest.TestCase):
    """用合成原始数据生成快照并校验。"""

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.name)
        self.brief_path = write_brief(self.folder, BRIEF)
        self.out_path = self.folder / "diagnosis.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> dict:
        """生成快照并返回内容。"""
        result = run_cli(self.brief_path, SAMPLE_RAW, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(self.out_path.read_text(encoding="utf-8"))

    def errors(self, data: dict) -> list[str]:
        """用 validate.py 的校验函数返回错误列表。"""
        return validate.validate_data(data, self.schema)

    def test_snapshot_passes_validation(self) -> None:
        """合成原始数据生成的快照零校验错误。"""
        data = self.build()
        self.assertEqual(self.errors(data), [])

    def test_metric_titles_follow_page_order(self) -> None:
        """指标标题与顺序与页面期望一致。"""
        data = self.build()
        titles = [metric["标题"] for metric in data["metrics"]]
        self.assertEqual(
            titles,
            [
                "整体技术人力成本",
                "整体折算人头",
                "营收业务（含共享平台）技术人力成本与折算人头",
                "收入与成本率",
            ],
        )
        self.assertEqual(data["scope"]["schema版本"], 4)
        self.assertEqual(data["scope"]["观察月份"], "2026-09")
        self.assertEqual(data["scope"]["数据截止"], "2026-09-30")

    def test_missing_period_is_null(self) -> None:
        """参考期次缺同口径整体成本与人头时写 null。"""
        data = self.build()
        overall_cost = data["metrics"][0]["数值"]
        overall_heads = data["metrics"][1]["数值"]
        self.assertIsNone(overall_cost["2026-05H1参考"])
        self.assertIsNone(overall_heads["2026-05H1参考"])
        self.assertEqual(overall_cost["2026-01实际"], 10.4)
        self.assertEqual(overall_cost["2026-09实际"], 12.4)
        self.assertEqual(overall_cost["年末测算"], 13.4)
        income = data["metrics"][3]["数值"]["收入万美元每月"]
        self.assertEqual(income["2026-05H1参考"], 12.4)
        self.assertEqual(income["年末目标"], 15.2)

    def test_missing_forecast_row_is_null_and_reported(self) -> None:
        """forecast.csv 缺少年末测算行时四个指标均写 null，并保留年末目标收入。"""
        raw_dir = copy_raw(self.folder, "raw-without-forecast")
        forecast = raw_dir / "forecast.csv"
        write_csv_lines(
            forecast,
            [line for line in csv_lines(forecast) if not line.startswith("年末测算,")],
        )

        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))

        self.assertEqual(data["scope"]["观察月份"], "2026-09")
        self.assertEqual(data["scope"]["数据截止"], "2026-09-30")
        self.assertEqual(data["scope"]["schema版本"], 4)
        forecast_values = [
            data["metrics"][0]["数值"]["年末测算"],
            data["metrics"][1]["数值"]["年末测算"],
            data["metrics"][2]["数值"]["成本"]["年末测算"],
            data["metrics"][2]["数值"]["折算人头"]["年末测算"],
            data["metrics"][3]["数值"]["收入万美元每月"]["年末测算"],
            data["metrics"][3]["数值"]["整体技术人力成本率"]["年末测算"],
            data["metrics"][3]["数值"]["营收业务技术人力成本率"]["年末测算"],
            data["metrics"][3]["数值"]["全口径技术成本率含运维设施"]["整体年末测算"],
        ]
        self.assertTrue(all(value is None for value in forecast_values))
        self.assertEqual(data["metrics"][3]["数值"]["收入万美元每月"]["年末目标"], 15.2)
        self.assertTrue(any("全口径年末测算缺口" in risk["标题"] for risk in data["risks"]))

    def test_missing_observation_period_is_explained_in_scope_notes(self) -> None:
        """观察期间内原始指标缺期次时追加确定性的 null 口径说明。"""
        raw_dir = self.folder / "raw-with-gap"
        shutil.copytree(SAMPLE_RAW, raw_dir)
        revenue = raw_dir / "revenue.csv"
        lines = revenue.read_text(encoding="utf-8").splitlines()
        revenue.write_text(
            "\n".join(line for line in lines if not line.startswith("2026-06,")) + "\n",
            encoding="utf-8",
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(self.errors(data), [])
        notes = data["口径说明"]
        self.assertIn("整体技术人力成本 缺 2026-05 数据，按 null 处理", notes)
        self.assertIn("整体折算人头 缺 2026-05 数据，按 null 处理", notes)
        self.assertIn("收入 缺 2026-06 数据，按 null 处理", notes)
        self.assertEqual(
            [note for note in notes if "收入 缺" in note],
            ["收入 缺 2026-06 数据，按 null 处理"],
        )

    def test_zero_revenue_makes_cost_rate_null_without_changing_scope(self) -> None:
        """收入全为 0 时成本率为 null，且不影响其它指标与观察范围。"""
        baseline = self.build()
        raw_dir = copy_raw(self.folder, "raw-zero-revenue")
        revenue = raw_dir / "revenue.csv"
        lines = csv_lines(revenue)
        write_csv_lines(
            revenue,
            [lines[0]] + [f"{line.split(',', 1)[0]},0" for line in lines[1:]],
        )

        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ZeroDivisionError", result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))

        rate = data["metrics"][3]["数值"]["整体技术人力成本率"]
        for period in ("2026-01实际", "2026-05H1参考", "2026-09实际"):
            self.assertIsNone(rate[period])
        self.assertEqual(data["scope"]["观察月份"], baseline["scope"]["观察月份"])
        self.assertEqual(data["scope"]["数据截止"], baseline["scope"]["数据截止"])
        self.assertEqual(data["metrics"][:3], baseline["metrics"][:3])
        self.assertEqual(
            data["metrics"][3]["数值"]["收入万美元每月"]["年末测算"],
            baseline["metrics"][3]["数值"]["收入万美元每月"]["年末测算"],
        )
        self.assertEqual(rate["年末测算"], baseline["metrics"][3]["数值"]["整体技术人力成本率"]["年末测算"])

    def test_strict_mode_fails_on_missing_observation_period(self) -> None:
        """严格模式遇观察期间指标缺失时中文报错且不写快照。"""
        raw_dir = self.folder / "raw-with-gap"
        shutil.copytree(SAMPLE_RAW, raw_dir)
        revenue = raw_dir / "revenue.csv"
        lines = revenue.read_text(encoding="utf-8").splitlines()
        revenue.write_text(
            "\n".join(line for line in lines if not line.startswith("2026-06,")) + "\n",
            encoding="utf-8",
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path, strict=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("严格模式失败", result.stderr)
        self.assertIn("收入 缺 2026-06 数据", result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_sections_are_generated(self) -> None:
        """洞察、来源口径、风险与建议都按任务书生成。"""
        data = self.build()
        self.assertTrue(data["insights"])
        self.assertTrue(data["definitions"])
        self.assertTrue(data["risks"])
        self.assertTrue(data["recommendations"])
        titles = [item["标题"] for item in data["insights"]]
        for question in BRIEF["必答问题"]:
            self.assertIn(question, titles)
        for recommendation in data["recommendations"]:
            for field in ("优先级", "标题", "依据", "缺失证据", "验证方式"):
                self.assertTrue(recommendation[field].strip())
        self.assertTrue(any("缺口" in risk["标题"] for risk in data["risks"]))

    def test_cost_change_question_answers_amount_and_percentage(self) -> None:
        """成本增减问题除期初期末绝对值外，还给出增减金额与增减百分比。"""
        brief = json.loads(json.dumps(BRIEF))
        brief["必答问题"] = ["观察期间内整体技术人力成本是多少？相对期初实际增减多少（金额与百分比）？"]
        folder = self.folder / "change-question"
        folder.mkdir(parents=True, exist_ok=True)
        path = write_brief(folder, brief)
        result = run_cli(path, SAMPLE_RAW, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(self.errors(data), [])

        answers = {item["标题"]: item["内容"] for item in data["insights"]}
        content = answers[brief["必答问题"][0]]
        # 期初 10.4 → 期末 12.4 万元/月：增加 2.0 万元/月，增幅 19.2%。
        self.assertIn("整体技术人力成本 10.4 → 12.4 万元/月", content)
        self.assertIn("增加 2.0 万元/月", content)
        self.assertIn("增幅 19.2%", content)

    def test_change_text_covers_decrease_and_missing_data(self) -> None:
        """增减文本覆盖减少与缺数据情形。"""
        self.assertEqual(
            build_diagnosis.change_text(12.4, 10.4, "万元/月"),
            "减少 2.0 万元/月（降幅 16.1%）",
        )
        self.assertEqual(
            build_diagnosis.change_text(None, 12.4, "万元/月"),
            "增减金额与百分比未提供（期初或期末缺同口径数据）",
        )
        self.assertEqual(
            build_diagnosis.change_text(0.0, 12.4, "万元/月"),
            "增加 12.4 万元/月（百分比未提供：期初为 0）",
        )

    def test_calibration_text_is_kept(self) -> None:
        """任务书口径与禁止事项写进来源口径说明。"""
        data = self.build()
        joined = "\n".join(data["definitions"])
        for name in ("技术人力成本", "折算人头", "收入", "成本率"):
            self.assertIn(name, joined)
        self.assertIn("禁止编造数字", joined)

    def test_period_window_accepts_text_form(self) -> None:
        """`观察期间` 写成文本时按月份顺序解析。"""
        brief = json.loads(json.dumps(BRIEF))
        brief["观察期间"] = "2026-01 至 2026-09（2026-05 为 H1 参考）"
        folder = self.folder / "text-window"
        folder.mkdir(parents=True, exist_ok=True)
        path = write_brief(folder, brief)
        result = run_cli(path, SAMPLE_RAW, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(self.errors(data), [])
        self.assertEqual(data["scope"]["观察月份"], "2026-09")
        self.assertEqual(data["scope"]["数据截止"], "2026-09-30")

    def test_boss_brief_builds_valid_snapshot(self) -> None:
        """Boss 脚本产出的正式任务书可生成通过 validate.py 的快照。"""
        folder = self.folder / "boss-brief"
        folder.mkdir()
        brief_path = folder / "brief.json"
        generated = subprocess.run(
            [sys.executable, str(MAKE_BRIEF), "--out", str(brief_path)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        result = run_cli(brief_path, SAMPLE_RAW, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(self.errors(data), [])
        self.assertEqual(data["scope"]["观察月份"], "2026-09")
        self.assertEqual(data["scope"]["数据截止"], "2026-09-30")

    def test_output_is_deterministic(self) -> None:
        """同一份任务书与原始数据得到同一份输出。"""
        self.build()
        first = self.out_path.read_bytes()
        other = self.folder / "diagnosis-again.json"
        result = run_cli(self.brief_path, SAMPLE_RAW, other)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(first, other.read_bytes())

    def test_non_csv_files_are_ignored_without_changing_snapshot(self) -> None:
        """原始目录中的 README 与临时文本文件不参与构建，快照字节保持一致。"""
        plain = copy_raw(self.folder, "raw-plain")
        noisy = copy_raw(self.folder, "raw-with-non-csv-files")
        (noisy / "README.md").write_text("这不是原始数据表。\n", encoding="utf-8")
        (noisy / "~tmp.txt").write_text("临时文本，不应被当作 CSV。\n", encoding="utf-8")

        plain_out = self.folder / "plain.json"
        noisy_out = self.folder / "noisy.json"
        plain_result = run_cli(self.brief_path, plain, plain_out)
        noisy_result = run_cli(self.brief_path, noisy, noisy_out)
        self.assertEqual(plain_result.returncode, 0, plain_result.stdout + plain_result.stderr)
        self.assertEqual(noisy_result.returncode, 0, noisy_result.stdout + noisy_result.stderr)

        self.assertEqual(noisy_out.read_bytes(), plain_out.read_bytes())

    def test_missing_column_exits_nonzero(self) -> None:
        """原始数据缺列时退出码非 0，并指出缺哪一列。"""
        raw_dir = self.folder / "raw"
        shutil.copytree(SAMPLE_RAW, raw_dir)
        revenue = raw_dir / "revenue.csv"
        lines = revenue.read_text(encoding="utf-8").splitlines()
        revenue.write_text("\n".join(["月份"] + [line.split(",")[0] for line in lines[1:]]) + "\n", encoding="utf-8")
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收入万美元", result.stdout + result.stderr)

    def test_forecast_missing_column_exits_nonzero(self) -> None:
        """forecast.csv 缺少必需列时退出码非 0，stderr 含列名。"""
        required_columns = (
            "期次",
            "整体技术人力成本万元",
            "营收业务技术人力成本万元",
            "整体折算人头",
            "营收业务折算人头",
            "运维设施成本万元",
            "收入万美元",
        )
        for missing_col in required_columns:
            with self.subTest(column=missing_col):
                raw_dir = copy_raw(self.folder, f"raw-forecast-missing-{missing_col}")
                forecast = raw_dir / "forecast.csv"
                lines = csv_lines(forecast)
                header = lines[0].split(",")
                idx = header.index(missing_col)
                write_csv_lines(
                    forecast,
                    [",".join(row.split(",")[:idx] + row.split(",")[idx + 1:]) for row in lines],
                )
                out_path = self.folder / f"diagnosis-missing-{missing_col}.json"
                result = run_cli(self.brief_path, raw_dir, out_path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("forecast.csv", result.stderr)
                self.assertIn(f"缺少必需列：{missing_col}", result.stderr)
                self.assertFalse(out_path.exists())

    def test_missing_file_exits_nonzero(self) -> None:
        """原始数据缺文件时退出码非 0。"""
        raw_dir = self.folder / "raw"
        shutil.copytree(SAMPLE_RAW, raw_dir)
        (raw_dir / "heads.csv").unlink()
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("heads.csv", result.stdout + result.stderr)

    def test_help_succeeds(self) -> None:
        """--help 正常显示命令行帮助。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--brief", result.stdout)
        self.assertIn("--raw", result.stdout)

    def test_missing_brief_argument_exits_nonzero_with_chinese_error(self) -> None:
        """缺少 --brief 时退出码非 0，并提示缺少参数。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--raw", str(SAMPLE_RAW), "--out", str(self.out_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少必需参数", result.stderr)
        self.assertIn("--brief", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_raw_argument_exits_nonzero_with_chinese_error(self) -> None:
        """缺少 --raw 时退出码非 0，并提示缺少参数。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--brief", str(self.brief_path), "--out", str(self.out_path)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少必需参数", result.stderr)
        self.assertIn("--raw", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_raw_directory_exits_nonzero_with_chinese_error(self) -> None:
        """原始数据目录不存在时退出码非 0，并给出中文错误。"""
        result = run_cli(self.brief_path, self.folder / "missing-raw", self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("原始数据目录不存在", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_brief_json_exits_nonzero_with_chinese_error(self) -> None:
        """任务书不是 JSON 时退出码非 0，并给出中文错误。"""
        invalid_brief = self.folder / "invalid-brief.json"
        invalid_brief.write_text("不是 JSON", encoding="utf-8")
        result = run_cli(invalid_brief, SAMPLE_RAW, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不是合法 JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_brief_without_rate_exits_nonzero(self) -> None:
        """任务书缺汇率时退出码非 0。"""
        folder = self.folder / "no-rate"
        folder.mkdir(parents=True, exist_ok=True)
        brief = json.loads(json.dumps(BRIEF))
        del brief["口径"]["汇率"]
        path = write_brief(folder, brief)
        raw_dir = folder / "raw"
        shutil.copytree(SAMPLE_RAW, raw_dir)
        (raw_dir / "fx.csv").unlink()
        result = run_cli(path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("汇率", result.stdout + result.stderr)

    def test_brief_without_rate_uses_fx_csv(self) -> None:
        """任务书缺汇率时从原始数据目录的 fx.csv 回落读取。"""
        folder = self.folder / "rate-from-fx"
        folder.mkdir(parents=True, exist_ok=True)
        brief = json.loads(json.dumps(BRIEF))
        del brief["口径"]["汇率"]
        path = write_brief(folder, brief)
        raw_dir = copy_raw(folder, "raw")

        result = run_cli(path, raw_dir, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertIn("汇率：7.2 人民币/美元", "\n".join(data["口径说明"]))
        self.assertEqual(
            data["metrics"][3]["数值"]["整体技术人力成本率"]["2026-01实际"],
            "约12.9%",
        )

    def test_unwritable_out_target_exits_nonzero(self) -> None:
        """--out 指向无法作为文件写出的目标时退出码非 0，stderr 报错且不留下部分 JSON。"""
        # 情形 1：--out 指向已存在的目录
        out_dir = self.folder / "existing-dir-target"
        out_dir.mkdir()
        result = run_cli(self.brief_path, SAMPLE_RAW, out_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(out_dir), result.stderr)
        self.assertIn("无法写出诊断快照", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(list(out_dir.iterdir()), [])

        # 情形 2：--out 指向父级为常规文件的非法路径
        file_as_parent = self.brief_path / "diagnosis.json"
        result2 = run_cli(self.brief_path, SAMPLE_RAW, file_as_parent)
        self.assertNotEqual(result2.returncode, 0)
        self.assertIn(str(file_as_parent), result2.stderr)
        self.assertIn("无法写出诊断快照", result2.stderr)
        self.assertNotIn("Traceback", result2.stderr)
        self.assertFalse(file_as_parent.exists())

        # 全程未在默认位置留下部分快照
        self.assertFalse(self.out_path.exists())

    def test_module_builds_snapshot_without_command_line(self) -> None:
        """模块接口可被直接调用，输出结构确定。"""
        brief = build_diagnosis.load_brief(self.brief_path)
        plan = build_diagnosis.parse_plan(brief, SAMPLE_RAW)
        raw = build_diagnosis.load_raw(SAMPLE_RAW)
        snapshot = build_diagnosis.build_snapshot(plan, raw)
        self.assertEqual(self.errors(snapshot), [])
        self.assertEqual(
            [metric["标题"] for metric in snapshot["metrics"]],
            [metric["标题"] for metric in self.build()["metrics"]],
        )

    def test_recommendation_units_follow_both_income_values(self) -> None:
        """年末测算与年末目标的收入数值各自带万美元/月单位。"""
        brief = build_diagnosis.load_brief(self.brief_path)
        plan = build_diagnosis.parse_plan(brief, SAMPLE_RAW)
        raw = build_diagnosis.load_raw(SAMPLE_RAW)
        facts = build_diagnosis.build_facts(plan, raw)
        recommendations = build_diagnosis.build_recommendations(plan, facts, raw)

        recommendation = next(
            item for item in recommendations if item["标题"] == "核对年末测算与年末目标的口径差异"
        )
        basis = recommendation["依据"]
        self.assertIn("年末测算 14.8 万美元/月", basis)
        self.assertIn("年末目标 15.2 万美元/月", basis)

    def test_answer_question_revops_cost_and_heads_share(self) -> None:
        """必答问题涉及营收业务与占比/比例时，回答包含成本与人头数值及明确的成本占比与人头占比数字。"""
        brief = json.loads(json.dumps(BRIEF))
        question = "营收业务（含共享平台）的技术人力成本与折算人头分别是多少？占整体口径的比例是多少？"
        brief["必答问题"] = [question]
        folder = self.folder / "revops-shares"
        folder.mkdir(parents=True, exist_ok=True)
        brief_path = write_brief(folder, brief)
        out_path = folder / "diagnosis.json"
        result = run_cli(brief_path, SAMPLE_RAW, out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(self.errors(data), [])

        target_insights = [item for item in data["insights"] if item["标题"] == question]
        self.assertEqual(len(target_insights), 1)
        insight = target_insights[0]
        self.assertEqual(insight["类型"], "事实")
        content = insight["内容"]

        # 验证包含营收业务成本数值、人头数值
        self.assertIn("8.3 → 9.9 万元/月", content)
        self.assertIn("8.3 → 9.9", content)
        # 验证明确包含成本占比与人头占比数字
        self.assertIn("成本占比 79.8%", content)
        self.assertIn("人头占比 79.8%", content)

    def test_answer_question_revops_shares_calculation_direct(self) -> None:
        """直接调用 answer_question 验证占比计算准确性及关键词命中（占比与比例）。"""
        facts = {
            "overall_cost": {"start": 10.0, "end": 20.0},
            "overall_heads": {"start": 40.0, "end": 80.0},
            "revops_cost": {"start": 4.0, "end": 5.0},
            "revops_heads": {"start": 10.0, "end": 24.0},
            "income": {"start": 10.0, "end": 20.0, "forecast": 25.0, "target": 30.0},
            "ratios": {"overall": {}, "revops": {}, "full": {}},
        }
        # 5.0 / 20.0 = 25.0%
        # 24.0 / 80.0 = 30.0%
        for q in (
            "营收业务（含共享平台）的技术人力成本与折算人头分别是多少？占整体口径的比例是多少？",
            "营收业务折算人头与成本占比是多少？",
        ):
            kind, content = build_diagnosis.answer_question(q, facts)
            self.assertEqual(kind, "事实")
            self.assertIn("4.0 → 5.0 万元/月", content)
            self.assertIn("10.0 → 24.0", content)
            self.assertIn("成本占比 25.0%", content)
            self.assertIn("人头占比 30.0%", content)

        # 边界情况：分母为 0 或 None 时写“未提供”
        zero_facts = {
            "overall_cost": {"start": 10.0, "end": 0.0},
            "overall_heads": {"start": 40.0, "end": None},
            "revops_cost": {"start": 4.0, "end": 5.0},
            "revops_heads": {"start": 10.0, "end": 24.0},
            "income": {"start": 10.0, "end": 20.0, "forecast": 25.0, "target": 30.0},
            "ratios": {"overall": {}, "revops": {}, "full": {}},
        }
        kind, content = build_diagnosis.answer_question("营收业务成本与人头占比", zero_facts)
        self.assertEqual(kind, "事实")
        self.assertIn("成本占比 未提供", content)
        self.assertIn("人头占比 未提供", content)

    def test_answer_question_rate_branch_also_answers_income(self) -> None:
        """问题同时问“率”与“收入”时，回答要给出收入起止数值与“万美元/月”单位。

        问题带“率”会命中成本率分支，若该分支不补收入数值，回答就完全漏掉收入。
        """
        facts = {
            "overall_cost": {"start": 10.4, "end": 12.4},
            "overall_heads": {"start": 10.4, "end": 12.4},
            "revops_cost": {"start": 8.3, "end": 9.9},
            "revops_heads": {"start": 8.3, "end": 9.9},
            "income": {"start": 11.2, "end": 13.6, "forecast": 14.8, "target": 15.2},
            "ratios": {
                "overall": {"start": 0.129, "end": 0.127},
                "revops": {"start": 0.103, "end": 0.101},
                "full": {"start": 0.155, "end": 0.152},
            },
        }
        question = "观察期间的收入是多少（万美元/月）？整体技术人力成本率与营收业务技术人力成本率各是多少？"
        kind, content = build_diagnosis.answer_question(question, facts)
        self.assertEqual(kind, "事实")
        self.assertIn("收入 11.2 → 13.6 万美元/月", content)
        self.assertIn("万美元/月", content)
        self.assertIn("整体技术人力成本率 约12.9% → 约12.7%", content)
        self.assertIn("营收业务技术人力成本率 约10.3% → 约10.1%", content)


class AbnormalRawDataTest(unittest.TestCase):
    """异常原始数据：缺期次、负数、重复行、CSV 多余列与重复列名。"""

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.name)
        self.brief_path = write_brief(self.folder, BRIEF)
        self.out_path = self.folder / "diagnosis.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_bytes(self, raw_dir: Path, out_path: Path) -> bytes:
        """用给定原始数据目录生成快照，返回输出字节。"""
        result = run_cli(self.brief_path, raw_dir, out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return out_path.read_bytes()

    def build_data(self, raw_dir: Path) -> dict:
        """用给定原始数据目录生成快照并返回内容，同时断言结构零错误。"""
        data = json.loads(self.build_bytes(raw_dir, self.out_path).decode("utf-8"))
        self.assertEqual(validate.validate_data(data, self.schema), [])
        return data

    def test_missing_period_row_becomes_null(self) -> None:
        """某期次整行缺失时该期次写 null，不填估算值。"""
        raw_dir = copy_raw(self.folder, "raw-missing-period")
        cost = raw_dir / "cost.csv"
        write_csv_lines(cost, [line for line in csv_lines(cost) if not line.startswith("2026-01,")])
        data = self.build_data(raw_dir)
        overall_cost = data["metrics"][0]["数值"]
        self.assertIsNone(overall_cost["2026-01实际"])
        self.assertEqual(overall_cost["2026-09实际"], 12.4)
        self.assertIn("整体技术人力成本 缺 2026-01、2026-05 数据，按 null 处理", data["口径说明"])
        titles = [item["标题"] for item in data["insights"]]
        self.assertIn("成本与收入增速关系暂不可判", titles)
        self.assertIn("成本结构暂不可判", titles)

    def test_empty_cell_becomes_null(self) -> None:
        """单元格留空表示该期次缺数据，只影响该列，其余列照常取数。"""
        raw_dir = copy_raw(self.folder, "raw-empty-cell")
        cost = raw_dir / "cost.csv"
        write_csv_lines(
            cost,
            [line.replace("2026-01,10.4,8.3,2.1", "2026-01,10.4,,2.1") for line in csv_lines(cost)],
        )
        data = self.build_data(raw_dir)
        revops_cost = data["metrics"][2]["数值"]["成本"]
        self.assertIsNone(revops_cost["2026-01实际"])
        self.assertEqual(revops_cost["2026-09实际"], 9.9)
        self.assertIn("营收业务技术人力成本 缺 2026-01、2026-05 数据，按 null 处理", data["口径说明"])

    def test_reference_period_with_overall_cost_exits_nonzero(self) -> None:
        """参考期次出现同口径整体成本时退出码非 0（schema v4 要求参考期次固定为 null）。"""
        raw_dir = copy_raw(self.folder, "raw-reference-overall")
        cost = raw_dir / "cost.csv"
        write_csv_lines(cost, csv_lines(cost) + ["2026-05,11.2,9.0,2.3"])
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema v4 要求整体口径的参考期次固定为 null", result.stdout + result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_invalid_utf8_csv_exits_nonzero_without_snapshot(self) -> None:
        """CSV 含非法 UTF-8 字节时报告读取错误，且不写出诊断快照。"""
        raw_dir = copy_raw(self.folder, "raw-invalid-utf8")
        cost = raw_dir / "cost.csv"
        cost.write_bytes(cost.read_bytes() + b"\xff\n")
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UnicodeDecodeError", result.stdout + result.stderr)
        self.assertIn("utf-8", result.stdout + result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_negative_cost_is_rejected(self) -> None:
        """负数成本应判为原始数据错误。

        成本不能为负数：负数成本会被原样写进快照，并生成负的成本率与负的成本占比，
        与“成本率=成本÷同期收入折人民币”的口径矛盾。
        """
        raw_dir = copy_raw(self.folder, "raw-negative-cost")
        cost = raw_dir / "cost.csv"
        write_csv_lines(
            cost,
            [line.replace("2026-01,10.4,8.3,2.1", "2026-01,-10.4,8.3,2.1") for line in csv_lines(cost)],
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cost.csv", result.stdout + result.stderr)

    def test_negative_heads_is_rejected(self) -> None:
        """负数折算人头应判为原始数据错误。

        折算人头不能为负数：负数人头会进入快照并影响人均成本等派生指标。
        """
        raw_dir = copy_raw(self.folder, "raw-negative-heads")
        heads = raw_dir / "heads.csv"
        write_csv_lines(
            heads,
            [line.replace("2026-01,10.4,8.3", "2026-01,-10.4,8.3") for line in csv_lines(heads)],
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("heads.csv", result.stdout + result.stderr)

    def test_negative_revenue_is_rejected(self) -> None:
        """负数收入应判为原始数据错误。

        收入不能为负数：负数收入会让成本率失去意义（成本 ÷ 负收入得到负的成本率）。
        """
        raw_dir = copy_raw(self.folder, "raw-negative-revenue")
        revenue = raw_dir / "revenue.csv"
        write_csv_lines(
            revenue,
            [line.replace("2026-01,11.2", "2026-01,-11.2") for line in csv_lines(revenue)],
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revenue.csv", result.stderr)
        self.assertIn("非负数", result.stderr)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_negative_forecast_revenue_is_rejected(self) -> None:
        """forecast.csv 的年末测算收入为负时应判为原始数据错误。

        年末测算与年末目标的收入同样不能为负数，否则年末成本率没有意义。
        """
        raw_dir = copy_raw(self.folder, "raw-negative-forecast-revenue")
        forecast = raw_dir / "forecast.csv"
        write_csv_lines(
            forecast,
            [
                line.replace("年末测算,13.4,10.7,13.4,10.7,,14.8", "年末测算,13.4,10.7,13.4,10.7,,-14.8")
                for line in csv_lines(forecast)
            ],
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forecast.csv", result.stderr)
        self.assertIn("非负数", result.stderr)
        self.assertNotIn("Traceback", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_duplicate_month_row_exits_nonzero(self) -> None:
        """同一月份重复行时报错退出。"""
        raw_dir = copy_raw(self.folder, "raw-duplicate-month")
        cost = raw_dir / "cost.csv"
        write_csv_lines(cost, csv_lines(cost) + ["2026-01,10.4,8.3,2.1"])
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("原始数据 cost.csv 中 月份 2026-01 重复", result.stdout + result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_duplicate_period_row_exits_nonzero(self) -> None:
        """forecast.csv 同一期次重复行时报错退出。"""
        raw_dir = copy_raw(self.folder, "raw-duplicate-period")
        forecast = raw_dir / "forecast.csv"
        write_csv_lines(forecast, csv_lines(forecast) + ["年末测算,13.4,10.7,13.4,10.7,,14.8"])
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("原始数据 forecast.csv 中 期次 年末测算 重复", result.stdout + result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_row_with_extra_field_exits_nonzero(self) -> None:
        """数据行的字段数多于表头时报错退出。"""
        raw_dir = copy_raw(self.folder, "raw-extra-field")
        cost = raw_dir / "cost.csv"
        write_csv_lines(
            cost,
            [line.replace("2026-01,10.4,8.3,2.1", "2026-01,10.4,8.3,2.1,备注") for line in csv_lines(cost)],
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("原始数据 cost.csv 第 2 行的字段数多于表头", result.stdout + result.stderr)
        self.assertFalse(self.out_path.exists())

    def test_extra_column_in_header_is_ignored(self) -> None:
        """表头多出一列时被忽略，输出与不含多余列时一致。"""
        plain = copy_raw(self.folder, "raw-plain")
        extra = copy_raw(self.folder, "raw-extra-column")
        cost = extra / "cost.csv"
        lines = csv_lines(cost)
        lines[0] = lines[0] + ",备注"
        lines[1] = lines[1] + ",合成备注"
        write_csv_lines(cost, lines)
        first = self.build_bytes(plain, self.folder / "plain.json")
        second = self.build_bytes(extra, self.folder / "extra.json")
        self.assertEqual(first, second)

    def test_quoted_text_with_comma_and_quote_keeps_numeric_columns(self) -> None:
        """合法 CSV 的文本列含逗号和引号时，数值列仍按列名读取并通过 schema。"""
        plain = copy_raw(self.folder, "raw-plain-quoted-text")
        quoted = copy_raw(self.folder, "raw-quoted-text")
        cost = quoted / "cost.csv"
        lines = csv_lines(cost)
        lines[0] = "月份,备注,整体技术人力成本万元,营收业务技术人力成本万元,运维设施成本万元"
        lines[1] = '2026-01,"研发, ""平台"" 团队",10.4,8.3,2.1'
        lines[2:] = [
            f'{line.split(",", 1)[0]},"普通备注",{line.split(",", 1)[1]}'
            for line in lines[2:]
        ]
        write_csv_lines(cost, lines)

        first = self.build_data(plain)
        second = self.build_data(quoted)
        self.assertEqual(second, first)

    def test_duplicated_column_name_is_rejected(self) -> None:
        """表头出现重复列名时报错退出，并指出文件与重复的列名。"""
        raw_dir = copy_raw(self.folder, "raw-duplicated-column")
        cost = raw_dir / "cost.csv"
        lines = csv_lines(cost)
        lines[0] = "月份,整体技术人力成本万元,整体技术人力成本万元,营收业务技术人力成本万元,运维设施成本万元"
        lines[1] = "2026-01,10.4,99.9,8.3,2.1"
        write_csv_lines(cost, lines)
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cost.csv", result.stdout + result.stderr)
        self.assertIn("整体技术人力成本万元", result.stdout + result.stderr)
        self.assertFalse(self.out_path.exists())


class ColumnOrderTest(unittest.TestCase):
    """原始 CSV 列顺序变化：按列名取数，快照与原顺序等价；缺列仍报出文件。"""

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.name)
        self.brief_path = write_brief(self.folder, BRIEF)
        self.out_path = self.folder / "diagnosis.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_reordered_raw(self, name: str) -> Path:
        """复制合成原始数据，并按 REORDERED_COLUMNS 重排各表的列顺序。"""
        raw_dir = copy_raw(self.folder, name)
        for filename, order in REORDERED_COLUMNS.items():
            path = raw_dir / filename
            path.write_text(
                reorder_csv_columns(path.read_text(encoding="utf-8"), list(order)),
                encoding="utf-8",
            )
        return raw_dir

    def test_reordered_columns_build_equivalent_snapshot(self) -> None:
        """列顺序变化后仍按列名取数，生成的快照与原顺序逐字节一致。"""
        plain = copy_raw(self.folder, "raw-plain-order")
        reordered = self.write_reordered_raw("raw-reordered-columns")

        for filename, order in REORDERED_COLUMNS.items():
            with self.subTest(file=filename):
                original_header = csv_lines(plain / filename)[0].split(",")
                new_header = csv_lines(reordered / filename)[0].split(",")
                self.assertNotEqual(new_header, original_header)
                self.assertEqual(sorted(new_header), sorted(original_header))
                self.assertEqual(new_header, list(order))

        plain_out = self.folder / "plain.json"
        reordered_out = self.folder / "reordered.json"
        plain_result = run_cli(self.brief_path, plain, plain_out)
        reordered_result = run_cli(self.brief_path, reordered, reordered_out)
        self.assertEqual(plain_result.returncode, 0, plain_result.stdout + plain_result.stderr)
        self.assertEqual(reordered_result.returncode, 0, reordered_result.stdout + reordered_result.stderr)

        data = json.loads(reordered_out.read_text(encoding="utf-8"))
        self.assertEqual(validate.validate_data(data, self.schema), [])
        self.assertEqual(data["metrics"][0]["数值"]["2026-01实际"], 10.4)
        self.assertEqual(data["metrics"][2]["数值"]["成本"]["2026-09实际"], 9.9)
        self.assertEqual(data["metrics"][3]["数值"]["收入万美元每月"]["年末目标"], 15.2)
        self.assertEqual(reordered_out.read_bytes(), plain_out.read_bytes())

    def test_missing_required_column_after_reorder_reports_source_file(self) -> None:
        """列顺序变化后缺少必需列仍失败，错误信息点出缺列与来源文件。"""
        raw_dir = self.write_reordered_raw("raw-reordered-missing-column")
        cost = raw_dir / "cost.csv"
        cost.write_text(
            drop_csv_column(cost.read_text(encoding="utf-8"), "运维设施成本万元"),
            encoding="utf-8",
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        message = result.stdout + result.stderr
        self.assertIn("cost.csv", message)
        self.assertIn("缺少必需列：运维设施成本万元", message)
        self.assertNotIn("KeyError", message)
        self.assertNotIn("Traceback", message)
        self.assertFalse(self.out_path.exists())

    def test_missing_required_column_after_reorder_raises_raw_data_error(self) -> None:
        """模块接口在列顺序变化且缺列时抛 RawDataError，消息里带文件名与列名。"""
        raw_dir = self.write_reordered_raw("raw-reordered-missing-column-module")
        cost = raw_dir / "cost.csv"
        cost.write_text(
            drop_csv_column(cost.read_text(encoding="utf-8"), "整体技术人力成本万元"),
            encoding="utf-8",
        )
        with self.assertRaises(build_diagnosis.RawDataError) as caught:
            build_diagnosis.build_from_files(self.brief_path, raw_dir)
        message = str(caught.exception)
        self.assertIn("cost.csv", message)
        self.assertIn("缺少必需列：整体技术人力成本万元", message)


class MinimalRawInputErrorTest(unittest.TestCase):
    """最小 raw fixture：必需数字列为空、CSV 出现重复列名与缺少必需列三种输入错误。

    fixture 只有起止两个月与年末两行，其余按必需表头写全，用来断言输入错误路径，
    不依赖 `da/sample-raw/` 的合成样本。
    """

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.temporary = temporary_directory()
        self.folder = Path(self.temporary.name)
        self.brief_path = write_brief(self.folder, BRIEF)
        self.out_path = self.folder / "diagnosis.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_minimal_raw(self, name: str, files: dict[str, str] | None = None) -> Path:
        """写出最小 raw 目录；`files` 里给出要覆盖的 CSV 文件名与文本。"""
        raw_dir = self.folder / name
        raw_dir.mkdir(parents=True, exist_ok=True)
        content = dict(MINIMAL_RAW_FILES)
        content.update(files or {})
        for filename, text in content.items():
            (raw_dir / filename).write_text(text, encoding="utf-8")
        return raw_dir

    def test_minimal_raw_builds_valid_snapshot(self) -> None:
        """最小 raw fixture 本身可生成零结构错误的诊断快照。"""
        raw_dir = self.write_minimal_raw("raw-minimal")
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(validate.validate_data(data, self.schema), [])

    def test_required_numeric_column_empty_exits_nonzero(self) -> None:
        """必需数字列为空时退出码非 0，并指出文件与列名。

        “列为空”有两种写法：表头里列名写空（该列没有名字，等同缺列），
        以及数据行字段数少于表头（该列在行里没有取值）。两者都按输入错误处理。
        """
        cases = {
            "empty-header-name": (
                "表头里必需数字列的列名写空",
                "月份,,营收业务技术人力成本万元,运维设施成本万元\n2026-01,10.4,8.3,2.1\n",
            ),
            "no-value-in-row": (
                "数据行字段数少于表头，该列在行里没有取值",
                "月份,整体技术人力成本万元,营收业务技术人力成本万元,运维设施成本万元\n2026-01\n",
            ),
        }
        for slug, (label, text) in cases.items():
            with self.subTest(scenario=label):
                raw_dir = self.write_minimal_raw(f"raw-empty-column-{slug}", {"cost.csv": text})
                result = run_cli(self.brief_path, raw_dir, self.out_path)
                self.assertNotEqual(result.returncode, 0)
                message = result.stdout + result.stderr
                self.assertIn("cost.csv", message)
                self.assertIn("整体技术人力成本万元", message)
                self.assertFalse(self.out_path.exists())

    def test_required_column_name_of_whitespace_exits_nonzero(self) -> None:
        """必需列名仅含空白字符时退出码非 0，并指出文件与具体列名。"""
        whitespace_header = (
            "月份,   ,营收业务技术人力成本万元,运维设施成本万元\n"
            "2026-01,10.4,8.3,2.1\n"
        )
        raw_dir = self.write_minimal_raw(
            "raw-whitespace-column-name", {"cost.csv": whitespace_header}
        )
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        message = result.stdout + result.stderr
        self.assertIn("cost.csv", message)
        self.assertIn("整体技术人力成本万元", message)
        self.assertFalse(self.out_path.exists())

    def test_duplicate_column_name_exits_nonzero(self) -> None:
        """CSV 出现重复列名时退出码非 0，并指出文件与重复的列名。"""
        duplicated = (
            "月份,整体技术人力成本万元,整体技术人力成本万元,营收业务技术人力成本万元,运维设施成本万元\n"
            "2026-01,10.4,99.9,8.3,2.1\n"
        )
        raw_dir = self.write_minimal_raw("raw-duplicated-column", {"cost.csv": duplicated})
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        message = result.stdout + result.stderr
        self.assertIn("cost.csv", message)
        self.assertIn("整体技术人力成本万元", message)
        self.assertFalse(self.out_path.exists())

    def test_missing_required_column_exits_nonzero_with_column_and_file(self) -> None:
        """删掉必需列后构建失败，错误信息点出缺失列名与输入文件。

        四张 CSV 各删一个必需列：失败信息必须写明缺哪一列、来自哪个文件，
        不能只留通用 KeyError 或裸 Traceback。
        """
        cases = (
            ("cost.csv", "运维设施成本万元"),
            ("heads.csv", "营收业务折算人头"),
            ("revenue.csv", "收入万美元"),
            ("forecast.csv", "整体技术人力成本万元"),
        )
        for filename, column in cases:
            with self.subTest(file=filename, column=column):
                raw_dir = self.write_minimal_raw(
                    f"raw-missing-column-{filename}",
                    {filename: drop_csv_column(MINIMAL_RAW_FILES[filename], column)},
                )
                result = run_cli(self.brief_path, raw_dir, self.out_path)
                self.assertNotEqual(result.returncode, 0)
                message = result.stdout + result.stderr
                self.assertIn(filename, message)
                self.assertIn(f"缺少必需列：{column}", message)
                self.assertNotIn("KeyError", message)
                self.assertNotIn("Traceback", message)
                self.assertFalse(self.out_path.exists())

    def test_missing_required_column_raises_readable_raw_data_error(self) -> None:
        """模块接口在缺列时抛 RawDataError，消息里带文件名与列名。"""
        raw_dir = self.write_minimal_raw(
            "raw-missing-column-module",
            {"cost.csv": drop_csv_column(MINIMAL_COST_CSV, "整体技术人力成本万元")},
        )
        with self.assertRaises(build_diagnosis.RawDataError) as caught:
            build_diagnosis.build_from_files(self.brief_path, raw_dir)
        message = str(caught.exception)
        self.assertIn("cost.csv", message)
        self.assertIn("缺少必需列：整体技术人力成本万元", message)


if __name__ == "__main__":
    unittest.main()
