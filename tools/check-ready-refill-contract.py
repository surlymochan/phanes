#!/usr/bin/env python3
"""Offline contract check for autodispatch low-queue refill messages.

The check uses a temporary autodispatch directory and replaces only the
external GitHub/git calls.  It never reads or writes the live backlog.
"""
import importlib.util
import json
import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_contract", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tick(module, temp_dir, gh, shell, replenish=None):
    module.D = str(temp_dir)
    module.BRAIN_LOG = str(temp_dir / "brain.log")
    module.queue = lambda: []
    module.gh = gh
    module.sh = shell
    if replenish is not None:
        module.replenish = replenish
    module.tick({}, {}, 0)
    return (temp_dir / "autodispatch.log").read_text()


def main():
    module = load_module()
    scratch_root = ROOT / ".openswarm-tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw_dir:
        temp_dir = pathlib.Path(raw_dir)
        backlog = temp_dir / "backlog"
        backlog.mkdir()
        (backlog / "local.md").write_text("# Local card\nlabels: p1\n\nBody\n")

        filed = []

        def file_issue(text, extra=()):
            filed.append(text.splitlines()[0])
            return "#501"

        module.file_issue = file_issue
        module.D = str(temp_dir)
        module.BRAIN_LOG = str(temp_dir / "brain.log")
        module.gh = lambda args, timeout=60: (
            (0, json.dumps([{"number": 42}]), "")
            if args[:3] == ["issue", "list", "--state"]
            else (0, "", "")
        )
        module.sh = lambda *args, **kwargs: (0, "", "")
        local = run_tick(module, temp_dir, module.gh, module.sh)
        assert filed == ["# Local card"], filed
        assert "filed 1 issue(s)" in local, local

        empty_dir = temp_dir / "empty"
        (empty_dir / "backlog").mkdir(parents=True)

        def planner_exists(args, timeout=60):
            if args[:3] == ["issue", "list", "--state"]:
                return 0, json.dumps([{"number": 42}]), ""
            return 0, "", ""

        empty = run_tick(
            module,
            empty_dir,
            planner_exists,
            lambda *args, **kwargs: (0, "", ""),
        )
        assert "WARNING queue low" in empty, empty

    print("ready refill contract: OK")


if __name__ == "__main__":
    main()
