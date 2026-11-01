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


class GitHubIssueBusLeaseTests(unittest.TestCase):
    def test_result_comment_sample_parses_to_dict_without_github(self):
        comment = """```openswarm-result
{"status":"ok","run_id":"offline-run-1","pr_url":null}
```"""
        payload = json.loads(comment.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertEqual(payload, {"status": "ok", "run_id": "offline-run-1", "pr_url": None})

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
