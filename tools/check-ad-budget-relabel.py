#!/usr/bin/env python3
"""Offline #987 contract check for budget-exceeded lane relabelling."""
import importlib.util
import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_budget_relabel", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = load_module()
    scratch_root = ROOT / ".openswarm-tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw_dir:
        module.D = raw_dir
        calls = []

        def fake_gh(args):
            calls.append(args)
            return 0, "", ""

        module.gh = fake_gh
        module.log = lambda *args, **kwargs: None
        assert module.finish(
            "lane-a",
            {"host": "instabox"},
            {"issue": 987, "run": "run-987"},
            {"status": "budget_exceeded", "error_class": "budget_exceeded", "summary": "budget"},
            {},
        )

        relabel = [call for call in calls if call[:2] == ["issue", "edit"]]
    assert relabel == [[
        "issue", "edit", "987",
        "--remove-label", "in-progress",
        "--remove-label", "lane-ready",
        "--add-label", "lane-failed",
    ]], relabel
    print("AD budget relabel contract: OK")


if __name__ == "__main__":
    main()
