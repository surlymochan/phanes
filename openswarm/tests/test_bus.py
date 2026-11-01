"""Offline issue-bus coverage using fake gh and local bare target repositories."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _load

xs = _load.load()
FAKE_GH = Path(_load.HERE) / "fixtures" / "fake_gh.py"


class BusTitleHelperTests(unittest.TestCase):
    def test_short_handles_empty_overlong_and_multiline_titles(self):
        self.assertEqual(xs.short(""), "")
        self.assertEqual(xs.short("x" * 221), "x" * 220)
        self.assertEqual(xs.short("first\nsecond\tthird"), "first second third")


class BusPureHelperTests(unittest.TestCase):
    def test_github_repo_from_url_normalizes_github_urls_and_rejects_invalid_values(self):
        valid = (
            "https://github.com/owner/repo",
            "https://github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo",
        )
        for value in valid:
            with self.subTest(value=value):
                self.assertEqual(xs.github_repo_from_url(value), "owner/repo")

        invalid = (
            "https://gitlab.com/owner/repo",
            "https://github.com//repo",
            "https://github.com/owner/",
            "https://github.com/owner/repo?ref=main",
            "https://github.com/owner/repo name",
            123,
            None,
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(xs.github_repo_from_url(value))

    def test_gh_pr_number_accepts_numeric_values_and_rejects_invalid_values(self):
        self.assertEqual(xs.gh_pr_number({"number": 12}), 12)
        self.assertEqual(xs.gh_pr_number({"number": "12"}), 12)

        invalid = (
            {"number": None},
            {},
            {"number": "not-a-number"},
            {"number": []},
            None,
            "number",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(xs.gh_pr_number(value))


class BusResultParserTests(unittest.TestCase):
    @staticmethod
    def result_comment(report):
        return "```openswarm-result\n" + json.dumps(report) + "\n```"

    def test_success_and_failure_result_comments_parse_non_empty_contract_fields(self):
        samples = (
            {"status": "done", "run_id": "run-success-753", "branch": "openswarm/run-success-753",
             "summary": "worker completed successfully", "files_changed": ["worker.txt"],
             "tests": "python3 -m unittest", "followups": [], "commit": "a" * 40,
             "error_class": "none"},
            {"status": "failed", "run_id": "run-failure-753", "branch": "openswarm/run-failure-753",
             "summary": "worker verification failed", "files_changed": [],
             "tests": "not run: verification failed", "followups": ["inspect test output"],
             "commit": None, "error_class": "verify_failed"},
        )

        for sample in samples:
            with self.subTest(status=sample["status"]):
                parsed = xs.bus_parse_result(self.result_comment(sample))
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["run_id"], sample["run_id"])
                self.assertEqual(parsed["status"], sample["status"])
                self.assertTrue(parsed["branch"])
                self.assertTrue(parsed["summary"])
                self.assertTrue(parsed["error_class"])

    def test_failed_result_missing_error_class_is_rejected(self):
        report = {"status": "failed", "run_id": "run-failure-missing-error", "summary": "failed",
                  "files_changed": [], "tests": "not run", "followups": [],
                  "branch": None, "commit": None}
        self.assertIsNone(xs.bus_parse_result(self.result_comment(report)))

    def test_result_comment_missing_required_field_is_rejected(self):
        report = {"status": "done", "summary": "completed", "files_changed": [], "tests": "ok",
                  "followups": [], "branch": "openswarm/run-required-753", "commit": None}
        required = ("status", "summary", "files_changed", "tests", "followups", "branch", "commit")
        for field in required:
            with self.subTest(field=field):
                missing = {key: value for key, value in report.items() if key != field}
                self.assertIsNone(xs.bus_parse_result(self.result_comment(missing)))

    def test_result_delivery_is_idempotent_but_conflicting_payload_stops_progress(self):
        commit = "a" * 40
        report = {"status": "done", "run_id": "run-797", "branch": "openswarm/run-797",
                  "summary": "completed", "files_changed": ["worker.txt"], "tests": "fake",
                  "followups": [], "commit": commit, "error_class": "none", "pr_url": None}
        comments = [{"user": {"login": "executor"}, "body": self.result_comment(report)},
                    {"user": {"login": "executor"}, "body": self.result_comment(dict(report))}]
        parsed = [xs.bus_parse_result(comment["body"], "run-797") for comment in comments]
        self.assertTrue(parsed[0]["branch"] and parsed[0]["commit"])
        self.assertFalse(xs.bus_result_conflict(parsed))
        conflict = dict(report, commit="b" * 40)
        parsed.append(xs.bus_parse_result(self.result_comment(conflict), "run-797"))
        self.assertTrue(xs.bus_result_conflict(parsed))


class BusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-bus-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "planner"
        self.data.mkdir()
        self.env_names = ("OPENSWARM_HOME", "OPENSWARM_GH_BIN", "OPENSWARM_FAKE_GH_STATE",
                          "OPENSWARM_FAKE_GH_LOGIN", "OPENSWARM_FAKE_GH_ISSUE_LOGIN",
                          "OPENSWARM_FAKE_GH_NOW", "OPENSWARM_BUS_TARGET_URL", "OPENSWARM_STATE_FILE",
                          "OPENSWARM_PI_BIN", "OPENSWARM_FLEET_GUARD_CONFIG", "OPENSWARM_FLEET_GUARD_REQUIRED",
                          "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                          "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1")
        self.old_env = {key: os.environ.get(key) for key in self.env_names}
        self.addCleanup(self.restore_env)
        # Git's detached auto-maintenance can write into these temporary bare
        # repositories after a worker's clone or push exits.  Install this
        # fixture-only override before creating any of the repositories.
        os.environ.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "maintenance.auto",
                           "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "gc.auto",
                           "GIT_CONFIG_VALUE_1": "0"})
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
                                                               "poll_seconds": 0.02, "labels_prefix": "openswarm"}})
        self.cfg = xs.bus_config(self.policy)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.restore_env()
        for key, value in self.constants.items():
            setattr(xs, key, value)
        # Git 后台维护可能在 worker 退出后短暂写入临时目录，清理时重试以等待其完成。
        tmp_path = self.tmp.name
        try:
            self.tmp.cleanup()
        except OSError:
            for attempt in range(3):
                if not os.path.exists(tmp_path):
                    break
                try:
                    shutil.rmtree(tmp_path)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)

    def restore_env(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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

    def worker_args(self, name="external-agent", budget=1, repos="org/target"):
        return argparse.Namespace(bus="org/bus", name=name, backend="pi", model=None, budget_minutes=budget,
                                  once=True, repos=repos, verify=None, card_type=None, paths=None, rework_of=None)

    def write_fake_pi(self, name="fake-pi", sleep=0):
        """Write an offline Pi stand-in that reports a done result, optionally after a long sleep."""
        started = self.root / (name + ".started")
        finished = self.root / (name + ".finished")
        path = self.root / name
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, time\n"
            f"open({str(started)!r}, 'a').write('started\\n')\n"
            f"time.sleep({sleep!r})\n"
            f"open({str(finished)!r}, 'w').write('finished\\n')\n"
            "with open('worker.txt', 'w') as stream:\n"
            "    stream.write('worker result\\n')\n"
            "report = {'status':'done','summary':'worker completed','files_changed':['worker.txt'],"
            "'tests':'fake executor','followups':[],'error_class':'none'}\n"
            "message = {'role':'assistant','content':[{'type':'text','text':json.dumps(report)}], "
            "'usage':{'input':2,'output':3}}\n"
            "print(json.dumps({'type':'message_end','message':message}), flush=True)\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def disable_fleet_guard(self):
        """Keep the resident worker off the host's fleet capacity guard.

        The worker spawns `openswarm run` as a child process; that child would
        otherwise reserve a fleet slot through the host configuration.
        """
        os.environ["OPENSWARM_FLEET_GUARD_CONFIG"] = str(self.root / "no-fleet-capacity.json")
        os.environ["OPENSWARM_FLEET_GUARD_REQUIRED"] = "0"

    def wait_for(self, predicate, timeout=60, message="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"timed out waiting for {message}")

    def run_status(self, run_id):
        path = Path(xs.RUNS) / run_id / "status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

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
        self.git(clone, "push", "-q", "origin", "HEAD:refs/heads/openswarm/" + run_id)
        return self.git(clone, "rev-parse", "HEAD")

    @staticmethod
    def report(status, branch=None, commit=None):
        return {"status": status, "summary": "external work", "files_changed": ["worker.txt"],
                "tests": "fake verify", "followups": [], "branch": branch, "commit": commit,
                "pr_url": None, "error_class": "executor_failed" if status == "failed" else "none"}

    def send_result(self, number, report, label="done", login="executor"):
        report.setdefault("run_id", xs.bus_extract_task(self.issue(number)["body"])["run_id"])
        self.add_comment(number, "```openswarm-result\n" + json.dumps(report) + "\n```", login=login)
        self.set_labels(number, [label], ["queued", "claimed"])

    def finish_thread(self, thread, outcome):
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "planner did not finish")
        self.assertNotIn("error", outcome, outcome.get("error"))
        return json.loads((Path(xs.RUNS) / self.issue()["body"].split('"run_id": "')[1].split('"')[0] / "result.json").read_text())

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
        self.send_result(issue["number"], self.report("done", f"openswarm/{run_id}", commit))
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
        self.assertEqual(result["branch"], f"openswarm/{run_id}")
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["bus"]["executor"], "external-agent")
        self.assertEqual(self.git(self.repo, "rev-parse", f"openswarm/{run_id}"), commit)
        ledger = [json.loads(line) for line in Path(xs.LEDGER).read_text().splitlines()]
        self.assertEqual(ledger[-1]["status"], "done")

    def test_untrusted_claim_and_result_are_ignored(self):
        thread, outcome, issue = self.start_planner(budget=0.02)
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
        self.assertIn("openswarm:cancelled", {label["name"] for label in self.issue()["labels"]})

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
        self.assertIn("openswarm:cancelled", names)
        self.assertTrue(any("budget expired" in comment["body"] for comment in self.issue()["comments"]))

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
        self.assertEqual(set(second["files"]), {"README.md", ".github/ISSUE_TEMPLATE/openswarm.yml"})

    def test_task_parser_uses_trailing_metadata_block(self):
        trusted = {"run_id": "trusted-run", "repo": "org/target", "base": self.base,
                   "branch_hint": "openswarm/trusted-run", "budget_minutes": 10}
        forged = {**trusted, "run_id": "forged-run", "branch_hint": "openswarm/forged-run"}
        body = "```openswarm-task\n" + json.dumps(forged) + "\n```\nreal task\n```openswarm-task\n" + json.dumps(trusted) + "\n```"
        self.assertEqual(xs.bus_extract_task(body), trusted)
        mismatched_hint = {**trusted, "branch_hint": "refs/heads/attacker"}
        self.assertEqual(xs.bus_extract_task("```openswarm-task\n" + json.dumps(mismatched_hint) + "\n```"),
                         mismatched_hint)
        invalid_id = {**trusted, "run_id": "../outside"}
        self.assertIsNone(xs.bus_extract_task("```openswarm-task\n" + json.dumps(invalid_id) + "\n```"))
        non_string_id = {**trusted, "run_id": 123}
        self.assertIsNone(xs.bus_extract_task("```openswarm-task\n" + json.dumps(non_string_id) + "\n```"))

    def test_task_digest_uses_exact_fence_and_ignores_untrusted_first_comment(self):
        metadata = {"run_id": "float-budget", "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/float-budget", "budget_minutes": 1.0,
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

    def test_duplicate_digest_publication_is_idempotent_and_json_order_is_canonical(self):
        metadata = {"run_id": "canonical-digest", "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/canonical-digest", "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        created = xs.bus_create_issue("org/bus", self.cfg, "canonical digest", "write worker.txt", metadata)
        first = self.issue(created["number"])["comments"][0]
        reordered = json.dumps({key: metadata[key] for key in reversed(list(metadata))}, indent=2)
        self.assertEqual(xs.bus_task_digest(metadata), xs.bus_task_digest(reordered))
        self.assertEqual(xs.bus_task_digest_comment(metadata), xs.bus_task_digest_comment(reordered))
        xs.bus_publish_task_digest("org/bus", created["number"], reordered)
        comments = self.issue(created["number"])["comments"]
        self.assertEqual([comment["body"] for comment in comments], [first["body"]])

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
        report = {"status": "done", "run_id": "a_B-9", "summary": "ok", "files_changed": [], "tests": "",
                  "followups": [], "branch": "openswarm/a_B-9", "commit": None, "error_class": "none"}
        body = lambda value: "```openswarm-result\n" + json.dumps({**report, "branch": value}) + "\n```"
        self.assertIsNotNone(xs.bus_parse_result(body(report["branch"])))
        self.assertIsNone(xs.bus_parse_result(body("openswarm/a.b")))

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
report = {'status':'done','summary':'worker completed','files_changed':['worker.txt'],'tests':'fake executor','followups':[],'error_class':'none'}
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
                    "branch_hint": "openswarm/ignored-hint", "verify": f"touch {self.root / 'untrusted-verify-ran'}",
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
        self.assertIn("openswarm:done", names)
        result_comments = [xs.bus_parse_result(comment["body"]) for comment in current["comments"]]
        report = next(report for report in result_comments if report)
        ledger = [json.loads(line) for line in (self.data / "ledger.jsonl").read_text().splitlines()]
        entry = next(row for row in ledger if row["id"] == run_id)
        self.assertEqual(entry["lessons_injected"], ["L-0300"])
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["branch"], f"openswarm/{run_id}")
        self.assertFalse((self.root / "untrusted-verify-ran").exists())
        self.assertTrue(any("Ignored task verify" in note for note in report["followups"]))
        self.assertTrue(report["commit"])
        self.assertIn("worker.txt", self.git(None, "--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", report["commit"]))

    def test_setup_failure_restores_git_environment(self):
        saved = {key: os.environ.get(key) for key in self.env_names}
        sentinel = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "test.sentinel",
                    "GIT_CONFIG_VALUE_0": "preserved"}
        failed = BusTests("runTest")
        try:
            for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                        "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
                os.environ.pop(key, None)
            os.environ.update(sentinel)
            with mock.patch.object(BusTests, "git", side_effect=OSError("initial git failure")):
                with self.assertRaisesRegex(OSError, "initial git failure"):
                    failed.setUp()
            failed.doCleanups()
            self.assertEqual({key: os.environ.get(key) for key in sentinel}, sentinel)
            self.assertIsNone(os.environ.get("GIT_CONFIG_KEY_1"))
            self.assertIsNone(os.environ.get("GIT_CONFIG_VALUE_1"))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if hasattr(failed, "tmp"):
                failed.tmp.cleanup()

    def test_worker_rejects_changed_task_body_and_unlisted_repo(self):
        metadata = {"run_id": "digest-check", "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/digest-check", "budget_minutes": 1,
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
        self.assertIn("openswarm:queued", {label["name"] for label in self.issue(issue["number"])["labels"]})

        other = {**metadata, "run_id": "repo-check", "repo": "org/other",
                 "branch_hint": "openswarm/invalid/ignored"}
        other_issue = xs.bus_create_issue("org/bus", self.cfg, "repo", "write a file", other)
        self.assertFalse(xs.bus_worker_run(other_issue, self.cfg, worker_args, self.policy))
        self.assertEqual(len(self.issue(other_issue["number"])["comments"]), 1)

        os.environ["OPENSWARM_FAKE_GH_ISSUE_LOGIN"] = "not-planner"
        forged_author_issue = xs.bus_create_issue("org/bus", self.cfg, "author", "write a file", metadata)
        os.environ["OPENSWARM_FAKE_GH_ISSUE_LOGIN"] = "planner"
        self.assertFalse(xs.bus_worker_run(forged_author_issue, self.cfg, worker_args, self.policy))
        self.assertEqual(len(self.issue(forged_author_issue["number"])["comments"]), 1)

    def test_two_executors_claim_the_same_card_and_only_the_earliest_allowlisted_claim_wins(self):
        """Two executors /claim one card at the same time; only the earliest allowlisted claim is honoured."""
        os.environ["OPENSWARM_FAKE_GH_NOW"] = "2026-01-01T00:00:00+00:00"
        (self.data / "policy.local.json").write_text(json.dumps({"pi": {"require_isolation": False}}))
        os.environ["OPENSWARM_PI_BIN"] = str(self.write_fake_pi("fake-pi"))
        self.policy = xs.deep_merge(self.policy, {"bus": {"allowed_executors": ["executor-a", "executor-b"]}})
        self.cfg = xs.bus_config(self.policy)
        metadata = {"run_id": "claim-race", "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/claim-race", "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        issue = xs.bus_create_issue("org/bus", self.cfg, "claim race", "write worker.txt", metadata)
        # An account outside the allowlist claims first; it must never win the card.
        self.add_comment(issue["number"], "/claim intruder", login="intruder")
        card = self.issue(issue["number"])
        identity = threading.local()
        outcomes = {}

        def worker(name):
            identity.login = name
            try:
                outcomes[name] = xs.bus_worker_run(card, self.cfg, self.worker_args(name=name), self.policy)
            except BaseException as error:  # retain thread failures as test evidence
                outcomes[name] = error

        original = xs.gh_transport

        def transport(endpoint, method="GET", payload=None, allow_not_found=False):
            # Every request of a worker acts as that worker's GitHub account.
            separator = "&" if "?" in endpoint else "?"
            return original(f"{endpoint}{separator}as={getattr(identity, 'login', 'planner')}",
                            method, payload, allow_not_found)

        threads = [threading.Thread(target=worker, args=(name,), daemon=True)
                   for name in ("executor-a", "executor-b")]
        with mock.patch.object(xs, "gh_transport", transport):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
                self.assertFalse(thread.is_alive(), "a bus worker did not finish")
        for name, outcome in outcomes.items():
            self.assertNotIsInstance(outcome, BaseException, f"{name}: {outcome}")
        comments = self.issue(issue["number"])["comments"]
        claims = [comment for comment in comments if xs.bus_claim(comment)]
        self.assertEqual(len(claims), 3, "both executors and the intruder must have claimed the card")
        allowed = [comment for comment in claims if comment["user"]["login"] in ("executor-a", "executor-b")]
        self.assertEqual(len(allowed), 2)
        # The clock is pinned, so the earliest allowlisted claim is the one with the lowest comment id.
        winner = min(allowed, key=lambda comment: (xs.to_epoch(comment["created_at"]) or 0, int(comment["id"])))
        self.assertEqual(int(winner["id"]), min(int(comment["id"]) for comment in allowed))
        self.assertNotEqual(winner["user"]["login"], "intruder")
        results = [comment for comment in comments if xs.bus_parse_result(comment["body"])]
        self.assertEqual(len(results), 1, "only the winning claim may report a result")
        self.assertEqual(results[0]["user"]["login"], winner["user"]["login"])
        self.assertEqual((self.root / "fake-pi.started").read_text().splitlines(), ["started"],
                         "the losing worker must not execute the task")
        report = xs.bus_parse_result(results[0]["body"])
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["branch"], "openswarm/claim-race")

    def test_worker_refuses_the_card_when_the_task_digest_does_not_match(self):
        """A card whose digest comment disagrees with the task fence is refused, never claimed."""
        worker_args = self.worker_args()

        def rewrite(number, mutate):
            state = self.state()
            row = next(item for item in state["repos"]["org/bus"]["issues"] if item["number"] == number)
            mutate(row)
            self.state_path.write_text(json.dumps(state))
            return row

        forged = {"run_id": "forged", "repo": "org/target", "base": self.base,
                  "branch_hint": "openswarm/forged", "budget_minutes": 9,
                  "report_schema": xs.RESULT_SCHEMA}
        cases = {
            "mismatch": lambda row: row["comments"][-1].update(
                {"body": xs.bus_task_digest_comment(json.dumps(forged, indent=2))}),
            "missing": lambda row: row.update({"comments": []}),
            "untrusted": lambda row: row["comments"][-1].update({"user": {"login": "intruder"}}),
        }
        for name, mutate in cases.items():
            os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "planner"
            metadata = {"run_id": "digest-" + name, "repo": "org/target", "base": self.base,
                        "branch_hint": "openswarm/digest-" + name, "budget_minutes": 1,
                        "report_schema": xs.RESULT_SCHEMA}
            issue = xs.bus_create_issue("org/bus", self.cfg, "digest", "write worker.txt", metadata)
            os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
            row = rewrite(issue["number"], mutate)
            self.assertFalse(xs.bus_worker_run(row, self.cfg, worker_args, self.policy), name)
            saved = self.issue(issue["number"])
            self.assertFalse(any(xs.bus_claim(comment) for comment in saved["comments"]),
                             f"{name}: a refused card must not be claimed")
            self.assertIn("openswarm:queued", {label["name"] for label in saved["labels"]}, name)

    def test_cancelled_label_stops_the_running_worker(self):
        """A cancelled label stops the worker: the executor is killed and no result is reported."""
        # The worker runs `openswarm run --backend pi` as a subprocess, which needs the fake Pi binary.
        (self.data / "policy.local.json").write_text(json.dumps({"pi": {"require_isolation": False}}))
        os.environ["OPENSWARM_PI_BIN"] = str(self.write_fake_pi("fake-pi-slow", sleep=60))
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "planner"
        metadata = {"run_id": "cancel-worker", "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/cancel-worker", "budget_minutes": 5,
                    "report_schema": xs.RESULT_SCHEMA}
        issue = xs.bus_create_issue("org/bus", self.cfg, "cancel", "write worker.txt", metadata)
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        outcome = {}

        def run():
            try:
                outcome["handled"] = xs.bus_worker_run(self.issue(issue["number"]), self.cfg,
                                                       self.worker_args(budget=5), self.policy)
            except BaseException as error:  # retain thread failures as test evidence
                outcome["error"] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        self.wait_for(lambda: (self.root / "fake-pi-slow.started").exists(),
                      message="the worker executor to start")
        self.wait_for(lambda: self.run_status("cancel-worker").get("executor_pid"),
                      message="the worker run to record its executor")
        self.set_labels(issue["number"], ["cancelled"], ["queued"])
        thread.join(timeout=90)
        self.assertFalse(thread.is_alive(), "the worker did not stop after the cancelled label")
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertFalse(outcome["handled"], "a cancelled card must not be reported as handled")
        saved = self.issue(issue["number"])
        self.assertFalse(any(xs.bus_parse_result(comment["body"]) for comment in saved["comments"]),
                         "a cancelled card must not carry a worker result")
        names = {label["name"] for label in saved["labels"]}
        self.assertIn("openswarm:cancelled", names)
        self.assertNotIn("openswarm:done", names)
        self.assertFalse((self.root / "fake-pi-slow.finished").exists(), "the worker executor was not stopped")

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

    def test_resident_worker_claims_one_card_reports_the_result_and_keeps_polling(self):
        """A resident worker claims a card once, writes the result back and stops on request."""
        self.disable_fleet_guard()
        os.environ["OPENSWARM_PI_BIN"] = str(self.write_fake_pi("fake-pi"))
        local_policy = {"pi": {"require_isolation": False},
                        "bus": {"repo": "org/bus", "allowed_executors": ["executor"], "poll_seconds": 0.02}}
        (self.data / "policy.local.json").write_text(json.dumps(local_policy))
        self.policy = xs.deep_merge(self.policy, local_policy)
        run_id = "resident-success-792"
        metadata = {"run_id": run_id, "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/resident-success", "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        issue = xs.bus_create_issue("org/bus", self.cfg, "resident worker success", "write worker.txt", metadata)
        stop_event = threading.Event()
        worker_args = argparse.Namespace(bus="org/bus", name="external-agent", backend="pi", model=None,
                                         budget_minutes=1, once=False, repos="org/target", verify=None,
                                         card_type=None, paths=None, rework_of=None, stop_event=stop_event)
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        thread = threading.Thread(target=lambda: xs.cmd_bus_worker(worker_args, self.policy), daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop_event.set(), thread.join(timeout=5)))
        self.wait_for(lambda: "openswarm:done" in {label["name"] for label in self.issue(issue["number"])["labels"]},
                      message="resident worker to finish and set openswarm:done label")
        stop_event.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "resident worker did not stop after the stop event was set")
        current = self.issue(issue["number"])
        names = {label["name"] for label in current["labels"]}
        self.assertIn("openswarm:done", names)
        self.assertNotIn("openswarm:queued", names)
        self.assertNotIn("openswarm:claimed", names)
        results = [parsed for parsed in
                   (xs.bus_parse_result(comment["body"]) for comment in current["comments"]) if parsed]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")
        self.assertEqual(results[0]["branch"], f"openswarm/{run_id}")
        claims = [comment for comment in current["comments"] if xs.bus_claim(comment)]
        self.assertEqual(len(claims), 1, "the card must be claimed exactly once")

    def test_resident_worker_backs_off_after_a_transient_error_and_recovers(self):
        """One temporary GitHub 429 is retried with backoff; the worker then claims the card once."""
        self.disable_fleet_guard()
        os.environ["OPENSWARM_PI_BIN"] = str(self.write_fake_pi("fake-pi"))
        local_policy = {"pi": {"require_isolation": False},
                        "bus": {"repo": "org/bus", "allowed_executors": ["executor"], "poll_seconds": 0.02}}
        (self.data / "policy.local.json").write_text(json.dumps(local_policy))
        self.policy = xs.deep_merge(self.policy, local_policy)
        run_id = "resident-retry-792"
        metadata = {"run_id": run_id, "repo": "org/target", "base": self.base,
                    "branch_hint": "openswarm/resident-retry", "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        issue = xs.bus_create_issue("org/bus", self.cfg, "resident retry", "write worker.txt", metadata)
        state = self.state()
        state["pending_errors"] = [{"status": 429, "message": "HTTP 429: Rate limit exceeded",
                                    "endpoint": "issues"}]
        self.state_path.write_text(json.dumps(state))
        stop_event = threading.Event()
        worker_args = argparse.Namespace(bus="org/bus", name="external-agent", backend="pi", model=None,
                                         budget_minutes=1, once=False, repos="org/target", verify=None,
                                         card_type=None, paths=None, rework_of=None, stop_event=stop_event)
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        thread = threading.Thread(target=lambda: xs.cmd_bus_worker(worker_args, self.policy), daemon=True)
        thread.start()
        self.addCleanup(lambda: (stop_event.set(), thread.join(timeout=5)))
        self.wait_for(lambda: "openswarm:done" in {label["name"] for label in self.issue(issue["number"])["labels"]},
                      message="resident worker to recover from the 429 and set openswarm:done label")
        stop_event.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "resident worker did not stop after the stop event was set")
        current = self.issue(issue["number"])
        self.assertIn("openswarm:done", {label["name"] for label in current["labels"]})
        results = [parsed for parsed in
                   (xs.bus_parse_result(comment["body"]) for comment in current["comments"]) if parsed]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")
        claims = [comment for comment in current["comments"] if xs.bus_claim(comment)]
        self.assertEqual(len(claims), 1, "the card must not be claimed twice after the transient error")

    def test_resident_worker_stop_signal_finishes_the_running_task_and_claims_no_other_card(self):
        """SIGTERM ends the resident loop after the running task reported its result."""
        self.disable_fleet_guard()
        (self.data / "policy.local.json").write_text(json.dumps({
            "pi": {"require_isolation": False},
            "bus": {"repo": "org/bus", "allowed_executors": ["executor"], "allowed_planners": ["planner"],
                    "worker_repos": ["org/target"], "poll_seconds": 0.02, "labels_prefix": "openswarm"}}))
        os.environ["OPENSWARM_PI_BIN"] = str(self.write_fake_pi("fake-pi-signal-worker", sleep=1))
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "planner"
        first = {"run_id": "signal-task-1", "repo": "org/target", "base": self.base,
                 "branch_hint": "openswarm/signal-task-1", "budget_minutes": 2,
                 "report_schema": xs.RESULT_SCHEMA}
        issue1 = xs.bus_create_issue("org/bus", self.cfg, "task 1", "write worker.txt", first)
        second = {"run_id": "signal-task-2", "repo": "org/target", "base": self.base,
                  "branch_hint": "openswarm/signal-task-2", "budget_minutes": 2,
                  "report_schema": xs.RESULT_SCHEMA}
        issue2 = xs.bus_create_issue("org/bus", self.cfg, "task 2", "write worker.txt", second)
        env = dict(os.environ, OPENSWARM_FAKE_GH_LOGIN="executor")
        command = [sys.executable, str(Path(_load.ROOT) / "bin" / "openswarm"), "bus-worker",
                   "--bus", "org/bus", "--name", "external-agent", "--backend", "pi",
                   "--budget-minutes", "2", "--repos", "org/target"]
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.terminate(), proc.wait()))
        self.wait_for(lambda: (self.root / "fake-pi-signal-worker.started").exists(),
                      message="the executor of task 1 to start")
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, f"worker stdout: {stdout}\nstderr: {stderr}")
        self.assertTrue((self.root / "fake-pi-signal-worker.finished").exists())
        saved1 = self.issue(issue1["number"])
        self.assertIn("openswarm:done", {label["name"] for label in saved1["labels"]})
        results1 = [parsed for parsed in
                    (xs.bus_parse_result(comment["body"]) for comment in saved1["comments"]) if parsed]
        self.assertEqual(len(results1), 1, "task 1 must have written back its result")
        claims1 = [comment for comment in saved1["comments"] if xs.bus_claim(comment)]
        self.assertEqual(len(claims1), 1, "task 1 must not be claimed twice")
        saved2 = self.issue(issue2["number"])
        self.assertIn("openswarm:queued", {label["name"] for label in saved2["labels"]})
        self.assertNotIn("openswarm:claimed", {label["name"] for label in saved2["labels"]})
        self.assertNotIn("openswarm:done", {label["name"] for label in saved2["labels"]})
        claims2 = [comment for comment in saved2["comments"] if xs.bus_claim(comment)]
        self.assertEqual(len(claims2), 0, "task 2 must not be claimed after the stop signal")

    def test_resident_worker_fails_after_consecutive_transient_errors(self):
        """Repeated temporary GitHub errors end the worker with an explicit failure."""
        self.disable_fleet_guard()
        local_policy = {"bus": {"repo": "org/bus", "allowed_executors": ["executor"],
                                "poll_seconds": 0.01, "poll_retries": 3}}
        (self.data / "policy.local.json").write_text(json.dumps(local_policy))
        policy = xs.deep_merge(self.policy, local_policy)
        state = self.state()
        state["pending_errors"] = [{"status": 503, "message": "HTTP 503: Service Unavailable",
                                    "endpoint": "issues"}] * 3
        self.state_path.write_text(json.dumps(state))
        worker_args = argparse.Namespace(bus="org/bus", name="external-agent", backend="pi", model=None,
                                         budget_minutes=1, once=False, repos="org/target", verify=None,
                                         card_type=None, paths=None, rework_of=None)
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        with self.assertRaises(xs.BusError) as error:
            xs.cmd_bus_worker(worker_args, policy)
        self.assertIn("consecutive transient errors", str(error.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
