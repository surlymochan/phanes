"""The fleet health probe and the host pause it writes (Refs #1367).

#1367 asks for ``swrm fleet health [--apply]``: the probe reads the disk, the
memory and the reachability of every fleet host, the thresholds live in the
policy, and ``--apply`` writes a host level pause for a sick host and releases
it again when the host recovers.  The pause is the interchange format the
dispatchers read: the lane scheduler of the control plane must not dispatch a
lane to a host the health check holds.

The openswarm tree was migrated to swrm (2d5ec1ae60), so the command belongs to
``swrm/bin/swrm`` (``fleet health``), the probing and the pause store to
``swrm/lib/dispatch/``, and the scheduler that has to honour the pause stays
in ``tools/lane-autodispatch/``.

The contracts asserted here are the ones the issue and its review rounds ask
for:

* a host whose disk is over the hard threshold is paused by ``--apply``, and the
  pause is visible to the scheduler: no lane of that host is dispatched;
* the hold of the health check does not lapse while the host is still sick, not
  even when the health check does not run again; it ends when the host
  recovers;
* the pause of an operator is never rewritten nor removed, and the health
  check keeps a hold of its own: the host stays paused after the operator
  released their pause;
* a probe that fails (the memory probe) does not hide a fault another probe
  measured (the disk is over the hard threshold): the sick host is still
  paused;
* the health command and the scheduler read the same pause directory, in the
  default configuration and with ``OPENSWARM_PAUSE_DIR`` set;
* the thresholds of the policy are the thresholds the health check judges with.

Every test is offline: a fake ``ssh`` on ``PATH`` answers the probes and the
whole control plane lives in a scratch directory inside the worktree.  The
suite stays red until the implementation lands.
"""

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                      # swrm/
REPO_ROOT = ROOT.parent                 # the worktree
SWRM = ROOT / "bin" / "swrm"
AUTODISPATCH = REPO_ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
PAUSE_STORE = ROOT / "lib" / "dispatch" / "pause.py"

HOST = "worker-a"
LANE = "C1"

# The fake remote host: it answers the probes of the health check out of a
# configuration file, so the test drives the answers and not the code.
FAKE_SSH = """#!@PYTHON@
import json, os, pathlib, sys

config = json.loads(pathlib.Path(os.environ["FLEET_HEALTH_FAKE_CONFIG"]).read_text())
args = sys.argv[1:]
command = " ".join(args[args.index("--") + 1:]) if "--" in args else " ".join(args)
lowered = command.lower()
DISK_HEADER = "Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
INODE_HEADER = "Filesystem Inodes IUsed IFree IUse% Mounted on\\n"


def row(percent):
    total = 1000000
    used = int(total * percent / 100)
    return "/dev/sda1 %d %d %d %d%% /" % (total, used, total - used, percent)


if int(config.get("ssh_rc") or 0):
    print("fake host: unreachable", file=sys.stderr)
    raise SystemExit(int(config["ssh_rc"]))
if "free" in lowered or "meminfo" in lowered:
    if int(config.get("mem_rc") or 0):
        print("fake host: the memory probe failed", file=sys.stderr)
        raise SystemExit(int(config["mem_rc"]))
    available = int(config.get("mem_avail_mb") or 4096)
    print("              total        used        free      shared  buff/cache   available")
    print("Mem:            16384        1024        4096           0       11264       %d" % available)
    print("Swap:           16384           0       16384")
elif "df" in lowered:
    if lowered.count("df ") > 1 or "-i" in lowered:
        sys.stdout.write(INODE_HEADER + row(int(config.get("inode_pct") or 0)) + "\\n")
    else:
        sys.stdout.write(DISK_HEADER + row(int(config.get("disk_pct") or 0)) + "\\n")
"""


def scratch_root():
    """The scratch directory of this suite, inside the worktree."""
    root = REPO_ROOT / ".tmp" / "fleet-health-tests"
    root.mkdir(parents=True, exist_ok=True)
    return root


