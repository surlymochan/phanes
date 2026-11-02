"""Command-level coverage for the merge-gate rerun and post-merge actions (Refs #1496).

`openswarm merge-gate` can rerun the failed CI run of a blocked pull request
once, and can run a post-merge hook once per new sha of `main`.  Both are
wired into `cmd_merge_gate`, so they are tested through the command: the gate
is the only place where the decisions of `lib/merge/rerun.py` and
`lib/merge/post_hook.py` become visible.
"""
import argparse
import contextlib
import io
import json
import pathlib
import shlex
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _load
xs = _load.load()

REPO = "acme/project"
PR = 7
HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40
MAIN_SHA = "c" * 40


def pr_payload(head=HEAD_SHA, conclusion="FAILURE"):
    """The `gh pr view --json` payload of a pull request with a red check."""
    return {"number": PR, "headRefOid": head, "headRefName": "topic",
            "labels": [], "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "build", "status": "COMPLETED",
                                    "conclusion": conclusion}]}


def run_row(run_id, name="build", conclusion="failure", head_sha=HEAD_SHA):
    """One `gh run list --json databaseId,conclusion,name,headSha` row."""
    return {"databaseId": run_id, "name": name, "conclusion": conclusion, "headSha": head_sha}


class FakeGh:
    """A fake gh CLI: pr view / run list / run rerun / pr merge / api."""

    def __init__(self, payload=None, runs=(), main_sha=MAIN_SHA, rerun_exit=0):
        self.payload = pr_payload() if payload is None else payload
        self.runs = list(runs)
        self.main_sha = main_sha
        self.rerun_exit = rerun_exit
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["pr", "view"]:
            return 0, json.dumps(self.payload), ""
        if argv[:2] == ["run", "list"]:
            return 0, json.dumps(self.runs), ""
        if argv[:2] == ["run", "rerun"]:
            if self.rerun_exit:
                return self.rerun_exit, "", "gh: could not rerun the run"
            return 0, "", ""
        if argv[:2] == ["pr", "merge"]:
            return 0, "merged", ""
        if argv[:1] == ["api"]:
            if not self.main_sha:
                return 1, "", "gh: main is unknown"
            return 0, json.dumps({"sha": self.main_sha}), ""
        return 1, "", f"unexpected gh call: {argv}"

    def rerun_calls(self):
        """Every `gh run rerun` call of the gate."""
        return [call for call in self.calls if call[:2] == ["run", "rerun"]]

    def run_list_calls(self):
        """Every `gh run list` call of the gate."""
        return [call for call in self.calls if call[:2] == ["run", "list"]]


