"""`openswarm/lib/merge/actions.py`: the merge-gate actions (Refs #1432).

The module owns three decisions of the box merge loop: the union merge of the
append-only documentation, the rerun of a failed CI job (once per job) and
the post-merge hook (once per new sha of the base branch).

The module is loaded by file path: ``openswarm/lib/merge`` is a namespace
package without an ``__init__.py`` (same trick as ``tests/test_merge_union.py``).

The union merge is tested against real git repositories in a temporary
directory (a fake git cannot say whether a merge really conflicted); the
rerun and the hook use a fake runner, so no ``gh`` and no project command is
ever executed.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "merge" / "actions.py"

spec = importlib.util.spec_from_file_location("merge_actions", MODULE)
actions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(actions)


class RecordingRunner:
    """A `subprocess.run` stand-in that records the commands it was given."""

    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return subprocess.run(command, **kwargs)

    def pushes(self):
        """Every `git push` of the runner."""
        return [command for command in self.commands if command[:2] == ["git", "push"]]

    def fetches(self):
        """Every `git fetch` of the runner."""
        return [command for command in self.commands if command[:2] == ["git", "fetch"]]


class FakeResult:
    """The little of `subprocess.CompletedProcess` the module looks at."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """A runner that never leaves the process; it records the commands.

    `responses` holds the answers of the runner: the key is the beginning of
    the command, the value the result of the command.
    """

    def __init__(self, returncode=0, stderr="", responses=None):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr
        self.responses = responses or {}

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        for prefix, result in self.responses.items():
            if list(command)[:len(prefix)] == list(prefix):
                return result
        return FakeResult(self.returncode, "", self.stderr)

    def commands(self):
        """Every command of the runner, as a list of lists."""
        return [list(call) for call in self.calls]

    def reruns(self):
        """Every `gh run rerun` of the runner."""
        return [command for command in self.commands() if command[:3] == ["gh", "run", "rerun"]]


class UnionCandidatesTests(unittest.TestCase):
    """The selection of the pull requests that may be union-merged."""

    def test_only_a_pull_request_with_union_conflicts_is_a_candidate(self):
        prs = [
            {"number": 12, "mergeable": "CONFLICTING", "headRefName": "b"},
            {"number": 4, "mergeable": "CONFLICTING", "headRefName": "a"},
            {"number": 2, "mergeable": "MERGEABLE", "headRefName": "c"},
            {"number": 1, "mergeable": "CONFLICTING", "headRefName": "d"},
        ]
        conflicts = {12: ["docs/guide.md"], 4: ["docs/x.md", "docs/y.md"],
                     2: ["docs/no.md"], 1: []}

        result = actions.union_candidates(prs, conflicts, ["docs/*.md"])

        self.assertEqual([pr["number"] for pr in result], [4, 12])

    def test_a_conflict_outside_the_union_files_is_not_a_candidate(self):
        prs = [{"number": 7, "mergeable": "CONFLICTING", "headRefName": "topic"}]
        conflicts = {7: ["docs/ctl-boot.md", "app.py"]}

        result = actions.union_candidates(prs, conflicts, ["docs/*.md"])

        self.assertEqual(result, [])

    def test_a_pull_request_without_conflicts_is_not_a_candidate(self):
        prs = [{"number": 7, "mergeable": "MERGEABLE", "headRefName": "topic"}]

        result = actions.union_candidates(prs, {}, ["docs/*.md"])

        self.assertEqual(result, [])

    def test_the_conflicts_may_be_given_as_text(self):
        prs = [{"number": 7, "mergeable": "CONFLICTING", "headRefName": "topic"}]

        result = actions.union_candidates(prs, {"7": "docs/ctl-boot.md\n"}, ["docs/*.md"])

        self.assertEqual([pr["number"] for pr in result], [7])


class UnionMergeTests(unittest.TestCase):
    """The union merge of the base branch into the branch of a pull request."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge-actions-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "repo"
        self.git("init", "-q", "--bare", str(self.remote), cwd=self.root)
        self.git("--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main",
                cwd=self.root)
        self.git("init", "-q", str(self.repo), cwd=self.root)
        head = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=str(self.repo),
                              capture_output=True, text=True).stdout.strip()
        if head != "main":
            self.git("branch", "-m", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "ctl-boot.md").write_text("start\nanchor\n", encoding="utf-8")
        (self.repo / "app.py").write_text("value = 'origin'\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-qb", "feature")
        self.git("push", "-q", "origin", "feature")

    # ------------------------------------------------------------------ utils

    def git(self, *args, cwd=None):
        """Run one git command and fail the test when it fails."""
        return subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo),
                              check=True, capture_output=True, text=True)

    def write(self, name, text):
        """Write a file of the repository and commit it."""
        (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "change " + name)

    def advance_main(self, name, text):
        """Move the base branch forward, on the remote as well."""
        self.git("checkout", "-q", "main")
        self.write(name, text)
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "feature")

    def advance_remote_main(self, name, text):
        """Move the base branch of the remote forward, from another clone.

        The local repository does not know the new base: it is behind, like
        the box that did not fetch for a while.
        """
        other = self.root / "other"
        if not other.exists():
            self.git("clone", "-q", str(self.remote), str(other), cwd=self.root)
        (other / name).write_text(text, encoding="utf-8")
        self.git("-C", str(other), "add", ".")
        self.git("-C", str(other), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "-qm", "advance main")
        self.git("-C", str(other), "push", "-q", "origin", "HEAD:main")

    def pr(self):
        """The `gh pr view` payload of the pull request of the fixture."""
        return {"number": 7, "headRefName": "feature", "baseRefName": "main"}

    def remote_sha(self, branch="feature"):
        """The sha of a branch of the remote."""
        result = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", branch],
                                capture_output=True, text=True)
        return result.stdout.strip()

    def remote_file(self, path, branch="feature"):
        """The content of a file of a branch of the remote."""
        result = subprocess.run(["git", "--git-dir", str(self.remote), "show",
                                 "{}:{}".format(branch, path)],
                                capture_output=True, text=True)
        return result.stdout

    def worktrees(self):
        """The worktrees of the repository."""
        output = self.git("worktree", "list", "--porcelain").stdout
        return [line.split(" ", 1)[1] for line in output.splitlines()
                if line.startswith("worktree ")]

    def union_merge(self, runner=None, **kwargs):
        """Run the union merge of the fixture."""
        self.runner = runner or RecordingRunner()
        return actions.union_merge(str(self.repo), self.pr(), "feature",
                                   ["docs/*.md"], runner=self.runner, **kwargs)

    # ------------------------------------------------------------------ tests

    def test_a_union_conflict_is_merged_and_pushed_back(self):
        """Only the documentation conflicts: both sides survive and land on the remote."""
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.write("docs/ctl-boot.md", "from-feature\n")
        before = self.remote_sha()

        result = self.union_merge()

        self.assertEqual(result["status"], "merged")
        self.assertTrue(result["pushed"])
        merged = self.remote_file("docs/ctl-boot.md")
        self.assertIn("from-main", merged)
        self.assertIn("from-feature", merged)
        self.assertNotEqual(self.remote_sha(), before)
        self.assertEqual(len(self.runner.pushes()), 1)

    def test_the_worktree_of_the_merge_is_removed_again(self):
        """The merge happens in a disposable worktree; the repository stays clean."""
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.write("docs/ctl-boot.md", "from-feature\n")

        self.union_merge()

        self.assertEqual(self.worktrees(), [str(self.repo)])
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_a_conflict_outside_the_union_files_aborts_the_merge(self):
        """A conflict in a code file: the merge is aborted and nothing is pushed."""
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.advance_main("app.py", "value = 'main'\n")
        self.write("docs/ctl-boot.md", "from-feature\n")
        self.write("app.py", "value = 'feature'\n")
        before = self.remote_sha()

        result = self.union_merge()

        self.assertEqual(result["status"], "mixed")
        self.assertEqual(result["files"], ["app.py"])
        self.assertFalse(result["pushed"])
        self.assertEqual(self.remote_sha(), before)
        self.assertEqual(self.runner.pushes(), [])
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_a_base_branch_that_is_already_merged_changes_nothing(self):
        """The head does not move, so nothing is pushed back."""
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.git("merge", "--no-edit", "-m", "merge main", "main")
        before = self.remote_sha()

        result = self.union_merge()

        self.assertEqual(result["status"], "noop")
        self.assertFalse(result["pushed"])
        self.assertEqual(self.runner.pushes(), [])
        self.assertEqual(self.remote_sha(), before)

    def test_an_unknown_base_branch_is_reported_as_an_error(self):
        """A base branch the repository does not know: an error, not a crash."""
        pr = {"number": 7, "headRefName": "feature", "baseRefName": "missing-base"}
        self.runner = RecordingRunner()

        result = actions.union_merge(str(self.repo), pr, "feature",
                                     ["docs/*.md"], runner=self.runner)

        self.assertEqual(result["status"], "error")
        self.assertIn("missing-base", result["error"])
        self.assertEqual(self.runner.pushes(), [])
        self.assertEqual(self.worktrees(), [str(self.repo)])

    # ------------------------------------------------------------ the base sha

    def test_the_base_branch_is_fetched_before_the_merge(self):
        """The base of the remote is fetched: the merge does not use the local one."""
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.write("docs/ctl-boot.md", "from-feature\n")

        result = self.union_merge()

        self.assertEqual(len(self.runner.fetches()), 1)
        self.assertEqual(result["base_source"], "remote")

    def test_a_stale_local_base_branch_is_not_used(self):
        """The local base is behind the remote: the sha of the remote is merged."""
        self.advance_remote_main("docs/ctl-boot.md", "from-main\n")
        self.write("docs/ctl-boot.md", "from-feature\n")
        before = self.remote_sha()

        result = self.union_merge()

        self.assertEqual(result["status"], "merged")
        self.assertTrue(result["pushed"])
        merged = self.remote_file("docs/ctl-boot.md")
        self.assertIn("from-main", merged)
        self.assertIn("from-feature", merged)
        self.assertNotEqual(self.remote_sha(), before)

    def test_the_stale_base_of_the_pull_request_is_not_used(self):
        """The base sha of the pull request is behind: the fetched base wins."""
        stale = self.git("rev-parse", "main").stdout.strip()
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.write("docs/ctl-boot.md", "from-feature\n")
        pr = dict(self.pr(), baseRefOid=stale)
        self.runner = RecordingRunner()

        result = actions.union_merge(str(self.repo), pr, "feature",
                                     ["docs/*.md"], runner=self.runner)

        self.assertEqual(result["status"], "merged")
        self.assertNotEqual(result["base"], stale)
        self.assertEqual(result["base_source"], "remote")

    def test_the_base_sha_of_the_caller_is_merged(self):
        """The caller knows the sha of the base branch: that one is merged."""
        self.advance_main("docs/ctl-boot.md", "from-main\n")
        self.write("docs/ctl-boot.md", "from-feature\n")
        base = self.git("rev-parse", "main").stdout.strip()

        result = self.union_merge(base_sha=base)

        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["base"], base)

    def test_a_base_that_is_newer_than_the_remote_wins(self):
        """The caller knows a base that is newer than the remote: it is merged."""
        self.git("checkout", "-q", "main")
        self.write("docs/ctl-boot.md", "from-main\n")
        newer = self.git("rev-parse", "main").stdout.strip()
        self.git("checkout", "-q", "feature")
        self.write("docs/ctl-boot.md", "from-feature\n")

        result = self.union_merge(base_sha=newer)

        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["base"], newer)
        self.assertEqual(result["base_source"], "caller")


