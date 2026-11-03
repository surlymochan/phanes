"""`openswarm doctor` reports lane prerequisites; it never installs or changes anything."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "bin" / "openswarm"

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

# Codex's sandbox tools are a Linux requirement; the doctor only reports them on Linux.
LINUX = sys.platform.startswith("linux")
SANDBOX_CHECKS = {"sandbox.bwrap", "sandbox.slirp4netns", "sandbox.iptables"} if LINUX else set()


class DoctorTests(unittest.TestCase):
    """The doctor runs against fake binaries, a temporary lanes.json, and a local proxy port."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="openswarm-doctor-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.lanes_file = self.tmp / "lanes.json"
        self.write_lanes()

    # ---------------------------------------------------------------- fixtures

    def fake_binary(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return str(path)

    def fake_gh(self, payload):
        """A `gh` that answers the branch-protection query and records its arguments."""
        calls = self.tmp / "gh-calls"
        path = self.bin / "gh"
        path.write_text("#!/bin/sh\n"
                        f'echo "$@" >> "{calls}"\n'
                        "echo '" + payload + "'\n")
        path.chmod(0o755)
        return calls

    def dispatch_config(self, **overrides):
        config = {"repo": "owner/repo", "review": {"status_context": "lane-review"}}
        config.update(overrides)
        path = self.tmp / "dispatch.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return str(path)

    def sandbox_binaries(self, include_slirp4netns=True):
        self.fake_binary("codex")
        self.fake_binary("bwrap")
        self.fake_binary("iptables")
        if include_slirp4netns:
            self.fake_binary("slirp4netns")

    def write_lanes(self, lanes=None):
        if lanes is None:
            lanes = [{"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                      "repo": str(self.repo)}]
        self.lanes_file.write_text(json.dumps({"lanes": lanes}), encoding="utf-8")

    def open_port(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        return listener.getsockname()[1]

    def closed_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def env(self, **extra):
        env = {"PATH": str(self.bin), "HOME": str(self.home),
               "OPENSWARM_HOME": str(self.home), "OPENSWARM_LANES_FILE": str(self.lanes_file)}
        env.update(extra)
        return env

    def doctor(self, *args, env=None, timeout=60):
        return subprocess.run([sys.executable, str(CLI), "doctor", *args],
                              env=self.env() if env is None else env,
                              text=True, capture_output=True, timeout=timeout)

    def checks(self, report, lane="C1"):
        for row in report["lanes"]:
            if row["name"] == lane:
                return {check["name"]: check for check in row["checks"]}
        self.fail(f"no doctor report for lane {lane}: {report}")

    def expected_disk_status(self):
        free = shutil.disk_usage(self.home).free
        if free < xs.DOCTOR_DISK_FAIL_BYTES:
            return "fail"
        return "warn" if free < xs.DOCTOR_DISK_WARN_BYTES else "ok"

    # ---------------------------------------------------------------- tests

    def test_healthy_codex_lane_passes_every_check(self):
        self.sandbox_binaries()
        port = self.open_port()
        result = self.doctor("--json", env=self.env(HTTPS_PROXY=f"http://127.0.0.1:{port}"))

        report = json.loads(result.stdout)
        self.assertEqual(len(report["lanes"]), 1)
        lane = report["lanes"][0]
        self.assertEqual(lane["name"], "C1")
        self.assertEqual(lane["backend"], "codex")
        checks = self.checks(report)
        self.assertEqual(set(checks), {"binary", "proxy", "disk"} | SANDBOX_CHECKS)
        for name in {"binary", "proxy"} | SANDBOX_CHECKS:
            self.assertEqual(checks[name]["status"], "ok", checks[name])
        # The worktree disk and the resource rows depend on the host, so the expected verdict is derived from them.
        disk = self.expected_disk_status()
        self.assertEqual(checks["disk"]["status"], disk)
        self.assertEqual(lane["status"], disk)
        resource_fail = sum(row["level"] == "FAIL" for row in report["resources"])
        failing = int(disk == "fail") + resource_fail
        self.assertEqual(report["ok"], failing == 0)
        self.assertEqual(report["summary"]["fail"], failing)
        self.assertEqual(result.returncode, 1 if failing else 0, result.stdout + result.stderr)

    @unittest.skipUnless(LINUX, "the Codex sandbox tools are only checked on Linux")
    def test_missing_slirp4netns_fails_with_a_fix_hint(self):
        self.sandbox_binaries(include_slirp4netns=False)
        result = self.doctor("--json")

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["lanes"][0]["status"], "fail")
        check = self.checks(report)["sandbox.slirp4netns"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("slirp4netns", check["detail"])
        self.assertIn("slirp4netns", check["fix"])
        self.assertGreaterEqual(report["summary"]["fail"], 1)

    def test_unreachable_proxy_fails(self):
        self.sandbox_binaries()
        port = self.closed_port()
        result = self.doctor("--json", env=self.env(HTTPS_PROXY=f"http://127.0.0.1:{port}"))

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        check = self.checks(report)["proxy"]
        self.assertEqual(check["status"], "fail")
        self.assertIn(f"127.0.0.1:{port}", check["detail"])
        self.assertTrue(check["fix"])

    def test_json_report_structure_is_stable(self):
        self.sandbox_binaries()
        result = self.doctor("--json")

        report = json.loads(result.stdout)
        self.assertEqual(set(report), {"ok", "lanes", "checks", "resources", "notes", "summary"})
        self.assertEqual(set(report["summary"]), {"ok", "warn", "fail", "lanes"})
        self.assertEqual(report["summary"]["lanes"], 1)
        lane = report["lanes"][0]
        self.assertEqual(set(lane), {"name", "backend", "status", "checks"})
        for check in lane["checks"]:
            self.assertEqual(set(check), {"name", "status", "detail", "fix"})
            self.assertIn(check["status"], ("ok", "warn", "fail"))
            self.assertIsInstance(check["detail"], str)
        # A lane without the env_file field is skipped instead of reported as missing.
        self.assertNotIn("env_file", self.checks(report))

    def test_copilot_events_do_not_fall_back_when_latest_run_has_no_log(self):
        runs = self.tmp / "runs"
        latest = runs / "20261102-120000-latest"
        older = runs / "20261102-110000-older"
        latest.mkdir(parents=True)
        older.mkdir()
        (latest / "status.json").write_text(json.dumps({"lane": "C1", "backend": "copilot"}))
        (older / "status.json").write_text(json.dumps({"lane": "C1", "backend": "copilot"}))
        (older / "events.1.copilot.jsonl").write_text('{"availableModels": []}\n')
        previous_runs = xs.RUNS
        self.addCleanup(setattr, xs, "RUNS", previous_runs)
        xs.RUNS = str(runs)

        self.assertIsNone(xs.doctor_latest_copilot_events("C1"))

    def test_env_file_permissions_are_reported(self):
        env_file = self.tmp / "lane.env"
        env_file.write_text("EXAMPLE=value\n")
        env_file.chmod(0o644)
        self.sandbox_binaries()
        self.write_lanes([{"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                           "repo": str(self.repo), "env_file": str(env_file)}])

        result = self.doctor("--json")
        self.assertEqual(result.returncode, 1)
        check = self.checks(json.loads(result.stdout))["env_file"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("chmod 600", check["fix"])

        env_file.chmod(0o600)
        result = self.doctor("--json")
        check = self.checks(json.loads(result.stdout))["env_file"]
        self.assertEqual(check["status"], "ok")

    def test_env_file_stricter_than_0600_is_accepted(self):
        env_file = self.tmp / "lane.env"
        env_file.write_text("EXAMPLE=value\n")
        env_file.chmod(0o400)
        self.sandbox_binaries()
        self.write_lanes([{"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                           "repo": str(self.repo), "env_file": str(env_file)}])

        result = self.doctor("--json")
        check = self.checks(json.loads(result.stdout))["env_file"]
        self.assertEqual(check["status"], "ok")

    def test_env_file_list_is_checked_file_by_file(self):
        self.sandbox_binaries()
        first = self.tmp / "first.env"
        second = self.tmp / "second.env"
        for path in (first, second):
            path.write_text("EXAMPLE=value\n")
            path.chmod(0o600)
        lane = {"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                "repo": str(self.repo), "env_file": [str(first), str(second)]}
        self.write_lanes([lane])

        result = self.doctor("--json")
        check = self.checks(json.loads(result.stdout))["env_file"]
        self.assertEqual(check["status"], "ok")
        self.assertIn(str(first), check["detail"])
        self.assertIn(str(second), check["detail"])

        # One loose file fails the check, and the detail names that file.
        second.chmod(0o644)
        result = self.doctor("--json")
        self.assertEqual(result.returncode, 1)
        check = self.checks(json.loads(result.stdout))["env_file"]
        self.assertEqual(check["status"], "fail")
        self.assertIn(str(second), check["detail"])

        # A missing file in the list fails too, and the detail names the missing path.
        missing = self.tmp / "missing.env"
        self.write_lanes([dict(lane, env_file=[str(first), str(missing)])])
        result = self.doctor("--json")
        self.assertEqual(result.returncode, 1)
        check = self.checks(json.loads(result.stdout))["env_file"]
        self.assertEqual(check["status"], "fail")
        self.assertIn(str(missing), check["detail"])

    def test_proxy_uses_the_lane_environment(self):
        self.sandbox_binaries()
        port = self.open_port()
        env_file = self.tmp / "lane.env"
        env_file.write_text(f"# lane environment\nHTTPS_PROXY=http://127.0.0.1:{port}\n")
        env_file.chmod(0o600)
        self.write_lanes([{"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                           "repo": str(self.repo), "env_file": str(env_file)}])

        result = self.doctor("--json")
        checks = self.checks(json.loads(result.stdout))
        self.assertEqual(checks["proxy"]["status"], "ok")
        self.assertEqual(checks["env_file"]["status"], "ok")
        self.assertIn(str(port), checks["proxy"]["detail"])

    def test_backend_environment_override_precedes_lane_binary(self):
        self.fake_binary("lane-cursor")
        override = self.fake_binary("override-cursor")
        lane_binary = self.fake_binary("lane-cursor")
        self.write_lanes([{"name": "K1", "backend": "cursor", "binary": lane_binary,
                           "repo": str(self.repo)}])

        result = self.doctor("--json", env=self.env(OPENSWARM_CURSOR_BIN=override))
        self.assertTrue(result.stdout, result.stderr)
        check = self.checks(json.loads(result.stdout), "K1")["binary"]
        self.assertEqual(check["status"], "ok")
        self.assertIn(override, check["detail"])

    def test_codex_environment_override_precedes_lane_binary(self):
        override = self.fake_binary("override-codex")
        lane_binary = self.fake_binary("lane-codex")
        self.write_lanes([{"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                           "repo": str(self.repo), "binary": lane_binary}])

        result = self.doctor("--json", env=self.env(OPENSWARM_CODEX_APP_SERVER_BIN=override))
        self.assertTrue(result.stdout, result.stderr)
        check = self.checks(json.loads(result.stdout))["binary"]
        self.assertEqual(check["status"], "ok")
        self.assertIn(os.path.realpath(override), check["detail"])
        self.assertNotIn(lane_binary, check["detail"])

    def test_agy_environment_override_precedes_lane_binary(self):
        override = self.fake_binary("override-agy")
        lane_binary = self.fake_binary("lane-agy")
        self.write_lanes([{"name": "G1", "backend": "agy", "binary": lane_binary,
                           "repo": str(self.repo)}])

        result = self.doctor("--json", env=self.env(OPENSWARM_AGY_BIN=override))
        self.assertTrue(result.stdout, result.stderr)
        check = self.checks(json.loads(result.stdout), "G1")["binary"]
        self.assertEqual(check["status"], "ok")
        self.assertIn(override, check["detail"])
        self.assertNotIn(lane_binary, check["detail"])

    def test_unknown_lane_is_rejected(self):
        self.sandbox_binaries()
        result = self.doctor("--lane", "NOPE")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown lane", result.stderr)

    def test_every_configured_lane_is_checked(self):
        self.sandbox_binaries()
        self.fake_binary("cursor-agent")
        self.write_lanes([
            {"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high", "repo": str(self.repo)},
            {"name": "K1", "backend": "cursor", "model": "composer-2.5", "repo": str(self.repo)},
        ])

        result = self.doctor("--json")
        report = json.loads(result.stdout)
        self.assertEqual([lane["name"] for lane in report["lanes"]], ["C1", "K1"])
        self.assertEqual(report["summary"]["lanes"], 2)
        # The Codex sandbox tools are a Codex requirement; other backends are not checked for them.
        if LINUX:
            self.assertIn("sandbox.slirp4netns", self.checks(report, "C1"))
            self.assertNotIn("sandbox.slirp4netns", self.checks(report, "K1"))
        self.assertEqual(self.checks(report, "K1")["binary"]["status"], "ok")

    @unittest.skipUnless(LINUX, "the Codex sandbox tools are only checked on Linux")
    def test_text_output_lists_the_fix_hints(self):
        self.sandbox_binaries(include_slirp4netns=False)
        result = self.doctor("--lane", "C1")

        self.assertEqual(result.returncode, 1)
        self.assertIn("sandbox.slirp4netns", result.stdout)
        self.assertIn("fix:", result.stdout)
        self.assertIn("lane  C1  backend=codex", result.stdout)

    def test_sandbox_tools_are_only_checked_on_linux(self):
        lane = {"name": "C1", "backend": "codex", "model": "gpt-6-luna", "effort": "high",
                "repo": str(self.repo)}
        policy = xs.load_policy()

        darwin = [check["name"] for check in xs.doctor_lane_report(lane, policy, system="darwin")["checks"]]
        linux = [check["name"] for check in xs.doctor_lane_report(lane, policy, system="linux")["checks"]]
        self.assertNotIn("sandbox.bwrap", darwin)
        self.assertIn("sandbox.bwrap", linux)
        self.assertIn("sandbox.slirp4netns", linux)
        self.assertIn("sandbox.iptables", linux)

    def test_the_branch_protection_check_reads_the_dispatcher_config(self):
        calls = self.fake_gh(json.dumps({"required_status_checks": {"contexts": ["lane-review"]},
                                         "enforce_admins": {"enabled": True}}))
        config = self.dispatch_config(repo="owner/other", review={"status_context": "custom-review"})

        result = self.doctor("--json", "--dispatch-config", config)

        report = json.loads(result.stdout)
        check = report["checks"][0]
        # The dispatcher publishes under 'custom-review'; a branch that only requires the old
        # default context enforces no review status, so the doctor warns.
        self.assertEqual(check["status"], "warn")
        self.assertIn("custom-review", check["detail"])
        # The repository of the dispatcher config is the one that is checked.
        self.assertIn("repos/owner/other/branches/main/protection", calls.read_text())

    def test_the_branch_protection_check_is_ok_when_the_gate_is_enforced(self):
        self.fake_gh(json.dumps({"required_status_checks": {"contexts": ["lane-review"]},
                                 "enforce_admins": {"enabled": True}}))
        config = self.dispatch_config()

        result = self.doctor("--json", "--dispatch-config", config)

        check = json.loads(result.stdout)["checks"][0]
        self.assertEqual(check["status"], "ok")
        self.assertIsNone(check["fix"])

    def test_without_the_dispatcher_config_the_gate_is_unknown_and_never_ok(self):
        # The dispatcher serves with a config that is not at the default location and
        # publishes under another context; the doctor must not report OK on the default
        # context of a guessed repository.
        calls = self.fake_gh(json.dumps({"required_status_checks": {"contexts": ["lane-review"]},
                                         "enforce_admins": {"enabled": True}}))
        self.dispatch_config(repo="owner/other", review={"status_context": "custom-review"})

        result = self.doctor("--json")

        report = json.loads(result.stdout)
        check = report["checks"][0]
        self.assertEqual(check["status"], "warn")
        self.assertIn("unknown", check["detail"])
        self.assertIn("--dispatch-config", check["fix"])
        # No repository is guessed: GitHub is not asked about a branch nobody protects.
        self.assertFalse(calls.exists())
        self.assertTrue(any("dispatcher config" in note for note in report["notes"]))

    def test_doctor_changes_nothing_on_disk(self):
        self.sandbox_binaries(include_slirp4netns=False)

        def tree():
            return sorted(str(path.relative_to(self.tmp)) for path in self.tmp.rglob("*"))

        before = tree()
        result = self.doctor("--lane", "C1")
        # The missing slirp4netns only fails the report on Linux; elsewhere the sandbox tools are not checked.
        self.assertEqual(result.returncode, 1 if LINUX else 0, result.stdout)
        self.assertEqual(tree(), before)


class BranchProtectionTests(unittest.TestCase):
    """The branch-protection check asks GitHub and reports; it never changes the settings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="openswarm-doctor-gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.commands = []

    def runner(self, value=None, error=""):
        def run(command, **kwargs):
            self.commands.append(list(command))
            return subprocess.CompletedProcess(command, 1 if error else 0,
                                               json.dumps(value) if value is not None else "", error)
        return run

    def gate(self, **overrides):
        base = {"repo": "owner/repo", "context": "lane-review", "branch": "main"}
        base.update(overrides)
        return base

    def test_an_unprotected_branch_warns_with_a_repair_command(self):
        check = xs.doctor_branch_protection(self.gate(), runner=self.runner(error="HTTP 404"))

        self.assertEqual(check["status"], "warn")
        self.assertIn("no protection", check["detail"])
        self.assertIn("gh api -X PUT repos/owner/repo/branches/main/protection --input -", check["fix"])

    def test_a_missing_review_context_warns(self):
        check = xs.doctor_branch_protection(self.gate(), runner=self.runner({
            "required_status_checks": {"contexts": ["ci"]}, "enforce_admins": {"enabled": True}}))

        self.assertEqual(check["status"], "warn")
        self.assertIn("lane-review", check["detail"])
        self.assertIn("gh api -X PUT", check["fix"])

    def test_a_protected_branch_that_requires_the_context_is_ok(self):
        check = xs.doctor_branch_protection(self.gate(), runner=self.runner({
            "required_status_checks": {"contexts": ["lane-review"]},
            "enforce_admins": {"enabled": True}}))

        self.assertEqual(check["status"], "ok")
        self.assertIsNone(check["fix"])

    def test_administrators_that_bypass_the_protection_warn(self):
        check = xs.doctor_branch_protection(self.gate(), runner=self.runner({
            "required_status_checks": {"contexts": ["lane-review"]},
            "enforce_admins": {"enabled": False}}))

        self.assertEqual(check["status"], "warn")
        self.assertIn("Administrators", check["detail"])
        self.assertIn("gh api -X PUT", check["fix"])

    def test_a_custom_context_of_the_dispatcher_config_is_required(self):
        config = {"repo": "owner/repo", "review": {"status_context": "custom-review"}}

        gate = xs.doctor_review_gate(config)
        check = xs.doctor_branch_protection(gate, runner=self.runner({
            "required_status_checks": {"contexts": ["lane-review"]},
            "enforce_admins": {"enabled": True}}))

        # The branch requires the old default context only: the review status of the
        # dispatcher is not enforced, so the doctor warns instead of reporting OK.
        self.assertEqual(check["status"], "warn")
        self.assertIn("custom-review", check["detail"])

    def test_a_custom_repository_of_the_dispatcher_config_is_queried(self):
        config = {"repo": "owner/other", "base_branch": "trunk"}

        gate = xs.doctor_review_gate(config)
        xs.doctor_branch_protection(gate, runner=self.runner({
            "required_status_checks": {"contexts": ["lane-review"]},
            "enforce_admins": {"enabled": True}}))

        self.assertEqual(self.commands[0][-1], "repos/owner/other/branches/trunk/protection")

    def test_without_a_dispatcher_config_the_gate_is_unknown(self):
        with mock.patch.object(xs, "doctor_git_repo", return_value="owner/remote"):
            gate = xs.doctor_review_gate(None)

        # The dispatcher's `--config` is required: the repository, the context and the branch
        # are unknown, not the ones of the current worktree.
        self.assertFalse(gate["known"])
        self.assertEqual(gate["repo"], "")
        self.assertEqual(gate["context"], "lane-review")

    def test_an_unknown_gate_warns_without_asking_github(self):
        with mock.patch.object(xs, "doctor_git_repo", return_value="owner/remote"):
            gate = xs.doctor_review_gate(None)

        check = xs.doctor_branch_protection(gate, runner=self.runner({
            "required_status_checks": {"contexts": ["lane-review"]},
            "enforce_admins": {"enabled": True}}))

        self.assertEqual(check["status"], "warn")
        self.assertIn("unknown", check["detail"])
        self.assertIn("--dispatch-config", check["fix"])
        self.assertEqual(self.commands, [])

    def test_a_dispatcher_config_without_a_repository_is_not_replaced_by_the_git_remote(self):
        with mock.patch.object(xs, "doctor_git_repo", return_value="owner/remote"):
            gate = xs.doctor_review_gate({"review": {"status_context": "custom-review"}})

        self.assertTrue(gate["known"])
        self.assertEqual(gate["repo"], "")
        self.assertEqual(gate["context"], "custom-review")

    def test_without_a_repository_the_check_warns(self):
        check = xs.doctor_branch_protection({"repo": "", "context": "lane-review", "branch": "main"},
                                            runner=self.runner())

        self.assertEqual(check["status"], "warn")
        self.assertIn("no GitHub repository", check["detail"])
        self.assertEqual(self.commands, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
