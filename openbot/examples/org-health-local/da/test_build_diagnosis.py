"""build_diagnosis.py 的单元测试。

用 `da/sample-raw/` 下的合成原始数据加一个内嵌的最小任务书生成快照，
再用 `validate.py` 的校验函数断言零错误；缺列时命令行退出码非 0。
"""
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


if __name__ == "__main__":
    unittest.main()
