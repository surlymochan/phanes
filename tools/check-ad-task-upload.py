#!/usr/bin/env python3
"""Offline contract check for autodispatch task-file uploads.

The task must cross the ProxyJump boundary through ssh stdin.  A fake task is
captured and scanned so this check does not need a live fleet host.
"""
import importlib.util
import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_task_upload", MODULE_PATH)
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

        def fake_sh(cmd, timeout=120, inp=None):
            calls.append((cmd, inp))
            if cmd[0] == "ssh":
                return 0, '{"id":"run-920"}', ""
            return 0, "", ""

        module.sh = fake_sh
        run, _ = module.dispatch(
            "lane-a", {"host": "instabox", "fleet": "fleet", "repo": "repo"},
            {"n": 920, "title": "upload contract", "body": "fake task body"}, "base",
        )
        assert run == "run-920"

        uploads = [(cmd, inp) for cmd, inp in calls if cmd[0] == "ssh"]
        assert len(uploads) == 2, uploads
        upload_cmd, task = uploads[0]
        assert upload_cmd[-1] == "cat > /tmp/task-lane-a.md", upload_cmd
        assert task and "fake task body" in task and "# #920 upload contract" in task
        assert not any(cmd[0] == "scp" for cmd, _ in calls), calls

    source = MODULE_PATH.read_text()
    assert '"scp"' not in source, "裸 scp upload remains in dispatch"
    assert "cat > /tmp/task-" in source, "ssh stdin upload contract is missing"
    print("ad task upload contract: OK")


if __name__ == "__main__":
    main()
