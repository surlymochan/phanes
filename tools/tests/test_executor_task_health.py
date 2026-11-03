"""Offline samples for executor task health thresholds (Refs #1826)."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "executor_task_health", Path(__file__).parents[1] / "executor-task-health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)

SHOW = "TasksCurrent={current}\nTasksMax=1000\nControlGroup=/system.slice/openswarm-executor-M9.service\n"
PS = "PID PPID STAT ELAPSED COMMAND\n10 1 S 0 executor\n11 10 Z 3 child\n"


class ExecutorTaskHealthTests(unittest.TestCase):
    def probe(self, current, pids):
        return health.inspect_service("openswarm-executor-M9.service", SHOW.format(current=current),
                                      set(pids), PS)

    def test_normal(self):
        self.assertEqual(self.probe(100, [10, 11])["level"], "ok")

    def test_soft_by_zombies_and_tasks(self):
        self.assertEqual(self.probe(601, [10, 11])["level"], "soft")
        self.assertEqual(health.inspect_service("x", SHOW.format(current=10), set(range(100, 151)),
                                                PS + "".join(f"{i} 10 Z 1 child\n" for i in range(100, 151)))["level"], "soft")

    def test_hard_and_parent_pid(self):
        result = self.probe(851, [10, 11])
        self.assertEqual(result["level"], "hard")
        self.assertEqual(result["zombie_parent_pids"], [10])

    def test_failed_collection_is_uncollected(self):
        self.assertEqual(health.unavailable("x", "systemctl unavailable")["level"], "uncollected")
        with self.assertRaises(ValueError):
            health.parse_systemctl_show("TasksCurrent=2\n")


if __name__ == "__main__":
    unittest.main()

class CollectionTests(unittest.TestCase):
    def test_collection_and_alert_snapshot(self):
        import json
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=__import__('os').environ.get('TMPDIR')) as tmp:
            root = Path(tmp)
            replies = iter(['systemd', 'openswarm-executor-M9.service loaded active running\n', PS,
                            SHOW.format(current=601), '10\n11\n'])
            result = health.update('host', root / 'health.json', root / 'health.alert', root / 'snapshots', lambda _: next(replies))
            self.assertEqual(result['services'][0]['level'], 'soft')
            self.assertEqual(next((root / 'snapshots').iterdir()).read_text(), PS)
            self.assertIn('zombie_parent_pids=[10]', (root / 'health.alert').read_text())
            self.assertTrue(json.loads((root / 'health.json').read_text())['hosts']['host']['executor_tasks']['services'][0]['collected'])

    def test_skip_and_failure(self):
        import subprocess
        self.assertEqual(health.collect('host', lambda _: 'skipped')[0]['status'], 'skipped')
        def fail(_):
            raise subprocess.CalledProcessError(1, 'ssh')
        self.assertEqual(health.collect('host', fail)[0]['status'], 'uncollected')
