"""Tests for the generated control-plane supervisor (Refs #1460)."""
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import time
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "lib" / "supervise" / "controlplane.py"
_spec = importlib.util.spec_from_file_location("controlplane_under_test", MODULE_PATH)
controlplane = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(controlplane)

# The loop names of the box (docs/ctl-boot.md) -- they contain "-" and "." and must
# still produce a script the shell accepts.
BOX_SERVICE_NAMES = ["ib-refresh-loop", "lane-autodispatch", "lane-merge", "bus-worker-loop", "health-loop"]


class ControlPlaneSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="controlplane-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.state = self.root / "state"
        self.service = {
            "name": "sleeper",
            "cmd": "sleep 30",
            "cwd": str(self.root),
            "env": {},
            "restart_backoff_max": 4,
        }
        self.services = [self.service]
        self.script = self.write_script(self.services)

    def write_script(self, services, name="supervisor.sh"):
        script = self.root / name
        script.write_text(controlplane.render_supervisor(services, self.state), encoding="utf-8")
        script.chmod(0o755)
        return script

    def write_runner(self, name, body):
        """Write an executable helper script and return its path."""
        runner = self.root / name
        runner.write_text(body, encoding="utf-8")
        runner.chmod(0o755)
        return runner

    def run_script(self, action, script=None, timeout=60):
        return subprocess.run(
            ["sh", str(script or self.script), action], capture_output=True, text=True, timeout=timeout
        )

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def lines(self, path):
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_rendered_script_passes_sh_n_and_lifecycle(self):
        subprocess.run(["sh", "-n", str(self.script)], check=True)
        self.assertEqual(self.run_script("start").returncode, 0)
        self.assertTrue(
            self.wait_for(lambda: controlplane.status(self.services, self.state)["sleeper"]["state"] == "running"),
            "service did not come up",
        )
        self.assertEqual(self.run_script("status").stdout.strip(), "sleeper:running")
        self.assertEqual(self.run_script("stop").returncode, 0)
        self.assertIn(controlplane.status(self.services, self.state)["sleeper"]["state"], ("dead", "stale-pid"))

    def test_box_service_names_render_valid_shell(self):
        """The loop names of the box contain "-" and "." -- dash rejects them as function names."""
        services = [
            {"name": name, "cmd": "sleep 30", "cwd": str(self.root), "env": {}, "restart_backoff_max": 4}
            for name in BOX_SERVICE_NAMES
        ]
        script = self.write_script(services, "box-supervisor.sh")
        subprocess.run(["sh", "-n", str(script)], check=True)
        text = script.read_text(encoding="utf-8")
        self.assertIn("start_health_loop() {", text)
        self.assertNotIn("start_health-loop", text)
        # the status output keeps the original names
        result = self.run_script("status", script=script)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.split(), ["%s:dead" % name for name in BOX_SERVICE_NAMES])

    def test_live_pidfile_prevents_duplicate_start(self):
        self.state.mkdir()
        process = subprocess.Popen(["sleep", "30"])

        def cleanup_process():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

        self.addCleanup(cleanup_process)
        (self.state / "sleeper.pid").write_text(str(process.pid), encoding="ascii")
        self.assertEqual(self.run_script("start").returncode, 0)
        self.assertEqual((self.state / "sleeper.pid").read_text().strip(), str(process.pid))

    def test_repeated_start_keeps_a_single_instance(self):
        counter = self.root / "starts.txt"
        runner = self.write_runner("runner.sh", "#!/bin/sh\necho x >> %s\nexec sleep 30\n" % counter)
        services = [{
            "name": "sleeper",
            "cmd": str(runner),
            "cwd": str(self.root),
            "env": {},
            "restart_backoff_max": 4,
        }]
        script = self.write_script(services, "single.sh")
        self.assertEqual(self.run_script("start", script=script).returncode, 0)
        self.assertTrue(self.wait_for(lambda: len(self.lines(counter)) >= 1), "service did not start")
        first_pid = (self.state / "sleeper.pid").read_text().strip()
        # a second start (cron @reboot and systemd firing together) must not duplicate
        self.assertEqual(self.run_script("start", script=script).returncode, 0)
        time.sleep(0.5)
        self.assertEqual(len(self.lines(counter)), 1)
        self.assertEqual((self.state / "sleeper.pid").read_text().strip(), first_pid)
        self.assertEqual(self.run_script("stop", script=script).returncode, 0)

    def test_script_survives_special_characters_in_the_command(self):
        """A command with '#' or brackets must not break the generated script."""
        for cmd in ["sleep 30 # a comment", "sh -c 'echo (unbalanced'", "echo 'has ) paren'"]:
            services = [{
                "name": "weird-loop",
                "cmd": cmd,
                "cwd": str(self.root),
                "env": {},
                "restart_backoff_max": 4,
            }]
            script = self.write_script(services, "weird.sh")
            result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "%s: %s" % (cmd, result.stderr))

    def test_concurrent_start_keeps_a_single_instance(self):
        counter = self.root / "starts.txt"
        runner = self.write_runner("runner.sh", "#!/bin/sh\necho x >> %s\nexec sleep 30\n" % counter)
        services = [{
            "name": "sleeper",
            "cmd": str(runner),
            "cwd": str(self.root),
            "env": {},
            "restart_backoff_max": 4,
        }]
        script = self.write_script(services, "concurrent.sh")
        first = subprocess.Popen(["sh", str(script), "start"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(["sh", str(script), "start"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(first.communicate(timeout=30)[0].decode(), "")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.communicate(timeout=30)[0].decode(), "")
        self.assertEqual(second.returncode, 0)
        time.sleep(0.5)
        self.assertEqual(len(self.lines(counter)), 1)
        self.assertEqual(self.run_script("stop", script=script).returncode, 0)

    def test_crashed_service_is_restarted(self):
        counter = self.root / "crashes.txt"
        runner = self.write_runner("crasher.sh", "#!/bin/sh\necho x >> %s\nexit 1\n" % counter)
        services = [{
            "name": "crasher",
            "cmd": str(runner),
            "cwd": str(self.root),
            "env": {},
            "restart_backoff_max": 4,
        }]
        script = self.write_script(services, "crasher-supervisor.sh")
        self.assertEqual(self.run_script("start", script=script).returncode, 0)
        self.assertTrue(
            self.wait_for(lambda: len(self.lines(counter)) >= 2, timeout=20),
            "crashed service was not restarted (crashes=%d)" % len(self.lines(counter)),
        )
        self.assertEqual(self.run_script("stop", script=script).returncode, 0)

    def test_stop_does_not_revive_the_service(self):
        counter = self.root / "crashes.txt"
        runner = self.write_runner("crasher.sh", "#!/bin/sh\necho x >> %s\nexit 1\n" % counter)
        services = [{
            "name": "crasher",
            "cmd": str(runner),
            "cwd": str(self.root),
            "env": {},
            "restart_backoff_max": 30,
        }]
        script = self.write_script(services, "stop-supervisor.sh")
        self.assertEqual(self.run_script("start", script=script).returncode, 0)
        self.assertTrue(self.wait_for(lambda: len(self.lines(counter)) >= 2, timeout=20))
        self.assertEqual(self.run_script("stop", script=script).returncode, 0)
        stopped_at = len(self.lines(counter))
        time.sleep(3)
        self.assertEqual(len(self.lines(counter)), stopped_at, "service was revived after stop")
        self.assertIn(controlplane.status(services, self.state)["crasher"]["state"], ("dead", "stale-pid"))

    def test_clean_exit_reports_dead(self):
        runner = self.write_runner("once.sh", "#!/bin/sh\nexit 0\n")
        services = [{
            "name": "once",
            "cmd": str(runner),
            "cwd": str(self.root),
            "env": {},
            "restart_backoff_max": 4,
        }]
        script = self.write_script(services, "once-supervisor.sh")
        self.assertEqual(self.run_script("start", script=script).returncode, 0)
        self.assertTrue(self.wait_for(lambda: not (self.state / "once.pid").exists()))
        self.assertEqual(controlplane.status(services, self.state)["once"]["state"], "dead")
        self.assertEqual(self.run_script("status", script=script).stdout.strip(), "once:dead")

    def test_boot_hooks(self):
        cron = controlplane.render_boot_hook("cron-reboot", "/opt/open swarm/supervisor")
        self.assertEqual(cron, "@reboot '/opt/open swarm/supervisor' start\n")
        unit = controlplane.render_boot_hook("systemd-user", "/opt/open swarm/supervisor")
        self.assertIn('ExecStart="/opt/open swarm/supervisor" start', unit)
        self.assertIn('ExecStop="/opt/open swarm/supervisor" stop', unit)
        self.assertIn("WantedBy=default.target", unit)
        plain_unit = controlplane.render_boot_hook("systemd-user", "/opt/supervisor")
        self.assertIn('ExecStart="/opt/supervisor" start', plain_unit)
        plist = controlplane.render_boot_hook("launchd", "/opt/open swarm/supervisor")
        self.assertIn("<key>RunAtLoad</key><true/>", plist)
        self.assertIn("<string>/opt/open swarm/supervisor</string>", plist)
        with self.assertRaises(ValueError):
            controlplane.render_boot_hook("rc", "/opt/supervisor")

    def test_load_services_rejects_invalid_tables(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json") as config:
            json.dump([self.service, self.service], config)
            config.flush()
            with self.assertRaisesRegex(ValueError, "duplicate"):
                controlplane.load_services(config.name)
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "extra": True}])
        with self.assertRaises(ValueError):
            controlplane.load_services([{key: value for key, value in self.service.items() if key != "cwd"}])
        with self.assertRaises(ValueError):
            controlplane.load_services("{}")
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "name": "bad name"}])
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "cmd": "sleep 30\nsleep 40"}])
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "cmd": "sleep 30 # a comment"}])
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "env": {"1BAD": "x"}}])
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "restart_backoff_max": 0}])
        with self.assertRaises(ValueError):
            controlplane.load_services([{**self.service, "restart_backoff_max": 3600}])

    def test_load_services_accepts_a_valid_table(self):
        services = controlplane.load_services([self.service])
        self.assertEqual(services, [self.service])

    def test_standby_validation_and_legacy_render_are_unchanged(self):
        base = controlplane.load_services([self.service])
        self.assertEqual(
            controlplane.render_supervisor(base, self.state),
            controlplane.render_supervisor([self.service], self.state),
        )
        for standby in [None, {}, {"check": []}, {"check": [""]}, {"check": [1]},
                        {"check": ["true"], "interval_s": 0},
                        {"check": ["true"], "interval_s": True},
                        {"check": ["true"], "other": 1}]:
            with self.subTest(standby=standby):
                with self.assertRaises(ValueError):
                    controlplane.load_services([{**self.service, "standby": standby}])
        accepted = controlplane.load_services([{
            **self.service, "standby": {"check": ["/bin/true"], "interval_s": 0.1}
        }])
        self.assertEqual(accepted[0]["standby"]["check"], ["/bin/true"])

    def test_standby_waits_for_lease_then_stops_after_two_failed_checks(self):
        ready = self.root / "lease"
        starts = self.root / "starts.txt"
        check = self.write_runner("check.sh", "#!/bin/sh\ntest -f '%s'\n" % ready)
        runner = self.write_runner("standby-service.sh", "#!/bin/sh\necho start >> '%s'\nexec sleep 30\n" % starts)
        services = [{**self.service, "cmd": str(runner), "standby": {
            "check": [str(check)], "interval_s": 0.1,
        }}]
        script = self.write_script(services, "standby.sh")
        text = script.read_text(encoding="utf-8")
        self.assertIn("_sup_misses", text)
        self.assertIn("standby-waiting", text)
        subprocess.run(["sh", "-n", str(script)], check=True)
        self.assertEqual(self.run_script("start", script=script).returncode, 0)
        self.assertTrue(self.wait_for(lambda: self.run_script("status", script=script).stdout.strip()
                                      == "sleeper:standby-waiting"))
        self.assertEqual(controlplane.status(services, self.state)["sleeper"]["state"], "standby-waiting")
        self.assertFalse(starts.exists())
        ready.touch()
        self.assertTrue(self.wait_for(starts.exists), "service did not start when lease became available")
        ready.unlink()
        self.assertTrue(self.wait_for(lambda: not (self.state / "sleeper.pid").exists(), timeout=8),
                        "service was not stopped after two failed checks")
        self.assertEqual(len(self.lines(starts)), 1)
        self.assertEqual(self.run_script("stop", script=script).returncode, 0)

    def test_backoff_is_capped_at_60_seconds(self):
        services = [{**self.service, "restart_backoff_max": 3600}]
        script = self.write_script(services, "backoff.sh")
        text = script.read_text(encoding="utf-8")
        self.assertNotIn("3600", text)
        self.assertIn('if [ "$_sup_backoff" -gt 60 ]; then _sup_backoff=60; fi', text)

    def test_status_reports_running_dead_and_stale_pid(self):
        self.assertEqual(controlplane.status(self.services, self.state)["sleeper"]["state"], "dead")
        self.state.mkdir()
        (self.state / "sleeper.pid").write_text("not-a-pid", encoding="ascii")
        self.assertEqual(controlplane.status(self.services, self.state)["sleeper"]["state"], "stale-pid")
        process = subprocess.Popen(["sleep", "30"])

        def cleanup_process():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

        self.addCleanup(cleanup_process)
        (self.state / "sleeper.pid").write_text(str(process.pid), encoding="ascii")
        record = controlplane.status(self.services, self.state)["sleeper"]
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["pid"], process.pid)


if __name__ == "__main__":
    unittest.main()
