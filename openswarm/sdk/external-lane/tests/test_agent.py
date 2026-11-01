import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
from external_lane.agent import ExternalLane, git_auth_env  # noqa: E402
from external_lane.github import GitHub, GitHubError, rate_limit_wait  # noqa: E402
from fakes import FakeGitHub, issue  # noqa: E402


class RateLimitTest(unittest.TestCase):
    def test_wait_rules(self):
        self.assertIsNone(rate_limit_wait(200, {}))
        self.assertIsNone(rate_limit_wait(403, {"x-ratelimit-remaining": "12"}))  # permission error, not a limit
        self.assertEqual(rate_limit_wait(403, {"Retry-After": "7"}), 7.0)
        self.assertEqual(rate_limit_wait(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}, now=1000), 101)
        self.assertEqual(rate_limit_wait(429, {}, attempt=1), 60.0)

    def test_client_retries_then_succeeds_and_spaces_writes(self):
        replies = [(429, {"Retry-After": "3"}, {"message": "slow down"}), (201, {}, {"id": 1}), (201, {}, {"id": 2})]
        sent, slept = [], []
        gh = GitHub("o/r", token="t", transport=lambda m, p, d=None: (sent.append((m, p)), replies.pop(0))[1],
                    sleep=slept.append, min_write_interval=1.0)
        self.assertEqual(gh.comment(5, "hi"), {"id": 1})
        self.assertEqual(slept[0], 3.0)
        gh.comment(5, "again")
        self.assertTrue(any(0 < s <= 1.0 for s in slept[1:]), slept)  # >=1s between writes
        self.assertEqual(len(sent), 3)

    def test_client_raises_on_permission_error(self):
        gh = GitHub("o/r", token="t", transport=lambda m, p, d=None: (403, {}, {"message": "Resource not accessible"}),
                    sleep=lambda s: None)
        with self.assertRaises(GitHubError) as cm:
            gh.get_issue(1)
        self.assertEqual(cm.exception.status, 403)

    def test_token_not_in_argv(self):
        env = git_auth_env("sekret", base={})
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn("sekret", env["GIT_CONFIG_VALUE_0"])  # base64, and only in env


