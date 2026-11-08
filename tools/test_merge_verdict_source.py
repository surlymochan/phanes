"""Contract tests for the merge loop's local tester verdict control."""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "bin" / "merge-loop.sh"
GOOD = {"enabled": True, "shadow_samples": 30, "tester_accuracy": 0.9,
        "lane_accuracy": 0.9, "boss_confirmed": True}


def run_loop(tmp_path, config=None, heads=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    (bin_dir / "gh").write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 1}, {"number": 2}, {"number": 3}]))
elif args[:2] == ["pr", "view"]:
    print(json.loads(os.environ["HEADS"])[args[2]])
''')
    (bin_dir / "gh").chmod(0o755)
    config_path = tmp_path / "control.json"
    if config is not None:
        config_path.write_text(config if isinstance(config, str) else json.dumps(config))
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CALLS=str(log),
               HEADS=json.dumps(heads or {"1": "abc", "2": "def", "3": "ghi"}),
               MERGE_VERDICT_CONFIG=str(config_path), MERGE_LOOP_ONCE="1",
               MERGE_HEARTBEAT=str(tmp_path / "heartbeat"))
    subprocess.run(["bash", str(LOOP)], env=env, check=True, capture_output=True, text=True)
    return [json.loads(line) for line in log.read_text().splitlines()]


def merged(calls):
    return [call[2] for call in calls if call[:2] == ["pr", "merge"]]


@pytest.mark.parametrize("config", [
    None,
    {**GOOD, "enabled": False},
    {**GOOD, "shadow_samples": 29},
    {**GOOD, "tester_accuracy": 0.8},
    {key: value for key, value in GOOD.items() if key != "boss_confirmed"},
    {**GOOD, "tester_accuracy": True},
    {**GOOD, "lane_accuracy": -1},
    "{invalid json",
])
def test_inactive_control_preserves_existing_merge_path(tmp_path, config):
    calls = run_loop(tmp_path, config)
    assert merged(calls) == ["1", "2", "3"]
    assert not any(call[:2] == ["pr", "view"] for call in calls)


def test_activated_control_accepts_only_current_tester_pass(tmp_path):
    config = {**GOOD, "verdicts": {
        "1": {"verdict": "PASS", "head_sha": "abc"},
        "2": {"verdict": "CHANGES", "head_sha": "def"},
        "3": {"verdict": "PASS", "head_sha": "old"},
    }}
    calls = run_loop(tmp_path, config)
    assert merged(calls) == ["1"]
    assert [call for call in calls if call[:2] == ["pr", "merge"]][0][-2:] == [
        "--match-head-commit", "abc"]


def test_activated_control_missing_verdict_blocks_merge(tmp_path):
    assert merged(run_loop(tmp_path, GOOD)) == []


def test_activated_control_blocks_failed_head_lookup(tmp_path):
    config = {**GOOD, "verdicts": {"1": {"verdict": "PASS", "head_sha": "abc"}}}
    assert merged(run_loop(tmp_path, config, heads={"2": "def"})) == []
