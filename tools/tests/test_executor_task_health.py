"""Reproduction of #1826: the health probe must watch the executor task and zombie watermark.

The issue asks for a *health probe* for the executor systemd services: on every fleet
host every ``openswarm-executor-*.service`` is sampled for its cgroup task watermark
(``TasksCurrent`` / ``TasksMax``) and for the number of zombie (``Z``) processes inside
that cgroup.  The result lands in the host entry of ``health.json``; the watermark
raises a threshold alert:

* ``Z > 50``  or ``TasksCurrent / TasksMax > 0.6``  -> ``soft``
* ``Z > 200`` or ``TasksCurrent / TasksMax > 0.85`` -> ``hard``

Only alerting happens - nothing is restarted, nothing is killed.  When the alert fires,
the *full* ``ps -eo pid,ppid,stat,etimes,args`` snapshot is written to disk **before**
the alert is appended to ``health.alert``, so the evidence survives.  A host without
systemd (the macOS box ``gocc``) is skipped, a failed sample is recorded as *not
collected* and never alerts.  The follow-up part of the card adds the duplicate
``fleet-capacity --sweep-loop gocc`` processes: more than two of them alert with the
parent processes and the run id of the parent.

None of that exists on the base commit (dfaffd73b): the repo carries no executor task
probe at all, so the leak of the executor services (zombies piling up until the cgroup
hits ``TasksMax`` and ``fork`` fails) stays invisible for hours.

**The tests below are red on the base on purpose**: they are the reproduction of #1826.
They turn green once the probe of the card ships in the repo and is wired to the
health summary.

Where the reproduction lives
----------------------------

The probe of the issue is the repo-internal artifact ``tools/executor-task-health.py``
(the file the reviewed pull request #1830 of the issue adds, driven by the control
plane health check ``~/phanes-ctl/bin/health-check.py``, which lives *outside* this
repository - see ``docs/ctl-boot.md``).  The vendored ``openswarm/`` copy is
deprecated (#1894 moved it to ``swrm/``) and carries no health probe either, so the
probe of the card is not ported from there:

* ``openswarm/lib/`` only holds ``dispatch/`` (``leak_sweep.py``, ``issue_meta.py``) -
  no service enumeration, no cgroup task counters, no zombie accounting;
* ``openswarm/bin/`` holds ``hub-openswarm-bridge``, ``openswarm-auto`` and
  ``openswarm-fleet`` - none of them probes an executor service;
* ``swrm/`` has no health probe at all: ``swrm/bin/`` has no executor/health entry,
  ``swrm/tests/`` has no executor task test and ``swrm/lib/`` does not exist;
* ``tools/`` only carries the *offline contracts* of the control plane
  (``tools/check-health-pause.py``, ``tools/check-health-hosts-ok-pause.py``,
  ``tools/check-health-disk-soft.py``, ``tools/check-health-quota-fresh.py``,
  ``tools/check-health-pause-ttl.py``) - they read ``health.json``, they never sample a
  remote executor service.

So the surviving place for the probe of the issue is ``tools/`` (the path the card of
the issue names), and this test file is the migrated counterpart of the test file of
the pull request of the issue (``tools/tests/test_executor_task_health.py``).

The probe interface the tests drive
-----------------------------------

The tests drive the probe of the reviewed pull request #1830 (the reference
implementation of the card):

* ``parse_systemctl_show(output)`` -> the ``key=value`` pairs of a ``systemctl show``
  sample (``TasksCurrent``, ``TasksMax``, ``ControlGroup``);
* ``inspect_service(service, show_output, cgroup_pids, ps_output)`` -> the watermark
  of one service (``tasks_current``, ``tasks_max``, ``tasks_ratio``, ``zombie_count``,
  ``zombie_parent_pids``, ``level``);
* ``unavailable(service, error)`` -> a service whose sample failed (``collected`` is
  false, ``level`` is ``uncollected``);
* ``collect(host, run=None)`` -> ``(result, ps_output)`` of one host, where ``run`` is
  the remote command runner (the tests pass a fake ssh);
* ``update(host, health_path, alert_path, snapshot_dir, run=None)`` -> writes the host
  entry of ``health.json``, saves the ps snapshot and appends to ``health.alert``.

The remote side is a fake host: a runner that answers the commands of the probe like
the real box does (``/run/systemd/system`` detection, ``systemctl list-units``,
``systemctl show``, the ``cgroup.procs`` of the unit and ``ps -eo
pid,ppid,stat,etimes,args``).  No network, no ssh, no systemd and no root are needed:
the tests are hermetic and write only into a scratch directory of the worktree.

Mapping of the acceptance of the issue to the tests
---------------------------------------------------

* "为每台主机每个 openswarm-executor-*.service 采集 TasksCurrent、TasksMax 与该服务
  cgroup 内 Z 状态进程数" ->
  ``ExecutorServiceCollectionTests.test_the_executor_services_are_enumerated_over_ssh``,
  ``ExecutorWatermarkThresholdTests`` (the three levels)
* "写入 health.json 对应主机条目" ->
  ``ExecutorAlertChainTests.test_the_probe_result_lands_in_the_host_entry_of_health_json``
* "Z>50 或 TasksCurrent/TasksMax>0.6 为 soft，Z>200 或 >0.85 为 hard" ->
  ``ExecutorWatermarkThresholdTests.test_more_than_fifty_zombies_is_soft``,
  ``...test_more_than_two_hundred_zombies_is_hard``,
  ``...test_the_task_ratio_over_sixty_percent_is_soft``,
  ``...test_the_task_ratio_over_eighty_five_percent_is_hard``
* "只告警，不自动重启" ->
  ``ExecutorAlertChainTests.test_the_probe_never_restarts_or_kills_the_executor_service``
* "告警时先落盘 ps 快照到约定目录，保留现场证据" ->
  ``ExecutorAlertChainTests.test_the_ps_snapshot_is_saved_before_the_alert``,
  ``...test_no_alert_is_written_when_the_snapshot_cannot_be_saved``
* "告警时写 health.alert（含服务名与父进程 pid）" ->
  ``ExecutorAlertChainTests.test_the_alert_names_the_service_and_the_zombie_parent_pid``
* "取数失败标未采集且不报警" ->
  ``ExecutorServiceCollectionTests.test_a_failed_service_probe_is_uncollected_and_never_alerts``,
  ``...test_a_failed_host_probe_is_uncollected_and_never_alerts``
* "对无 systemd 的主机（macOS gocc）优雅跳过" ->
  ``ExecutorServiceCollectionTests.test_a_host_without_systemd_is_skipped``
* "统计同一命令行的常驻进程数，超过 2 个即告警并列出父进程" + "记录 sweep-loop 的父
  进程 run id" ->
  ``ExecutorSweepLoopTests.test_more_than_two_sweep_loop_processes_alert_with_the_parent_run_id``,
  ``...test_two_sweep_loop_processes_do_not_alert``
* "仓内实现与测试" (the probe is invokable by the health check) ->
  ``ExecutorProbeCommandLineTests.test_the_probe_updates_health_json_and_the_alert_from_the_command_line``

Run:

    python3 -m unittest tools.tests.test_executor_task_health -v
"""

