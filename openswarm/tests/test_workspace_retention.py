"""Executor workspace retention (Refs #1698).

``openswarm/lib`` is a namespace package (no ``__init__.py``), so the module
is loaded by file path instead of by import.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
MODULE_PATH = ROOT / "lib" / "resources" / "workspace_retention.py"
spec = importlib.util.spec_from_file_location("resources_workspace_retention", MODULE_PATH)
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

NOW = 1800000000.0
HOUR = 3600


def usage_at(percent):
    """A disk probe that reports a fixed watermark."""
    def usage(path):
        return {"path": str(path), "disk_free_gb": 1.0, "disk_used_pct": percent}
    return usage


def no_holders(path):
    """A holder probe: nobody works in the workspace."""
    return []


class RetentionPolicyTests(unittest.TestCase):
    """The retention windows and the disk watermarks come from the policy."""

    def test_defaults_are_conservative(self):
        config = retention.retention_config({})
        self.assertEqual(config["success_hours"], 24.0)
        self.assertEqual(config["failure_hours"], 72.0)
        self.assertEqual(config["disk_threshold_pct"], 85.0)
        self.assertEqual(config["disk_alert_pct"], 85.0)
        self.assertFalse(config["reclaim_abandoned"])

    def test_policy_overrides_the_defaults(self):
        config = retention.retention_config(
            {"executor": {"retention": {"success_hours": 6, "failure_hours": 12,
                                         "disk_threshold_pct": 70, "disk_alert_pct": 60}}})
        self.assertEqual(config["success_hours"], 6.0)
        self.assertEqual(config["failure_hours"], 12.0)
        self.assertEqual(config["disk_threshold_pct"], 70.0)
        self.assertEqual(config["disk_alert_pct"], 60.0)

    def test_a_broken_value_is_refused_instead_of_widening_the_cleanup(self):
        for section in ({"success_hours": -1}, {"failure_hours": "24h"}, {"disk_threshold_pct": 140},
                        {"disk_alert_pct": True}, {"reclaim_abandoned": "yes"}):
            with self.subTest(section=section):
                with self.assertRaises(ValueError):
                    retention.retention_config({"executor": {"retention": section}})

    def test_the_shipped_policy_keeps_the_documented_windows(self):
        policy = json.loads((ROOT / "policy.json").read_text(encoding="utf-8"))
        config = retention.retention_config(policy)
        self.assertEqual(config["success_hours"], 24.0)
        self.assertEqual(config["failure_hours"], 72.0)
        self.assertEqual(config["disk_threshold_pct"], 85.0)
        self.assertEqual(config["disk_alert_pct"], 85.0)
        self.assertFalse(config["reclaim_abandoned"])


class RetentionPlanTests(unittest.TestCase):
    """The tier of a run decides how long its workspace is kept."""

    def setUp(self):
        self.config = retention.retention_config({})

    def record(self, tier, age, status="done", state="finished", size=1024, run_id="r1"):
        return {"id": run_id, "path": f"/runs/{run_id}", "tier": tier, "status": status,
                "state": state, "reference": NOW - age, "size_bytes": size}

    def test_a_successful_run_is_kept_for_24_hours(self):
        for age, expired in ((23 * HOUR, False), (24 * HOUR, False), (24 * HOUR + 1, True)):
            with self.subTest(age=age):
                plan = retention.plan_retention([self.record("success", age)], NOW, self.config, 90.0)
                self.assertEqual(len(plan["candidates"]), 1 if expired else 0)

    def test_a_failed_run_is_kept_for_72_hours(self):
        for age, expired in ((71 * HOUR, False), (72 * HOUR, False), (72 * HOUR + 1, True)):
            with self.subTest(age=age):
                plan = retention.plan_retention([self.record("failure", age, status="failed")],
                                                NOW, self.config, 90.0)
                self.assertEqual(len(plan["candidates"]), 1 if expired else 0)

    def test_a_running_run_is_never_a_candidate(self):
        plan = retention.plan_retention([self.record("running", 365 * 24 * HOUR, status="")],
                                        NOW, self.config, 99.0)
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["expired"], [])
        self.assertEqual(plan["retained"][0]["reason"], "run in flight")

    def test_the_windows_are_configurable(self):
        config = retention.retention_config({"executor": {"retention": {"success_hours": 1}}})
        plan = retention.plan_retention([self.record("success", 2 * HOUR)], NOW, config, 90.0)
        self.assertEqual(len(plan["candidates"]), 1)


class RetentionDiskTests(unittest.TestCase):
    """The disk watermark gates the cleanup."""

    def setUp(self):
        self.config = retention.retention_config({})
        self.record = {"id": "r1", "path": "/runs/r1", "tier": "success", "status": "done",
                       "state": "finished", "reference": NOW - 100 * HOUR, "size_bytes": 10}

    def test_a_healthy_host_keeps_the_expired_workspaces(self):
        plan = retention.plan_retention([self.record], NOW, self.config, 40.0)
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(len(plan["expired"]), 1)
        self.assertIn("below threshold", plan["reason"])

    def test_a_full_host_cleans_the_expired_workspaces(self):
        plan = retention.plan_retention([self.record], NOW, self.config, 85.0)
        self.assertEqual(len(plan["candidates"]), 1)
        self.assertEqual(plan["priority"], "high")

    def test_an_unknown_watermark_deletes_nothing(self):
        plan = retention.plan_retention([self.record], NOW, self.config, None)
        self.assertEqual(plan["candidates"], [])
        self.assertIn("unknown", plan["reason"])

    def test_the_priority_is_high_only_above_the_alert_watermark(self):
        config = retention.retention_config({"executor": {"retention": {"disk_threshold_pct": 50,
                                                                        "disk_alert_pct": 90}}})
        plan = retention.plan_retention([self.record], NOW, config, 60.0)
        self.assertEqual(plan["priority"], "normal")
        plan = retention.plan_retention([self.record], NOW, config, 95.0)
        self.assertEqual(plan["priority"], "high")

    def test_a_pressured_host_frees_the_biggest_workspaces_first(self):
        records = [dict(self.record, id="small", size_bytes=10),
                   dict(self.record, id="big", size_bytes=1000)]
        plan = retention.plan_retention(records, NOW, self.config, 95.0)
        self.assertEqual([row["run_id"] for row in plan["candidates"]], ["big", "small"])


class RunRecordTests(unittest.TestCase):
    """The run directory decides the tier of a run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def write_run(self, run_id, status, state="finished", finished=None, runner_pid=None, cwd=None):
        path = self.runs / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "task.md").write_text("do the thing\n", encoding="utf-8")
        record = {"id": run_id, "state": state, "status": status}
        if finished is not None:
            record["finished"] = finished
        if runner_pid is not None:
            record["runner_pid"] = runner_pid
        if cwd is not None:
            record["cwd"] = str(cwd)
        (path / "status.json").write_text(json.dumps(record), encoding="utf-8")
        (path / "result.json").write_text(json.dumps({"id": run_id, "status": status}), encoding="utf-8")
        return path

    def test_a_finished_successful_run_is_a_success(self):
        self.write_run("r1", "done", finished=NOW)
        record = retention.read_run(str(self.runs / "r1"), holders=no_holders)
        self.assertEqual(record["tier"], "success")

    def test_a_finished_failed_run_is_a_failure(self):
        self.write_run("r2", "failed", finished=NOW)
        record = retention.read_run(str(self.runs / "r2"), holders=no_holders)
        self.assertEqual(record["tier"], "failure")

    def test_a_live_run_is_never_cleaned(self):
        self.write_run("r3", "", state="running", runner_pid=4242)
        record = retention.read_run(str(self.runs / "r3"), alive=lambda pid: True, holders=no_holders)
        self.assertEqual(record["tier"], "running")
        self.assertTrue(record["busy"])

    def test_an_in_flight_run_is_kept_even_when_its_runner_is_gone(self):
        # The executor can die and leave children behind that still write into
        # the workspace: an in-flight run is never a cleanup candidate.
        self.write_run("r4", "", state="running", runner_pid=4242)
        record = retention.read_run(str(self.runs / "r4"), alive=lambda pid: False, holders=no_holders)
        self.assertEqual(record["tier"], "running")
        self.assertFalse(record["busy"])

    def test_an_abandoned_run_is_only_reclaimed_when_the_policy_says_so(self):
        self.write_run("r5", "", state="running", runner_pid=4242)
        config = retention.retention_config({"executor": {"retention": {"reclaim_abandoned": True}}})
        record = retention.read_run(str(self.runs / "r5"), alive=lambda pid: False,
                                    holders=no_holders, config=config)
        self.assertEqual(record["tier"], "failure")

    def test_the_age_of_an_abandoned_run_is_measured_from_its_last_activity(self):
        path = self.write_run("r6", "", state="running", runner_pid=4242)
        old = time.time() - 100 * HOUR
        for entry in path.iterdir():
            os.utime(entry, (old, old))
        os.utime(path, (old, old))
        record = retention.read_run(str(path), alive=lambda pid: False, holders=no_holders)
        self.assertLessEqual(record["reference"], old + 1)
        self.assertGreater(record["reference"], old - 10)

    def test_a_run_whose_process_table_cannot_be_read_is_kept(self):
        self.write_run("r7", "done", finished=NOW)
        record = retention.read_run(str(self.runs / "r7"), holders=lambda path: None)
        self.assertTrue(record["busy"])

    def test_the_scan_skips_files_and_symlinks(self):
        self.write_run("r8", "done", finished=NOW)
        (self.runs / "stray.txt").write_text("not a run\n", encoding="utf-8")
        os.symlink(str(self.runs / "r8"), str(self.runs / "link"))
        records = retention.scan_runs(str(self.runs), holders=no_holders)
        self.assertEqual([row["id"] for row in records], ["r8"])


