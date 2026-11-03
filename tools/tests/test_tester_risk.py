"""tester-risk.py 的数据读取与类型处理（Refs #1854）。

夹具是真实 PR 的 `gh pr view --json ...` / `gh issue view --json ...` 原样输出：
files 是对象数组、reviews 是空数组、comments 里带 `<!-- lane-review pr= -->` 标记。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tester-risk.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
spec = importlib.util.spec_from_file_location("tester_risk", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def real_pr(number):
    """真实 PR 的 gh 输出（副本，可安全改写）。"""
    return load(f"tester-risk-pr-{number}.json")


def real_issue(number):
    return load(f"tester-risk-issue-{number}.json")


def variant(number, lane_rounds=None, files=None, drop_needs_human=True, **changes):
    """从真实样本派生变体：只保留前 N 个 lane-review 评论、截断文件数、去掉 needs-human。"""
    pr = real_pr(number)
    if drop_needs_human:
        pr["labels"] = [x for x in pr["labels"] if x.get("name") != "needs-human"]
    if files is not None:
        pr["files"] = pr["files"][:files]
    if lane_rounds is not None:
        kept, seen = [], 0
        for comment in pr["comments"]:
            if "<!-- lane-review" in (comment.get("body") or ""):
                if seen >= lane_rounds:
                    continue
                seen += 1
            kept.append(comment)
        pr["comments"] = kept
    pr.update(changes)
    return pr


class RealSampleTests(unittest.TestCase):
    """三个真实样本（#1680 / #1647 / #1743）都能跑通并给出风险结论。"""

    def test_real_pr_1680_is_full_read(self):
        result = module.classify(real_pr(1680), real_issue(1667))
        self.assertEqual(result["level"], "全读")
        self.assertEqual(result["sampling"], "100%")
        self.assertTrue(any("review 轮次 4" in reason for reason in result["reasons"]))
        self.assertTrue(any("needs-human" in reason for reason in result["reasons"]))

    def test_real_pr_1647_is_full_read(self):
        result = module.classify(real_pr(1647), real_issue(1392))
        self.assertEqual(result["level"], "全读")
        self.assertTrue(any("大 diff" in reason for reason in result["reasons"]))
        self.assertTrue(any("review 轮次 4" in reason for reason in result["reasons"]))

    def test_real_pr_1743_is_full_read(self):
        result = module.classify(real_pr(1743), real_issue(1721))
        self.assertEqual(result["level"], "全读")
        self.assertTrue(any("大 diff: 20 文件" in reason for reason in result["reasons"]))
        self.assertTrue(any("lane-failed" in reason for reason in result["reasons"]))


class GhShapeTests(unittest.TestCase):
    """gh 输出的真实形态：files 是对象数组、reviews 为空、轮次在 lane-review 评论里。"""

    def test_files_are_object_arrays_not_counts(self):
        pr = real_pr(1743)
        self.assertIsInstance(pr["files"], list)
        self.assertIsInstance(pr["files"][0], dict)
        result = module.classify(pr, real_issue(1721))
        self.assertTrue(any("20 文件" in reason for reason in result["reasons"]))

    def test_empty_reviews_do_not_hide_lane_review_rounds(self):
        pr = real_pr(1680)
        self.assertEqual(pr["reviews"], [])
        result = module.classify(pr, real_issue(1667))
        self.assertTrue(any("review 轮次 4 >= 4" in reason for reason in result["reasons"]))
        self.assertTrue(any("lane-review 评论" in reason for reason in result["reasons"]))

    def test_round_threshold_is_inclusive(self):
        below = module.classify(variant(1680, lane_rounds=3, files=1), real_issue(1667))
        self.assertEqual(below["level"], "抽检")
        self.assertEqual(below["sampling"], "20%")
        at_threshold = module.classify(variant(1680, lane_rounds=4, files=1), real_issue(1667))
        self.assertEqual(at_threshold["level"], "全读")

    def test_file_count_threshold_is_inclusive(self):
        small = dict(additions=10, deletions=5)
        below = module.classify(variant(1743, lane_rounds=0, files=19, **small), real_issue(1721))
        self.assertFalse(any("大 diff" in reason for reason in below["reasons"]))
        at_threshold = module.classify(variant(1743, lane_rounds=0, files=20, **small), real_issue(1721))
        self.assertTrue(any("大 diff: 20 文件" in reason for reason in at_threshold["reasons"]))

    def test_churn_threshold_is_inclusive(self):
        pr = variant(1743, lane_rounds=0, files=1, additions=999, deletions=1)
        result = module.classify(pr, real_issue(1721))
        self.assertTrue(any("大 diff: 1 文件, +999/-1 行" in reason for reason in result["reasons"]))


