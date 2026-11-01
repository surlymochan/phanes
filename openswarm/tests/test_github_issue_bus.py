"""Lease state-machine coverage for the GitHub issue bus."""
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
