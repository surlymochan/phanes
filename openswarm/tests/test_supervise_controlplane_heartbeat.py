"""Heartbeat status coverage (Refs #1518)."""
import importlib.util
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "lib" / "supervise" / "controlplane.py"
spec = importlib.util.spec_from_file_location("controlplane_heartbeat", MODULE_PATH)
controlplane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controlplane)


class HeartbeatStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.pid = self.root / "worker.pid"
        self.pid.write_text(str(__import__("os").getpid()))
        self.beat = self.root / "beat"
        self.service = {"name": "worker", "heartbeat": "beat", "heartbeat_max_s": 180}

    def state(self, now, beat, resume=None):
        self.beat.write_text(beat)
        resume_path = None
        if resume is not None:
            resume_path = self.root / "box-resume"
            resume_path.write_text(resume)
        return controlplane.status([self.service], self.root, now=now, resume_file=resume_path)["worker"]

    def test_fresh_heartbeat_is_running(self):
        result = self.state("2026-11-02T05:13:00+08:00", "2026-11-02T05:12:00+08:00")
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["heartbeat_age_s"], 60)

    def test_old_heartbeat_is_stalled(self):
        result = self.state("2026-11-02T05:13:00+08:00", "2026-11-02T05:09:00+08:00")
        self.assertEqual(result["state"], "stalled")
        self.assertEqual(result["heartbeat_age_s"], 240)

    def test_recent_vm_resume_is_resuming(self):
        result = self.state("2026-11-02T05:13:00+08:00", "2026-11-02T04:45:00+08:00", "2026-11-02T05:12:30+08:00")
        self.assertEqual(result["state"], "resuming")

    def test_dead_pid_takes_priority_over_heartbeat(self):
        self.pid.write_text("99999999")
        result = self.state("2026-11-02T05:13:00+08:00", "2026-11-02T04:00:00+08:00")
        self.assertEqual(result["state"], "dead")

    def test_missing_heartbeat_keeps_running(self):
        self.beat.unlink(missing_ok=True)
        result = controlplane.status([self.service], self.root, now="2026-11-02T05:13:00+08:00")["worker"]
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["heartbeat"], "missing")

    def test_renderer_emits_one_line_per_service(self):
        self.assertEqual(controlplane.render_status_lines({"worker": {"state": "running"}}), "worker:running")