class WorkspaceDiscoveryTests(unittest.TestCase):
    """The worktree of a run is found, not only its run directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "runs").mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_id = "20261103-010203-abcdef"

    def write_run(self, state="finished", status="done", finished=None):
        path = self.home / "runs" / self.run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "task.md").write_text("payload\n", encoding="utf-8")
        record = {"id": self.run_id, "state": state, "status": status, "cwd": str(self.repo)}
        if finished is not None:
            record["finished"] = finished
        (path / "status.json").write_text(json.dumps(record), encoding="utf-8")
        (path / "result.json").write_text(json.dumps({"id": self.run_id, "status": status}), encoding="utf-8")
        return path

    def test_the_worktree_next_to_the_repository_is_a_target(self):
        self.write_run(finished=NOW)
        worktree = self.root / f".openswarm-wt-repo-{self.run_id}"
        worktree.mkdir()
        records = retention.scan_runs(str(self.home / "runs"), holders=no_holders, home_root=str(self.home))
        targets = [target["path"] for target in records[0]["targets"]]
        self.assertIn(str(self.home / "runs" / self.run_id), targets)
        self.assertIn(str(worktree), targets)

    def test_the_managed_worktree_under_the_home_is_a_target(self):
        self.write_run(finished=NOW)
        worktree = self.home / "worktrees" / f"repo-{self.run_id}"
        worktree.mkdir(parents=True)
        records = retention.scan_runs(str(self.home / "runs"), holders=no_holders, home_root=str(self.home))
        targets = [target["path"] for target in records[0]["targets"]]
        self.assertIn(str(worktree), targets)

    def test_the_run_directory_is_the_last_target_that_goes(self):
        self.write_run(finished=NOW)
        worktree = self.root / f".openswarm-wt-repo-{self.run_id}"
        worktree.mkdir()
        records = retention.scan_runs(str(self.home / "runs"), holders=no_holders, home_root=str(self.home))
        roots = retention.workspace_roots(str(self.home), records)
        self.assertIn(str(self.root), roots["siblings"])
        outcome = retention.expire_workspaces(
            [{"run_id": self.run_id, "run_dir": str(self.home / "runs" / self.run_id),
              "targets": records[0]["targets"], "tier": "success", "status": "done",
              "age_seconds": 100 * HOUR}],
            roots, audit_path=str(self.home / "audit.jsonl"), now=NOW, holders=no_holders)
        self.assertEqual([row["kind"] for row in outcome["deleted"]], ["worktree", "run_dir"])
        self.assertFalse(worktree.exists())
        self.assertFalse((self.home / "runs" / self.run_id).exists())


class WorktreeMetadataTests(unittest.TestCase):
    """The ``.git`` pointer of a worktree is never followed blindly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "runs").mkdir(parents=True)
        (self.home / "worktrees").mkdir(parents=True)
        self.run_id = "r1"

    def worktree(self, name=None):
        path = self.home / "worktrees" / (name or f"repo-{self.run_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def pointer(self, path, gitdir):
        (path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        return path

    def test_a_pointer_to_an_arbitrary_directory_is_not_followed(self):
        precious = self.root / "precious"
        precious.mkdir()
        (precious / "keep.txt").write_text("not ours\n", encoding="utf-8")
        worktree = self.pointer(self.worktree(), precious)
        self.assertIsNone(retention.owned_worktree_gitdir(str(worktree)))
        retention.remove_worktree(str(worktree))
        # The directory the pointer named is untouched.
        self.assertTrue((precious / "keep.txt").exists())
        self.assertFalse(worktree.exists())

    def test_the_metadata_of_another_worktree_is_not_removed(self):
        repo = self.root / "repo"
        metadata = repo / ".git" / "worktrees" / "other"
        metadata.mkdir(parents=True)
        (metadata / "gitdir").write_text(str(self.root / "elsewhere" / ".git"), encoding="utf-8")
        worktree = self.pointer(self.worktree(), metadata)
        self.assertIsNone(retention.owned_worktree_gitdir(str(worktree)))
        retention.remove_worktree(str(worktree))
        self.assertTrue(metadata.exists())

    def test_the_metadata_of_the_worktree_goes_with_it(self):
        repo = self.root / "repo"
        metadata = repo / ".git" / "worktrees" / "wt1"
        metadata.mkdir(parents=True)
        worktree = self.worktree()
        (metadata / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
        self.pointer(worktree, metadata)
        self.assertEqual(retention.owned_worktree_gitdir(str(worktree)), str(metadata))
        retention.remove_worktree(str(worktree))
        self.assertFalse(worktree.exists())
        self.assertFalse(metadata.exists())

    def test_a_worktree_outside_the_roots_is_refused_by_the_cleanup(self):
        stranger = self.root / ".openswarm-wt-repo-other"
        stranger.mkdir()
        (stranger / ".git").write_text(f"gitdir: {self.root / 'precious'}\n", encoding="utf-8")
        roots = retention.workspace_roots(str(self.home))
        outcome = retention.expire_workspaces(
            [{"run_id": self.run_id, "run_dir": str(self.home / "runs" / self.run_id),
              "targets": [{"kind": "worktree", "path": str(stranger)}], "tier": "success",
              "status": "done", "age_seconds": 100 * HOUR}],
            roots, audit_path=str(self.home / "audit.jsonl"), now=NOW, holders=no_holders)
        self.assertEqual(outcome["deleted"], [])
        self.assertEqual(outcome["refused"][0]["reason"], "outside the workspace roots")
        self.assertTrue(stranger.exists())


class PathSafetyTests(unittest.TestCase):
    """The cleanup never leaves the allowed workspace roots."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "runs").mkdir(parents=True)
        self.audit = self.root / "retention-audit.jsonl"
        self.run_id = "r1"

    def candidate(self, path, kind="run_dir", run_id=None):
        return {"run_id": run_id or self.run_id, "path": str(path), "run_dir": str(path),
                "targets": [{"kind": kind, "path": str(path)}], "tier": "success",
                "status": "done", "age_seconds": 100 * HOUR}

    def expire(self, candidate, roots=None):
        roots = roots or retention.workspace_roots(str(self.home))
        return retention.expire_workspaces([candidate], roots, audit_path=str(self.audit), now=NOW,
                                           holders=no_holders)

    def test_a_candidate_outside_the_root_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not ours\n", encoding="utf-8")
        outcome = self.expire(self.candidate(outside))
        self.assertEqual(outcome["deleted"], [])
        self.assertEqual(outcome["refused"][0]["reason"], "outside the workspace roots")
        self.assertTrue((outside / "keep.txt").exists())

    def test_the_root_itself_is_never_removed(self):
        outcome = self.expire(self.candidate(self.home))
        self.assertEqual(outcome["deleted"], [])
        self.assertEqual(len(outcome["refused"]), 1)
        self.assertTrue(self.home.exists())

    def test_a_symlink_out_of_the_root_is_refused(self):
        outside = self.root / "outside-target"
        outside.mkdir()
        link = self.home / "runs" / "escape"
        os.symlink(str(outside), str(link))
        outcome = self.expire(self.candidate(link))
        self.assertEqual(outcome["deleted"], [])
        self.assertTrue(outside.exists())

    def test_a_worktree_outside_the_home_without_the_run_id_is_refused(self):
        stranger = self.root / ".openswarm-wt-repo-other-run"
        stranger.mkdir()
        roots = retention.workspace_roots(str(self.home))
        outcome = self.expire(self.candidate(stranger, kind="worktree"), roots)
        self.assertEqual(outcome["deleted"], [])
        self.assertTrue(stranger.exists())

    def test_a_worktree_outside_the_home_of_another_run_is_refused(self):
        stranger = self.root / ".openswarm-wt-repo-other-run"
        stranger.mkdir()
        roots = retention.workspace_roots(str(self.home), [{"repo": str(self.root / "repo")}])
        outcome = self.expire(self.candidate(stranger, kind="worktree"), roots)
        self.assertEqual(outcome["deleted"], [])
        self.assertTrue(stranger.exists())

    def test_the_worktree_of_the_run_next_to_its_repository_is_allowed(self):
        repo = self.root / "repo"
        repo.mkdir()
        worktree = self.root / f".openswarm-wt-repo-{self.run_id}"
        worktree.mkdir()
        roots = retention.workspace_roots(str(self.home), [{"repo": str(repo)}])
        outcome = self.expire(self.candidate(worktree, kind="worktree"), roots)
        self.assertEqual(len(outcome["deleted"]), 1)
        self.assertFalse(worktree.exists())


class LiveProcessTests(unittest.TestCase):
    """A run that is worked on is never deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "runs").mkdir(parents=True)
        self.audit = self.root / "retention-audit.jsonl"
        # A fake /proc: the holder probe only needs "<pid>/cwd" symlinks.
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def write_run(self, run_id="r1", state="finished", status="done", finished=NOW):
        path = self.home / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "task.md").write_text("payload\n", encoding="utf-8")
        record = {"id": run_id, "state": state, "status": status}
        if finished is not None:
            record["finished"] = finished
        (path / "status.json").write_text(json.dumps(record), encoding="utf-8")
        return path

    def hold(self, pid, path):
        """Make the fake process ``pid`` work in ``path``."""
        entry = self.proc / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.realpath(str(path)), str(entry / "cwd"))

    def test_a_workspace_a_process_works_in_is_kept(self):
        path = self.write_run()
        records = retention.scan_runs(str(self.home / "runs"), alive=lambda pid: True,
                                      holders=lambda target: [4321])
        self.assertTrue(records[0]["busy"])
        plan = retention.plan_retention(records, NOW, retention.retention_config({}), 95.0)
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["retained"][0]["reason"], "workspace in use")

    def test_a_process_that_appears_after_the_scan_stops_the_deletion(self):
        # The scan saw an idle workspace; a process entered it afterwards.
        # The deletion must read the process table again and keep it.
        path = self.write_run(finished=NOW - 100 * HOUR)
        probe = retention.holder_probe(proc_root=str(self.proc))
        records = retention.scan_runs(str(self.home / "runs"), alive=lambda pid: False,
                                      holders=probe)
        self.assertFalse(records[0]["busy"])
        plan = retention.plan_retention(records, NOW, retention.retention_config({}), 95.0)
        self.assertEqual(len(plan["candidates"]), 1)
        self.hold(4321, path)
        outcome = retention.expire_workspaces(
            [{"run_id": "r1", "run_dir": str(path),
              "targets": [{"kind": "run_dir", "path": str(path)}], "tier": "success",
              "status": "done", "age_seconds": 100 * HOUR}],
            retention.workspace_roots(str(self.home)), audit_path=str(self.audit), now=NOW,
            alive=lambda pid: True, holders=probe)
        self.assertEqual(outcome["deleted"], [])
        self.assertIn("holds the workspace", outcome["skipped"][0]["reason"])
        self.assertTrue(path.exists())

    def test_the_probe_reads_the_process_table_again_after_a_refresh(self):
        path = self.write_run()
        probe = retention.holder_probe(proc_root=str(self.proc))
        self.assertEqual(probe(str(path)), [])
        self.hold(4321, path)
        # The snapshot of the scan is still cached ...
        self.assertEqual(probe(str(path)), [])
        probe.refresh()
        # ... and the refresh makes the probe read /proc again.
        self.assertEqual(probe(str(path)), [4321])

    def test_a_run_that_started_again_is_not_deleted(self):
        path = self.write_run(state="running", finished=None)
        candidate = {"run_id": "r1", "path": str(path), "run_dir": str(path),
                     "targets": [{"kind": "run_dir", "path": str(path)}], "tier": "success",
                     "status": "done", "age_seconds": 100 * HOUR}
        roots = retention.workspace_roots(str(self.home))
        outcome = retention.expire_workspaces([candidate], roots, audit_path=str(self.audit),
                                               now=NOW, holders=no_holders)
        self.assertEqual(outcome["deleted"], [])
        self.assertEqual(outcome["skipped"][0]["reason"], "run is in flight again")
        self.assertTrue(path.exists())

    def test_a_run_whose_process_came_back_is_not_deleted(self):
        path = self.write_run()
        (path / "status.json").write_text(json.dumps({"id": "r1", "state": "finished",
                                                       "status": "done", "finished": NOW,
                                                       "runner_pid": 4242}), encoding="utf-8")
        candidate = {"run_id": "r1", "path": str(path), "run_dir": str(path),
                     "targets": [{"kind": "run_dir", "path": str(path)}], "tier": "success",
                     "status": "done", "age_seconds": 100 * HOUR}
        roots = retention.workspace_roots(str(self.home))
        outcome = retention.expire_workspaces([candidate], roots, audit_path=str(self.audit),
                                               now=NOW, alive=lambda pid: True, holders=no_holders)
        self.assertEqual(outcome["deleted"], [])
        self.assertIn("alive", outcome["skipped"][0]["reason"])
        self.assertTrue(path.exists())

    def test_a_live_process_in_the_workspace_is_detected(self):
        path = self.write_run()
        holder = subprocess.Popen(["sleep", "30"], cwd=str(path))
        self.addCleanup(holder.kill)
        found = retention.workspace_holders(str(path))
        self.assertIsNotNone(found)
        self.assertIn(holder.pid, found)


