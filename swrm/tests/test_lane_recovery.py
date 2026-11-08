import contextlib
import itertools
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

import _load


xs = _load.load()


class LaneRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lane-recovery-", dir=pathlib.Path(__file__).resolve().parents[2])
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Lane Test")
        self.git("config", "user.email", "lane-test@example.invalid")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", "base.txt")
        self.git("commit", "-q", "-m", "base")
        self.lane = {"name": "L1", "backend": "pi", "model": "test", "repo": str(self.repo)}
        self.data = self.root / "data"
        self.runs = self.data / "runs"
        self.wt = self.data / "lanes" / "L1" / "wt"
        self.run_id = "run-test"

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def setup_run(self):
        rdir = self.runs / self.run_id
        rdir.mkdir(parents=True)
        (rdir / "status.json").write_text(json.dumps({"state": "running", "lane": "L1"}))
        (rdir / "assignment.json").write_text(json.dumps({"run_id": self.run_id, "task": "continue",
                                                           "workspace_path": str(self.wt), "session_file": "saved-session.jsonl",
                                                           "base": self.git("rev-parse", "HEAD")}))
        self.wt.parent.mkdir(parents=True)
        self.git("worktree", "add", "--detach", "-q", str(self.wt), "HEAD")
        self.git("checkout", "-q", "-b", f"swrm/{self.run_id}", cwd=self.wt)
        (self.data / "lanes" / "L1" / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.data / "lanes" / "L1" / "state.json").write_text(json.dumps({
            "state": "busy", "current_run": self.run_id}))

    def recover(self):
        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "RUNS", str(self.runs)), \
                mock.patch.object(xs, "lane_config", return_value=[self.lane]):
            return xs.lane_recover_assignment(self.lane, {"state": "busy", "current_run": self.run_id})

    def test_unpushed_worktree_changes_recover_without_cleaning(self):
        self.setup_run()
        (self.wt / "local-commit.txt").write_text("local only\n")
        self.git("add", "local-commit.txt", cwd=self.wt)
        self.git("commit", "-q", "-m", "local unpushed commit", cwd=self.wt)
        (self.wt / "partial.txt").write_text("unfinished work\n")
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "workspace")
        with mock.patch.object(xs, "DATA", str(self.data)):
            ws = xs.lane_prepare_workspace(self.lane, self.run_id, self.git("rev-parse", "HEAD"), preserve=True)
        self.assertEqual((pathlib.Path(ws["root"]) / "partial.txt").read_text(), "unfinished work\n")

    def test_failed_prepare_preserves_recovered_workspace_and_local_commits(self):
        self.setup_run()
        (self.wt / "local.txt").write_text("local commit\n")
        self.git("add", "local.txt", cwd=self.wt)
        self.git("commit", "-qm", "local progress", cwd=self.wt)
        head = self.git("rev-parse", "HEAD", cwd=self.wt)
        (self.wt / "base.txt").write_text("staged progress\n")
        self.git("add", "base.txt", cwd=self.wt)
        (self.wt / "base.txt").write_text("unstaged progress\n")
        (self.wt / "partial.txt").write_text("unfinished work\n")
        (self.wt / ".gitignore").write_text("ignored.txt\n")
        (self.wt / "ignored.txt").write_text("ignored progress\n")
        before = self.git("status", "--porcelain", "--ignored", cwd=self.wt)
        staged = self.git("diff", "--cached", cwd=self.wt)
        spec, error = self.recover()
        self.assertIsNone(error)
        spec.update(started=xs.NOW(), verify="must not run")
        policy = xs.load_policy()
        policy.setdefault("workspace", {})["prepare"] = {str(self.repo): "exit 42"}
        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "RUNS", str(self.runs)), \
                mock.patch.object(xs, "append_ledger"), \
                mock.patch.object(xs, "load_policy", return_value=policy), \
                mock.patch.object(xs, "run_verify") as verify:
            result = xs.lane_do_run(self.lane, spec, mock.Mock(), mock.Mock(), spec)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "prepare_failed")
        self.assertEqual(result["attempts"][0]["rc"], 42)
        verify.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), head)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.wt), f"swrm/{self.run_id}")
        self.assertEqual(self.git("status", "--porcelain", "--ignored", cwd=self.wt), before)
        self.assertEqual(self.git("diff", "--cached", cwd=self.wt), staged)
        self.assertEqual((self.wt / "base.txt").read_text(), "unstaged progress\n")
        self.assertEqual((self.wt / "partial.txt").read_text(), "unfinished work\n")
        self.assertEqual((self.wt / "ignored.txt").read_text(), "ignored progress\n")

    def check_failed_session_recovery_preserves_workspace(self, read_only=False, backend="pi", timeout=False):
        self.setup_run()
        self.lane.update(backend=backend, effort="high")
        assignment = self.runs / self.run_id / "assignment.json"
        saved = json.loads(assignment.read_text())
        saved["thread_id"] = "saved-thread"
        assignment.write_text(json.dumps(saved))
        (self.wt / "local.txt").write_text("local commit\n")
        self.git("add", "local.txt", cwd=self.wt)
        self.git("commit", "-qm", "local progress", cwd=self.wt)
        head = self.git("rev-parse", "HEAD", cwd=self.wt)
        (self.wt / "base.txt").write_text("staged progress\n")
        self.git("add", "base.txt", cwd=self.wt)
        (self.wt / "base.txt").write_text("unstaged progress\n")
        (self.wt / "partial.txt").write_text("unfinished work\n")
        (self.wt / ".gitignore").write_text("ignored.txt\n")
        (self.wt / "ignored.txt").write_text("ignored progress\n")
        before = self.git("status", "--porcelain", "--ignored", cwd=self.wt)
        staged = self.git("diff", "--cached", cwd=self.wt)
        spec, error = self.recover()
        self.assertIsNone(error)
        spec.update(started=xs.NOW(), budget_minutes=90, read_only=read_only, verify="must not run")
        child = mock.Mock(pid=12345)
        if timeout:
            child.wait_response.side_effect = TimeoutError("session restore timed out")
        else:
            child.wait_response.return_value = {"success": False, "error": "saved session unavailable"}
        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "RUNS", str(self.runs)), \
                mock.patch.object(xs, "append_ledger"), \
                mock.patch.object(xs, "run_workspace_prepare", return_value=None), \
                mock.patch.object(xs, "reserve_provider_slot", return_value=True), \
                mock.patch.object(xs, "run_verify", return_value={"rc": 0}) as verify:
            result = xs.lane_do_run(self.lane, spec, child, mock.Mock(), spec)
        self.assertTrue((self.wt / "partial.txt").exists(), "session recovery failure discarded unfinished work")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "exec_error")
        self.assertIsNone(result["commit"])
        self.assertEqual(result["worktree"], str(self.wt))
        verify.assert_not_called()
        request = child.send.call_args_list[0].args[0]
        self.assertEqual(request.get("type") or request.get("method"),
                         "switch_session" if backend == "pi" else "thread/resume")
        self.assertEqual(child.send.call_count, 1)
        rdir = self.runs / self.run_id
        self.assertIn("session recovery failed", json.loads((rdir / "status.json").read_text())["recovery_abandoned"])
        events = [json.loads(line) for line in (rdir / f"events.1.{backend}.jsonl").read_text().splitlines()]
        self.assertTrue(any(event["type"] == "recovery_abandoned" and event["run_id"] == self.run_id
                            for event in events))
        self.assertFalse(any(event["type"] == "recovery" for event in events))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), head)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.wt), f"swrm/{self.run_id}")
        self.assertEqual(self.git("status", "--porcelain", "--ignored", cwd=self.wt), before)
        self.assertEqual(self.git("diff", "--cached", cwd=self.wt), staged)
        self.assertEqual((self.wt / "base.txt").read_text(), "unstaged progress\n")
        self.assertEqual((self.wt / "partial.txt").read_text(), "unfinished work\n")
        self.assertEqual((self.wt / "ignored.txt").read_text(), "ignored progress\n")

    def test_failed_pi_session_recovery_preserves_workspace(self):
        self.check_failed_session_recovery_preserves_workspace()

    def test_failed_pi_session_recovery_preserves_read_only_workspace(self):
        self.check_failed_session_recovery_preserves_workspace(read_only=True)

    def test_timed_out_pi_session_recovery_preserves_read_only_workspace(self):
        self.check_failed_session_recovery_preserves_workspace(read_only=True, timeout=True)

    def test_failed_codex_session_recovery_preserves_read_only_workspace(self):
        self.check_failed_session_recovery_preserves_workspace(read_only=True, backend="codex")

    @contextlib.contextmanager
    def serve_context(self):
        with mock.patch.object(xs, "DATA", str(self.data)), \
                mock.patch.object(xs, "RUNS", str(self.runs)), \
                mock.patch.object(xs, "lane_config", return_value=[self.lane]), \
                mock.patch.object(xs, "lanes_by_name", return_value=[self.lane]), \
                mock.patch.object(xs, "lane_ctl_listener", return_value=mock.Mock()), \
                mock.patch.object(xs, "lane_control_messages", return_value=False), \
                mock.patch.object(xs.traceback, "print_exc"):
            yield

    def test_restart_during_recovery_keeps_run_identifiable(self):
        self.setup_run()

        class SupervisorStopped(BaseException):
            pass

        with self.serve_context(), \
                mock.patch.object(xs, "lane_child", side_effect=SupervisorStopped):
            with self.assertRaises(SupervisorStopped):
                xs.cmd_lane_serve(self.lane, {})
            state = xs.jload(xs.lane_state_path("L1"))
            self.assertEqual(state["state"], "starting")
            self.assertEqual(state["current_run"], self.run_id)
            spec, error = xs.lane_recover_assignment(self.lane, state)
        self.assertIsNone(error)
        self.assertEqual(spec["run_id"], self.run_id)

    def test_restart_after_child_launch_failure_keeps_run_identifiable(self):
        self.setup_run()

        class SupervisorStopped(BaseException):
            pass

        with self.serve_context(), \
                mock.patch.object(xs, "NOW", return_value=100), \
                mock.patch.object(xs, "lane_child", side_effect=RuntimeError("launch failed")), \
                mock.patch.object(xs.select, "select", side_effect=SupervisorStopped), \
                mock.patch.object(xs, "lane_do_run") as run:
            with self.assertRaises(SupervisorStopped):
                xs.cmd_lane_serve(self.lane, {})
            state = xs.jload(xs.lane_state_path("L1"))
            self.assertEqual(state["state"], "crashed")
            self.assertEqual(state["current_run"], self.run_id)
            spec, error = xs.lane_recover_assignment(self.lane, state)
        run.assert_not_called()
        self.assertIsNone(error)
        self.assertEqual(spec["run_id"], self.run_id)

    def test_child_launch_retry_resumes_same_run_without_restarting_supervisor(self):
        self.setup_run()
        child = mock.Mock(pid=12345)

        def finish_run(lane, spec, executor, server, current):
            self.assertIs(executor, child)
            self.assertEqual(spec["run_id"], self.run_id)
            self.assertTrue(spec["_recovered"])
            state = xs.jload(xs.lane_state_path("L1"))
            self.assertEqual(state["state"], "busy")
            self.assertEqual(state["current_run"], self.run_id)
            spec["down_requested"] = True

        def check_pending_run(server, lane, current, executor, turn, pending):
            self.assertEqual(current.get("run_id"), self.run_id)
            return False

        with self.serve_context(), \
                mock.patch.object(xs, "NOW", side_effect=itertools.count(100, 10)), \
                mock.patch.object(xs, "lane_child", side_effect=[RuntimeError("launch failed"), child]) as launch, \
                mock.patch.object(xs, "lane_control_messages", side_effect=check_pending_run), \
                mock.patch.object(xs, "lane_do_run", side_effect=finish_run) as run:
            xs.cmd_lane_serve(self.lane, {})
        self.assertEqual(launch.call_count, 2)
        run.assert_called_once()
        self.assertEqual(len(list(self.runs.iterdir())), 1)

    def check_recovered_child_exits_before_turn(self, read_only=False):
        self.setup_run()
        assignment = self.runs / self.run_id / "assignment.json"
        spec = json.loads(assignment.read_text())
        spec.update(read_only=read_only, verify="must not run", started=100)
        assignment.write_text(json.dumps(spec))
        (self.wt / "local.txt").write_text("local commit\n")
        self.git("add", "local.txt", cwd=self.wt)
        self.git("commit", "-qm", "local progress", cwd=self.wt)
        head = self.git("rev-parse", "HEAD", cwd=self.wt)
        (self.wt / "base.txt").write_text("staged progress\n")
        self.git("add", "base.txt", cwd=self.wt)
        (self.wt / "base.txt").write_text("unstaged progress\n")
        (self.wt / "partial.txt").write_text("unfinished work\n")
        (self.wt / ".gitignore").write_text("ignored.txt\n")
        (self.wt / "ignored.txt").write_text("ignored progress\n")
        before = self.git("status", "--porcelain", "--ignored", cwd=self.wt)
        staged = self.git("diff", "--cached", cwd=self.wt)
        dead_child = mock.Mock(pid=12345)
        dead_child.alive.return_value = False
        replacement = mock.Mock(pid=12346)
        with self.serve_context(), \
                mock.patch.object(xs, "NOW", side_effect=itertools.count(100, 10)), \
                mock.patch.object(xs, "lane_child", side_effect=[dead_child, replacement]), \
                mock.patch.object(xs, "lane_control_messages", return_value=True), \
                mock.patch.object(xs, "append_ledger"), \
                mock.patch.object(xs, "run_verify") as verify, \
                mock.patch.object(xs, "lane_do_run") as run:
            xs.cmd_lane_serve(self.lane, {})
        run.assert_not_called()
        verify.assert_not_called()
        result = json.loads((self.runs / self.run_id / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "exec_error")
        self.assertEqual(result["worktree"], str(self.wt))
        self.assertIsNone(result["commit"])
        self.assertEqual(len(list(self.runs.iterdir())), 1)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.wt), head)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.wt), f"swrm/{self.run_id}")
        self.assertEqual(self.git("status", "--porcelain", "--ignored", cwd=self.wt), before)
        self.assertEqual(self.git("diff", "--cached", cwd=self.wt), staged)
        self.assertEqual((self.wt / "base.txt").read_text(), "unstaged progress\n")
        self.assertEqual((self.wt / "partial.txt").read_text(), "unfinished work\n")
        self.assertEqual((self.wt / "ignored.txt").read_text(), "ignored progress\n")

    def test_child_exit_before_recovered_turn_preserves_workspace(self):
        self.check_recovered_child_exits_before_turn()

    def test_child_exit_before_recovered_read_only_turn_preserves_workspace(self):
        self.check_recovered_child_exits_before_turn(read_only=True)

    def test_pushed_commit_recovers_at_remote_tip(self):
        self.setup_run()
        (self.wt / "saved.txt").write_text("saved\n")
        self.git("add", "saved.txt", cwd=self.wt)
        self.git("commit", "-q", "-m", "saved progress", cwd=self.wt)
        bare = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare), cwd=self.wt)
        self.git("push", "-q", "origin", f"swrm/{self.run_id}", cwd=self.wt)
        self.git("fetch", "-q", "origin", cwd=self.wt)
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "commit")
        self.assertEqual(spec["_recovery_point"], self.git("rev-parse", "HEAD", cwd=self.wt))

    def test_remote_branch_at_base_is_not_commit_recovery(self):
        self.setup_run()
        bare = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare), cwd=self.wt)
        self.git("push", "-q", "origin", f"swrm/{self.run_id}", cwd=self.wt)
        self.git("fetch", "-q", "origin", cwd=self.wt)
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "workspace")

    def test_mismatched_run_status_is_abandoned(self):
        self.setup_run()
        status_path = self.runs / self.run_id / "status.json"
        status_path.write_text(json.dumps({"state": "finished", "lane": "L1"}))
        spec, error = self.recover()
        self.assertIsNone(spec)
        self.assertIn("not queued/running", error)

    def test_ad_requires_exact_issue_owner_and_explicit_live_status(self):
        path = self.root / "ad.json"
        lane = dict(self.lane, recovery_ad={"state_path": str(path)})
        spec = {"run_id": self.run_id, "title": "#1720 recovery"}
        self.assertIn("unavailable", xs.lane_ad_recovery_view(lane, spec))
        path.write_text(json.dumps({"L1": {"issue": 1720, "run": self.run_id}}))
        with mock.patch.object(xs, "gh_transport", return_value={"state": "open", "labels": [{"name": "in-progress"}]}):
            self.assertIsNone(xs.lane_ad_recovery_view(lane, spec))
            path.write_text(json.dumps({"L1": {"issue": 1720, "run": self.run_id},
                                        "L2": {"issue": 1720, "run": "replacement"}}))
            self.assertIn("reassigned", xs.lane_ad_recovery_view(lane, spec))
        path.write_text(json.dumps({"L1": {"issue": 1720, "run": self.run_id}}))
        for view in ({"state": "open", "labels": []}, {"state": "closed", "labels": ["in-progress"]}, {}):
            with self.subTest(view=view), mock.patch.object(xs, "gh_transport", return_value=view):
                self.assertIn("not explicitly", xs.lane_ad_recovery_view(lane, spec))

    def test_remote_ad_reads_run_ownership_and_issue_status(self):
        lane = dict(self.lane, recovery_ad={"host": "ad-host", "repo": "owner/repo"})
        replies = [mock.Mock(returncode=0, stdout=json.dumps({"L1": {"issue": 1720, "run": self.run_id}})),
                   mock.Mock(returncode=0, stdout=json.dumps({"state": "open", "labels": ["in-progress"]}))]
        with mock.patch.object(xs.subprocess, "run", side_effect=replies) as run:
            self.assertIsNone(xs.lane_ad_recovery_view(lane, {"run_id": self.run_id, "title": "#1720 resume"}))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-1], "cat phanes-ctl/autodispatch/state.json")
        self.assertEqual(run.call_args_list[1].args[0][-1], "gh api repos/owner/repo/issues/1720")

    def test_stale_tracking_ref_without_remote_confirmation_is_workspace_only(self):
        self.setup_run()
        (self.wt / "saved.txt").write_text("local progress")
        self.git("add", "saved.txt", cwd=self.wt)
        self.git("commit", "-qm", "local progress", cwd=self.wt)
        tip = self.git("rev-parse", "HEAD", cwd=self.wt)
        self.git("update-ref", f"refs/remotes/origin/swrm/{self.run_id}", tip)
        spec, error = self.recover()
        self.assertIsNone(error)
        self.assertEqual(spec["_recovery_level"], "workspace")

    def test_missing_pi_session_does_not_create_new_session(self):
        child = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            xs.lane_begin_turn(self.lane, child, "continue", str(self.wt), "events", {},
                               {"_recovered": True, "run_id": self.run_id})
        child.send.assert_not_called()

    def test_codex_resumes_saved_persistent_thread(self):
        child = mock.Mock()
        child.wait_response.side_effect = [{"thread": {"id": "saved-thread"}}, {"turn": {"id": "new-turn"}}]
        spec = {"_recovered": True, "run_id": self.run_id, "thread_id": "saved-thread"}
        with mock.patch.object(xs, "lane_save_assignment"), mock.patch.object(xs, "load_policy", return_value={}):
            xs.lane_begin_turn(dict(self.lane, backend="codex", effort="high"), child, "continue", str(self.wt), "events", {}, spec)
        request = child.send.call_args_list[0].args[0]
        self.assertEqual(request["method"], "thread/resume")
        self.assertEqual(request["params"]["threadId"], "saved-thread")

    def test_pi_session_location_is_read_from_active_state(self):
        child = mock.Mock()
        child.wait_response.side_effect = [{"success": True, "data": {"cancelled": False}},
                                          {"success": True, "data": {"sessionId": "saved", "sessionFile": "/saved.jsonl"}}]
        spec = {"run_id": self.run_id}
        with mock.patch.object(xs, "lane_save_assignment") as save:
            xs.lane_begin_turn(self.lane, child, "continue", str(self.wt), "events", {}, spec)
        self.assertEqual(child.send.call_args_list[1].args[0]["type"], "get_state")
        self.assertEqual(spec["session_file"], "/saved.jsonl")
        save.assert_called_once_with(spec)

    def test_pi_unsuccessful_switch_is_rejected(self):
        for response in ({"success": False}, {}, {"success": True, "error": "missing session"}):
            child = mock.Mock()
            child.wait_response.return_value = response
            with self.subTest(response=response), self.assertRaisesRegex(RuntimeError, "restore"):
                xs.lane_begin_turn(self.lane, child, "continue", str(self.wt), "events", {},
                                   {"_recovered": True, "run_id": self.run_id, "session_file": "saved"})
            self.assertEqual(child.send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
