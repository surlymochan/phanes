"""build_diagnosis.py 的单元测试。

用 `da/sample-raw/` 下的合成原始数据加一个内嵌的最小任务书生成快照，
再用 `validate.py` 的校验函数断言零错误；缺列时命令行退出码非 0。

异常原始数据（缺期次、负数、重复行、CSV 多余列）按当前行为断言：已支持的情形断言
退出码与错误信息，已知 bug 用 `unittest.expectedFailure` 标注并写明原因，不改实现。

输入错误（必需数字列为空、CSV 出现重复列名）另用最小 raw fixture 覆盖：命令行退出码
非 0，错误信息指出文件与列名。
"""
from __future__ import annotations

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

    def test_missing_file_exits_nonzero(self) -> None:
        """原始数据缺文件时退出码非 0。"""
        raw_dir = self.folder / "raw"
        shutil.copytree(SAMPLE_RAW, raw_dir)
        (raw_dir / "heads.csv").unlink()
        result = run_cli(self.brief_path, raw_dir, self.out_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("heads.csv", result.stdout + result.stderr)

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
        self.assertTrue("IsADirectoryError" in result.stderr or "Is a directory" in result.stderr)
        self.assertEqual(list(out_dir.iterdir()), [])

        # 情形 2：--out 指向父级为常规文件的非法路径
        file_as_parent = self.brief_path / "diagnosis.json"
        result2 = run_cli(self.brief_path, SAMPLE_RAW, file_as_parent)
        self.assertNotEqual(result2.returncode, 0)
        self.assertIn(str(file_as_parent), result2.stderr)
        self.assertTrue("NotADirectoryError" in result2.stderr or "Not a directory" in result2.stderr)
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

    @unittest.expectedFailure
    def test_negative_cost_is_rejected(self) -> None:
        """负数成本应判为原始数据错误。

        已知 bug：当前实现只校验数值是否有限，不校验符号（da/build_diagnosis.py:283-295）。
        -10.4 会被原样写进快照，并生成负的成本率（约-12.9%）与负的成本占比（-79.8%），
        与“成本率=成本÷同期收入折人民币”的口径矛盾。修复后本用例应改回普通用例。
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


class MinimalRawInputErrorTest(unittest.TestCase):
    """最小 raw fixture：必需数字列为空与 CSV 出现重复列名两种输入错误。

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
        content = {
            "cost.csv": MINIMAL_COST_CSV,
            "heads.csv": MINIMAL_HEADS_CSV,
            "revenue.csv": MINIMAL_REVENUE_CSV,
            "forecast.csv": MINIMAL_FORECAST_CSV,
        }
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


if __name__ == "__main__":
    unittest.main()
