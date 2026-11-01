import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "check-org-health-privacy.py"
TMP_ROOT = ROOT / ".openswarm-tmp"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_org_health_privacy", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrivacyCheckerTests(unittest.TestCase):
    def test_violation_sample_reports_file_and_line(self):
        checker = load_checker()
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as raw_dir:
            root = pathlib.Path(raw_dir)
            (root / "data").mkdir()
            (root / "notes.txt").write_text("安全说明\n联系 test@example.com\n", encoding="utf-8")
            (root / "data" / "sample.json").write_text(json.dumps({"value": 1}), encoding="utf-8")
            findings = checker.check(root)
        self.assertTrue(any("notes.txt:2" in item and "邮箱地址" in item for item in findings))
        self.assertTrue(any("sample.json:1" in item and "synthetic" in item for item in findings))

    def test_compliant_sample_has_no_findings(self):
        checker = load_checker()
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as raw_dir:
            root = pathlib.Path(raw_dir)
            (root / "data").mkdir()
            (root / "notes.txt").write_text("这是不含真实数据的合成示例。\n", encoding="utf-8")
            (root / "data" / "sample.json").write_text(
                json.dumps({"synthetic": True, "value": 1}), encoding="utf-8"
            )
            self.assertEqual(checker.check(root), [])


if __name__ == "__main__":
    unittest.main()