class RerunOnceTests(unittest.TestCase):
    """A failed CI job is rerun once, and only once."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge-actions-rerun-")
        self.addCleanup(self.temp.cleanup)
        self.seen = pathlib.Path(self.temp.name) / "reruns.json"

    def run_row(self, run_id, name="build", conclusion="failure", jobs=None):
        """One `gh run list --json databaseId,name,conclusion` row."""
        row = {"databaseId": run_id, "name": name, "conclusion": conclusion}
        if jobs is not None:
            row["jobs"] = jobs
        return row

    def job(self, job_id, name="build (ubuntu)", conclusion="failure"):
        """One job of a run."""
        return {"databaseId": job_id, "name": name, "status": "completed",
                "conclusion": conclusion}

    def runner(self, jobs=(), **kwargs):
        """A runner that answers `gh run view` with the given jobs."""
        payload = json.dumps({"jobs": list(jobs)})
        responses = {("gh", "run", "view"): FakeResult(0, payload)}
        return FakeRunner(responses=responses, **kwargs)

    def seen_runs(self):
        """The runs of the state file."""
        payload = json.loads(self.seen.read_text(encoding="utf-8"))
        return payload["runs"]

    def test_a_failed_job_is_rerun(self):
        """The failed job of the pull request is rerun once."""
        runner = self.runner([self.job(501)])

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["rerun"])
        self.assertEqual(runner.reruns(), [["gh", "run", "rerun", "11", "--job", "501"]])
        self.assertIn("11:501", self.seen_runs())

    def test_the_same_job_is_not_rerun_twice(self):
        """The second round finds the job in the state file and leaves it alone."""
        runner = self.runner([self.job(501)])
        actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["skip"])
        self.assertIn("already rerun", result[0]["reason"])
        self.assertEqual(len(runner.reruns()), 1)

    def test_a_macos_job_is_not_rerun(self):
        """The default ignore pattern keeps the macOS job out of the rerun."""
        runner = self.runner([self.job(501, name="build (macOS)")])

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["skip"])
        self.assertIn("ignore pattern", result[0]["reason"])
        self.assertEqual(runner.reruns(), [])
        self.assertFalse(self.seen.exists())

    def test_a_mixed_workflow_does_not_rerun_the_macos_job(self):
        """A workflow with a macOS job and a Linux job: only the Linux job is rerun."""
        runner = self.runner([self.job(501, name="build (macOS)"),
                              self.job(502, name="build (ubuntu)")])

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertEqual([(row["job_id"], row["action"]) for row in result],
                         [("501", "skip"), ("502", "rerun")])
        self.assertEqual(runner.reruns(), [["gh", "run", "rerun", "11", "--job", "502"]])
        self.assertNotIn("501", json.dumps(runner.commands()))
        self.assertIn("11:502", self.seen_runs())

    def test_the_jobs_are_read_with_gh_run_view(self):
        """The jobs of a run are read with `gh run view`."""
        runner = self.runner([self.job(501)])

        actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertIn(["gh", "run", "view", "11", "--json", "jobs"], runner.commands())

    def test_the_jobs_may_be_given_in_the_row(self):
        """The caller may pass the jobs of the run: `gh run view` is not needed."""
        runner = self.runner()

        actions.rerun_once(7, [self.run_row(11, jobs=[self.job(501)])],
                           str(self.seen), runner=runner)

        self.assertNotIn(["gh", "run", "view", "11", "--json", "jobs"], runner.commands())
        self.assertEqual(runner.reruns(), [["gh", "run", "rerun", "11", "--job", "501"]])

    def test_a_run_with_unknown_jobs_is_left_alone(self):
        """Without the jobs of a run the ignore pattern cannot be applied."""
        runner = self.runner()

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["skip"])
        self.assertIn("unknown", result[0]["reason"])
        self.assertEqual(runner.reruns(), [])
        self.assertFalse(self.seen.exists())

    def test_a_macos_workflow_is_not_rerun(self):
        """The name of the workflow is ignored as well."""
        runner = self.runner([self.job(501)])

        result = actions.rerun_once(7, [self.run_row(11, name="macOS build")],
                                    str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["skip"])
        self.assertIn("ignore pattern", result[0]["reason"])
        self.assertEqual(runner.reruns(), [])

    def test_the_ignore_pattern_can_be_replaced(self):
        """A pattern of the caller replaces the default one."""
        runner = self.runner([self.job(501, name="build (macOS)"),
                              self.job(502, name="flaky")])

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner,
                                    ignore_checks_re="flaky")

        self.assertEqual([(row["job_id"], row["action"]) for row in result],
                         [("501", "rerun"), ("502", "skip")])
        self.assertEqual(runner.reruns(), [["gh", "run", "rerun", "11", "--job", "501"]])

    def test_a_green_run_is_not_rerun(self):
        """Only a failed run is worth a second chance."""
        runner = self.runner()

        result = actions.rerun_once(7, [self.run_row(14, conclusion="success")],
                                    str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["skip"])
        self.assertEqual(runner.reruns(), [])

    def test_a_failed_rerun_command_is_tried_again(self):
        """A command that failed is not remembered: the next round tries again."""
        runner = self.runner([self.job(501)], returncode=1, stderr="gh: could not rerun")

        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)

        self.assertEqual([row["action"] for row in result], ["skip"])
        self.assertIn("rerun failed", result[0]["reason"])
        self.assertFalse(self.seen.exists())

        runner = self.runner([self.job(501)])
        result = actions.rerun_once(7, [self.run_row(11)], str(self.seen), runner=runner)
        self.assertEqual([row["action"] for row in result], ["rerun"])

    def test_the_state_file_is_written_atomically(self):
        """The state file holds the jobs, and no temporary file is left behind."""
        actions.rerun_once(7, [self.run_row(11)], str(self.seen),
                           runner=self.runner([self.job(501)]))

        self.assertEqual(sorted(os.listdir(self.temp.name)), ["reruns.json"])
        payload = json.loads(self.seen.read_text(encoding="utf-8"))
        self.assertEqual(payload["runs"]["11:501"]["pr"], 7)


class PostMergeHookTests(unittest.TestCase):
    """The hook runs once per new sha of the base branch."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge-actions-hook-")
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "last-sha"

    def test_the_hook_runs_when_the_sha_moved(self):
        """A new sha of the base branch runs the hook."""
        runner = FakeRunner()

        result = actions.post_merge_hook(["./deploy.sh"], str(self.path), "abc123",
                                          runner=runner)

        self.assertEqual(result, {"status": "ok", "sha": "abc123"})
        self.assertEqual(runner.commands(), [["./deploy.sh"]])
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), "abc123")

    def test_the_hook_does_not_run_twice_for_the_same_sha(self):
        """The sha of the last successful run is remembered."""
        runner = FakeRunner()
        actions.post_merge_hook(["./deploy.sh"], str(self.path), "abc123", runner=runner)

        result = actions.post_merge_hook(["./deploy.sh"], str(self.path), "abc123",
                                          runner=runner)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(len(runner.commands()), 1)

    def test_the_hook_runs_again_after_the_base_branch_moved(self):
        """Another sha of the base branch runs the hook again."""
        runner = FakeRunner()
        actions.post_merge_hook(["./deploy.sh"], str(self.path), "abc123", runner=runner)

        result = actions.post_merge_hook(["./deploy.sh"], str(self.path), "def456",
                                          runner=runner)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(runner.commands()), 2)
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), "def456")

    def test_a_failed_hook_is_not_remembered_and_is_retried(self):
        """A failed hook keeps the old version and is tried again in the next round."""
        runner = FakeRunner(returncode=1, stderr="boom")

        result = actions.post_merge_hook(["./deploy.sh"], str(self.path), "abc123",
                                          runner=runner)

        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["error"])
        self.assertFalse(self.path.exists())

        runner = FakeRunner()
        result = actions.post_merge_hook(["./deploy.sh"], str(self.path), "abc123",
                                          runner=runner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.path.read_text(encoding="utf-8").strip(), "abc123")

    def test_the_command_may_be_given_as_a_command_line(self):
        """A command line is split without a shell."""
        runner = FakeRunner()

        actions.post_merge_hook("make deploy", str(self.path), "abc123", runner=runner)

        self.assertEqual(runner.commands(), [["make", "deploy"]])

    def test_an_empty_sha_is_refused(self):
        """An empty sha is a bug of the caller, not a silent skip."""
        with self.assertRaises(ValueError):
            actions.post_merge_hook(["./deploy.sh"], str(self.path), "", runner=FakeRunner())


if __name__ == "__main__":
    unittest.main()