import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"

# The probe of the issue: the card names tools/executor-task-health.py (the file the
# reviewed pull request #1830 adds).  The other paths are accepted as well, so the
# probe may live next to the other tools without the test going stale.
PROBE_CANDIDATES = (
    TOOLS / "executor-task-health.py",
    TOOLS / "health" / "executor-task-health.py",
    ROOT / "swrm" / "bin" / "executor-task-health.py",
    ROOT / "swrm" / "executor-task-health.py",
)


def find_probe():
    """The probe of the issue, or None when the repo does not carry it (yet)."""
    candidates = list(PROBE_CANDIDATES)
    candidates += sorted(TOOLS.glob("*executor*task*.py"))
    candidates += sorted(ROOT.glob("swrm/**/*executor*task*.py"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


PROBE_PATH = find_probe()
MODULE = None
if PROBE_PATH is not None:
    SPEC = importlib.util.spec_from_file_location("executor_task_health", PROBE_PATH)
    MODULE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(MODULE)

# The executor service of the samples and the lane that serves it.
SERVICE = "openswarm-executor-M19.service"
SERVICE2 = "openswarm-executor-M9.service"
HOST = "gores1.test.invalid"
OTHER_HOST = "m9.test.invalid"
PARENT_PID = 1210977  # the lane serve process that leaks the zombies
RUN_ID = "20261103-214705-b66c3f"

PS_HEADER = "  PID  PPID STAT ELAPSED COMMAND"


def ps_row(pid, ppid, stat, args, elapsed=3600):
    """One row of a `ps -eo pid,ppid,stat,etimes,args` table."""
    return "%6d %6d %-4s %8d %s" % (pid, ppid, stat, elapsed, args)


def executor_ps(zombies=0, parent=PARENT_PID):
    """A ps table of one executor service with the given number of zombie children."""
    rows = [PS_HEADER, ps_row(parent, 1, "S", "/usr/bin/openswarm-executor --lane M19")]
    for index in range(zombies):
        rows.append(ps_row(parent + 1 + index, parent, "Z", "/usr/bin/openswarm-executor --lane M19"))
    return "\n".join(rows) + "\n"


def sweep_ps(count=3):
    """A ps table with the duplicate `fleet-capacity --sweep-loop gocc` processes."""
    rows = [
        PS_HEADER,
        ps_row(PARENT_PID, 1, "S", "/usr/bin/openswarm-executor --lane M19"),
        ps_row(627802, 1, "S", "bwrap --bind / / " + RUN_ID),
    ]
    for index in range(count):
        rows.append(ps_row(627900 + index, 627802, "S", "/usr/bin/fleet-capacity --sweep-loop gocc"))
    return "\n".join(rows) + "\n"


class FakeHost:
    """A fake fleet host: it answers the commands of the probe like the real box.

    Every command the probe sends is recorded, so the tests can assert that the probe
    only *reads* (it never restarts or kills a leaking executor service).
    """

    def __init__(self, name=HOST, systemd=True, services=None, ps=None, fail_services=()):
        self.name = name
        self.systemd = systemd
        self.services = services if services is not None else {}
        self.ps = ps if ps is not None else PS_HEADER + "\n"
        self.fail_services = set(fail_services)
        self.commands = []

    # -- the services of the host -------------------------------------------------
    def service(self, name, tasks_current=100, tasks_max=1000, pids=()):
        """Register an executor service with its cgroup task watermark."""
        self.services[name] = {
            "tasks_current": tasks_current,
            "tasks_max": tasks_max,
            "cgroup": "/system.slice/" + name,
            "pids": list(pids),
        }
        return self

    def listing(self):
        """The `systemctl list-units` output of the host."""
        return "\n".join("%s loaded active running" % name for name in sorted(self.services))

    # -- the ssh surface used by the probe ----------------------------------------
    def run(self, command):
        """Run one remote command and return its stdout (the ssh contract of the probe)."""
        self.commands.append(command)
        return answer_command(self, command)

    # -- the fixtures --------------------------------------------------------------
    def profile(self):
        """The profile of the host, as the fake ssh process reads it."""
        return {
            "name": self.name,
            "systemd": self.systemd,
            "services": self.services,
            "ps": self.ps,
            "fail_services": sorted(self.fail_services),
        }


def service_of(command, host):
    """The executor service a remote command talks about."""
    for name in host.services:
        if name in command or name.replace(".service", "") in command:
            return name
    return ""


def answer_command(host, command):
    """Answer one remote command the way the fake host would.

    The answers follow the shape of the real box:

    * ``if [ -d /run/systemd/system ]`` -> ``systemd`` or ``skipped``;
    * ``systemctl list-units 'openswarm-executor-*.service'`` -> the unit list;
    * ``systemctl show <unit>`` -> ``TasksCurrent``/``TasksMax``/``ControlGroup``;
    * the ``cgroup.procs`` of the unit -> the pids of the cgroup;
    * ``ps -eo pid,ppid,stat,etimes,args`` -> the process table.
    """
    if "/run/systemd/system" in command:
        return "systemd\n" if host.systemd else "skipped\n"
    if "command -v systemctl" in command or "which systemctl" in command:
        if not host.systemd:
            # A box without systemd has no systemctl at all.
            raise subprocess.CalledProcessError(1, command)
        return "/usr/bin/systemctl\n"
    if not host.systemd and "systemctl" in command:
        # No systemd, no systemctl: the command fails like on the real box.
        raise subprocess.CalledProcessError(1, command)
    if "list-unit" in command:
        return host.listing() + "\n"
    if "systemctl show" in command:
        name = service_of(command, host)
        if name in host.fail_services:
            raise subprocess.CalledProcessError(1, command)
        info = host.services[name]
        return "TasksCurrent=%d\nTasksMax=%d\nControlGroup=%s\n" % (
            info["tasks_current"], info["tasks_max"], info["cgroup"])
    if "cgroup" in command:
        name = service_of(command, host)
        pids = host.services[name]["pids"] if name in host.services else []
        return " ".join(str(pid) for pid in pids) + "\n"
    if command.strip().startswith("ps") or " ps " in command:
        return host.ps
    return ""


def ssh_command(argv, host_name):
    """The remote command of an ssh call: everything after the host argument."""
    for index, argument in enumerate(argv):
        if argument == host_name or argument.endswith("@" + host_name):
            return " ".join(argv[index + 1:])
    raise AssertionError("the probe did not address the host %s (argv=%r)" % (host_name, argv))


def fake_ssh_main(argv):
    """Entry point of the fake ssh binary: answer one remote command like the host."""
    profile = json.loads(Path(os.environ["FAKE_SSH_PROFILE"]).read_text())
    host = FakeHost(
        name=profile["name"],
        systemd=profile["systemd"],
        services=profile["services"],
        ps=profile["ps"],
        fail_services=profile["fail_services"],
    )
    command = ssh_command(list(argv), host.name)
    log = Path(os.environ["FAKE_SSH_LOG"])
    with log.open("a") as handle:
        handle.write(command + "\n")
    try:
        sys.stdout.write(answer_command(host, command))
    except subprocess.CalledProcessError as error:
        sys.stderr.write(str(error) + "\n")
        return error.returncode or 1
    return 0


def call_probe(name, *args, **kwargs):
    """Call an entry point of the probe with the fake ssh runner.

    The runner is passed as the ``run`` keyword when the entry point takes it, so the
    call does not depend on the position of the parameter.
    """
    function = getattr(MODULE, name, None)
    if not callable(function):
        raise AssertionError("the executor task probe must expose %s()" % name)
    runner = kwargs.pop("runner", None)
    try:
        parameters = list(inspect.signature(function).parameters)
    except (TypeError, ValueError):
        parameters = []
    if "run" in parameters:
        return function(*args, run=runner, **kwargs)
    if runner is not None and len(parameters) > len(args):
        # the runner is the second positional parameter
        return function(*args, runner, **kwargs)
    return function(*args, **kwargs)


def find_executor_payload(health, host):
    """The executor task payload the probe wrote for a host.

    The probe owns the shape of the payload, not the test: the payload is looked up in
    the host entry first and, when the host entry is the plain probe status the health
    loop writes, in the rest of the document.  A payload is a mapping with a ``status``
    and a ``services`` list.
    """
    entry = health.get("hosts", {}).get(host)
    for scope in (entry, health):
        payload = search_payload(scope)
        if payload is not None:
            return payload
    return None


def search_payload(node):
    """The first executor task payload inside a JSON document."""
    if isinstance(node, dict):
        if isinstance(node.get("services"), list) and "status" in node:
            return node
        for value in node.values():
            found = search_payload(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = search_payload(item)
            if found is not None:
                return found
    return None


def host_status(health, host):
    """The probe status of a host in the health summary."""
    entry = health.get("hosts", {}).get(host)
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("status")
    return None


class ProbeTestCase(unittest.TestCase):
    """The shared fixtures of the probe tests."""

    def setUp(self):
        if MODULE is None:
            self.fail(
                "the executor task health probe of #1826 is not in the repo: expected %s "
                "(the probe samples the executor services, writes health.json and raises "
                "the watermark alerts)" % ", ".join(str(path.relative_to(ROOT)) for path in PROBE_CANDIDATES))
        self.root = Path(tempfile.mkdtemp(dir=str(self.scratch_dir())))
        self.addCleanup(self._cleanup)

    @staticmethod
    def scratch_dir():
        """A temporary directory inside the worktree (the repo keeps `.tmp/` ignored)."""
        base = ROOT / ".tmp"
        base.mkdir(exist_ok=True)
        return base

    def _cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- the hosts of the tests ----------------------------------------------------
    def ok_host(self):
        """A host whose executor service is healthy."""
        host = FakeHost()
        host.service(SERVICE, tasks_current=100, tasks_max=1000, pids=[PARENT_PID])
        host.ps = executor_ps(zombies=0)
        return host

    def soft_host(self):
        """A host whose executor service crossed the soft watermark (zombies)."""
        host = FakeHost()
        pids = [PARENT_PID] + [PARENT_PID + 1 + index for index in range(51)]
        host.service(SERVICE, tasks_current=120, tasks_max=1000, pids=pids)
        host.ps = executor_ps(zombies=51)
        return host

    def hard_host(self):
        """A host whose executor service crossed the hard watermark (task ratio)."""
        host = FakeHost()
        host.service(SERVICE, tasks_current=900, tasks_max=1000, pids=[PARENT_PID])
        host.ps = executor_ps(zombies=3)
        return host

    def sweep_host(self, count=3):
        """A host with the duplicate `fleet-capacity --sweep-loop gocc` processes."""
        host = FakeHost()
        host.service(SERVICE, tasks_current=100, tasks_max=1000, pids=[PARENT_PID])
        host.ps = sweep_ps(count=count)
        return host

    # -- the probe calls -----------------------------------------------------------
    def collect(self, host):
        """Collect one host and return the result of the probe."""
        outcome = call_probe("collect", host.name, runner=host.run)
        if isinstance(outcome, tuple):
            return outcome[0]
        return outcome

    def update(self, host, health=None, snapshot_dir=None):
        """Run the probe for one host and return (result, health_path, alert_path)."""
        health_path = self.root / "health.json"
        if health is not None:
            health_path.write_text(json.dumps(health, indent=2) + "\n")
        alert_path = self.root / "health.alert"
        if snapshot_dir is None:
            snapshot_dir = self.root / "snapshots"
        result = call_probe(
            "update", host.name, health_path, alert_path, snapshot_dir, runner=host.run)
        if isinstance(result, tuple):
            result = result[0]
        return result, health_path, alert_path

    def snapshots(self, snapshot_dir=None):
        """The ps snapshots the probe saved."""
        directory = Path(snapshot_dir) if snapshot_dir else self.root / "snapshots"
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.iterdir() if path.is_file())


class ExecutorProbePresenceTests(ProbeTestCase):
    """The probe of the issue has to be in the repo at all."""

    def test_the_executor_task_probe_ships_in_the_repo(self):
        self.assertIsNotNone(
            PROBE_PATH,
            "the executor task health probe of #1826 is missing from the repo (looked at %s)" % ", ".join(
                str(path.relative_to(ROOT)) for path in PROBE_CANDIDATES))


class ExecutorWatermarkThresholdTests(ProbeTestCase):
    """The three levels of the watermark: ok, soft and hard."""

    def probe_service(self, tasks_current, tasks_max, zombies):
        """The probe result of one service with the given watermark."""
        pids = set([PARENT_PID] + [PARENT_PID + 1 + index for index in range(zombies)])
        show = "TasksCurrent=%d\nTasksMax=%d\nControlGroup=/system.slice/%s\n" % (
            tasks_current, tasks_max, SERVICE)
        return call_probe("inspect_service", SERVICE, show, pids, executor_ps(zombies=zombies))

    def test_a_healthy_executor_service_is_ok(self):
        result = self.probe_service(100, 1000, 0)
        self.assertEqual(result["level"], "ok", result)
        self.assertEqual(result["zombie_count"], 0, result)

    def test_more_than_fifty_zombies_is_soft(self):
        result = self.probe_service(120, 1000, 51)
        self.assertEqual(result["level"], "soft", result)
        self.assertEqual(result["zombie_count"], 51, result)

    def test_more_than_two_hundred_zombies_is_hard(self):
        result = self.probe_service(120, 1000, 201)
        self.assertEqual(result["level"], "hard", result)
        self.assertEqual(result["zombie_count"], 201, result)

    def test_the_task_ratio_over_sixty_percent_is_soft(self):
        result = self.probe_service(601, 1000, 0)
        self.assertEqual(result["level"], "soft", result)
        self.assertAlmostEqual(result["tasks_ratio"], 0.601, places=3, msg=result)

    def test_the_task_ratio_over_eighty_five_percent_is_hard(self):
        result = self.probe_service(900, 1000, 0)
        self.assertEqual(result["level"], "hard", result)
        self.assertAlmostEqual(result["tasks_ratio"], 0.9, places=3, msg=result)

    def test_the_zombie_parent_pids_are_reported(self):
        result = self.probe_service(120, 1000, 51)
        self.assertEqual(result["zombie_parent_pids"], [PARENT_PID], result)

    def test_an_incomplete_systemctl_sample_is_not_collected(self):
        show = "TasksCurrent=10\n"
        try:
            result = call_probe("inspect_service", SERVICE, show, set(), executor_ps(zombies=0))
        except ValueError:
            return  # the probe rejects the incomplete sample
        self.assertEqual(result.get("level"), "uncollected", result)
        self.assertFalse(result.get("collected", False), result)


class ExecutorServiceCollectionTests(ProbeTestCase):
    """The probe enumerates the executor services of a host and samples them."""

    def test_the_executor_services_are_enumerated_over_ssh(self):
        host = self.ok_host()
        host.service(SERVICE2, tasks_current=50, tasks_max=1000, pids=[])
        result = self.collect(host)
        self.assertEqual(result.get("status"), "collected", result)
        names = [entry.get("service") for entry in result.get("services", [])]
        self.assertIn(SERVICE, names, result)
        self.assertIn(SERVICE2, names, result)
        listing = [command for command in host.commands if "list-unit" in command]
        self.assertTrue(listing, "the probe must enumerate the executor units (commands=%r)" % host.commands)
        self.assertTrue(
            any("openswarm-executor" in command for command in listing),
            "the probe must ask for the openswarm-executor units (commands=%r)" % host.commands)
        shown = [command for command in host.commands if "show" in command and SERVICE in command]
        self.assertTrue(shown, "the probe must ask for the task counters of the unit (commands=%r)" % host.commands)

    def test_a_failed_service_probe_is_uncollected_and_never_alerts(self):
        host = self.ok_host()
        host.fail_services.add(SERVICE)
        result, health_path, alert_path = self.update(host)
        services = result.get("services", [])
        self.assertTrue(services, result)
        entry = services[0]
        self.assertFalse(entry.get("collected", True), result)
        self.assertEqual(entry.get("level"), "uncollected", result)
        self.assertFalse(alert_path.exists(), "a failed sample must not alert")
        health = json.loads(health_path.read_text())
        payload = find_executor_payload(health, HOST)
        self.assertIsNotNone(payload, health)
        entries = payload.get("services", [])
        self.assertTrue(entries, health)
        self.assertFalse(entries[0].get("collected", True), health)
        self.assertEqual(entries[0].get("level"), "uncollected", health)

    def test_a_failed_host_probe_is_uncollected_and_never_alerts(self):
        host = self.ok_host()

        def broken(command):
            host.commands.append(command)
            raise subprocess.CalledProcessError(1, command)

        outcome = call_probe("collect", host.name, runner=broken)
        result = outcome[0] if isinstance(outcome, tuple) else outcome
        self.assertEqual(result.get("status"), "uncollected", result)
        self.assertFalse(result.get("services"), result)

    def test_a_host_without_systemd_is_skipped(self):
        host = self.ok_host()
        host.systemd = False
        result, health_path, alert_path = self.update(host)
        self.assertEqual(result.get("status"), "skipped", result)
        self.assertFalse(alert_path.exists(), "a host without systemd must not alert")
        self.assertFalse(
            [command for command in host.commands if "list-unit" in command],
            "a host without systemd must not be sampled (commands=%r)" % host.commands)
        health = json.loads(health_path.read_text())
        payload = find_executor_payload(health, HOST)
        self.assertIsNotNone(payload, health)
        self.assertEqual(payload.get("status"), "skipped", health)


class ExecutorAlertChainTests(ProbeTestCase):
    """The alert chain: ps snapshot first, then the alert - and never a restart."""

    def test_the_ps_snapshot_is_saved_before_the_alert(self):
        host = self.soft_host()
        result, health_path, alert_path = self.update(host)
        self.assertEqual(result.get("services", [{}])[0].get("level"), "soft", result)
        snapshots = self.snapshots()
        self.assertEqual(len(snapshots), 1, "the probe must save exactly one ps snapshot")
        saved = snapshots[0].read_text()
        self.assertIn("PID", saved, "the snapshot must be the full ps table")
        self.assertIn(str(PARENT_PID), saved, "the snapshot must hold the live process table")
        alert = alert_path.read_text()
        self.assertIn(snapshots[0].name, alert, "the alert must point at the saved snapshot")

    def test_the_alert_names_the_service_and_the_zombie_parent_pid(self):
        host = self.soft_host()
        result, health_path, alert_path = self.update(host)
        self.assertTrue(alert_path.exists(), "the soft watermark must alert")
        alert = alert_path.read_text()
        self.assertIn(SERVICE, alert, "the alert must name the service")
        self.assertIn(str(PARENT_PID), alert, "the alert must name the parent pid of the zombies")
        self.assertIn("soft", alert, "the alert must carry the level")

    def test_a_hard_watermark_alerts(self):
        host = self.hard_host()
        result, health_path, alert_path = self.update(host)
        self.assertEqual(result.get("services", [{}])[0].get("level"), "hard", result)
        alert = alert_path.read_text()
        self.assertIn(SERVICE, alert)
        self.assertIn("hard", alert)

    def test_the_probe_never_restarts_or_kills_the_executor_service(self):
        host = self.hard_host()
        self.update(host)
        for command in host.commands:
            for forbidden in ("restart", "kill", "stop"):
                self.assertNotIn(
                    forbidden, command,
                    "the probe only alerts, it must not run %r (commands=%r)" % (forbidden, host.commands))

    def test_no_alert_is_written_when_the_snapshot_cannot_be_saved(self):
        host = self.soft_host()
        blocked = self.root / "blocked"
        blocked.write_text("not a directory\n")
        try:
            self.update(host, snapshot_dir=blocked)
        except Exception:
            pass  # the probe may report the failure loudly
        alert_path = self.root / "health.alert"
        self.assertFalse(
            alert_path.exists() and alert_path.read_text().strip(),
            "no alert may be written when the ps snapshot could not be saved")

    def test_the_probe_result_lands_in_the_host_entry_of_health_json(self):
        host = self.soft_host()
        health = {
            "at": "2026-11-03 19:15:00",
            "hosts": {HOST: "degraded", OTHER_HOST: "ok"},
            "hosts_ok": [OTHER_HOST],
            "hosts_degraded": [HOST],
            "cooldown": [],
        }
        result, health_path, alert_path = self.update(host, health=health)
        written = json.loads(health_path.read_text())
        payload = find_executor_payload(written, HOST)
        self.assertIsNotNone(payload, "the probe result must land in the host entry of health.json")
        self.assertEqual(payload.get("status"), "collected", written)
        services = payload.get("services", [])
        self.assertTrue(services, written)
        self.assertEqual(services[0].get("service"), SERVICE, written)
        self.assertEqual(services[0].get("zombie_count"), 51, written)
        self.assertEqual(services[0].get("zombie_parent_pids"), [PARENT_PID], written)
        # the health summary of the control plane must survive the probe
        self.assertEqual(written.get("hosts", {}).get(OTHER_HOST), "ok", written)
        self.assertEqual(written.get("hosts_ok"), [OTHER_HOST], written)
        self.assertEqual(written.get("hosts_degraded"), [HOST], written)
        self.assertEqual(written.get("at"), "2026-11-03 19:15:00", written)
        self.assertEqual(host_status(written, HOST), "degraded", written)


class ExecutorSweepLoopTests(ProbeTestCase):
    """The duplicate `fleet-capacity --sweep-loop gocc` processes of the card."""

    def test_more_than_two_sweep_loop_processes_alert_with_the_parent_run_id(self):
        host = self.sweep_host(count=3)
        result, health_path, alert_path = self.update(host)
        self.assertTrue(alert_path.exists(), "more than two sweep-loop processes must alert")
        alert = alert_path.read_text()
        self.assertIn("sweep-loop", alert, "the alert must name the sweep loop")
        self.assertIn("627900", alert, "the alert must list the parent processes")
        self.assertIn(RUN_ID, alert, "the alert must carry the run id of the parent")
        written = json.loads(health_path.read_text())
        payload = find_executor_payload(written, HOST)
        self.assertIsNotNone(payload, written)
        sweep = search_key(payload, "sweep")
        self.assertIsNotNone(sweep, "the probe must report the sweep loop processes: %r" % (payload,))
        self.assertEqual(sweep.get("count"), 3, sweep)

    def test_two_sweep_loop_processes_do_not_alert(self):
        host = self.sweep_host(count=2)
        result, health_path, alert_path = self.update(host)
        if alert_path.exists():
            self.assertNotIn("sweep-loop", alert_path.read_text())


def search_key(node, needle):
    """The first mapping below `node` whose key contains `needle`."""
    if isinstance(node, dict):
        for key, value in node.items():
            if needle in str(key).lower() and isinstance(value, dict):
                return value
        for value in node.values():
            found = search_key(value, needle)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = search_key(item, needle)
            if found is not None:
                return found
    return None


class ExecutorProbeCommandLineTests(ProbeTestCase):
    """The probe is invoked by the control plane health check as a command."""

    def cli_options(self):
        """The option names of the probe command line (read from its --help)."""
        result = subprocess.run(
            [sys.executable, str(PROBE_PATH), "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        text = result.stdout or ""
        options = re.findall(r"(--[a-zA-Z0-9][a-zA-Z0-9-]*)", text)
        return text, options

    def option(self, options, needle, help_text):
        """The option of the probe command line that carries the given word."""
        for option in options:
            if needle in option:
                return option
        self.fail("the probe command line must offer a %s option (--help: %s)" % (needle, help_text))

    def test_the_probe_updates_health_json_and_the_alert_from_the_command_line(self):
        help_text, options = self.cli_options()
        health_option = self.option(options, "health", help_text)
        alert_option = self.option(options, "alert", help_text)
        snapshot_option = self.option(options, "snapshot", help_text)

        host = self.soft_host()
        profile = self.root / "host.json"
        profile.write_text(json.dumps(host.profile()))
        log = self.root / "ssh.log"
        fake_ssh = self.root / "ssh"
        fake_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import importlib.util, os, sys\n"
            "spec = importlib.util.spec_from_file_location('fake_host_support', os.environ['FAKE_SSH_SUPPORT'])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "sys.exit(module.fake_ssh_main(sys.argv[1:]))\n")
        fake_ssh.chmod(0o755)

        health_path = self.root / "health.json"
        alert_path = self.root / "health.alert"
        snapshot_dir = self.root / "snapshots"
        environment = dict(os.environ)
        environment["PATH"] = str(self.root) + os.pathsep + environment.get("PATH", "")
        environment["FAKE_SSH_PROFILE"] = str(profile)
        environment["FAKE_SSH_LOG"] = str(log)
        environment["FAKE_SSH_SUPPORT"] = str(Path(__file__).resolve())

        result = subprocess.run(
            [sys.executable, str(PROBE_PATH), HOST,
             health_option, str(health_path),
             alert_option, str(alert_path),
             snapshot_option, str(snapshot_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        self.assertEqual(result.returncode, 0, "the probe failed: %s%s" % (result.stdout, result.stderr))

        written = json.loads(health_path.read_text())
        payload = find_executor_payload(written, HOST)
        self.assertIsNotNone(payload, "the command line probe must write the host entry: %r" % (written,))
        self.assertEqual(payload.get("status"), "collected", written)
        alert = alert_path.read_text()
        self.assertIn(SERVICE, alert, "the command line probe must alert")
        self.assertTrue(self.snapshots(snapshot_dir), "the command line probe must save the ps snapshot")
        self.assertTrue(log.exists(), "the command line probe must sample the host over ssh")


if __name__ == "__main__":
    unittest.main()