class FreedBytesTests(unittest.TestCase):
    """The audit log says how much disk the cleanup really gave back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "runs").mkdir(parents=True)
        self.audit = self.home / "retention-audit.jsonl"

    def make_run(self, run_id, size=1000):
        path = self.home / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "task.md").write_text("x" * size, encoding="utf-8")
        return path

    def candidate(self, run_id, path, age=100 * HOUR, tier="success"):
        return {"run_id": run_id, "path": str(path), "run_dir": str(path),
                "targets": [{"kind": "run_dir", "path": str(path)}], "tier": tier,
                "status": "done", "age_seconds": age,
                "size_bytes": retention.allocated_bytes(str(path))}

    def test_the_freed_bytes_are_the_blocks_the_filesystem_held(self):
        path = self.make_run("r1", size=1000)
        blocks = retention.allocated_bytes(str(path))
        self.assertGreater(blocks, 0)
        # The apparent size is not what the disk gave back.
        self.assertNotEqual(blocks, retention.directory_size(str(path)))

    def test_a_hundred_expired_workspaces_release_what_the_audit_log_says(self):
        expected = 0
        candidates = []
        for index in range(100):
            path = self.make_run(f"run-{index:03d}", size=1000 + index)
            expected += retention.allocated_bytes(str(path))
            candidates.append(self.candidate(f"run-{index:03d}", path))
        free_before = retention.fs_free_bytes(str(self.home / "runs"))
        outcome = retention.expire_workspaces(candidates, retention.workspace_roots(str(self.home)),
                                               audit_path=str(self.audit), now=NOW, holders=no_holders)
        free_after = retention.fs_free_bytes(str(self.home / "runs"))
        self.assertEqual(len(outcome["deleted"]), 100)
        self.assertEqual(outcome["refused"], [])
        self.assertEqual(outcome["freed_bytes"], expected)
        entries = retention.audit_entries(str(self.audit))
        self.assertEqual(len(entries), 100)
        self.assertEqual(sum(entry["freed_bytes"] for entry in entries), expected)
        self.assertEqual(outcome["freed_bytes"], sum(entry["freed_bytes"] for entry in entries))
        # The filesystem really gave the space back: the free space of the
        # host grew by what the audit log claims, not by an estimate.
        released = free_after - free_before
        self.assertGreater(released, 0)
        self.assertLessEqual(abs(released - expected), max(128 * 1024, expected // 5))
        self.assertEqual(list((self.home / "runs").iterdir()), [])

    def test_the_audit_log_carries_the_run_id_the_age_and_the_freed_bytes(self):
        path = self.make_run("r1", size=1000)
        outcome = retention.expire_workspaces([self.candidate("r1", path, age=3600)],
                                               retention.workspace_roots(str(self.home)),
                                               audit_path=str(self.audit), now=NOW, holders=no_holders)
        self.assertFalse(path.exists())
        self.assertEqual(len(outcome["deleted"]), 1)
        entry = retention.audit_entries(str(self.audit))[0]
        self.assertEqual(entry["run_id"], "r1")
        self.assertEqual(entry["age_seconds"], 3600)
        self.assertGreater(entry["freed_bytes"], 0)
        self.assertEqual(entry["freed_bytes"], outcome["freed_bytes"])

    def test_a_file_hard_linked_outside_the_workspace_is_not_counted_as_released(self):
        path = self.make_run("r1", size=1000)
        payload = path / "task.md"
        outside = self.root / "outside-link.md"
        os.link(str(payload), str(outside))
        blocks = retention.allocated_bytes(str(path))
        released = retention.released_bytes(str(path))
        # The link outside the workspace keeps the blocks of the file: only
        # the directory itself goes away.
        self.assertLess(released, blocks)
        outcome = retention.expire_workspaces([self.candidate("r1", path)],
                                               retention.workspace_roots(str(self.home)),
                                               audit_path=str(self.audit), now=NOW, holders=no_holders)
        self.assertEqual(outcome["deleted"][0]["freed_bytes"], released)
        # The blocks of the file are still allocated: the outside link keeps
        # them, so the audit must not have claimed them.
        self.assertTrue(outside.exists())
        self.assertEqual(os.lstat(str(outside)).st_blocks * retention.BLOCK_SIZE, blocks - released)

    def test_a_file_that_a_process_holds_open_is_not_counted_as_released(self):
        path = self.make_run("r1", size=1000)
        payload = path / "task.md"
        blocks = retention.allocated_bytes(str(path))
        with open(payload, "rb") as handle:
            released = retention.released_bytes(str(path), retention.open_file_inodes())
            self.assertLess(released, blocks)
        # Once the descriptor is closed the blocks go away with the file.
        self.assertEqual(retention.released_bytes(str(path)), blocks)

    def test_the_audit_log_and_the_freed_space_agree(self):
        # The audited bytes are the blocks the filesystem gave back: the free
        # space of the host grows by about that amount.
        candidates = []
        for index in range(20):
            path = self.make_run(f"run-{index:03d}", size=2000 + index)
            candidates.append(self.candidate(f"run-{index:03d}", path))
        free_before = retention.fs_free_bytes(str(self.home / "runs"))
        outcome = retention.expire_workspaces(candidates, retention.workspace_roots(str(self.home)),
                                               audit_path=str(self.audit), now=NOW, holders=no_holders)
        free_after = retention.fs_free_bytes(str(self.home / "runs"))
        audited = outcome["freed_bytes"]
        entries = retention.audit_entries(str(self.audit))
        self.assertEqual(len(entries), 20)
        self.assertEqual(sum(entry["freed_bytes"] for entry in entries), audited)
        released = free_after - free_before
        self.assertGreater(released, 0)
        self.assertLessEqual(abs(released - audited), max(128 * 1024, audited // 5))


class HealthTests(unittest.TestCase):
    """The health report warns above the alert watermark."""

    def setUp(self):
        self.config = retention.retention_config({})
        self.records = [{"id": "r1", "path": "/runs/r1", "tier": "success", "status": "done",
                         "state": "finished", "reference": NOW - 100 * HOUR, "size_bytes": 2048}]

    def test_a_quiet_disk_does_not_warn(self):
        report = retention.health_report(self.records, self.config, 42.0, NOW)
        self.assertFalse(report["alert"])
        self.assertEqual(report["priority"], "normal")
        self.assertEqual(report["expired"]["success"], 1)
        self.assertEqual(report["expired_bytes"], 2048)

    def test_a_full_disk_warns_and_raises_the_cleanup_priority(self):
        report = retention.health_report(self.records, self.config, 91.7, NOW)
        self.assertTrue(report["alert"])
        self.assertEqual(report["priority"], "high")
        self.assertIn("91.7%", report["message"])

    def test_an_unknown_watermark_does_not_warn(self):
        report = retention.health_report(self.records, self.config, None, NOW)
        self.assertFalse(report["alert"])


class SweepTests(unittest.TestCase):
    """The sweep of the CLI expires the workspaces of this host."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / "runs").mkdir(parents=True)
        self.previous = xs.DATA
        xs.DATA = str(self.home)
        self.addCleanup(self.restore)

    def restore(self):
        xs.DATA = self.previous

    def watermark(self, percent):
        """Pin the disk watermark of the host the sweep sees."""
        return mock.patch.object(xs.workspace_retention_module(), "disk_watermark", return_value=percent)

    def write_run(self, run_id, status, age_hours, state="finished"):
        path = self.home / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "task.md").write_text("payload\n", encoding="utf-8")
        (path / "status.json").write_text(json.dumps(
            {"id": run_id, "state": state, "status": status, "finished": time.time() - age_hours * HOUR}),
            encoding="utf-8")

    def test_a_healthy_host_keeps_every_workspace(self):
        self.write_run("old", "done", 100)
        report = xs.executor_retention_sweep({}, usage=usage_at(20.0))
        self.assertTrue(report["ok"])
        self.assertEqual(report["deleted"], [])
        self.assertTrue((self.home / "runs" / "old").exists())

    def test_a_full_host_expires_the_old_workspaces_and_writes_the_audit_log(self):
        self.write_run("old-ok", "done", 100)
        self.write_run("old-failed", "failed", 100)
        self.write_run("fresh", "done", 1)
        report = xs.executor_retention_sweep({}, usage=usage_at(92.0))
        self.assertTrue(report["ok"])
        self.assertEqual(report["applied"], True)
        self.assertEqual(sorted(row["run_id"] for row in report["deleted"]), ["old-failed", "old-ok"])
        self.assertGreater(report["freed_bytes"], 0)
        self.assertTrue((self.home / "runs" / "fresh").exists())
        entries = retention.audit_entries(report["audit_path"])
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(entry["freed_bytes"] for entry in entries), report["freed_bytes"])

    def test_a_run_in_flight_is_never_expired(self):
        self.write_run("busy", "", 100, state="running")
        report = xs.executor_retention_sweep({}, usage=usage_at(99.0))
        self.assertEqual(report["deleted"], [])
        self.assertTrue((self.home / "runs" / "busy").exists())

    def test_the_cli_reports_the_retention_state(self):
        self.write_run("old", "done", 100)
        output = io.StringIO()
        with self.watermark(20.0), contextlib.redirect_stdout(output):
            report = xs.cmd_workspaces(argparse.Namespace(workspaces_cmd="status", json=True, apply=False), {})
        printed = json.loads(output.getvalue())
        self.assertEqual(printed["expired"], 1)
        self.assertEqual(report["expired"], 1)
        self.assertEqual(printed["applied"], False)

    def test_the_cli_cleanup_needs_apply(self):
        self.write_run("old", "done", 100)
        output = io.StringIO()
        with self.watermark(92.0), contextlib.redirect_stdout(output):
            xs.cmd_workspaces(argparse.Namespace(workspaces_cmd="cleanup", json=True, apply=False), {})
        self.assertTrue((self.home / "runs" / "old").exists())
        output = io.StringIO()
        with self.watermark(92.0), contextlib.redirect_stdout(output):
            report = xs.cmd_workspaces(argparse.Namespace(workspaces_cmd="cleanup", json=True, apply=True), {})
        self.assertFalse((self.home / "runs" / "old").exists())
        self.assertEqual(report["applied"], True)


class DoctorRetentionTests(unittest.TestCase):
    """`openswarm doctor` warns when the disk asks for a cleanup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        (self.home / "runs").mkdir(parents=True)
        self.previous = xs.DATA
        xs.DATA = str(self.home)
        self.addCleanup(self.restore)

    def restore(self):
        xs.DATA = self.previous

    def row(self, percent):
        rows = xs.doctor_resources([], {}, sleep=lambda _: None)
        row = next(row for row in rows if row["kind"] == "workspace_retention")
        self.assertEqual(row["used_pct"], percent)
        return row

    def test_a_quiet_disk_is_ok(self):
        with mock.patch.object(xs.workspace_retention_module(), "disk_watermark", return_value=42.0):
            row = self.row(42.0)
        self.assertEqual(row["level"], "OK")
        self.assertEqual(row["priority"], "normal")

    def test_a_full_disk_warns(self):
        with mock.patch.object(xs.workspace_retention_module(), "disk_watermark", return_value=91.7):
            row = self.row(91.7)
        self.assertEqual(row["level"], "WARN")
        self.assertEqual(row["priority"], "high")


if __name__ == "__main__":
    unittest.main()