class FleetHealthPauseTests(unittest.TestCase):
    """The fleet health probe and the host pause it writes."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="health-", dir=str(scratch_root())))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(FAKE_SSH.replace("@PYTHON@", sys.executable))
        ssh.chmod(0o755)
        # the fleet the health check probes: one remote host and one lane on it
        (self.home / "fleet.json").write_text(json.dumps(
            {"hosts": [{"name": HOST, "kind": "ssh", "ssh": HOST}]}), encoding="utf-8")
        repo = self.home / "repo"
        repo.mkdir()
        (self.home / "lanes.json").write_text(json.dumps(
            {"lanes": [{"name": LANE, "host": HOST, "backend": "codex",
                        "repo": str(repo), "model": "gpt-5-codex", "effort": "high"}]}),
            encoding="utf-8")
        self.fake = self.root / "fake.json"
        self.write_fake(disk_pct=99)
        # the test process shares the home of the command, so the scheduler
        # (the consumer the issue names) reads the same pause directory
        self.remember_env()
        os.environ["HOME"] = str(self.home)
        os.environ["OPENSWARM_HOME"] = str(self.home)
        os.environ.pop("OPENSWARM_PAUSE_DIR", None)

    # ------------------------------------------------------------------ fixtures

    def write_fake(self, **answers):
        """The answers of the fake remote host."""
        self.fake.write_text(json.dumps(answers), encoding="utf-8")

    def remember_env(self):
        """Keep the environment of the test process and restore it afterwards."""
        keys = ("HOME", "OPENSWARM_HOME", "OPENSWARM_PAUSE_DIR")
        previous = {key: os.environ.get(key) for key in keys}
        self.addCleanup(self.restore_env, previous)
        return previous

    @staticmethod
    def restore_env(previous):
        """Put the environment of the test process back."""
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def env(self, **overrides):
        """The environment of the health command: the scratch home and the fake ssh."""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("OPENSWARM_", "XSWARM_"))}
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["HOME"] = str(self.home)
        env["OPENSWARM_HOME"] = str(self.home)
        env["TMPDIR"] = str(self.root)
        env["FLEET_HEALTH_FAKE_CONFIG"] = str(self.fake)
        env.update(overrides)
        return env

    # ------------------------------------------------------------------ the command

    def health(self, *args, **kwargs):
        """Run ``swrm fleet health`` and return its JSON report."""
        env = kwargs.pop("env", None) or self.env(**kwargs)
        done = subprocess.run([sys.executable, str(SWRM), "fleet", "health", "--json", *args],
                              env=env, capture_output=True, text=True, timeout=120)
        if "invalid choice" in done.stderr and "health" in done.stderr:
            self.fail("swrm has no `fleet health` command yet: the health probe of #1367 is "
                      "not implemented in swrm/bin/swrm")
        self.assertEqual(done.returncode, 0, done.stderr)
        try:
            return json.loads(done.stdout)
        except ValueError:
            self.fail(f"the health report is not JSON: {done.stdout!r} {done.stderr!r}")

    def row(self, report):
        """The report row of the probed host."""
        hosts = report.get("hosts")
        if isinstance(hosts, dict):
            rows = [hosts[HOST]] if HOST in hosts else []
        else:
            rows = [row for row in hosts or [] if row.get("host") == HOST]
        self.assertEqual(len(rows), 1, f"the report has no row for {HOST}: {report}")
        return rows[0]

    # ------------------------------------------------------------------ the pause store

    def pause_store(self):
        """The real pause-store API, reading the same records as the command."""
        spec = importlib.util.spec_from_file_location("dispatch_pause_under_test", PAUSE_STORE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.PauseStore(self.home)

    def records(self):
        """Every pause record of the shared pause store."""
        directory = self.home / "pauses"
        found = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and isinstance(record.get("scope"), str):
                record["_path"] = str(path)
                found.append(record)
        return found

    def active_records(self, now=None):
        """The pause records that are not expired."""
        now = time.time() if now is None else now
        active = []
        for record in self.records():
            expires = record.get("expires_at")
            if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= now:
                continue
            active.append(record)
        return active

    def write_record(self, record):
        """Write a pause record into the shared pause store."""
        directory = self.home / "pauses"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (record["scope"].replace(":", "_").replace("@", "_") + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def remove_record(self, scope):
        """Remove the pause record of a scope: an operator releases their own pause."""
        removed = False
        for record in self.records():
            if record["scope"] == scope:
                pathlib.Path(record["_path"]).unlink()
                removed = True
        return removed

    def holds_host(self, scope, host=HOST):
        """Whether a pause scope holds a host.

        A hold that a program keeps under a scope of its own
        (``host:<host>@health``) holds the host as well: the health check never
        overwrites the pause of an operator, and the release of the operator
        never frees the host (Refs #1367).
        """
        base = f"host:{host}"
        return scope == base or str(scope).startswith(base + "@")

    def markers(self, root=None):
        """The ``PAUSE-HOST-<host>`` markers of the shared pause directory."""
        shared = pathlib.Path(root or os.environ.get("OPENSWARM_PAUSE_DIR") or self.home)
        found = []
        for directory in (shared, shared / "health", shared / "pauses", shared / "pauses" / "health"):
            path = directory / f"PAUSE-HOST-{HOST}"
            if path.exists():
                found.append(path)
        return found

    # ------------------------------------------------------------------ the scheduler

    def scheduler(self):
        """The lane scheduler: the consumer the issue names.

        The module reads the directory of its own markers from ``HOME`` when it
        is imported, so it is imported with the scratch home in place.
        """
        spec = importlib.util.spec_from_file_location("lane_autodispatch_under_test", AUTODISPATCH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def scheduler_pauses_host(self, now=None):
        """Whether the lane scheduler refuses a lane of the probed host."""
        scheduler = self.scheduler()
        scopes = scheduler.paused_scopes(now)
        if scheduler.lane_paused(LANE, {"host": HOST}, scopes):
            return True
        checker = getattr(scheduler, "host_paused", None)
        return bool(checker(HOST)) if callable(checker) else False

    # ------------------------------------------------------------------ the tests

    def test_the_health_command_reports_the_fault_of_a_sick_host(self):
        """``swrm fleet health`` probes the fleet hosts and reports the fault."""
        report = self.health()
        row = self.row(report)
        self.assertEqual(row["status"], "hard", report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertIn("disk", str(row["reason"]).lower(), report)

    def test_without_apply_the_health_command_pauses_nothing(self):
        """``fleet health`` reports; only ``--apply`` pauses a host."""
        report = self.health()
        row = self.row(report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertFalse(row["paused"], report)
        self.assertFalse(self.active_records(), "the health check paused a host without --apply")
        self.assertFalse(self.markers(), "the health check wrote a marker without --apply")

    def test_apply_pauses_a_sick_host_and_the_scheduler_refuses_it(self):
        """``--apply`` writes the host pause; no lane of that host is dispatched."""
        report = self.health("--apply")
        row = self.row(report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertTrue(row["paused"], report)
        self.assertEqual(row["action"], "paused", report)
        self.assertTrue(self.active_records() or self.markers(),
                        "the health check wrote no pause for the sick host")
        self.assertTrue(self.scheduler_pauses_host(),
                        "the scheduler would dispatch a lane to the sick host")

    def test_a_failed_probe_does_not_hide_the_fault_of_another_probe(self):
        """The memory probe fails; the disk is over the hard threshold: the host is paused."""
        self.write_fake(disk_pct=99, mem_rc=1)
        report = self.health("--apply")
        row = self.row(report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertTrue(row["paused"], report)
        self.assertTrue(self.scheduler_pauses_host(),
                        "the fault of the disk probe was lost with the failed memory probe")

    def test_an_unreachable_host_is_paused_after_its_failure_streak(self):
        """A host that cannot be reached is paused once its failure streak is reached."""
        self.write_fake(ssh_rc=255)
        report = {}
        for _ in range(5):
            report = self.health("--apply")
            if self.row(report)["decision"] == "pause":
                break
        row = self.row(report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertTrue(self.scheduler_pauses_host(),
                        "the scheduler would dispatch a lane to the unreachable host")

    def test_a_later_transport_failure_preserves_measured_disk_fault(self):
        """A memory SSH failure must not discard the successful disk measurement."""
        self.write_fake(disk_pct=99, mem_rc=255)
        report = self.health("--apply")
        row = self.row(report)
        self.assertEqual(row["status"], "hard", report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertEqual(row["metrics"]["disk_pct"], 99, report)
        self.assertEqual(row["metrics"]["ssh_fail_streak"], 1, report)
        self.assertTrue(row["paused"], report)
        self.assertTrue(self.scheduler_pauses_host())
        # A failed probe also cannot establish recovery of the existing hold.
        self.write_fake(disk_pct=10, mem_rc=255)
        report = self.health("--apply")
        self.assertEqual(self.row(report)["decision"], "none", report)
        self.assertTrue(self.scheduler_pauses_host())
        self.write_fake(disk_pct=10)
        self.health("--apply")
        self.assertFalse(self.scheduler_pauses_host())

    def test_operator_marker_survives_health_pause_and_recovery(self):
        """Health keeps its own record without touching an external marker."""
        directory = self.home / "pauses"
        directory.mkdir()
        marker = directory / f"PAUSE-HOST-{HOST}"
        body = "operator maintenance\n"
        marker.write_text(body)
        self.health("--apply")
        self.assertEqual(marker.read_text(), body)
        self.write_fake(disk_pct=10)
        self.health("--apply")
        self.assertEqual(marker.read_text(), body)
        self.assertFalse(self.active_records())
        self.assertTrue(self.scheduler_pauses_host())

    def test_health_hold_survives_operator_marker_removal(self):
        """Removing an external marker cannot release the independent health hold."""
        directory = self.home / "pauses"
        directory.mkdir()
        marker = directory / f"PAUSE-HOST-{HOST}"
        marker.write_text("operator maintenance\n")
        self.health("--apply")
        marker.unlink()
        self.assertTrue(self.scheduler_pauses_host())
        self.assertTrue(any(self.holds_host(record["scope"])
                            for record in self.active_records()))
        self.write_fake(disk_pct=10)
        self.health("--apply")
        self.assertFalse(self.scheduler_pauses_host())
        self.assertFalse(self.active_records())

    def test_the_health_pause_does_not_expire_before_the_host_recovers(self):
        """The hold of a sick host stays valid until the host recovers."""
        self.health("--apply")
        later = time.time() + 30 * 24 * 3600
        self.assertTrue(self.scheduler_pauses_host(later),
                        "the health pause lapsed while the host was still sick")
        records = self.active_records(later)
        self.assertTrue(any(self.holds_host(record["scope"]) for record in records),
                        "the pause-store record of the sick host lapsed while the host was still sick")
        # the host recovered: the next health run releases the hold
        self.write_fake(disk_pct=10, mem_avail_mb=4096)
        report = self.health("--apply")
        row = self.row(report)
        self.assertEqual(row["status"], "ok", report)
        self.assertNotEqual(row["decision"], "pause", report)
        self.assertFalse(self.scheduler_pauses_host(), "the recovered host is still paused")
        self.assertFalse(self.active_records(), "the pause of the recovered host was not released")
        self.assertFalse(self.markers(), "the marker of the recovered host was not removed")

    def test_the_pause_of_an_operator_is_never_overwritten(self):
        """The pause of an operator keeps its scope; the health check holds the host on its own."""
        self.write_record({"scope": f"host:{HOST}", "reason": "operator: maintenance",
                           "created_at": time.time(), "expires_at": None})
        report = self.health("--apply")
        row = self.row(report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertTrue(row["paused"], report)
        records = {record["scope"]: record for record in self.active_records()}
        self.assertIn(f"host:{HOST}", records, report)
        self.assertEqual(records[f"host:{HOST}"]["reason"], "operator: maintenance",
                         "the health check overwrote the pause of the operator")
        # the operator releases their own pause: the sick host stays paused
        self.assertTrue(self.remove_record(f"host:{HOST}"))
        records = self.active_records()
        self.assertTrue(any(self.holds_host(record["scope"]) for record in records),
                        "the pause store does not hold the sick host any more")
        scheduler = self.scheduler()
        scopes = scheduler.paused_scopes()
        self.assertTrue(scheduler.lane_paused(LANE, {"host": HOST}, scopes),
                        "the pause store does not report the sick host as paused")
        self.assertTrue(self.scheduler_pauses_host(),
                        "the health hold did not survive the release of the operator pause")
        # the host recovered: the health hold is released as well
        self.write_fake(disk_pct=10, mem_avail_mb=4096)
        report = self.health("--apply")
        self.assertNotEqual(self.row(report)["decision"], "pause", report)
        self.assertFalse(self.scheduler_pauses_host(), "the recovered host is still paused")

    def test_pause_store_api_recognizes_independent_health_hold(self):
        """Operator resume cannot release health's hold, even between health checks."""
        store = self.pause_store()
        manual = store.pause(f"host:{HOST}", reason="operator: maintenance", now=time.time())
        self.health("--apply")
        self.assertIn(manual, store.active(time.time()))
        self.assertTrue(store.resume(f"host:{HOST}"))
        # Query before another health run, with no marker to mask a store defect.
        for marker in self.markers():
            marker.unlink()
        later = time.time() + 30 * 24 * 3600
        self.assertTrue(store.is_paused(host=HOST, now=later))
        self.assertFalse(store.is_paused(host=HOST + "-other", now=later))
        self.assertTrue(self.scheduler_pauses_host(later))
        self.write_fake(disk_pct=10, mem_avail_mb=4096)
        self.health("--apply")
        self.assertFalse(store.is_paused(host=HOST, now=time.time()))
        self.assertFalse(store.active(time.time()))

    def test_pause_store_api_preserves_manual_pause_on_recovery(self):
        store = self.pause_store()
        manual = store.pause(f"host:{HOST}", reason="operator: maintenance", now=time.time())
        self.health("--apply")
        self.write_fake(disk_pct=10, mem_avail_mb=4096)
        self.health("--apply")
        self.assertEqual(store.active(time.time()), [manual])
        self.assertTrue(store.is_paused(host=HOST, now=time.time()))
        store.resume(f"host:{HOST}")
        self.assertFalse(store.is_paused(host=HOST, now=time.time()))

    def test_the_health_command_and_the_scheduler_share_the_pause_directory(self):
        """The hold of the health check is written where the scheduler reads it."""
        self.health("--apply")
        self.assertTrue(self.scheduler_pauses_host(),
                        "the scheduler would dispatch a lane to the sick host")
        # a configured pause directory is the one both programs use
        other = self.root / "pause-dir"
        other.mkdir()
        os.environ["OPENSWARM_PAUSE_DIR"] = str(other)
        self.health("--apply", env=self.env(OPENSWARM_PAUSE_DIR=str(other)))
        self.assertTrue(list(other.rglob("PAUSE-HOST-*")),
                        "the health check did not write its marker into the configured pause directory")
        self.assertTrue(self.scheduler_pauses_host(),
                        "the scheduler does not read the configured pause directory")
        self.assertTrue(self.pause_store().is_paused(host=HOST, now=time.time()))

    def test_the_thresholds_of_the_policy_decide_the_pause(self):
        """The thresholds the policy sets are the thresholds the health check judges with."""
        (self.home / "policy.local.json").write_text(json.dumps({
            "resources": {"health": {"disk_pause_pct": 50, "disk_resume_pct": 40}},
            "health": {"disk_pause_pct": 50, "disk_resume_pct": 40},
            "fleet_health": {"disk_pause_pct": 50, "disk_resume_pct": 40},
        }), encoding="utf-8")
        self.write_fake(disk_pct=60)
        report = self.health("--apply")
        row = self.row(report)
        self.assertEqual(row["decision"], "pause", report)
        self.assertTrue(self.scheduler_pauses_host(),
                        "the scheduler would dispatch a lane to the host the policy holds")


if __name__ == "__main__":
    unittest.main()
