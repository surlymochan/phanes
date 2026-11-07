"""Host scope regressions use command text, fake children and isolated ledger files only."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()


class HostScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="host-scope-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in (("DATA", str(self.root)), ("RUNS", str(self.root / "runs")),
                            ("LEDGER", str(self.root / "ledger.jsonl"))):
            patch = mock.patch.object(xs, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.policy = xs.load_policy()

    def test_wrapped_commands_keep_all_inner_enforcement(self):
        prefixes = ("sudo", "sudo -u operator", "sudo -n -uoperator --",
                    "/usr/bin/sudo --user=operator", "sudo --user operator --",
                    "sudo env NAME=value", "env -u NAME sudo", "nohup sudo",
                    "timeout -k 2s 10s sudo", "sudo timeout --signal TERM 10s",
                    "sudo -u operator env -i NAME=value nohup timeout 10s",
                    "command exec sudo", "nice -n 5 sudo", "env -S 'sudo -u operator'", "sudo bash -lc")
        for prefix in prefixes:
            for command, risk in (("git push origin main", "push"), ("cat .env", "secret print"),
                                  ("npm publish", "publish")):
                combined = f"{prefix} {command}" if not prefix.endswith("-lc") else f"{prefix} '{command}'"
                with self.subTest(command=combined):
                    event = {"type": "tool_execution_start", "args": {"command": combined}}
                    for policy in ({}, {"risk": {"enforce": [risk]}}):
                        finding = xs.enforced_risk(event, policy, str(self.root), scope="host")
                        self.assertIsNotNone(finding)
                        self.assertEqual(finding["risk"], risk)
                        self.assertIsNot(finding.get("enforced"), False)
        for command in ("echo 'sudo git push origin main'", "sudo echo 'git push origin main'"):
            event = {"type": "tool_execution_start", "args": {"command": command}}
            finding = xs.enforced_risk(event, {}, str(self.root), scope="host")
            self.assertTrue(finding is None or finding.get("enforced") is False)

    def test_wrapped_rm_and_sudo_remain_host_warnings(self):
        event = {"type": "tool_execution_start", "args": {"command": "sudo -u operator timeout 10s rm -rf /outside"}}
        finding = xs.enforced_risk(event, {"risk": {"enforce": ["rm outside worktree"]}},
                                   str(self.root), scope="host")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["risk"], "rm outside worktree")
        self.assertFalse(finding["enforced"])
        self.assertNotIn("enforced", xs.enforced_risk(event, {}, str(self.root)))

    def test_wrapper_cwd_does_not_change_later_shell_commands(self):
        with mock.patch.object(xs, "executor_worktree_root", return_value=str(self.root)):
            self.assertTrue(xs.rm_outside_worktree("command cd /outside; rm local", str(self.root)))
            for prefix in ("env -C /outside", "sudo -D /outside"):
                self.assertTrue(xs.rm_outside_worktree(f"{prefix} rm local", str(self.root)))
                self.assertTrue(xs.rm_outside_worktree(f"{prefix} bash -lc 'rm local'", str(self.root)))
                self.assertFalse(xs.rm_outside_worktree(f"{prefix} true; rm local", str(self.root)))

    def test_rm_scope_canonicalizes_symlinked_worktree_root(self):
        alias = self.root.parent / (self.root.name + "-alias")
        alias.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(xs, "executor_worktree_root", return_value=str(alias)):
            self.assertFalse(xs.rm_outside_worktree("rm local", str(self.root)))

    def test_fake_executor_records_warnings_and_stops_wrapped_enforced_risks(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for i, (command, risk, rc) in enumerate((("sudo launchctl list", "sudo", 0),
                                                ("rm -rf /outside", "rm outside worktree", 0),
                                                ("sudo git push origin main", "push", 133),
                                                ("sudo cat .env", "secret print", 133),
                                                ("sudo npm publish", "publish", 133))):
            with self.subTest(command=command):
                rdir = self.root / "runs" / str(i)
                rdir.mkdir(parents=True)
                xs.set_status(str(rdir), scope="host")
                event = {"type": "tool_execution_start", "args": {"command": command}}
                script = "import json, time; print(json.dumps(" + repr(event) + "), flush=True); time.sleep(0.2)"
                events = rdir / "events.jsonl"
                self.assertEqual(xs.run_proc([sys.executable, "-c", script], str(repo),
                                             os.environ.copy(), 5, str(events)), rc)
                status = json.loads((rdir / "status.json").read_text())
                if rc:
                    self.assertEqual(status["risk_blocks"][0]["risk"], risk)
                else:
                    warnings = [json.loads(line) for line in events.read_text().splitlines()
                                if json.loads(line).get("type") == "risk_warning"]
                    self.assertEqual(warnings[0]["risk"], risk)

    def test_host_warnings_still_answer_lane_permission_requests(self):
        for method in ("session/request_permission", "item/commandExecution/requestApproval", "execCommandApproval"):
            for command in ("sudo launchctl list", "rm -rf /outside"):
                with self.subTest(method=method, command=command):
                    backend = "cursor" if method == "session/request_permission" else "codex"
                    rdir = self.root / "runs" / (backend + str(len(list((self.root / 'runs').glob('*')))))
                    rdir.mkdir(parents=True)
                    lane = {"name": "fake", "backend": backend, "model": "fake"}
                    params = {"command": command} if backend == "codex" else {
                        "toolCall": {"rawInput": {"command": command}},
                        "options": [{"kind": "allow_once", "optionId": "once"}]}
                    request = {"id": "permission", "method": method, "params": params}
                    complete = {"method": "turn/completed", "params": {"turn": {}}} if backend == "codex" else {
                        "id": "prompt", "result": {"stopReason": "end_turn"}}
                    child = mock.Mock(pid=os.getpid(), lane=lane)
                    expected = ({"outcome": {"outcome": "selected", "optionId": "once"}} if backend == "cursor"
                                else {"decision": "approved" if method == "execCommandApproval" else "accept"})
                    final = {"status": "done", "summary": "fake command completed"}
                    message = {"method": "item/completed", "params": {
                        "item": {"type": "agentMessage", "text": json.dumps(final)}}}
                    # A second read must have a reply: otherwise the executor would hang.
                    def read_event(_timeout):
                        if not getattr(read_event, "requested", False):
                            read_event.requested = True
                            return request
                        self.assertTrue(child.send.called, "permission request was skipped after warning")
                        self.assertEqual(child.send.call_args.args[0]["result"], expected)
                        if backend == "codex" and not getattr(read_event, "reported", False):
                            read_event.reported = True
                            return message
                        return complete
                    child.next_event.side_effect = read_event
                    spec = {"scope": "host", "started": xs.NOW(), "budget_minutes": 1}
                    with mock.patch.object(xs, "lane_begin_turn"), mock.patch.object(xs, "lane_control_messages"), \
                            mock.patch.object(xs, "lane_abort_child") as abort:
                        result = xs.lane_run_turn(lane, child, spec, "task", {"path": str(self.root)},
                                                  str(rdir), None, {"cursor_prompt_id": "prompt"}, [])
                    self.assertEqual(result[2], None if backend == "codex" else "no_report")
                    if backend == "codex":
                        self.assertEqual(result[0], final)
                        self.assertEqual(result[3], 0)
                    abort.assert_not_called()
                    reply = child.send.call_args.args[0]
                    self.assertEqual(reply["id"], "permission")
                    self.assertEqual(reply["result"], expected)
                    events = [json.loads(line) for line in Path(result[4]).read_text().splitlines()]
                    self.assertTrue(any(event.get("type") == "risk_warning" for event in events))
                    self.assertFalse(any(event.get("type") == "error" for event in events))

    def test_codex_lane_keeps_repo_and_other_host_risk_approvals_denied(self):
        for method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            for scope, command, risk in (("repo", "sudo launchctl list", "sudo"),
                                         ("repo", "rm -rf /outside", "rm outside worktree"),
                                         ("host", "sudo git push origin main", "push"),
                                         ("host", "rm -rf /outside; cat .env", "secret print"),
                                         ("host", "sudo npm publish", "publish"),
                                         ("host", "echo safe", None)):
                with self.subTest(method=method, scope=scope, command=command):
                    rdir = self.root / "runs" / str(len(list((self.root / 'runs').glob('*'))))
                    rdir.mkdir(parents=True)
                    lane = {"name": "fake", "backend": "codex", "model": "fake"}
                    child = mock.Mock(pid=os.getpid(), lane=lane)
                    child.next_event.side_effect = [
                        {"id": "permission", "method": method, "params": {"command": command}},
                        {"method": "turn/completed", "params": {"turn": {}}}]
                    spec = {"scope": scope, "started": xs.NOW(), "budget_minutes": 1}
                    with mock.patch.object(xs, "lane_begin_turn"), mock.patch.object(xs, "lane_control_messages"), \
                            mock.patch.object(xs, "lane_abort_child") as abort:
                        result = xs.lane_run_turn(lane, child, spec, "task", {"path": str(self.root)},
                                                  str(rdir), None, {}, [])
                    self.assertEqual(child.send.call_args.args[0]["result"],
                                     {"decision": "abort" if method == "execCommandApproval" else "decline"})
                    self.assertEqual(result[2], "risk_blocked" if risk else "no_report")
                    if risk:
                        self.assertEqual(spec["risk_blocks"][0]["risk"], risk)
                        abort.assert_called()
                    else:
                        abort.assert_not_called()

    def test_host_warning_does_not_approve_file_or_permission_requests(self):
        for method, expected in (("item/fileChange/requestApproval", {"decision": "decline"}),
                                 ("applyPatchApproval", {"decision": "abort"}),
                                 ("item/permissions/requestApproval", {"permissions": {}})):
            with self.subTest(method=method):
                child = mock.Mock()
                events = self.root / "events.jsonl"
                self.assertTrue(xs.lane_approval_response(child, {"id": "permission", "method": method},
                                                         str(events), host_warning=True))
                self.assertEqual(child.send.call_args.args[0]["result"], expected)

    def test_run_and_lane_write_scope_through_ledger_to_daily_report(self):
        final = {"status": "done", "summary": "fake completed", "files_changed": ["result.txt"]}
        ws = {"mode": "direct", "root": str(self.root), "path": str(self.root)}
        decision = {"ok": True, "backend": "pi", "model": "fake"}
        attempt = {"backend": "pi", "model": "fake", "seconds": 1, "error_class": None, "meta": {}}
        for scope in ("host", "repo"):
            with self.subTest(scope=scope):
                args = argparse.Namespace(run_id=None, prompt="task", task_file=None, title="run-" + scope,
                                          cwd=str(self.root), type="impl", difficulty=3, context_tokens=0,
                                          backend="pi", model=None, effort=None, budget_minutes=1, detach=False,
                                          dry_run=False, no_worktree=True, base=None, verify=None, no_fallback=True,
                                          keep_worktree=True, no_auto_review=True)
                if scope == "host":
                    args.scope = scope  # Omitted scope must keep the repo default.
                with mock.patch.object(xs, "get_state", return_value={}), \
                        mock.patch.object(xs, "route", return_value=decision), \
                        mock.patch.object(xs, "prepare_workspace", return_value=ws), \
                        mock.patch.object(xs, "workspace_note", return_value=None), \
                        mock.patch.object(xs, "run_workspace_prepare", return_value=None), \
                        mock.patch.object(xs, "run_executor_with_rate_limit", return_value=([dict(attempt)], final, None)), \
                        mock.patch.object(xs, "finalize_workspace", return_value={}), \
                        contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exited:
                    xs.cmd_run(args, self.policy)
                self.assertEqual(exited.exception.code, 0)
                rid = "lane-" + scope
                rdir = self.root / "runs" / rid
                rdir.mkdir(parents=True)
                spec = {"run_id": rid, "task": "task", "title": rid, "started": xs.NOW(), "budget_minutes": 1}
                if scope == "host":
                    spec["scope"] = scope
                with mock.patch.object(xs, "git", return_value=(0, "", "")), \
                        mock.patch.object(xs, "finalize_workspace", return_value={}):
                    xs.lane_finalize_run({"name": "fake", "backend": "pi", "model": "fake"}, spec,
                                          ws, final, {}, None, 0, str(rdir / "events.jsonl"))
        entries = [json.loads(line) for line in Path(xs.LEDGER).read_text().splitlines()]
        self.assertEqual(len(entries), 4)
        report = xs.daily_report(self.policy)
        for entry in entries:
            scope = "host" if entry["title"].endswith("host") else "repo"
            self.assertEqual(entry.get("scope"), scope)
            saved = self.root / "runs" / entry["id"]
            self.assertEqual(json.loads((saved / "status.json").read_text())["scope"], scope)
            self.assertEqual(json.loads((saved / "result.json").read_text())["scope"], scope)
            self.assertEqual(next(row for row in report["slowest_runs"] if row["id"] == entry["id"])["scope"], scope)