class MergeGateActionTests(unittest.TestCase):
    """The gate reruns a failed run once and runs the post-merge hook per sha."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openswarm-merge-gate-actions-")
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name, "home")
        self.home.mkdir(parents=True)
        patcher = mock.patch.object(xs, "DATA", str(self.home))
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "reviews.json").write_text(json.dumps(
            {f"{REPO}#{PR}": {"sha": HEAD_SHA, "verdict": "approved"}}))
        self.counter = self.home / "hook-calls"
        self.counter.write_text("")
        self.code = self.home / "hook-code"
        self.code.write_text("0")

    # ------------------------------------------------------------------ utils

    def run_gate(self, gh, **flags):
        """Run the gate with the fake gh runner; returns (exit code, payload)."""
        args = argparse.Namespace(repo=REPO, pr=PR, dry_run=False, json=True,
                                  rerun_failed_once=False, ignore_checks_re=None,
                                  post_merge_hook=None)
        for key, value in flags.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = xs.cmd_merge_gate(args, None, runner=gh)
        return code, json.loads(buffer.getvalue())

    def hook_command(self):
        """The `--post-merge-hook` command: it records the sha, then fails on demand."""
        script = self.home / "hook.py"
        script.write_text(
            "import os, pathlib, sys\n"
            "counter = pathlib.Path(sys.argv[1])\n"
            "counter.write_text(counter.read_text()"
            " + os.environ.get('OPENSWARM_MAIN_SHA', '') + '\\n')\n"
            "sys.exit(int(pathlib.Path(sys.argv[2]).read_text().strip() or '0'))\n")
        return " ".join(shlex.quote(part) for part in
                        [sys.executable, str(script), str(self.counter), str(self.code)])

    def hook_calls(self):
        """The shas the hook was run with, one per line."""
        return [line for line in self.counter.read_text().splitlines() if line]

    # ------------------------------------------------------------------ rerun

    def test_a_failed_run_of_the_current_head_is_rerun_once(self):
        """The failed run of the head sha is rerun, and only in the first round."""
        gh = FakeGh(runs=[run_row(11)])
        code, payload = self.run_gate(gh, rerun_failed_once=True)
        self.assertEqual(code, 1)
        record = payload["results"][0]
        self.assertEqual(record["status"], "blocked:ci(rerun:11)")
        self.assertEqual(gh.rerun_calls(), [["run", "rerun", "11", "--failed"]])

        # The second round finds the run in the ledger and leaves it alone.
        code, payload = self.run_gate(gh, rerun_failed_once=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(gh.rerun_calls()), 1)
        self.assertEqual(payload["results"][0]["status"], "blocked:ci")
        self.assertIn("already rerun", payload["results"][0]["reruns"][0]["reason"])

    def test_the_run_list_asks_for_the_head_sha(self):
        """The run list carries the head sha, so the runs can be filtered."""
        gh = FakeGh(runs=[run_row(11)])
        self.run_gate(gh, rerun_failed_once=True)
        fields = gh.run_list_calls()[0][gh.run_list_calls()[0].index("--json") + 1]
        self.assertIn("headSha", fields)

    def test_a_run_of_an_older_commit_is_not_rerun(self):
        """A run of a commit that a later push replaced must not be rerun."""
        gh = FakeGh(runs=[run_row(11, head_sha=OLD_SHA)])
        code, payload = self.run_gate(gh, rerun_failed_once=True)
        self.assertEqual(code, 1)
        self.assertEqual(gh.rerun_calls(), [])
        record = payload["results"][0]
        self.assertEqual(record["status"], "blocked:ci")
        self.assertEqual(record["reruns"], [])
        self.assertEqual(record["reruns_skipped"][0]["run_id"], 11)

    def test_a_macos_run_is_not_rerun(self):
        """The default ignore pattern keeps the macOS job out of the rerun."""
        gh = FakeGh(runs=[run_row(12, name="build (macOS)")])
        code, payload = self.run_gate(gh, rerun_failed_once=True)
        self.assertEqual(code, 1)
        self.assertEqual(gh.rerun_calls(), [])
        record = payload["results"][0]
        self.assertEqual(record["status"], "blocked:ci")
        self.assertIn("ignore pattern", record["reruns"][0]["reason"])

    def test_the_ignore_pattern_overrides_the_default_one(self):
        """`--ignore-checks-re` replaces the macOS pattern of the library."""
        gh = FakeGh(runs=[run_row(12, name="build (macOS)"), run_row(13, name="flaky")])
        code, payload = self.run_gate(gh, rerun_failed_once=True, ignore_checks_re="flaky")
        self.assertEqual(code, 1)
        # The macOS run is rerun now (the default pattern no longer applies),
        # the run named "flaky" is the ignored one.
        self.assertEqual(gh.rerun_calls(), [["run", "rerun", "12", "--failed"]])
        reasons = {row["run_id"]: row["reason"] for row in payload["results"][0]["reruns"]}
        self.assertIn("ignore pattern", reasons[13])

    def test_a_broken_ignore_pattern_is_reported_as_an_error(self):
        """A broken pattern is a user error, not a crash of the whole gate."""
        gh = FakeGh(runs=[run_row(11)])
        with self.assertRaises(xs.MergeGateError):
            self.run_gate(gh, rerun_failed_once=True, ignore_checks_re="(")
        self.assertEqual(gh.rerun_calls(), [])

    def test_a_dry_run_reruns_nothing(self):
        """A dry run reports the rerun, but runs nothing and writes no ledger."""
        gh = FakeGh(runs=[run_row(11)])
        code, payload = self.run_gate(gh, rerun_failed_once=True, dry_run=True)
        self.assertEqual(code, 1)
        self.assertEqual(gh.rerun_calls(), [])
        self.assertEqual(payload["results"][0]["status"], "blocked:ci(rerun:11)")
        self.assertFalse((self.home / "merge-reruns.json").exists())

    # ------------------------------------------------------------- post hook

    def test_the_post_merge_hook_runs_after_a_merge(self):
        """A landed merge runs the hook once, with the sha of main."""
        gh = FakeGh(payload=pr_payload(conclusion="SUCCESS"))
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command())
        self.assertEqual(code, 0)
        self.assertEqual(payload["results"][0]["action"], "merged")
        self.assertEqual(payload["post_merge_hook"]["status"], "ok")
        self.assertEqual(payload["post_merge_hook"]["main_sha"], MAIN_SHA)
        self.assertEqual(self.hook_calls(), [MAIN_SHA])

    def test_the_post_merge_hook_does_not_run_again_for_the_same_sha(self):
        """The sha of the last successful run is remembered: it does not run twice."""
        gh = FakeGh(payload=pr_payload(conclusion="SUCCESS"))
        self.run_gate(gh, post_merge_hook=self.hook_command())
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command())
        self.assertEqual(code, 0)
        self.assertEqual(payload["post_merge_hook"]["status"], "skipped")
        self.assertEqual(self.hook_calls(), [MAIN_SHA])

    def test_the_post_merge_hook_runs_when_main_moved_without_a_merge(self):
        """`main` moved on its own (another merge): the hook runs anyway."""
        gh = FakeGh()
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command())
        self.assertEqual(code, 1)
        self.assertEqual(payload["results"][0]["action"], "none")
        self.assertEqual(payload["post_merge_hook"]["status"], "ok")
        self.assertEqual(self.hook_calls(), [MAIN_SHA])

    def test_a_failed_post_merge_hook_is_retried(self):
        """A failed hook does not remember the sha: the next round tries again."""
        self.code.write_text("1")
        gh = FakeGh(payload=pr_payload(conclusion="SUCCESS"))
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command())
        self.assertEqual(payload["post_merge_hook"]["status"], "failed")
        self.assertEqual(self.hook_calls(), [MAIN_SHA])

        # The sha was not remembered, so the next round runs the hook again.
        self.code.write_text("0")
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command())
        self.assertEqual(payload["post_merge_hook"]["status"], "ok")
        self.assertEqual(self.hook_calls(), [MAIN_SHA, MAIN_SHA])

    def test_a_dry_run_does_not_run_the_post_merge_hook(self):
        """A dry run reports the hook, but runs nothing."""
        gh = FakeGh(payload=pr_payload(conclusion="SUCCESS"))
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command(), dry_run=True)
        self.assertEqual(payload["post_merge_hook"]["status"], "planned")
        self.assertEqual(self.hook_calls(), [])
        self.assertFalse((self.home / "post-merge-hook.json").exists())

    def test_an_unreadable_main_is_reported_as_a_gh_error(self):
        """A failing `gh api` call is reported, the verdicts of the PRs stay."""
        gh = FakeGh(payload=pr_payload(conclusion="SUCCESS"), main_sha=None)
        code, payload = self.run_gate(gh, post_merge_hook=self.hook_command())
        self.assertEqual(code, 0)
        self.assertEqual(payload["post_merge_hook"]["status"], "gh_error")
        self.assertEqual(payload["results"][0]["action"], "merged")
        self.assertEqual(self.hook_calls(), [])


if __name__ == "__main__":
    unittest.main()
