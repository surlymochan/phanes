"""Lease state-machine and offline result parsing coverage for the GitHub issue bus."""
import json
import unittest

try:
    import _load
except ModuleNotFoundError:
    from openswarm.tests import _load

xs = _load.load()


def claim(worker, created_at, comment_id):
    return {"id": comment_id, "body": f"/claim {worker}",
            "user": {"login": worker}, "created_at": created_at}


def result(status="ok", run_id="offline-run-1", branch=None, error_class="protocol_error"):
    return {"status": status, "run_id": run_id, "summary": "worker result",
            "files_changed": [], "tests": "not run", "followups": [],
            "branch": branch if branch is not None else f"openswarm/{run_id}",
            "commit": None, "error_class": error_class}


def result_comment(payload):
    return "```openswarm-result\n" + json.dumps(payload) + "\n```"


class GitHubIssueBusLeaseTests(unittest.TestCase):
    def test_result_comment_sample_parses_to_dict_without_github(self):
        comment = """```openswarm-result
{"status":"ok","run_id":"offline-run-1","pr_url":null}
```"""
        payload = json.loads(comment.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertEqual(payload, {"status": "ok", "run_id": "offline-run-1", "pr_url": None})

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


if __name__ == "__main__":
    unittest.main()
