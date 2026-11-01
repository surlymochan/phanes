"""Regression tests for per-agent package cache isolation and leak reporting."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import report

sys.path.insert(0, str(Path(__file__).parent / "harness"))
# arms imports the benchmark's environment module, which depends on the optional
# swebench package. These tests exercise only path_env, so provide its small config API.
sys.modules.setdefault(
    "swe_env",
    types.SimpleNamespace(ROOT=Path.cwd(), GIT=["git"], visible_command=lambda inst, venv: ""),
)
import arms


class AgentCacheEnvironmentTest(unittest.TestCase):
    def test_each_run_gets_private_offline_cache_directories(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            root = Path(tmp) / "events" / "cache"
            first = arms.path_env(Path("/venv"), cache_root=root)
            second = arms.path_env(Path("/venv"), cache_root=root)

            variables = ("UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME")
            for variable in variables:
                self.assertTrue(Path(first[variable]).is_dir())
                self.assertTrue(Path(second[variable]).is_dir())
                self.assertNotEqual(first[variable], second[variable])
                self.assertNotIn(str(Path.home() / ".cache"), first[variable])
                self.assertNotIn(str(Path.home() / ".cache"), second[variable])
            self.assertEqual(first["UV_OFFLINE"], "1")
            self.assertEqual(first["PIP_NO_INDEX"], "1")
            shared_cache = str(Path.home() / ".cache")
            self.assertFalse(any(shared_cache in value for value in first.values()))
            self.assertFalse(any(shared_cache in value for value in second.values()))
            self.assertTrue(all(not list(Path(first[v]).iterdir()) for v in variables))
            self.assertTrue(all(not list(Path(second[v]).iterdir()) for v in variables))

    def test_report_counts_shared_cache_reads(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp:
            events = Path(tmp) / "raw" / "baseline" / "instance-1" / "r1" / "agent-events.jsonl"
            events.parent.mkdir(parents=True)
            calls = [
                {"type": "tool_execution_start", "command": "ls ~/.cache/uv"},
                {"type": "tool_execution_start", "command": "cat ~/.cache/pip/wheels"},
                {"type": "tool_execution_start", "command": "pytest -q"},
            ]
            events.write_text("".join(json.dumps(call) + "\n" for call in calls))

            scan = report.leak_scan(events.parents[3])

            self.assertEqual(scan["cache_reads"], 2)
            self.assertEqual(scan["hits"], {})


if __name__ == "__main__":
    unittest.main()
