"""Memory probe output, per-host dispatch guard and brain log alerts (Refs #1672)."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("resources_memory", HERE.parent / "lib" / "resources" / "memory.py")
memory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(memory)

engine_spec = importlib.util.spec_from_file_location("dispatch_engine_under_test",
                                                     HERE.parent / "lib" / "dispatch" / "engine.py")
E = importlib.util.module_from_spec(engine_spec)
engine_spec.loader.exec_module(E)

SHA = "a" * 40


class MemoryProbeTests(unittest.TestCase):
    def test_memavailable_is_reported_in_reproducible_units(self):
        fixture = "MemTotal: 800000 kB\nMemFree: 10000 kB\nMemAvailable: 368640 kB\n"
        with mock.patch("builtins.open", mock.mock_open(read_data=fixture)):
            self.assertEqual(memory.memory_usage("fixture"),
                             {"available_kb": 368640, "available_mb": 360.0})

    def test_missing_memavailable_is_an_error(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="MemTotal: 10 kB\n")):
            with self.assertRaisesRegex(ValueError, "MemAvailable"):
                memory.memory_usage("fixture")

    def test_remote_snippet_is_one_line_and_reports_the_same_sample(self):
        fixture = "MemTotal: 800000 kB\nMemFree: 10000 kB\nMemAvailable: 368640 kB\n"
        snippet = memory.remote_snippet()
        self.assertNotIn("\n", snippet)
        buffer = io.StringIO()
        namespace = {"__name__": "__main__", "open": lambda *a, **k: io.StringIO(fixture)}
        with contextlib.redirect_stdout(buffer):
            exec(snippet, namespace)
        self.assertEqual(memory.parse_remote_sample(buffer.getvalue()),
                         {"available_kb": 368640, "available_mb": 360.0})

    def test_fallback_snippet_reports_the_same_field(self):
        buffer = io.StringIO()
        namespace = {"__name__": "__main__", "open": lambda *a, **k: io.StringIO("MemAvailable: 368640 kB\n")}
        with contextlib.redirect_stdout(buffer):
            exec(memory.FALLBACK_SNIPPET, namespace)
        self.assertEqual(memory.parse_remote_sample(buffer.getvalue())["available_kb"], 368640)

    def test_remote_sample_without_memavailable_is_an_error(self):
        with self.assertRaises(ValueError):
            memory.parse_remote_sample("")


class GuardStateTests(unittest.TestCase):
    def test_guard_pauses_once_and_resumes_automatically(self):
        state = {}
        self.assertEqual(memory.update_guard(state, "gores2", 255.9, 256), (True, True))
        self.assertTrue(memory.host_low(state, "gores2"))
        self.assertEqual(memory.update_guard(state, "gores2", 240, 256), (True, False))
        self.assertEqual(memory.update_guard(state, "gores2", 256, 256), (False, True))
        self.assertFalse(memory.host_low(state, "gores2"))
        self.assertEqual(memory.update_guard(state, "gores2", 300, 256), (False, False))

    def test_guard_is_per_host(self):
        state = {}
        memory.update_guard(state, "gores2", 120, 256)
        self.assertTrue(memory.host_low(state, "gores2"))
        self.assertFalse(memory.host_low(state, "gores3"))

    def test_resume_watermark_keeps_the_host_paused_until_it_recovers(self):
        state = {}
        memory.update_guard(state, "gores2", 120, 256, 320)
        self.assertEqual(memory.update_guard(state, "gores2", 300, 256, 320), (True, False))
        self.assertEqual(memory.update_guard(state, "gores2", 320, 256, 320), (False, True))

    def test_zero_threshold_disables_guard(self):
        self.assertEqual(memory.update_guard({}, "gores2", 0, 0), (False, False))


class MemoryRunner:
    """Answers the memory probe of every host with its own reading."""

    def __init__(self, available_mb_by_host):
        self.available = dict(available_mb_by_host)
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for host, mb in self.available.items():
            if f" {host} python3 -" in text:
                return 0, json.dumps({"available_kb": int(mb * 1024)}), ""
        return 0, "", ""


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memory-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = os.path.join(self.tmp, "state")
        self.cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(self.cfg_path, "w") as f:
            json.dump({"repo": "acme/project", "local_repo": self.tmp, "state_dir": self.state_dir,
                       "ssh": {"control_dir": os.path.join(self.tmp, "cm")},
                       "memory_guard": {"minimum_available_mb": 256, "probe_interval_s": 0}}, f)
        self.cfg = E.load_config(self.cfg_path)

    def make(self, runner):
        out = open(os.devnull, "w")
        self.addCleanup(out.close)
        return E.Dispatcher(self.cfg, runner=runner, out=out)

    @staticmethod
    def lanes():
        return {"CX9": {"host": "box-low", "backend": "codex"},
                "M17": {"host": "box-ok", "backend": "pi"}}

    def dispatch(self, d, state, lanes, numbers):
        d.main_sha = lambda: SHA
        d.label_state = lambda n: "fresh"
        d.dispatch = lambda lane, lc, item, base: (f"run-{item['n']}", "verify")
        items = [{"n": n, "title": f"card {n}", "body": "", "backend": None, "strict": False, "prio": 1}
                 for n in numbers]
        before = self.busy(state)
        d.assign_issues(lanes, {lane: {"lane": "idle"} for lane in lanes}, state, items)
        return self.busy(state) - before

    @staticmethod
    def busy(state):
        return {L for L, st in state.items() if isinstance(st, dict) and st.get("issue")}


class PerHostGuardTests(GuardBase):
    def test_low_memory_remote_host_is_paused_while_other_hosts_dispatch(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertTrue(state["_memory_guard"]["box-low"]["low"])
        self.assertFalse(state["_memory_guard"]["box-ok"]["low"])
        self.assertEqual(self.dispatch(d, state, lanes, [1]), {"M17"})
        self.assertTrue(any("box-low python3 -" in call for call in runner.calls))

    def test_guard_lifts_itself_when_the_host_recovers(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertEqual(self.dispatch(d, state, lanes, [1]), {"M17"})
        runner.available["box-low"] = 800                      # the host freed its memory
        d.update_memory_guard(lanes, state)
        self.assertFalse(state["_memory_guard"]["box-low"]["low"])
        self.assertEqual(self.dispatch(d, state, lanes, [2]), {"CX9"})

    def test_low_memory_host_has_no_idle_lane_for_review_dispatch(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        idle = d.idle_lanes(lanes, {L: {"lane": "idle"} for L in lanes}, state)
        self.assertEqual(idle, ["M17"])

    def test_per_host_watermark_overrides_the_global_one(self):
        self.cfg["memory_guard"]["hosts"] = {"box-low": {"minimum_available_mb": 64}}
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertFalse(state["_memory_guard"]["box-low"]["low"])
        self.assertEqual(self.dispatch(d, state, lanes, [1, 2]), {"CX9", "M17"})

    def test_local_host_is_measured_in_process(self):
        self.cfg["local_hosts"] = ["box-low"]
        runner = MemoryRunner({})
        d = self.make(runner)
        with mock.patch.object(d.memory_module(), "memory_usage",
                               return_value={"available_kb": 368640, "available_mb": 360.0}):
            d.update_memory_guard({"CX9": {"host": "box-low", "backend": "codex"}}, {})
        self.assertEqual(runner.calls, [])

    def test_failed_probe_keeps_the_previous_state(self):
        class Failing(MemoryRunner):
            def __call__(self, cmd, timeout=120, inp=None, env=None):
                text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
                self.calls.append(text)
                return 255, "", "ssh: connect failed"

        d = self.make(Failing({}))
        state = {}
        d.update_memory_guard({"CX9": {"host": "box-low", "backend": "codex"}}, state)
        self.assertNotIn("_memory_guard", state)

    def test_zero_watermark_disables_the_guard(self):
        self.cfg["memory_guard"]["minimum_available_mb"] = 0
        runner = MemoryRunner({"box-low": 12})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.dispatch(d, state, lanes, [1, 2]), {"CX9", "M17"})

    def test_disabling_the_guard_lifts_the_pause_of_a_low_memory_host(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertTrue(state["_memory_guard"]["box-low"]["low"])
        self.assertEqual(self.dispatch(d, state, lanes, [1]), {"M17"})
        state["M17"] = None                       # the healthy host finished its card
        self.cfg["memory_guard"]["minimum_available_mb"] = 0      # the operator turns the guard off
        d.update_memory_guard(lanes, state)
        self.assertFalse(state["_memory_guard"]["box-low"]["low"])
        self.assertEqual(self.dispatch(d, state, lanes, [2, 3]), {"CX9", "M17"})

    def test_disabled_guard_ignores_a_stale_low_water_mark(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertTrue(state["_memory_guard"]["box-low"]["low"])
        self.cfg["memory_guard"]["minimum_available_mb"] = 0
        self.assertFalse(d.host_memory_low(state, "box-low"))
        self.assertEqual(self.dispatch(d, state, lanes, [3, 4]), {"CX9", "M17"})

    def test_dropping_a_host_watermark_lifts_its_pause(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.assertTrue(state["_memory_guard"]["box-low"]["low"])
        self.cfg["memory_guard"]["hosts"] = {"box-low": {"minimum_available_mb": 0}}
        d.update_memory_guard(lanes, state)
        self.assertFalse(state["_memory_guard"]["box-low"]["low"])
        self.assertEqual(self.dispatch(d, state, lanes, [1, 2]), {"CX9", "M17"})

    def test_disabling_the_guard_is_written_to_the_brain_log(self):
        runner = MemoryRunner({"box-low": 120, "box-ok": 900})
        d = self.make(runner)
        lanes, state = self.lanes(), {}
        d.update_memory_guard(lanes, state)
        self.cfg["memory_guard"]["minimum_available_mb"] = 0
        d.update_memory_guard(lanes, state)
        with open(os.path.join(self.state_dir, "brain-log.md"), encoding="utf-8") as stream:
            self.assertIn("memory guard: disabled", stream.read())


class BrainLogTests(GuardBase):
    def brain_text(self, path):
        with open(path, encoding="utf-8") as stream:
            return stream.read()

    def test_default_brain_log_receives_the_low_memory_alert(self):
        runner = MemoryRunner({"box-low": 120})
        d = self.make(runner)
        d.update_memory_guard({"CX9": {"host": "box-low", "backend": "codex"}}, {})
        brain = os.path.join(self.state_dir, "brain-log.md")
        text = self.brain_text(brain)
        self.assertIn("memory guard: box-low", text)
        self.assertIn("120.0 MiB available", text)
        self.assertIn("paused", text)

    def test_configured_brain_log_receives_the_alert(self):
        brain = os.path.join(self.tmp, "brain.md")
        self.cfg["brain_log"] = brain
        runner = MemoryRunner({"box-low": 120})
        d = self.make(runner)
        d.update_memory_guard({"CX9": {"host": "box-low", "backend": "codex"}}, {})
        self.assertIn("memory guard: box-low", self.brain_text(brain))

    def test_brain_log_defaults_to_the_state_directory(self):
        d = self.make(MemoryRunner({}))
        self.assertEqual(d.brain_log_path(), os.path.join(self.state_dir, "brain-log.md"))

    def test_recovery_is_logged_to_the_brain_log(self):
        runner = MemoryRunner({"box-low": 120})
        d = self.make(runner)
        lanes = {"CX9": {"host": "box-low", "backend": "codex"}}
        state = {}
        d.update_memory_guard(lanes, state)
        runner.available["box-low"] = 800
        d.update_memory_guard(lanes, state)
        text = self.brain_text(os.path.join(self.state_dir, "brain-log.md"))
        self.assertIn("recovered", text)


if __name__ == "__main__":
    unittest.main()