class ClaimTest(unittest.TestCase):
    def lane(self, gh, k=1):
        return ExternalLane("o/r", "dot", max_inflight=k, gh=gh, token="", log=lambda m: None)

    def test_claim_next_takes_best_priority_and_comments(self):
        gh = FakeGitHub([issue(5, ["lane-ready", "backend:dot", "p2"]), issue(9, ["lane-ready", "backend:dot", "p0"]),
                         issue(3, ["lane-ready", "backend:other", "p0"])])
        got = self.lane(gh).claim_next()
        self.assertEqual(got["number"], 9)
        self.assertEqual(gh.labels_of(9), {"lane-claimed", "backend:dot", "p0"})
        self.assertEqual(gh.comments_by[9], ["claimed by dot"])
        self.assertIn("lane-ready", gh.labels_of(5))

    def test_max_inflight_blocks_second_claim(self):
        gh = FakeGitHub([issue(1, ["lane-claimed", "backend:dot"]), issue(2, ["lane-ready", "backend:dot"])])
        self.assertIsNone(self.lane(gh, 1).claim_next())
        self.assertEqual(self.lane(gh, 2).claim_next()["number"], 2)

    def test_claim_rechecks_fresh_labels(self):
        gh = FakeGitHub([issue(2, ["lane-ready", "backend:dot", "in-progress"])])
        self.assertIsNone(self.lane(gh).claim(2))
        self.assertEqual(gh.calls, [])

    def test_fail_and_open_pr(self):
        gh = FakeGitHub([issue(4, ["lane-claimed", "backend:dot"]), issue(6, ["lane-claimed", "backend:dot"])])
        lane = self.lane(gh)
        lane.fail(4, "tests do not pass")
        self.assertEqual(gh.labels_of(4), {"lane-failed", "backend:dot"})
        self.assertIn("tests do not pass", gh.comments_by[4][-1])
        url = lane.open_pr(6, "fleet/dot-issue6-1", "feat: x", files=[{"filename": "a.py", "additions": 40}])
        self.assertTrue(url.endswith("/9000"))
        self.assertTrue(gh.created_prs[0]["body"].startswith("Closes #6"))
        self.assertEqual(gh.labels_of(6), {"lane-pr", "backend:dot"})

    def test_open_pr_refuses_thin_diff(self):
        gh = FakeGitHub([issue(6, ["lane-claimed", "backend:dot"])])
        self.assertIsNone(self.lane(gh).open_pr(6, "b", "t", files=[{"filename": "a.py", "additions": 3}]))
        self.assertEqual(gh.created_prs, [])
        self.assertIn("lane-failed", gh.labels_of(6))

    def test_rework_reuses_pr_branch(self):
        gh = FakeGitHub([issue(7, ["lane-ready", "backend:dot"], body="重基PR：#55 fleet/dot-issue7-1\n\nfix it")])
        branch, pr = self.lane(gh).branch_for(gh.get_issue(7))
        self.assertEqual((branch, pr), ("fleet/dot-issue7-1", 55))


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class RunOnceTest(unittest.TestCase):
    """claim -> work command -> verify -> push -> PR, against a local bare repository (no network)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.remote = os.path.join(t, "remote.git")
        seed = os.path.join(t, "seed")
        git("init", "-q", "--bare", "-b", "main", self.remote)
        git("init", "-q", "-b", "main", seed)
        git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
        git("push", "-q", self.remote, "main", cwd=seed)
        os.environ.setdefault("GIT_AUTHOR_NAME", "t"); os.environ.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        os.environ.setdefault("GIT_COMMITTER_NAME", "t"); os.environ.setdefault("GIT_COMMITTER_EMAIL", "t@t")

    def tearDown(self):
        self.tmp.cleanup()

    def lane(self, gh):
        lane = ExternalLane("o/r", "dot", gh=gh, token="", log=lambda m: None)
        lane.remote_url = self.remote
        return lane

    WORK = ("python3 -c \"import os; open('feature.py','w').write(''.join(f'x{i} = {i}\\n' for i in range(40)))\" && "
            "git add feature.py && git commit -q -m \"feat: #$ISSUE_NUMBER add feature\"")

    def test_success_pushes_branch_and_opens_pr(self):
        gh = FakeGitHub([issue(11, ["lane-ready", "backend:dot"], body="do it\n\n验收：`python3 -c 'import feature'`")])
        url = self.lane(gh).run_once(os.path.join(self.tmp.name, "work"), self.WORK)
        self.assertIsNotNone(url, gh.comments_by)
        pr = gh.created_prs[0]
        self.assertRegex(pr["head"], r"^fleet/dot-issue11-\d{8}-\d{6}$")
        self.assertIn(pr["head"], git("branch", "--list", "fleet/*", cwd=self.remote))
        self.assertEqual(pr["title"], "feat: #11 add feature")
        self.assertEqual(gh.labels_of(11), {"lane-pr", "backend:dot"})

    def test_failing_acceptance_labels_failed_and_pushes_nothing(self):
        gh = FakeGitHub([issue(12, ["lane-ready", "backend:dot"], body="验收：`python3 -c 'raise SystemExit(3)'`")])
        self.assertIsNone(self.lane(gh).run_once(os.path.join(self.tmp.name, "work"), self.WORK))
        self.assertEqual(gh.labels_of(12), {"lane-failed", "backend:dot"})
        self.assertIn("acceptance", gh.comments_by[12][-1])
        self.assertEqual(git("branch", "--list", "fleet/*", cwd=self.remote).strip(), "")

    def test_no_commit_is_a_failure(self):
        gh = FakeGitHub([issue(13, ["lane-ready", "backend:dot"])])
        self.assertIsNone(self.lane(gh).run_once(os.path.join(self.tmp.name, "work"), "true"))
        self.assertIn("no commit", gh.comments_by[13][-1])


if __name__ == "__main__":
    unittest.main()
