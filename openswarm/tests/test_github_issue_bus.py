"""Lease state-machine and offline result parsing coverage for the GitHub issue bus."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    import _load
except ModuleNotFoundError:
    from openswarm.tests import _load

xs = _load.load()
FAKE_GH = Path(_load.HERE) / "fixtures" / "fake_gh.py"


def claim(worker, created_at, comment_id):
    return {"id": comment_id, "body": f"/claim {worker}",
            "user": {"login": worker}, "created_at": created_at}


def result(status="done", run_id="offline-run-1", branch=None, error_class="none"):
    return {"status": status, "run_id": run_id, "summary": "worker result",
            "files_changed": [], "tests": "not run", "followups": [],
            "branch": branch if branch is not None else f"openswarm/{run_id}",
            "commit": None, "error_class": error_class}


def result_comment(payload):
    return "```openswarm-result\n" + json.dumps(payload) + "\n```"


class GitHubIssueBusLeaseTests(unittest.TestCase):
    def test_unclaim_releases_claim_and_keeps_failure_classifiable(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    {"id": 2, "body": "/unclaim worker-a", "user": {"login": "worker-a"},
                     "created_at": "2026-01-01T00:00:10+00:00"},
                    {"id": 3, "body": result_comment({**result("failed"), "error_class": "task_changed",
                                                            "branch": None}),
                     "user": {"login": "worker-a"}, "created_at": "2026-01-01T00:00:20+00:00"}]
        self.assertIsNone(xs.bus_winning_claim(comments, {"worker-a"}, 60,
                                               xs.to_epoch("2026-01-01T00:00:30+00:00")))
        winning, parsed = xs.bus_winning_result(comments, {"worker-a"}, 60)
        self.assertEqual(winning["id"], 1)
        self.assertEqual(parsed["error_class"], "task_changed")

    def test_task_drift_distinguishes_base_from_other_json_changes(self):
        task = {"run_id": "offline-run-1", "repo": "org/target", "base": "a" * 40, "budget_minutes": 1}
        issue = {"user": {"login": "planner"},
                 "body": "```openswarm-task\n" + json.dumps({**task, "budget_minutes": 2}) + "\n```"}
        self.assertEqual(xs.bus_task_drift_reason(task, issue, [], {"planner"}), "task_changed")
        issue["body"] = "```openswarm-task\n" + json.dumps({**task, "base": "b" * 40}) + "\n```"
        self.assertEqual(xs.bus_task_drift_reason(task, issue, [], {"planner"}), "base_changed")

    def test_unadvertised_base_is_classified_before_checkout(self):
        task = {"repo": "org/target", "base": "a" * 40}
        with mock.patch.object(xs, "sh", return_value=(0, "b" * 40 + "\trefs/heads/main\n", "")) as run:
            self.assertFalse(xs.bus_worker_base_available(task, "target.git"))
        run.assert_called_once()
        self.assertEqual(xs.bus_error_class(xs.BaseUnavailableError("push the base first")),
                         "base_unavailable")

    def test_reachable_base_is_advertised_by_branch_or_tag(self):
        base = "a" * 40
        task = {"repo": "org/target", "base": base}
        with mock.patch.object(xs, "sh", return_value=(0, base + "\trefs/tags/release\n", "")):
            self.assertTrue(xs.bus_worker_base_available(task, "target.git"))

    def test_result_comment_sample_parses_to_dict_without_github(self):
        comment = """```openswarm-result
{"status":"done","run_id":"offline-run-1","error_class":"none","pr_url":null}
```"""
        payload = json.loads(comment.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertEqual(payload, {"status": "done", "run_id": "offline-run-1", "error_class": "none", "pr_url": None})

    def test_claim_result_contract_migrates_labels_and_keeps_first_valid_claim(self):
        issue = {"labels": ["openswarm:queued"], "comments": []}
        issue["comments"].append(claim("worker-a", "2026-01-01T00:00:00+00:00", 1))
        issue["labels"] = [label for label in issue["labels"] if label != "openswarm:queued"]
        issue["labels"].append("openswarm:claimed")
        issue["comments"].append(claim("worker-b", "2026-01-01T00:00:01+00:00", 2))
        winner = xs.bus_winning_claim(issue["comments"], {"worker-a", "worker-b"}, 300,
                                      xs.to_epoch("2026-01-01T00:00:02+00:00"))
        self.assertEqual(winner["id"], 1)
        self.assertEqual(issue["labels"], ["openswarm:claimed"])

    def test_illegal_claim_and_result_contract_are_rejected(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    {"id": 2, "body": "/claim worker/a", "user": {"login": "worker-a"},
                     "created_at": "2026-01-01T00:00:01+00:00"}]
        self.assertIsNone(xs.bus_claim(comments[1]))
        self.assertEqual(xs.bus_winning_claim(comments, {"worker-a"}, 300,
                                               xs.to_epoch("2026-01-01T00:00:02+00:00"))["id"], 1)
        for invalid in (
                {**result(), "error_class": None},
                {**result(), "error_class": "not-a-contract-error"},
                {**result(), "status": "failed", "error_class": "none"},
                {key: value for key, value in result().items() if key != "error_class"}):
            self.assertIsNone(xs.bus_parse_result(result_comment(invalid), "offline-run-1"))

    def test_invalid_result_fields_are_not_terminal_results(self):
        missing_run_id = result()
        del missing_run_id["run_id"]
        missing_error_class = result("error")
        del missing_error_class["error_class"]
        mismatched_branch = result(branch="openswarm/other-run")
        for payload in (missing_run_id, missing_error_class, mismatched_branch):
            with self.subTest(payload=payload):
                self.assertIsNone(xs.bus_parse_result(result_comment(payload), "offline-run-1"))

    def test_winning_claimer_ignores_damaged_comment_and_accepts_later_valid_one(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    {"id": 2, "body": result_comment({**result(), "branch": "openswarm/other-run"}),
                     "user": {"login": "worker-a"}, "created_at": "2026-01-01T00:00:01+00:00"},
                    {"id": 3, "body": result_comment(result()), "user": {"login": "worker-a"},
                     "created_at": "2026-01-01T00:00:02+00:00"}]
        winner = xs.bus_winning_claim(comments, {"worker-a"}, 60,
                                      xs.to_epoch("2026-01-01T00:00:03+00:00"))
        accepted = [xs.bus_parse_result(comment["body"], "offline-run-1")
                    for comment in reversed(comments[comments.index(winner) + 1:])
                    if comment["user"]["login"] == winner["user"]["login"]]
        self.assertEqual(accepted[0]["run_id"], "offline-run-1")
        self.assertIsNone(accepted[1])

    def test_unexpired_claim_renews_for_same_worker(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    claim("worker-a", "2026-01-01T00:00:30+00:00", 2)]
        winner = xs.bus_winning_claim(comments, {"worker-a", "worker-b"}, 60,
                                      xs.to_epoch("2026-01-01T00:00:45+00:00"))
        self.assertEqual(winner["id"], 2)

    def test_expired_claim_allows_other_worker_to_take_over(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    claim("worker-b", "2026-01-01T00:02:00+00:00", 2)]
        winner = xs.bus_winning_claim(comments, {"worker-a", "worker-b"}, 60,
                                      xs.to_epoch("2026-01-01T00:02:01+00:00"))
        self.assertEqual(winner["id"], 2)

    def test_other_worker_cannot_take_unexpired_claim(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    claim("worker-b", "2026-01-01T00:00:30+00:00", 2)]
        winner = xs.bus_winning_claim(comments, {"worker-a", "worker-b"}, 60,
                                      xs.to_epoch("2026-01-01T00:00:45+00:00"))
        self.assertEqual(winner["id"], 1)

    def test_expired_lone_claim_is_available_for_recovery(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1)]
        winner = xs.bus_winning_claim(comments, {"worker-a", "worker-b"}, 60,
                                      xs.to_epoch("2026-01-01T00:01:00+00:00"))
        self.assertIsNone(winner)

    def test_released_result_of_an_earlier_round_does_not_override_a_later_takeover(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1),
                    {"id": 2, "body": "/unclaim worker-a", "user": {"login": "worker-a"},
                     "created_at": "2026-01-01T00:00:10+00:00"},
                    {"id": 3, "body": result_comment({**result("failed"), "error_class": "task_changed",
                                                            "branch": None}),
                     "user": {"login": "worker-a"}, "created_at": "2026-01-01T00:00:20+00:00"},
                    claim("worker-b", "2026-01-01T00:01:00+00:00", 4)]
        winning, parsed = xs.bus_winning_result(comments, {"worker-a", "worker-b"}, 60,
                                                xs.to_epoch("2026-01-01T00:05:00+00:00"))
        self.assertIsNone(winning)
        self.assertIsNone(parsed)
        # Without a later takeover the released round stays terminal.
        winning, parsed = xs.bus_winning_result(comments[:3], {"worker-a", "worker-b"}, 60,
                                                xs.to_epoch("2026-01-01T00:05:00+00:00"))
        self.assertEqual(winning["id"], 1)
        self.assertEqual(parsed["error_class"], "task_changed")

    def test_restart_reuses_unexpired_claim_without_creating_another(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1)]
        winner = xs.bus_winning_claim(comments, {"worker-a", "worker-b"}, 60,
                                      xs.to_epoch("2026-01-01T00:00:30+00:00"))
        self.assertEqual(winner["id"], comments[0]["id"])
        self.assertEqual(len(comments), 1)


class GitHubIssueBusWorkerRaceTests(unittest.TestCase):
    """Worker-level regression: a card changed after the claim stops the worker before checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-bus-race-")
        self.root = Path(self.tmp.name)
        self.data = self.root / "planner"
        self.data.mkdir()
        self.env_names = ("OPENSWARM_HOME", "OPENSWARM_GH_BIN", "OPENSWARM_FAKE_GH_STATE",
                          "OPENSWARM_FAKE_GH_LOGIN", "OPENSWARM_FAKE_GH_ISSUE_LOGIN",
                          "OPENSWARM_FAKE_GH_NOW", "OPENSWARM_BUS_TARGET_URL", "OPENSWARM_STATE_FILE",
                          "OPENSWARM_PI_BIN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                          "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1")
        self.old_env = {key: os.environ.get(key) for key in self.env_names}
        self.addCleanup(self.restore_env)
        # Git's detached auto-maintenance can write into these temporary bare
        # repositories after a worker's clone or push exits.
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
        try:
            self.tmp.cleanup()
        except OSError:
            shutil.rmtree(self.tmp.name, ignore_errors=True)

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
        return json.loads(self.state_path.read_text())

    def issue(self, number):
        return next(row for row in self.state()["repos"]["org/bus"]["issues"] if row["number"] == number)

    def worker_args(self, name="executor", budget=1):
        return argparse.Namespace(bus="org/bus", name=name, backend="pi", model=None, budget_minutes=budget,
                                  once=True, repos="org/target", verify=None, card_type=None, paths=None,
                                  rework_of=None)

    def create_card(self, run_id):
        metadata = {"run_id": run_id, "repo": "org/target", "base": self.base,
                    "branch_hint": f"openswarm/{run_id}", "budget_minutes": 1,
                    "report_schema": xs.RESULT_SCHEMA}
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "planner"
        os.environ["OPENSWARM_FAKE_GH_ISSUE_LOGIN"] = "planner"
        issue = xs.bus_create_issue("org/bus", self.cfg, "race card", "write worker.txt", metadata)
        return issue["number"], metadata

    def rewrite_task(self, number, mutate):
        """Rewrite the card's task JSON the way a planner edit would."""
        state = self.state()
        row = next(item for item in state["repos"]["org/bus"]["issues"] if item["number"] == number)
        fence, task = xs.bus_extract_task_fence(row["body"])
        row["body"] = row["body"].replace(fence, json.dumps(mutate(dict(task)), ensure_ascii=False, indent=2))
        self.state_path.write_text(json.dumps(state))

    def write_fake_pi(self, name="fake-pi"):
        """Write an offline Pi stand-in that reports a done result."""
        path = self.root / name
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "with open('worker.txt', 'w') as stream:\n"
            "    stream.write('worker result\\n')\n"
            "report = {'status':'done','summary':'worker completed','files_changed':['worker.txt'],"
            "'tests':'fake executor','followups':[],'error_class':'none'}\n"
            "message = {'role':'assistant','content':[{'type':'text','text':json.dumps(report)}], "
            "'usage':{'input':2,'output':3}}\n"
            "print(json.dumps({'type':'message_end','message':message}), flush=True)\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def remote_branches(self):
        return self.git(None, "ls-remote", "--heads", str(self.bare))

    def assert_no_worker_checkout(self, run_id):
        self.assertFalse((Path(xs.DATA) / "bus-worker" / run_id / "repo").exists(),
                         "the worker must not check out a card that changed after the claim")
        self.assertNotIn(f"refs/heads/openswarm/{run_id}", self.remote_branches(),
                         "the worker must not push a branch for a card that changed after the claim")

    def test_worker_releases_the_claim_when_the_task_json_changed_after_the_claim(self):
        number, metadata = self.create_card("race-task-changed")
        card = self.issue(number)
        digest = card["comments"][0]["body"]
        # The planner edits the task JSON after the worker read and validated the card.
        self.rewrite_task(number, lambda task: {**task, "budget_minutes": 2})
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        self.assertTrue(xs.bus_worker_run(card, self.cfg, self.worker_args(), self.policy))
        bodies = [comment["body"] for comment in self.issue(number)["comments"]]
        self.assertIn("/unclaim executor", bodies)
        reports = [report for report in (xs.bus_parse_result(body, metadata["run_id"]) for body in bodies) if report]
        self.assertEqual(len(reports), 1, bodies)
        self.assertEqual(reports[0]["status"], "failed")
        self.assertEqual(reports[0]["error_class"], "task_changed")
        self.assertEqual(reports[0]["run_id"], metadata["run_id"])
        self.assertIsNone(reports[0]["branch"])
        self.assert_no_worker_checkout(metadata["run_id"])
        self.assertEqual(self.issue(number)["comments"][0]["body"], digest)

    def test_worker_releases_the_claim_when_the_base_changed_after_the_claim(self):
        number, metadata = self.create_card("race-base-changed")
        card = self.issue(number)
        self.rewrite_task(number, lambda task: {**task, "base": "b" * 40})
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        self.assertTrue(xs.bus_worker_run(card, self.cfg, self.worker_args(), self.policy))
        bodies = [comment["body"] for comment in self.issue(number)["comments"]]
        self.assertIn("/unclaim executor", bodies)
        reports = [report for report in (xs.bus_parse_result(body, metadata["run_id"]) for body in bodies) if report]
        self.assertEqual(len(reports), 1, bodies)
        self.assertEqual(reports[0]["error_class"], "base_changed")
        self.assert_no_worker_checkout(metadata["run_id"])

    def test_planner_reads_the_released_claim_failure_as_a_terminal_result(self):
        number, metadata = self.create_card("race-planner-reads")
        card = self.issue(number)
        self.rewrite_task(number, lambda task: {**task, "budget_minutes": 2})
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        self.assertTrue(xs.bus_worker_run(card, self.cfg, self.worker_args(), self.policy))
        comments = self.issue(number)["comments"]
        winning, parsed = xs.bus_winning_result(comments, {"executor"}, 300)
        self.assertIsNotNone(winning)
        self.assertEqual(parsed["error_class"], "task_changed")
        self.assertEqual(parsed["status"], "failed")
        self.assertEqual(parsed["run_id"], metadata["run_id"])

    def test_unchanged_card_keeps_its_digest_and_completes_the_claim_result_loop(self):
        (self.data / "policy.local.json").write_text(json.dumps({"pi": {"require_isolation": False}}))
        os.environ["OPENSWARM_PI_BIN"] = str(self.write_fake_pi())
        number, metadata = self.create_card("race-unchanged")
        card = self.issue(number)
        digest = card["comments"][0]["body"]
        os.environ["OPENSWARM_FAKE_GH_LOGIN"] = "executor"
        self.assertTrue(xs.bus_worker_run(card, self.cfg, self.worker_args(), self.policy))
        bodies = [comment["body"] for comment in self.issue(number)["comments"]]
        self.assertNotIn("/unclaim executor", bodies)
        self.assertEqual(self.issue(number)["comments"][0]["body"], digest)
        reports = [report for report in (xs.bus_parse_result(body, metadata["run_id"]) for body in bodies) if report]
        self.assertEqual(len(reports), 1, bodies)
        self.assertEqual(reports[0]["status"], "done")
        self.assertEqual(reports[0]["run_id"], metadata["run_id"])
        self.assertEqual(reports[0]["error_class"], "none")
        self.assertEqual(reports[0]["branch"], f"openswarm/{metadata['run_id']}")
        self.assertIn(f"refs/heads/openswarm/{metadata['run_id']}", self.remote_branches())
        self.assertEqual(xs.bus_winning_claim(self.issue(number)["comments"], {"executor"}, 300)["id"],
                         next(comment["id"] for comment in self.issue(number)["comments"]
                              if comment["body"] == f"/claim {self.worker_args().name}"))

if __name__ == "__main__":
    unittest.main()
