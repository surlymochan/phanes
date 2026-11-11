"""Offline issue-bus coverage using fake gh and local bare target repositories."""
import argparse
import contextlib
import datetime
import inspect
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import _load

xs = _load.load()
FAKE_GH = Path(_load.HERE) / "fixtures" / "fake_gh.py"


class BusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="swrm-bus-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "planner"
        self.data.mkdir()
        self.state_path = self.root / "github.json"
        self.state_path.write_text(json.dumps({"repos": {"org/bus": {"labels": [], "issues": [], "files": {}}}}))
        self.bare = self.root / "target.git"
        self.repo = self.root / "target"
        self.git(None, "init", "--bare", "-q", str(self.bare))
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "config", "user.name", "Test User")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "base.txt").write_text("base\n")
        self.git(self.repo, "add", "base.txt")
        self.git(self.repo, "commit", "-qm", "base")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "branch", "-M", "main")
        self.git(self.repo, "remote", "add", "origin", str(self.bare))
        self.git(self.repo, "push", "-q", "origin", "main")
        self.env_names = ("OPENSWARM_HOME", "OPENSWARM_GH_BIN", "OPENSWARM_FAKE_GH_STATE",
                          "OPENSWARM_FAKE_GH_LOGIN", "OPENSWARM_FAKE_GH_ISSUE_LOGIN",
                          "OPENSWARM_BUS_TARGET_URL", "OPENSWARM_STATE_FILE",
                          "OPENSWARM_PI_BIN")
        self.old_env = {key: os.environ.get(key) for key in self.env_names}
        os.environ.update({"OPENSWARM_HOME": str(self.data), "OPENSWARM_GH_BIN": str(FAKE_GH),
                           "OPENSWARM_FAKE_GH_STATE": str(self.state_path), "OPENSWARM_FAKE_GH_LOGIN": "planner",
                           "OPENSWARM_FAKE_GH_ISSUE_LOGIN": "planner",
                           "OPENSWARM_BUS_TARGET_URL": str(self.bare), "OPENSWARM_STATE_FILE": _load.FIXTURE_STATE})
        FAKE_GH.chmod(FAKE_GH.stat().st_mode | stat.S_IEXEC)
        self.constants = {key: getattr(xs, key) for key in ("DATA", "RUNS", "LEDGER", "WORKTREES", "BREAKERS")}
        xs.DATA = str(self.data)
        xs.RUNS = str(self.data / "runs")
        xs.LEDGER = str(self.data / "ledger.jsonl")
        xs.WORKTREES = str(self.data / "worktrees")
        xs.BREAKERS = str(self.data / "breakers.json")
        self.policy = xs.deep_merge(xs.load_policy(), {"bus": {"repo": "org/bus", "allowed_executors": ["executor"],
                                                               "allowed_planners": ["planner"],
                                                               "worker_repos": ["org/target"],
                                                               "poll_seconds": 0.02, "labels_prefix": "swrm"}})
        self.cfg = xs.bus_config(self.policy)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in self.constants.items():
            setattr(xs, key, value)
        self.tmp.cleanup()

    @staticmethod
    def git(cwd, *args):
        command = ["git"] + (["-C", str(cwd)] if cwd else []) + list(args)
        return subprocess.check_output(command, stderr=subprocess.PIPE, text=True).strip()

    def state(self):
        for _ in range(10):
            try:
                return json.loads(self.state_path.read_text())
            except (OSError, ValueError):
                time.sleep(0.005)
        self.fail("fake GitHub state was not readable")

    def issue(self, number=1):
        return next(row for row in self.state()["repos"]["org/bus"]["issues"] if row["number"] == number)

    def add_comment(self, number, body, login="executor"):
        previous = os.environ.get("OPENSWARM_FAKE_GH_LOGIN")
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = login
        try:
            return xs.bus_comment("org/bus", number, body)
        finally:
            os.environ["OPENSWARM_FAKE_GH_LOGIN"] = previous or "planner"

    def set_labels(self, number, add, remove):
        labels = xs.bus_label_names(self.cfg)
        return xs.bus_set_issue_labels("org/bus", number, self.cfg,
                                       add=[labels[name] for name in add],
                                       remove=[labels[name] for name in remove])

    def run_args(self, budget=1.0, task="create a file", executor=None):
        return argparse.Namespace(type="impl", difficulty=3, context_tokens=0, backend="github-issue", model=None,
                                  effort=None, prompt=task, task_file=None, title="bus test", cwd=str(self.repo),
                                  base=self.base, no_worktree=False, verify=None, verify_baseline=False,
                                  keep_worktree=False, no_fallback=False, budget_minutes=budget,
                                  detach=False, run_id=None, dry_run=False, executor=executor)

    def start_planner(self, budget=1.0, task="create a file", executor=None):
        outcome = {}

        def run():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    xs.cmd_run(self.run_args(budget=budget, task=task, executor=executor), self.policy)
            except SystemExit as error:
                outcome["exit"] = error.code
            except BaseException as error:  # retain thread failures as test evidence
                outcome["error"] = error

        with mock.patch.object(xs, "bus_remote", return_value=(str(self.repo), str(self.bare), "org/target")):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            self.addCleanup(lambda: thread.join(timeout=5))
            for _ in range(250):
                issues = self.state()["repos"]["org/bus"]["issues"]
                if issues and issues[0]["comments"]:
                    return thread, outcome, issues[0]
                time.sleep(0.01)
        self.fail("planner did not create its issue")

    def push_result_branch(self, run_id, filename="worker.txt"):
        clone = self.root / ("executor-" + run_id)
        self.git(None, "clone", "-q", str(self.bare), str(clone))
        self.git(clone, "config", "user.name", "Executor")
        self.git(clone, "config", "user.email", "executor@example.invalid")
        (clone / filename).write_text("completed\n")
        self.git(clone, "add", filename)
        self.git(clone, "commit", "-qm", "external result")
        self.git(clone, "push", "-q", "origin", "HEAD:refs/heads/swrm/" + run_id)
        return self.git(clone, "rev-parse", "HEAD")

    @staticmethod
    def report(status, branch=None, commit=None):
        return {"status": status, "summary": "external work", "files_changed": ["worker.txt"],
                "tests": "fake verify", "followups": [], "branch": branch, "commit": commit, "pr_url": None}

    def send_result(self, number, report, label="done", login="executor"):
        self.add_comment(number, "```swrm-result\n" + json.dumps(report) + "\n```", login=login)
        self.set_labels(number, [label], ["queued", "claimed"])

    def finish_thread(self, thread, outcome):
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "planner did not finish")
        self.assertNotIn("error", outcome, outcome.get("error"))
        return json.loads((Path(xs.RUNS) / self.issue()["body"].split('"run_id": "')[1].split('"')[0] / "result.json").read_text())

    def test_poll_seconds_bounds(self):
        for value, expected in [(None, 15), (120, 30), (0, 0.01)]:
            bus = {"repo": "org/bus"}
            if value is not None:
                bus["poll_seconds"] = value
            self.assertEqual(xs.bus_config({"bus": bus})["poll_seconds"], expected)

    def test_shipped_default_policy_polls_every_15_seconds(self):
        base = json.loads((Path(xs.SKILL_DIR) / "policy.json").read_text())
        merged = xs.deep_merge(base, {"bus": {"repo": "org/bus"}})
        self.assertEqual(xs.bus_config(merged)["poll_seconds"], 15)

    def test_status_claim_timing(self):
        issues = [{"number": 1, "title": "[swrm] claimed", "created_at": "2026-01-01T00:00:00Z"},
                  {"number": 2, "title": "[swrm] waiting", "created_at": "2026-01-01T00:00:00Z"}]
        comments = [{"id": 1, "body": "/claim agent", "user": {"login": "executor"},
                     "created_at": "2026-01-01T00:00:17Z"}]
        for json_output in (True, False):
            with mock.patch.object(xs, "gh_pages", return_value=issues), \
                 mock.patch.object(xs, "bus_comments", side_effect=[comments, []]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    xs.cmd_bus_status(argparse.Namespace(repo=None, json=json_output), self.policy)
            if json_output:
                rows = json.loads(output.getvalue())
                self.assertEqual(rows[0]["claimed_at"], "2026-01-01T00:00:17Z")
                self.assertEqual(rows[0]["claim_latency_seconds"], 17)
                self.assertIsNone(rows[1]["claimed_at"])
                self.assertIsNone(rows[1]["claim_latency_seconds"])
            else:
                lines = output.getvalue().splitlines()
                self.assertTrue(lines[0].endswith("claimed_in=17s"))
                self.assertTrue(lines[1].endswith("claimed_in=-"))

    def test_create_claim_fetch_branch_verify_and_done_result(self):
        thread, outcome, issue = self.start_planner(task="create worker.txt", executor="external-agent")
        metadata = xs.bus_extract_task(issue["body"])
        self.assertEqual(metadata["repo"], "org/target")
        self.assertEqual(metadata["base"], self.base)
        self.assertIn("executor:external-agent", xs.issue_label_names(issue))
        run_id = metadata["run_id"]
        self.add_comment(issue["number"], "/claim external-agent")
        self.set_labels(issue["number"], ["claimed"], ["queued"])
        commit = self.push_result_branch(run_id)
        self.send_result(issue["number"], self.report("done", f"swrm/{run_id}", commit))
        saved_issue = self.issue()
        self.assertEqual(xs.bus_label_names(self.cfg)["done"], xs.issue_label_names(saved_issue).intersection(
            {xs.bus_label_names(self.cfg)["done"]}).pop())
        self.assertTrue(any(xs.bus_parse_result(comment["body"]) for comment in saved_issue["comments"]))
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), {"outcome": outcome, "labels": saved_issue["labels"],
                                              "comments": saved_issue["comments"]})
        self.assertEqual(outcome.get("exit"), 0)
        result = json.loads((Path(xs.RUNS) / run_id / "result.json").read_text())
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["branch"], f"swrm/{run_id}")
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["bus"]["executor"], "external-agent")
        self.assertEqual(self.git(self.repo, "rev-parse", f"swrm/{run_id}"), commit)
        ledger = [json.loads(line) for line in Path(xs.LEDGER).read_text().splitlines()]
        self.assertEqual(ledger[-1]["status"], "done")

    def test_untrusted_claim_and_result_are_ignored(self):
        thread, outcome, issue = self.start_planner(budget=0.035)
        self.add_comment(issue["number"], "/claim intruder", login="not-allowed")
        self.add_comment(issue["number"], "/claim external-agent", login="executor")
        self.set_labels(issue["number"], ["claimed"], ["queued"])
        self.send_result(issue["number"], self.report("done"), login="not-allowed")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get("exit"), 1)
        run_id = xs.bus_extract_task(issue["body"])["run_id"]
        result = json.loads((Path(xs.RUNS) / run_id / "result.json").read_text())
        self.assertEqual(result["status"], "budget_exceeded")
        self.assertIsNone(result["result"])
        # The result is untrusted, but the allowed executor's claim still holds
        # the card. Budget expiry must leave its labels intact.
        self.assertEqual({label["name"] for label in self.issue()["labels"]}, {"swrm:done"})
        events = (Path(xs.RUNS) / run_id / "events.jsonl").read_text()
        self.assertIn("budget cancellation refused", events)
        self.assertIn("--force", events)

    def test_concurrent_label_writers_do_not_clobber_each_other(self):
        created = xs.bus_create_issue("org/bus", self.cfg, "race", "label race", {
            "run_id": "label-race", "repo": "org/target", "base": self.base,
            "budget_minutes": 1, "report_schema": xs.RESULT_SCHEMA})
        number = created["number"]
        self.set_labels(number, ["claimed", "failed"], [])
        names = xs.bus_label_names(self.cfg)
        real_transport = xs.gh_transport
        reads = []
        both_read = threading.Event()

        def gated(endpoint, method="GET", payload=None, allow_not_found=False):
            # Hold any full-list replacement until both writers have taken
            # their read snapshot: the exact interleave that used to erase a
            # concurrently added label. Incremental writers never PUT, so
            # for them the gate never engages.
            if method == "PUT":
                both_read.wait(timeout=10)
            result = real_transport(endpoint, method, payload, allow_not_found)
            if method == "GET" and endpoint == f"repos/org/bus/issues/{number}":
                reads.append(1)
                if len(reads) == 2:
                    both_read.set()
            return result

        def write(add, remove):
            xs.bus_set_issue_labels("org/bus", number, self.cfg, add=add, remove=remove)

        with mock.patch.object(xs, "gh_transport", gated):
            threads = [threading.Thread(target=write, args=([names["cancelled"]], [names["failed"]])),
                       threading.Thread(target=write, args=([names["done"]], [names["queued"]]))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        final = xs.issue_label_names(self.issue(number))
        self.assertIn(names["cancelled"], final)
        self.assertIn(names["done"], final)
        self.assertNotIn(names["queued"], final)
        self.assertNotIn(names["failed"], final)

    def test_set_labels_tolerates_removing_absent_label(self):
        created = xs.bus_create_issue("org/bus", self.cfg, "absent", "absent label removal", {
            "run_id": "absent-label", "repo": "org/target", "base": self.base,
            "budget_minutes": 1, "report_schema": xs.RESULT_SCHEMA})
        number = created["number"]
        names = xs.bus_label_names(self.cfg)
        # The issue carries only swrm:queued; removing a state label it
        # does not have must not fail the whole update (the incremental
        # DELETE answers 404 for a label the issue does not carry).
        self.set_labels(number, ["claimed"], ["done"])
        final = xs.issue_label_names(self.issue(number))
        self.assertIn(names["claimed"], final)
        self.assertNotIn(names["done"], final)

    def test_failed_executor_result_is_recorded(self):
        thread, outcome, issue = self.start_planner()
        self.add_comment(issue["number"], "/claim external-agent")
        self.set_labels(issue["number"], ["claimed"], ["queued"])
        self.send_result(issue["number"], self.report("failed", None, None), label="failed")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), {"outcome": outcome, "issue": self.issue()})
        self.assertEqual(outcome.get("exit"), 1)
        run_id = xs.bus_extract_task(issue["body"])["run_id"]
        result = json.loads((Path(xs.RUNS) / run_id / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["status"], "failed")

    def test_result_status_wins_over_terminal_labels(self):
        thread, outcome, issue = self.start_planner()
        self.add_comment(issue["number"], "/claim external-agent")
        self.set_labels(issue["number"], ["claimed"], ["queued"])
        self.send_result(issue["number"], self.report("done", None, None), label="failed")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), {"outcome": outcome, "issue": self.issue()})
        self.assertEqual(outcome.get("exit"), 0)
        run_id = xs.bus_extract_task(issue["body"])["run_id"]
        result = json.loads((Path(xs.RUNS) / run_id / "result.json").read_text())
        self.assertEqual(result["status"], "done")

    def test_budget_expiry_cancels_issue(self):
        thread, outcome, issue = self.start_planner(budget=0.015)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get("exit"), 1)
        names = {label["name"] for label in self.issue()["labels"]}
        self.assertIn("swrm:cancelled", names)
        self.assertTrue(any("budget expired" in comment["body"] for comment in self.issue()["comments"]))

    def test_budget_expiry_preserves_reserved_and_claimed_cards(self):
        for hold in ("reservation", "claim", "claimed-label"):
            with self.subTest(hold=hold):
                create_issue = xs.bus_create_issue

                def create_held_issue(*args, **kwargs):
                    issue = create_issue(*args, **kwargs)
                    body = self.reserve_body() if hold == "reservation" else "/claim external-agent"
                    self.add_comment(issue["number"], body)
                    if hold == "claimed-label":
                        self.set_labels(issue["number"], ["claimed"], ["queued"])
                    return issue

                # Install the hold before the planner can reach its deadline check.
                with mock.patch.object(xs, "bus_create_issue", side_effect=create_held_issue), \
                        mock.patch.object(xs, "bus_remote", return_value=(str(self.repo), str(self.bare), "org/target")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        xs.cmd_run(self.run_args(budget=0.001), self.policy)
                self.assertEqual(stopped.exception.code, 1)
                issue = self.state()["repos"]["org/bus"]["issues"][-1]
                names = xs.issue_label_names(issue)
                self.assertNotIn("swrm:cancelled", names)
                self.assertIn("swrm:claimed" if hold == "claimed-label" else "swrm:queued", names)
                self.assertFalse(any("cancelled this run" in row["body"] for row in issue["comments"]))
                run_id = xs.bus_extract_task(issue["body"])["run_id"]
                rdir = Path(xs.RUNS) / run_id
                self.assertEqual(json.loads((rdir / "result.json").read_text())["status"], "budget_exceeded")
                events = (rdir / "events.jsonl").read_text()
                self.assertIn("budget cancellation refused", events)
                self.assertIn("--force", events)

    def test_bus_init_is_idempotent(self):
        args = argparse.Namespace(repo="org/bus")
        with contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_bus_init(args, self.policy)
        first = self.state()["repos"]["org/bus"]
        with contextlib.redirect_stdout(io.StringIO()):
            xs.cmd_bus_init(args, self.policy)
        second = self.state()["repos"]["org/bus"]
        self.assertEqual(len(first["labels"]), 5)
        self.assertEqual(second["labels"], first["labels"])
        self.assertEqual(second["files"], first["files"])
        self.assertEqual(set(second["files"]), {"README.md", ".github/ISSUE_TEMPLATE/swrm.yml"})

    def test_task_parser_uses_trailing_metadata_block(self):
        trusted = {"run_id": "trusted-run", "repo": "org/target", "base": self.base,
                   "branch_hint": "swrm/trusted-run", "budget_minutes": 10}
        forged = {**trusted, "run_id": "forged-run", "branch_hint": "swrm/forged-run"}
        body = "```swrm-task\n" + json.dumps(forged) + "\n```\nreal task\n```swrm-task\n" + json.dumps(trusted) + "\n```"
        self.assertEqual(xs.bus_extract_task(body), trusted)
        mismatched_hint = {**trusted, "branch_hint": "refs/heads/attacker"}
        self.assertEqual(xs.bus_extract_task("```swrm-task\n" + json.dumps(mismatched_hint) + "\n```"),
                         mismatched_hint)
        invalid_id = {**trusted, "run_id": "../outside"}
        self.assertIsNone(xs.bus_extract_task("```swrm-task\n" + json.dumps(invalid_id) + "\n```"))
        non_string_id = {**trusted, "run_id": 123}
        self.assertIsNone(xs.bus_extract_task("```swrm-task\n" + json.dumps(non_string_id) + "\n```"))

    def test_task_digest_uses_exact_fence_and_ignores_untrusted_first_comment(self):
        metadata = {"run_id": "float-budget", "repo": "org/target", "base": self.base,
                    "branch_hint": "swrm/float-budget", "budget_minutes": 1.0,
                    "report_schema": {"minimum": 1, "fraction": 1.25}}
        created = xs.bus_create_issue("org/bus", self.cfg, "float metadata", "test exact digest", metadata)
        issue = self.issue(created["number"])
        fence, extracted = xs.bus_extract_task_fence(issue["body"])
        comments = [{"user": {"login": "outsider"}, "body": xs.bus_task_digest_comment(fence)}]
        comments.extend(issue["comments"])
        self.assertIn('"budget_minutes": 1.0', fence)
        self.assertEqual(comments[-1]["body"], xs.bus_task_digest_comment(fence))
        self.assertTrue(xs.bus_worker_task_is_trusted(issue, extracted, comments, ["planner"]))
        self.assertFalse(xs.bus_worker_task_is_trusted(issue, extracted, comments[:1], ["planner"]))

    def test_cancel_comment_redacts_provider_credentials(self):
        created = xs.bus_create_issue("org/bus", self.cfg, "cancel", "test cancel", {
            "run_id": "cancel-secret", "repo": "org/target", "base": self.base,
            "budget_minutes": 1, "report_schema": xs.RESULT_SCHEMA})
        secret = "ghp_12345678901234567890"
        xs.bus_cancel_issue("org/bus", created["number"], self.cfg, f"gh failed with {secret}")
        comment = self.issue(created["number"])["comments"][-1]["body"]
        self.assertNotIn(secret, comment)
        self.assertIn("[REDACTED]", comment)

    def test_result_branch_validation_matches_run_id_pattern(self):
        report = {"status": "done", "summary": "ok", "files_changed": [], "tests": "",
                  "followups": [], "branch": "swrm/a_B-9", "commit": None}
        body = lambda value: "```swrm-result\n" + json.dumps({**report, "branch": value}) + "\n```"
        self.assertIsNotNone(xs.bus_parse_result(body(report["branch"])))
        self.assertIsNone(xs.bus_parse_result(body("swrm/a.b")))

    def test_cmd_run_redacts_github_bus_errors_in_result_and_ledger(self):
        secret = "github_pat_12345678901234567890"
        args = self.run_args()
        with mock.patch.object(xs, "new_run_id", return_value="redact-bus-error"), \
                mock.patch.object(xs, "cmd_bus_run", side_effect=xs.BusError(f"gh failed: {secret}")), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            xs.cmd_run(args, self.policy)
        rdir = Path(xs.RUNS) / "redact-bus-error"
        result = json.loads((rdir / "result.json").read_text())
        ledger = Path(xs.LEDGER).read_text()
        self.assertNotIn(secret, result["error"])
        self.assertNotIn(secret, ledger)
        self.assertIn("[REDACTED]", result["error"])

    def test_unpushed_base_fails_before_issue_creation(self):
        (self.repo / "local-only.txt").write_text("not pushed\n")
        self.git(self.repo, "add", "local-only.txt")
        self.git(self.repo, "commit", "-qm", "local only")
        args = self.run_args()
        args.base = self.git(self.repo, "rev-parse", "HEAD")
        stderr = io.StringIO()
        with mock.patch.object(xs, "bus_remote", return_value=(str(self.repo), str(self.bare), "org/target")), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as exited:
            xs.cmd_run(args, self.policy)
        self.assertEqual(exited.exception.code, 2)
        report = json.loads((Path(xs.RUNS) / next(Path(xs.RUNS).iterdir()).name / "result.json").read_text())
        self.assertEqual(report["error"], "push the base first")
        self.assertEqual(self.state()["repos"]["org/bus"]["issues"], [])

    def test_reference_worker_claims_runs_pushes_and_reports(self):
        fake_pi = self.root / "fake-pi"
        fake_pi.write_text("""#!/usr/bin/env python3
import json
with open('worker.txt', 'w') as stream:
    stream.write('worker result\\n')
report = {'status':'done','summary':'worker completed','files_changed':['worker.txt'],'tests':'fake executor','followups':[]}
message = {'role':'assistant','content':[{'type':'text','text':json.dumps(report)}], 'usage':{'input':2,'output':3}}
print(json.dumps({'type':'message_end','message':message}), flush=True)
""")
        fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)
        os.environ["OPENSWARM_PI_BIN"] = str(fake_pi)
        knowledge = self.root / "knowledge"
        (knowledge / "lessons").mkdir(parents=True)
        (knowledge / "card-types.yaml").write_text("types:\n  - fix\n")
        (knowledge / "lessons" / "L-0300.yaml").write_text(
            "id: L-0300\nrule: Validate worker edits with a focused test.\nguard_level: L2\n"
            "applies_to:\n  card_types: [fix]\n  paths: [src/*.py]\n  backends: [pi]\n")
        self.policy = xs.deep_merge(self.policy, {"knowledge": {"repo": str(knowledge)},
                                                 "pi": {"require_isolation": False}})
        local_policy = {"knowledge": {"repo": str(knowledge)}, "pi": {"require_isolation": False},
                        "bus": {"repo": "org/bus", "allowed_executors": ["executor"], "poll_seconds": 0.02}}
        (self.data / "policy.local.json").write_text(json.dumps(local_policy))
        run_id = "worker-e2e"
        metadata = {"run_id": run_id, "repo": "org/target", "base": self.base,
                    "branch_hint": "swrm/ignored-hint", "verify": f"touch {self.root / 'untrusted-verify-ran'}",
                    "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        issue = xs.bus_create_issue("org/bus", self.cfg, "worker e2e", "write worker.txt", metadata)
        worker_args = argparse.Namespace(bus="org/bus", name="external-agent", backend="pi", model=None,
                                         budget_minutes=1, once=True, repos="org/target", verify=None,
                                         card_type="fix", paths=["src/worker.py"])
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        xs.cmd_bus_worker(worker_args, self.policy)
        current = self.issue(issue["number"])
        names = {label["name"] for label in current["labels"]}
        self.assertIn("swrm:done", names)
        result_comments = [xs.bus_parse_result(comment["body"]) for comment in current["comments"]]
        report = next(report for report in result_comments if report)
        ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
        entry = next(row for row in ledger if row["id"] == run_id)
        self.assertEqual(entry["lessons_injected"], ["L-0300"])
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["branch"], f"swrm/{run_id}")
        self.assertFalse((self.root / "untrusted-verify-ran").exists())
        self.assertTrue(any("Ignored task verify" in note for note in report["followups"]))
        self.assertTrue(report["commit"])
        self.assertIn("worker.txt", self.git(None, "--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", report["commit"]))

    def test_worker_rejects_changed_task_body_and_unlisted_repo(self):
        metadata = {"run_id": "digest-check", "repo": "org/target", "base": self.base,
                    "branch_hint": "swrm/digest-check", "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        issue = xs.bus_create_issue("org/bus", self.cfg, "digest", "write a file", metadata)
        state = self.state()
        changed = next(row for row in state["repos"]["org/bus"]["issues"] if row["number"] == issue["number"])
        changed["body"] = changed["body"].replace('"budget_minutes": 1', '"budget_minutes": 2')
        self.state_path.write_text(json.dumps(state))
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        worker_args = argparse.Namespace(bus="org/bus", name="external-agent", backend="pi", model=None,
                                         budget_minutes=1, once=True, repos="org/target", verify=None)
        self.assertFalse(xs.bus_worker_run(changed, self.cfg, worker_args, self.policy))
        self.assertEqual(len(self.issue(issue["number"])["comments"]), 1)
        self.assertIn("swrm:queued", {label["name"] for label in self.issue(issue["number"])["labels"]})

        other = {**metadata, "run_id": "repo-check", "repo": "org/other",
                 "branch_hint": "swrm/invalid/ignored"}
        other_issue = xs.bus_create_issue("org/bus", self.cfg, "repo", "write a file", other)
        self.assertFalse(xs.bus_worker_run(other_issue, self.cfg, worker_args, self.policy))
        self.assertEqual(len(self.issue(other_issue["number"])["comments"]), 1)

        os.environ["OPENSWARM_FAKE_GH_ISSUE_LOGIN"] = "not-planner"
        forged_author_issue = xs.bus_create_issue("org/bus", self.cfg, "author", "write a file", metadata)
        os.environ["OPENSWARM_FAKE_GH_ISSUE_LOGIN"] = "planner"
        self.assertFalse(xs.bus_worker_run(forged_author_issue, self.cfg, worker_args, self.policy))
        self.assertEqual(len(self.issue(forged_author_issue["number"])["comments"]), 1)

    def test_bus_redacts_events_results_and_gh_errors(self):
        secrets = "ghp_12345678901234567890 github_pat_12345678901234567890 sk-12345678901234567890 Bearer abcdefghijklmnop"
        event_path = Path(xs.RUNS) / "redact" / "events.jsonl"
        xs.bus_event(str(event_path), secrets)
        event = event_path.read_text()
        for token in ("ghp_123", "github_pat_", "sk-123", "Bearer abc"):
            self.assertNotIn(token, event)
        self.assertGreaterEqual(event.count("[REDACTED]"), 4)

        rdir = Path(xs.RUNS) / "redact"
        with self.assertRaises(SystemExit):
            xs.bus_finish_run("redact", str(rdir), "redact test", str(self.repo), str(self.bare), None,
                              {"status": "failed", "summary": secrets, "files_changed": [], "tests": "",
                               "followups": [], "branch": None, "commit": None}, None, "failed", time.time())
        saved = (rdir / "result.json").read_text()
        ledger = Path(xs.LEDGER).read_text()
        self.assertNotIn("github_pat_", saved)
        self.assertNotIn("ghp_123", saved)
        self.assertNotIn("sk-123", ledger)

        with mock.patch.object(xs.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="",
                                                                               stderr="github_pat_12345678901234567890")):
            with self.assertRaises(xs.BusError) as error:
                xs.gh_transport("repos/org/bus/issues")
        self.assertNotIn("github_pat_", str(error.exception))

    # ------------------------------------------------------------------
    # #2159: reservation receipt, visible card state, withdrawal guard
    # ------------------------------------------------------------------

    RESERVE_MARKER = "swrm-bus:reserve"

    @staticmethod
    def stamp(seconds_ago=0, base=None):
        """An ISO-8601 UTC timestamp, optionally in the past."""
        moment = (base or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(seconds=seconds_ago)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def reserve_body(self, worker="external-agent", moment=None):
        """The machine-marked receipt a worker writes before it claims a card."""
        return f"<!-- {self.RESERVE_MARKER} worker={worker} at={moment or self.stamp()} -->"

    def create_bus_card(self, run_id, title="reservation card"):
        """Create a queued bus card and return its issue number."""
        created = xs.bus_create_issue("org/bus", self.cfg, title, "write worker.txt", {
            "run_id": run_id, "repo": "org/target", "base": self.base,
            "budget_minutes": 1, "report_schema": xs.RESULT_SCHEMA})
        return created["number"]

    def backdate_comment(self, number, comment_id, seconds_ago):
        """Move a posted comment (and the timestamp inside its receipt) into the past."""
        state = self.state()
        issue = next(row for row in state["repos"]["org/bus"]["issues"] if row["number"] == number)
        comment = next(row for row in issue["comments"] if row["id"] == comment_id)
        stamp = self.stamp(seconds_ago)
        comment["created_at"] = stamp
        comment["body"] = re.sub(r"at=\S+", f"at={stamp}", comment["body"])
        self.state_path.write_text(json.dumps(state))

    @staticmethod
    def call_cancel(number, reason, force, cfg):
        """Call the cancellation path; --force is the documented override."""
        parameters = inspect.signature(xs.bus_cancel_issue).parameters
        for name in ("force", "force_cancel", "forced", "override"):
            if name in parameters:
                return xs.bus_cancel_issue("org/bus", number, cfg, reason, **{name: True} if force else {})
        return xs.bus_cancel_issue("org/bus", number, cfg, reason)

    def cancel(self, number, reason="planner stop", force=False, cfg=None):
        """Cancel a bus card; return (cancelled, refusal-or-trace text)."""
        cfg = cfg or self.cfg
        before = len(self.issue(number)["comments"])
        failure = ""
        try:
            self.call_cancel(number, reason, force, cfg)
        except Exception as error:  # noqa: BLE001 - a refused withdrawal is reported as an error
            failure = str(error)
        issue = self.issue(number)
        cancelled = "swrm:cancelled" in xs.issue_label_names(issue)
        posted = " ".join(comment.get("body") or "" for comment in issue["comments"][before:])
        return cancelled, f"{failure} {posted}"

    @staticmethod
    def state_of(row):
        """The progress state a bus status row reports."""
        for key in ("bus_state", "state"):
            value = row.get(key)
            if isinstance(value, str):
                return value
        return None

    def test_worker_writes_a_reservation_receipt_before_it_claims(self):
        """#2159: the worker reserves the card before the claim, so the dispatcher can see it."""
        number = self.create_bus_card("reserve-order")
        worker_args = argparse.Namespace(bus="org/bus", name="external-agent", backend="pi", model=None,
                                         budget_minutes=1, once=True, repos="org/target", verify=None)
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        at_clone = {}

        def watched_clone(task, workdir, allowed_repos=None):
            at_clone["comments"] = list(self.issue(number)["comments"])
            raise xs.BusError("clone stopped by the test")

        with mock.patch.object(xs, "bus_worker_clone", side_effect=watched_clone):
            xs.bus_worker_run(self.issue(number), self.cfg, worker_args, self.policy)

        comments = self.issue(number)["comments"]
        receipts = [index for index, comment in enumerate(comments)
                    if self.RESERVE_MARKER in (comment.get("body") or "")]
        claims = [index for index, comment in enumerate(comments) if xs.bus_claim(comment) == "external-agent"]
        self.assertTrue(receipts, "the worker must leave a reservation receipt comment")
        self.assertTrue(claims, "the worker must still claim the card")
        self.assertLess(receipts[0], claims[0], "the receipt must be written before the claim")
        self.assertTrue(any(self.RESERVE_MARKER in (comment.get("body") or "") for comment in at_clone["comments"]),
                        "the receipt must be written before the clone starts")
        body = comments[receipts[0]]["body"]
        self.assertIn("worker=external-agent", body)
        self.assertRegex(body, r"at=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
        self.assertEqual(comments[receipts[0]]["user"]["login"], "executor")

    def test_bus_status_reports_the_reservation_state_and_latency(self):
        """#2159: bus status exposes reserved_at, reserve_latency_seconds and the card state."""
        base = datetime.datetime.now(datetime.timezone.utc)
        created = self.stamp(9, base)
        reserved_at = self.stamp(0, base)
        issues = [{"number": number, "title": f"[swrm] card {number}", "created_at": created,
                   "labels": [{"name": "swrm:queued"}]} for number in (1, 2, 3, 4, 5)]
        issues[3]["labels"] = [{"name": "swrm:done"}]
        receipt = {"id": 1, "body": self.reserve_body("external-agent", reserved_at),
                   "user": {"login": "executor"}, "created_at": reserved_at}
        claim = {"id": 2, "body": "/claim external-agent", "user": {"login": "executor"},
                 "created_at": reserved_at}
        result = {"id": 3, "body": "```swrm-result\n" + json.dumps(self.report("done")) + "\n```",
                  "user": {"login": "executor"}, "created_at": reserved_at}
        outsider = {"id": 4, "body": self.reserve_body("intruder", reserved_at),
                    "user": {"login": "not-allowed"}, "created_at": reserved_at}
        comments = {1: [receipt], 2: [], 3: [claim], 4: [result], 5: [outsider]}

        def fake_pages(endpoint, *args, **kwargs):
            if endpoint.endswith("/comments"):
                return comments[int(endpoint.split("/issues/")[1].split("/")[0])]
            return issues

        with mock.patch.object(xs, "gh_pages", side_effect=fake_pages), \
             mock.patch.object(xs, "bus_comments", side_effect=lambda repo, number: comments[number]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                xs.cmd_bus_status(argparse.Namespace(repo=None, json=True), self.policy)

        rows = {row["number"]: row for row in json.loads(output.getvalue())}
        reserved = rows[1]
        self.assertIn("reserved_at", reserved, "bus status must report the reservation timestamp")
        self.assertIn("reserve_latency_seconds", reserved, "bus status must report the reservation latency")
        self.assertIsNotNone(reserved["reserved_at"])
        self.assertAlmostEqual(xs.to_epoch(reserved["reserved_at"]), xs.to_epoch(reserved_at), delta=1)
        self.assertEqual(reserved["reserve_latency_seconds"], 9)
        self.assertEqual(self.state_of(reserved), "reserved")
        self.assertIsNone(rows[2]["reserved_at"])
        self.assertIsNone(rows[2]["reserve_latency_seconds"])
        self.assertEqual(self.state_of(rows[2]), "queued")
        self.assertIn(self.state_of(rows[3]), ("claimed", "running"))
        self.assertEqual(self.state_of(rows[4]), "done")
        self.assertIsNone(rows[5]["reserved_at"], "a receipt from an unlisted worker is not trusted")
        self.assertEqual(self.state_of(rows[5]), "queued")
        for row in rows.values():
            self.assertIn(self.state_of(row), ("queued", "reserved", "claimed", "running", "done"))

    def test_fresh_reservation_blocks_the_withdrawal(self):
        """#2159: a dispatcher must not withdraw a card a worker has just reserved."""
        number = self.create_bus_card("fresh-reserve")
        self.add_comment(number, self.reserve_body(), login="executor")
        cancelled, message = self.cancel(number, "planner stop")
        self.assertFalse(cancelled, "a fresh reservation must block the withdrawal")
        self.assertIn("--force", message)

    def test_claim_blocks_the_withdrawal(self):
        """#2159: a claimed card must not be withdrawn silently either."""
        number = self.create_bus_card("claimed-card")
        self.add_comment(number, "/claim external-agent", login="executor")
        self.set_labels(number, ["claimed"], ["queued"])
        cancelled, message = self.cancel(number, "planner stop")
        self.assertFalse(cancelled, "a claimed card must block the withdrawal")
        self.assertIn("--force", message)

    def test_forced_withdrawal_cancels_the_card_and_leaves_a_trace(self):
        """#2159: --force withdraws the card anyway, but the comment records it."""
        number = self.create_bus_card("forced-withdrawal")
        self.add_comment(number, self.reserve_body(), login="executor")
        cancelled, message = self.cancel(number, "planner stop", force=True)
        self.assertTrue(cancelled, "--force must cancel the card anyway")
        self.assertTrue(any(hint in message.lower() for hint in ("forc", "override")),
                        f"the forced withdrawal must leave a trace, got: {message!r}")

    def test_expired_reservation_does_not_block_the_withdrawal(self):
        """#2159: a reservation older than the freshness window no longer holds the card."""
        number = self.create_bus_card("expired-reserve")
        receipt = self.add_comment(number, self.reserve_body(), login="executor")
        self.backdate_comment(number, receipt["id"], 600)
        cancelled, _ = self.cancel(number, "planner stop")
        self.assertTrue(cancelled, "a reservation older than the freshness window must not block")

    def test_reservation_from_an_unlisted_worker_is_ignored(self):
        """#2159: only allowlisted workers can hold a reservation, like the claim rule."""
        number = self.create_bus_card("unlisted-reserve")
        self.add_comment(number, self.reserve_body(), login="not-allowed")
        self.add_comment(number, "/claim external-agent", login="not-allowed")
        cancelled, _ = self.cancel(number, "planner stop")
        self.assertTrue(cancelled, "a receipt or claim from an unlisted worker must not block")

    def test_reservation_window_is_configurable(self):
        """#2159: the documented policy key must change the freshness window."""
        self.assertEqual(self.cfg["reserve_ttl_seconds"], 300)
        number = self.create_bus_card("configured-window")
        receipt = self.add_comment(number, self.reserve_body(), login="executor")
        self.backdate_comment(number, receipt["id"], 5)
        cancelled, _ = self.cancel(number)
        self.assertFalse(cancelled, "the default window must still protect this receipt")
        cfg = xs.bus_config(xs.deep_merge(self.policy, {"bus": {"reserve_ttl_seconds": 1}}))
        self.assertEqual(cfg["reserve_ttl_seconds"], 1)
        cancelled, _ = self.cancel(number, cfg=cfg)
        self.assertTrue(cancelled, "the configured shorter window must expire this receipt")

    def test_bus_docs_document_the_reservation_protocol(self):
        """#2159: the bus protocol documentation must describe the receipt and the override."""
        text = (Path(xs.SKILL_DIR) / "docs" / "BUS.md").read_text(encoding="utf-8")
        self.assertTrue(self.RESERVE_MARKER in text,
                        "swrm/docs/BUS.md must document the reservation marker of the bus protocol")
        self.assertTrue("--force" in text,
                        "swrm/docs/BUS.md must document the --force override of the withdrawal guard")


class FakeGhReplaceRetryTests(unittest.TestCase):
    """fake_gh's state write must survive the transient Windows replace lock
    (PermissionError / WinError 5 when a concurrent handle holds the state
    file) instead of crashing the gh call -- the test_bus Windows flake."""

    def load_fixture(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fake_gh", FAKE_GH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run_fixture_main(self, module, state_path):
        env = {"OPENSWARM_FAKE_GH_STATE": str(state_path)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(module.sys, "argv", ["fake_gh.py", "api", "user"]), \
                contextlib.redirect_stdout(out):
            rc = module.main()
        return rc, out.getvalue()

    def test_transient_replace_lock_is_retried(self):
        module = self.load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "github.json"
            state.write_text(json.dumps({"repos": {}}))
            real_replace = os.replace
            calls = {"n": 0}

            def flaky_replace(src, dst):
                calls["n"] += 1
                if calls["n"] <= 2:
                    error = PermissionError(13, "simulated WinError 5 lock")
                    error.winerror = 5
                    raise error
                return real_replace(src, dst)

            with mock.patch.object(os, "replace", flaky_replace):
                rc, out = self.run_fixture_main(module, state)
            self.assertEqual(rc, 0)
            self.assertEqual(calls["n"], 3)
            self.assertEqual(json.loads(out)["login"], "executor")
            self.assertEqual(json.loads(state.read_text()), {"repos": {}})

    def test_permanent_replace_lock_still_raises_after_bounded_attempts(self):
        module = self.load_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "github.json"
            state.write_text(json.dumps({"repos": {}}))
            calls = {"n": 0}

            def locked_replace(src, dst):
                calls["n"] += 1
                error = PermissionError(13, "simulated WinError 5 lock")
                error.winerror = 5
                raise error

            with mock.patch.object(module, "REPLACE_ATTEMPTS", 3), \
                    mock.patch.object(module, "REPLACE_BASE_DELAY", 0), \
                    mock.patch.object(os, "replace", locked_replace):
                with self.assertRaises(PermissionError):
                    self.run_fixture_main(module, state)
            self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
