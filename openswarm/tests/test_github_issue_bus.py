"""Lease state-machine and offline result parsing coverage for the GitHub issue bus."""
import json
import unittest
from unittest import mock

try:
    import _load
except ModuleNotFoundError:
    from openswarm.tests import _load

xs = _load.load()


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

    def test_restart_reuses_unexpired_claim_without_creating_another(self):
        comments = [claim("worker-a", "2026-01-01T00:00:00+00:00", 1)]
        winner = xs.bus_winning_claim(comments, {"worker-a", "worker-b"}, 60,
                                      xs.to_epoch("2026-01-01T00:00:30+00:00"))
        self.assertEqual(winner["id"], comments[0]["id"])
        self.assertEqual(len(comments), 1)


if __name__ == "__main__":
    unittest.main()
