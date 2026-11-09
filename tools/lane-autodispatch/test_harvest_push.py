#!/usr/bin/env python3
"""The harvest push of the AD (Refs #1640): a push that failed without any error
output (empty stderr) but whose branch is already on origin does not stop the
harvest: the update landed, the PR is opened."""
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BRANCH = "fleet/lane-issue1-run"


def fake_git(push_rc=0, push_err="", remote_head=HEAD, pushed_head=HEAD):
    """Return a `sh` replacement answering the push, the ls-remote and the rev-parse."""
    def sh(cmd, timeout=120, inp=None):
        argv = cmd if isinstance(cmd, list) else [cmd]
        if "push" in argv:
            return push_rc, "", push_err
        if "ls-remote" in argv:
            return (0, f"{remote_head}\trefs/heads/{argv[-1]}\n", "") if remote_head else (0, "", "")
        if "rev-parse" in argv:
            return (0, f"{pushed_head}\n", "") if pushed_head else (1, "", "")
        return 0, "", ""
    return sh


def run_finish(sh_impl):
    """Run the harvest of a finished run against the fake git."""
    st = {"issue": 1, "run": "run", "base": "base"}
    info = {"branch": "branch", "commit": "commit", "repo": "repo"}
    with mock.patch.object(ad, "sh", sh_impl), \
         mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
         mock.patch.object(ad, "filter_harvest", return_value=(HEAD, [], False)), \
         mock.patch.object(ad, "pr_validation_mode", return_value="warn"), \
         mock.patch.object(ad, "log"), \
         mock.patch.object(ad, "gh", return_value=(0, "", "")) as gh:
        result = ad.finish("lane", {"host": "host"}, st, info, {})
    return result, gh


class HarvestPushTest(unittest.TestCase):
    def test_push_ok(self):
        with mock.patch.object(ad, "sh", fake_git(push_rc=0)), mock.patch.object(ad, "log"):
            self.assertEqual(ad.harvest_push("lane", 1, BRANCH, HEAD), (True, None))

    def test_empty_stderr_and_branch_on_origin_continues(self):
        with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="")), \
             mock.patch.object(ad, "log") as log:
            ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertIn("continuing to PR", log.call_args.args[0])

    def test_empty_stderr_without_branch_on_origin_fails(self):
        with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="", remote_head="")), \
             mock.patch.object(ad, "log"):
            ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
        self.assertFalse(ok)
        self.assertIn("not on origin", error)

    def test_empty_stderr_with_another_head_on_origin_fails(self):
        with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="", remote_head=OTHER_HEAD)), \
             mock.patch.object(ad, "log"):
            ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
        self.assertFalse(ok)
        self.assertIn("not at the pushed head", error)

    def test_stderr_with_a_reason_stays_a_failure(self):
        with mock.patch.object(ad, "sh", fake_git(push_rc=1, push_err="! [rejected] non-fast-forward")), \
             mock.patch.object(ad, "log"):
            ok, error = ad.harvest_push("lane", 1, BRANCH, HEAD)
        self.assertFalse(ok)
        self.assertIn("non-fast-forward", error)

    def test_finish_opens_the_pr_after_an_empty_stderr_push_failure(self):
        result, gh = run_finish(fake_git(push_rc=1, push_err=""))
        self.assertEqual(result["status"], "pr-created")
        self.assertTrue(any(call.args[0][:2] == ["pr", "create"] for call in gh.call_args_list))

    def test_finish_stops_at_the_push_when_the_branch_is_not_on_origin(self):
        result, gh = run_finish(fake_git(push_rc=1, push_err="", remote_head=""))
        self.assertEqual(result["status"], "push-failed")
        self.assertIn("push failed", result["error"])
        self.assertFalse(any(call.args[0][:2] == ["pr", "create"] for call in gh.call_args_list))


if __name__ == "__main__":
    unittest.main()