class GapReportingTests(unittest.TestCase):
    """数据缺失要显式标记降级，不能静默按 0 处理。"""

    def test_missing_round_data_is_reported_as_gap(self):
        pr = variant(1680, files=1)
        pr.pop("comments")
        pr.pop("reviews")
        result = module.classify(pr, real_issue(1667))
        self.assertEqual(result["level"], "抽检")
        self.assertTrue(any("review 轮次数据缺失" in reason for reason in result["reasons"]))

    def test_missing_comments_falls_back_to_gh_reviews_and_stays_degraded(self):
        pr = variant(1680, files=1)
        pr.pop("comments")
        result = module.classify(pr, real_issue(1667))
        self.assertTrue(any("review 轮次数据缺失" in reason for reason in result["reasons"]))
        self.assertTrue(any("gh reviews 兜底 0" in reason for reason in result["reasons"]))

    def test_missing_issue_is_reported_as_gap(self):
        result = module.classify(variant(1680, lane_rounds=0, files=1), None)
        self.assertEqual(result["level"], "抽检")
        self.assertTrue(any("关联 issue 失败类型数据缺失" in reason for reason in result["reasons"]))

    def test_missing_diff_data_is_reported_as_gap(self):
        pr = variant(1680, lane_rounds=0, files=1)
        pr["additions"] = None
        result = module.classify(pr, real_issue(1667))
        self.assertEqual(result["level"], "抽检")
        self.assertTrue(any("diff 数据缺失" in reason for reason in result["reasons"]))


GH_STUB = '''#!/usr/bin/env python3
"""gh stub：回放录制好的真实 gh 输出，让实时读取路径在 CI 无网络时也能覆盖。"""
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if os.environ.get("TESTER_RISK_GH_FAIL"):
    sys.stderr.write("gh: simulated failure\\n")
    raise SystemExit(1)
fixtures = Path(os.environ["TESTER_RISK_FIXTURES"])
with Path(os.environ["TESTER_RISK_GH_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
kind = "pr" if args[0] == "pr" else "issue"
sys.stdout.write((fixtures / f"tester-risk-{kind}-{args[2]}.json").read_text(encoding="utf-8"))
'''


class CliTests(unittest.TestCase):
    """CLI 入口：离线 --input 走真实样本，输出合法 JSON 且退出码为 0。"""

    def run_cli(self, pr_number, issue_number, extra=()):
        with tempfile.TemporaryDirectory(prefix="tester-risk-") as tmp:
            payload = Path(tmp) / "input.json"
            payload.write_text(json.dumps({"pr": real_pr(pr_number),
                                           "issue": real_issue(issue_number)}), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SCRIPT), str(pr_number), "--input", str(payload),
                                   *extra], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_cli_on_real_samples(self):
        for pr_number, issue_number in ((1680, 1667), (1647, 1392), (1743, 1721)):
            with self.subTest(pr=pr_number):
                result = self.run_cli(pr_number, issue_number)
                self.assertEqual(result["pr"], pr_number)
                self.assertEqual(result["level"], "全读")
                self.assertTrue(result["reasons"])

    def test_cli_survives_unexpected_file_shape(self):
        with tempfile.TemporaryDirectory(prefix="tester-risk-") as tmp:
            payload = Path(tmp) / "input.json"
            payload.write_text(json.dumps({"pr": {"files": "not-a-list"}}), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SCRIPT), "1680", "--input", str(payload)],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("diff 数据缺失", json.loads(proc.stdout)["reasons"])

    def test_cli_reports_broken_input_without_traceback(self):
        with tempfile.TemporaryDirectory(prefix="tester-risk-") as tmp:
            payload = Path(tmp) / "input.json"
            payload.write_text("{not json", encoding="utf-8")
            proc = subprocess.run([sys.executable, str(SCRIPT), "1680", "--input", str(payload)],
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("tester-risk:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class LivePathTests(unittest.TestCase):
    """不带 --input 的实时读取路径：gh stub 回放真实 gh 输出，校验退出码与风险结论。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tester-risk-live-")
        self.addCleanup(self.tmp.cleanup)
        self.bindir = Path(self.tmp.name) / "bin"
        self.bindir.mkdir()
        self.stub = self.bindir / "gh"
        self.stub.write_text(GH_STUB, encoding="utf-8")
        self.stub.chmod(0o755)
        self.log = Path(self.tmp.name) / "gh.log"

    def run_cli(self, pr_number, extra_env=None):
        env = dict(os.environ, PATH=f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
                   TESTER_RISK_FIXTURES=str(FIXTURES), TESTER_RISK_GH_LOG=str(self.log))
        env.pop("TESTER_RISK_GH_FAIL", None)
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(SCRIPT), str(pr_number)],
                              capture_output=True, text=True, env=env)

    def test_live_cli_on_real_samples(self):
        for pr_number in (1680, 1647, 1743):
            with self.subTest(pr=pr_number):
                proc = self.run_cli(pr_number)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertEqual(result["pr"], pr_number)
                self.assertEqual(result["level"], "全读")
                self.assertEqual(result["sampling"], "100%")
                self.assertTrue(result["reasons"])

    def test_live_path_reads_comments_and_closing_issue(self):
        proc = self.run_cli(1680)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.log.read_text(encoding="utf-8").splitlines()
        pr_call = next(c for c in calls if c.startswith("pr view 1680"))
        self.assertIn("comments", pr_call)
        self.assertIn("closingIssuesReferences", pr_call)
        self.assertTrue(any(c.startswith("issue view 1667") for c in calls))
        reasons = json.loads(proc.stdout)["reasons"]
        self.assertTrue(any("review 轮次 4 >= 4（lane-review 评论）" in r for r in reasons))

    def test_live_path_gh_failure_exits_two_without_traceback(self):
        proc = self.run_cli(1680, {"TESTER_RISK_GH_FAIL": "1"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("tester-risk:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
