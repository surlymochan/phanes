#!/usr/bin/env python3
"""The collapse guard of the fix/rebase harvest (Refs #1833).

A fix/rebase round that does not continue on the head the PR had, but only produces an
incremental patch, replaces the whole PR when the AD pushes the harvest back to the PR
branch (2026-11-03: #1807 8 files/1780 lines -> 1/11, #1804 6/1401 -> 3/39, #1715
8/868 -> 2/23, #1755 8/746 -> 1/19, #1653 6/200 -> 2/19, #1824 9/835 -> 1/17).  Before such
a push the AD therefore checks:

  1. the new head must contain the head the PR had before the round
     (`git merge-base --is-ancestor`), otherwise the harvest replaced the PR;
  2. the number of changed files and the number of changed lines relative to the base must
     not collapse: either one of them dropping by more than the threshold (50% by default)
     refuses the push (the two dimensions are independent: "or", not "and");
  3. a git command that fails refuses the push as well (fail closed);
  4. a card whose body cannot be read or parsed is not an empty card: the harvest is
     refused as well (the target PR of a fix/rebase card is unknown then).

A refused harvest pushes nothing, leaves the PR branch alone, does not consume a review
round, labels the issue lane-failed and writes the verdict (PR number, both heads, both
sizes, the reason) to the brain log.  A card that declares a full rewrite is exempt.

The tests drive the public harvest entry point (`ad.finish`) with a real git repository and
a fake `gh`, so they pin the behaviour and not the shape of the implementation.

Migration: the issue named `openswarm/tests/test_harvest_collapse_guard.py`; the vendored
openswarm tree moved to swrm/ (#1894) and the AD itself lives in `tools/lane-autodispatch/`,
so the suite sits next to the other AD suites (`test_harvest_filter.py`).
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("autodispatch", Path(__file__).with_name("autodispatch.py"))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)

ISSUE = 1833
PR = 1845
BRANCH = "fleet/M8-issue1833-20261103-192907-5e4c2d"
# The card of a fix/rebase round names the PR it works on: that is how the AD finds the head
# the PR had before the round.
CARD = f"# AD fix 收割护栏\n\n重基PR：#{PR} {BRANCH}\n"
# A card that declares a full rewrite may replace the history: the guard is waived.
REWRITE_CARD = CARD + "\n整体重写：是\n"
# An ordinary implementation card: no target PR, nothing to compare against.
PLAIN_CARD = "# 实现一个新功能\n\n按卡面实现并写测试。\n"
# An ordinary card that quotes another PR: quoting a PR is no fix/rebase card.
PLAIN_CARD_WITH_PR = "# 实现一个新功能\n\n参考 PR #999 的做法，按卡面实现并写测试。\n"
# The six collapsed PRs of 2026-11-03 as recorded in issue #1833 ("files/lines" relative to
# the base, before and after the harvest).
RECORDED_INCIDENTS = (
    {"pr": 1807, "files_before": 8, "lines_before": 1780, "files_after": 1, "lines_after": 11},
    {"pr": 1804, "files_before": 6, "lines_before": 1401, "files_after": 3, "lines_after": 39},
    {"pr": 1715, "files_before": 8, "lines_before": 868, "files_after": 2, "lines_after": 23},
    {"pr": 1755, "files_before": 8, "lines_before": 746, "files_after": 1, "lines_after": 19},
    {"pr": 1653, "files_before": 6, "lines_before": 200, "files_after": 2, "lines_after": 19},
    {"pr": 1824, "files_before": 9, "lines_before": 835, "files_after": 1, "lines_after": 17},
)
MANIFEST_NAME = "restore-collapsed-heads-20261103.json"
# The repositories that may hold the real commits of the collapsed PRs: the AD harvests
# from them, so the box has them.  The real before/after heads only prove something when
# the objects are there.
AD_LOCAL = os.environ.get("AD_LOCAL", ad.LOCAL)
REAL_REPOSITORIES = (AD_LOCAL, os.path.expanduser("~/work/phanes-repo"),
                      os.path.expanduser("~/phanes-repo"))


def manifest_paths():
    """Every place the restore manifest may live (the dispatch box keeps it out of the repo)."""
    candidates = [os.environ.get("AD_HARVEST_COLLAPSE_MANIFEST", ""),
                  str(Path.home() / "gocc" / "autodispatch" / "bak" / MANIFEST_NAME),
                  str(Path.home() / "phanes-ctl" / "autodispatch" / "bak" / MANIFEST_NAME)]
    explicit = os.environ.get("AD_HARVEST_COLLAPSE_MANIFEST")
    if explicit:
        return [explicit]  # an explicitly requested but missing manifest must fail
    return [path for path in candidates if path and os.path.isfile(path)]


def manifest_entries(path):
    """Read the restore manifest: the real before/after heads of the collapsed PRs."""
    with open(path, encoding="utf-8") as stream:
        payload = json.load(stream)
    items = payload
    if isinstance(payload, dict):
        for key in ("entries", "items", "incidents", "restores", "prs", "collapsed"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = [dict(value, pr=key) for key, value in payload.items() if isinstance(value, dict)]
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pr = item.get("pr") or item.get("pr_number") or item.get("number") or item.get("issue")
        before = item.get("before") or item.get("old_head") or item.get("before_sha")
        after = item.get("after") or item.get("new_head") or item.get("after_sha")
        base = item.get("base") or item.get("base_sha") or item.get("base_commit")
        if isinstance(before, dict):
            before = before.get("sha") or before.get("head")
        if isinstance(after, dict):
            after = after.get("sha") or after.get("head")
        if isinstance(base, dict):
            base = base.get("sha")
        if before and after:
            entries.append({"pr": str(pr), "base": base, "before": before, "after": after})
    return entries


class HarvestFixture:
    """A git repository with a PR branch and the harvest of a lane result on top of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.brain_log = self.root / "brain-log.md"
        self.repo_index = 0
        for patch in (mock.patch.object(ad, "D", str(self.state)),
                      mock.patch.object(ad, "BRAIN_LOG", str(self.brain_log))):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    # --- the git fixture -----------------------------------------------------------

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-C", str(cwd or self.repo), *args], text=True).strip()

    def build_repo(self):
        """A work repository with a bare origin; returns the base commit."""
        self.repo_index += 1
        self.remote = self.root / f"remote{self.repo_index}.git"
        self.repo = self.root / f"work{self.repo_index}"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("remote", "add", "origin", str(self.remote))
        self.write("base", "base\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", f"{self.base}:refs/heads/main")
        self.git("fetch", "-q", "origin")
        return self.base

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def lines(self, count, prefix="line"):
        return "".join(f"{prefix} {index}\n" for index in range(count))

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def scenario_files_collapse(self):
        """The head before the round changed 4 files/40 lines, the harvest only 1 file/40 lines."""
        for name in "abcd":
            self.write(name, self.lines(10, name))
        old = self.commit("old")
        for name in "bcd":
            (self.repo / name).unlink()
        self.write("a", self.lines(40, "a"))
        new = self.commit("new")
        return old, new

    def scenario_lines_collapse(self):
        """The head before the round changed 1 file/100 lines, the harvest only 1 file/4 lines."""
        self.write("big", self.lines(100, "big"))
        old = self.commit("old")
        (self.repo / "big").unlink()
        self.write("small", self.lines(4, "small"))
        new = self.commit("new")
        return old, new

    def scenario_replaced(self):
        """The harvest head does not contain the head the PR had: the PR was replaced."""
        self.write("old", self.lines(10, "old"))
        old = self.commit("old")
        self.git("checkout", "-q", "-B", "harvest", self.base)
        self.write("other", self.lines(10, "other"))
        new = self.commit("new")
        return old, new

    def scenario_grown(self):
        """The harvest continues on the head of the PR and keeps the change size."""
        self.write("old", self.lines(10, "old"))
        old = self.commit("old")
        self.write("new", self.lines(10, "new"))
        new = self.commit("new")
        return old, new

    # --- the harvest ----------------------------------------------------------------

    def harvest(self, old, new, *, card=CARD, body_error=None, pr_payload=None, pr_error=None,
                harvest_ref=None, lane_ref=True, real_filter=False, ancestry_error=None):
        """Run the harvest of a lane result and return (result, gh calls, sh calls)."""
        self.old_head = old
        self.new_head = new
        # the PR branch still points at the head the PR had before the round
        self.git("push", "-q", "origin", f"{old}:refs/heads/{BRANCH}")
        self.git("fetch", "-q", "origin")
        self.git("update-ref", f"refs/heads/{BRANCH}", old)
        if lane_ref:
            self.git("update-ref", "refs/fleet/lane", new)
        gh_calls, sh_calls = [], []

        def fake_gh(args, timeout=60):
            gh_calls.append(list(args))
            if args[:2] == ["issue", "view"]:
                if body_error is not None:
                    return body_error
                return (0, json.dumps({"body": card, "labels": []}), "")
            if args[:2] == ["pr", "view"]:
                # Match the real CLI: this field is not available via pr view.
                if "baseRefOid" in args[args.index("--json") + 1].split(","):
                    return (1, "", 'Unknown JSON field: "baseRefOid"')
                return (0, json.dumps({"headRefOid": old, "baseRefName": "main"}), "")
            if args[:2] == ["pr", "list"]:
                return (0, json.dumps([{"number": PR, "headRefName": BRANCH}]), "")
            return (0, "", "")

        real_sh = ad.sh

        def spy_sh(cmd, timeout=120, inp=None):
            """Run the command for real (the git reads of the guard need it) and record it."""
            sh_calls.append(cmd)
            if isinstance(cmd, list) and "--is-ancestor" in cmd and ancestry_error is not None:
                if isinstance(ancestry_error, Exception):
                    raise ancestry_error
                return ancestry_error
            if isinstance(cmd, list) and cmd[:2] == ["gh", "api"]:
                # gh api has no -R option; the endpoint must identify the repo.
                target = card.split("重基PR：#", 1)[1].split()[0]
                self.assertEqual(cmd, ["gh", "api", f"repos/{ad.REPO}/pulls/{target}"])
                if pr_error is not None:
                    return pr_error
                payload = {"head": {"sha": old}, "base": {"sha": self.base}}
                return (0, json.dumps(payload if pr_payload is None else pr_payload), "")
            return real_sh(cmd, timeout=timeout, inp=inp)

        state = {}
        st = {"issue": ISSUE, "run": "run", "base": self.base}
        info = {"branch": "branch", "commit": "commit", "repo": "repo"}
        with mock.patch.object(ad, "LOCAL", str(self.repo)), \
             mock.patch.object(ad, "harvest_fetch", return_value=(True, "", 0)), \
             mock.patch.object(ad, "filter_harvest", return_value=(harvest_ref or new, [], False),
                               side_effect=ad.filter_harvest if real_filter else None), \
             mock.patch.object(ad, "pr_validation_mode", return_value="warn"), \
             mock.patch.object(ad, "gh", side_effect=fake_gh), \
             mock.patch.object(ad, "sh", side_effect=spy_sh):
            result = ad.finish("lane", {"host": "host"}, st, info, state)
        return result, gh_calls, sh_calls

    # --- the assertions ------------------------------------------------------------

    @staticmethod
    def pushes(sh_calls):
        pushed = []
        for cmd in sh_calls:
            text = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
            if "push" in text:
                pushed.append(text)
        return pushed

    @staticmethod
    def pr_writes(gh_calls):
        return [args for args in gh_calls
                if args[:2] in (["pr", "create"], ["pr", "edit"], ["pr", "ready"], ["pr", "close"])]

    @staticmethod
    def lane_failed(gh_calls):
        return [args for args in gh_calls
                if args[:2] == ["issue", "edit"] and "lane-failed" in args]

    def brain(self):
        return self.brain_log.read_text(encoding="utf-8") if self.brain_log.exists() else ""

    def remote_head(self, branch=BRANCH):
        """The head the branch has on the remote (the PR branch the harvest pushes back to)."""
        line = self.git("ls-remote", "origin", f"refs/heads/{branch}")
        return line.split()[0] if line else ""

    def import_real_objects(self, entry):
        """Bring the real commits of the AD's repository into the fixture repository.

        The real before/after heads of the collapsed PRs only prove something when the
        objects are there: the replay then runs the ancestry and the size check on the
        real history. Missing objects must fail the replay, not count as a rejection."""
        for source in REAL_REPOSITORIES:
            if not os.path.exists(os.path.join(source, ".git")): continue
            fetch = subprocess.run(["git", "-C", str(self.repo), "fetch", "-q", source,
                                    entry["base"], entry["before"], entry["after"]],
                                   capture_output=True, text=True)
            if fetch.returncode: continue
            if all(self.has_object(entry[key]) for key in ("base", "before", "after")): return True
        return False

    def has_object(self, sha):
        probe = subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                               capture_output=True, text=True)
        return probe.returncode == 0

    def assert_refused(self, result, gh_calls, sh_calls, *, heads=True, sizes=()):
        """The harvest was refused: nothing was pushed, the PR is untouched, the issue is back."""
        self.assertEqual(self.pushes(sh_calls), [], "the refused harvest pushed something")
        self.assertEqual(self.pr_writes(gh_calls), [], "the refused harvest touched the PR")
        self.assertEqual(self.remote_head(), self.old_head, "the refused harvest moved the PR branch")
        self.assertTrue(self.lane_failed(gh_calls), "the refused harvest did not label lane-failed")
        self.assertNotEqual(result.get("status"), "pr-created")
        self.assertNotEqual(result.get("status"), "pr-updated")
        brain = self.brain()
        self.assertTrue(brain.strip(), "the refused harvest wrote no brain log")
        if heads:
            self.assertIn(str(PR), brain)
            self.assertIn(self.old_head[:8], brain)
            self.assertIn(self.new_head[:8], brain)
        for size in sizes:
            self.assertIn(str(size), brain)

    def assert_allowed(self, result, gh_calls, sh_calls):
        """The harvest passed the guard: it was pushed and the issue stays untouched."""
        self.assertTrue(self.pushes(sh_calls), "the accepted harvest pushed nothing")
        self.assertEqual(self.lane_failed(gh_calls), [], "the accepted harvest labelled lane-failed")


class HarvestCollapseGuardTest(HarvestFixture, unittest.TestCase):
    """The guard refuses a harvest that would collapse the PR it pushes back to."""

    def test_file_count_collapse_is_refused(self):
        # only the file count collapses (4 -> 1 files, the lines stay at 40): the two
        # dimensions are independent, the file count alone refuses the push
        self.build_repo()
        old, new = self.scenario_files_collapse()
        result, gh_calls, sh_calls = self.harvest(old, new)
        self.assert_refused(result, gh_calls, sh_calls, sizes=(4, 40, 1))

    def test_line_count_collapse_is_refused(self):
        # only the line count collapses (1 -> 1 file, 100 -> 4 lines)
        self.build_repo()
        old, new = self.scenario_lines_collapse()
        result, gh_calls, sh_calls = self.harvest(old, new)
        self.assert_refused(result, gh_calls, sh_calls, sizes=(100, 4))

    def test_complete_collapse_uses_the_rejection_flow(self):
        # A real filter reports an empty tree diff when a fix undoes the entire
        # PR. It still needs the target PR's metadata and the guard's audit trail.
        self.build_repo()
        self.write("old", self.lines(10))
        old = self.commit("old")
        (self.repo / "old").unlink()
        new = self.commit("undo the entire PR")
        with mock.patch.object(ad, "mark_done", wraps=ad.mark_done) as mark_done:
            result, gh_calls, sh_calls = self.harvest(old, new, real_filter=True)
        self.assert_refused(result, gh_calls, sh_calls)
        self.assertEqual(result["status"], "harvest-collapse-rejected")
        self.assertTrue(result["done"])
        self.assertIn(["gh", "api", f"repos/{ad.REPO}/pulls/{PR}"], sh_calls)
        ledger = (self.state / "runs" / "run.log").read_text(encoding="utf-8")
        for evidence in (f"PR #{PR}", old[:12], new[:12], "(1, 10)->(0, 0)", "collapsed"):
            self.assertIn(evidence, result["error"])
            self.assertIn(evidence, self.brain())
            self.assertIn(evidence, ledger)
        mark_done.assert_called_once()
        self.assertEqual(mark_done.call_args.args[0], {"_done": [ISSUE]})

    def test_empty_harvest_still_reads_the_card_and_records_refusal(self):
        cases = ((CARD, (1, "", "body unavailable"), "harvest-collapse-rejected", "cannot be read"),
                 (CARD, (0, "not json", ""), "harvest-collapse-rejected", "cannot be parsed"),
                 (PLAIN_CARD, None, "lane-failed", "no effective change"),
                 (REWRITE_CARD, None, "lane-failed", "no effective change"))
        for card, body_error, status, reason in cases:
            with self.subTest(card=card, body_error=body_error):
                self.build_repo()
                self.write("old", self.lines(10))
                old = self.commit("old")
                (self.repo / "old").unlink()
                new = self.commit("undo the entire PR")
                result, gh_calls, sh_calls = self.harvest(
                    old, new, card=card, body_error=body_error, real_filter=True)
                self.assert_refused(result, gh_calls, sh_calls, heads=False)
                self.assertEqual(result["status"], status)
                self.assertTrue(result["done"])
                self.assertIn(reason, self.brain())
                ledger = (self.state / "runs" / "run.log").read_text(encoding="utf-8")
                self.assertIn(reason, ledger)

    def test_head_that_does_not_contain_the_old_head_is_refused(self):
        self.build_repo()
        old, new = self.scenario_replaced()
        result, gh_calls, sh_calls = self.harvest(old, new)
        self.assert_refused(result, gh_calls, sh_calls)

    def test_ancestor_with_a_normal_size_is_allowed(self):
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new)
        self.assert_allowed(result, gh_calls, sh_calls)

    def test_filtered_head_must_contain_the_old_head(self):
        self.build_repo()
        self.write(".lane-review/PR-1845.md", "review artifact\n")
        self.write("old", self.lines(10))
        old = self.commit("old with review artifact")
        self.write("new", self.lines(10))
        new = self.commit("fix")
        result, gh_calls, sh_calls = self.harvest(old, new, real_filter=True)
        self.assert_refused(result, gh_calls, sh_calls, heads=False)
        self.assertIn("does not contain", result["error"])
        check = next(cmd for cmd in sh_calls if "--is-ancestor" in cmd)
        self.assertNotEqual(check[-1], new)
        self.assertNotEqual(check[-1], "refs/fleet/lane")
        self.assertIn(check[-1][:12], self.brain())

    def test_rewrite_marker_waives_the_guard(self):
        # a card that declares a full rewrite may replace the history and shrink the diff
        self.build_repo()
        old, new = self.scenario_files_collapse()
        result, gh_calls, sh_calls = self.harvest(old, new, card=REWRITE_CARD)
        self.assert_allowed(result, gh_calls, sh_calls)

    def test_failing_git_check_fails_closed(self):
        # the head of the PR cannot be resolved locally: the guard may not let the push pass
        self.build_repo()
        old, new = self.scenario_grown()
        missing = "0" * 40
        result, gh_calls, sh_calls = self.harvest(
            old, new, pr_payload={"head": {"sha": missing}, "base": {"sha": self.base}})
        self.assert_refused(result, gh_calls, sh_calls, heads=False)

    def test_ancestry_check_errors_use_the_rejection_flow(self):
        errors = (subprocess.TimeoutExpired("git merge-base --is-ancestor", 120),
                  OSError("git ancestry check unavailable"),
                  (128, "", "git ancestry check failed"))
        for error in errors:
            with self.subTest(error=error):
                self.build_repo()
                old, new = self.scenario_grown()
                with mock.patch.object(ad, "mark_done", wraps=ad.mark_done) as mark_done:
                    result, gh_calls, sh_calls = self.harvest(old, new, ancestry_error=error)
                self.assert_refused(result, gh_calls, sh_calls)
                self.assertEqual(result["status"], "harvest-collapse-rejected")
                self.assertTrue(result["done"])
                self.assertIn("ancestry check failed", result["error"])
                self.assertIn(str(error) if isinstance(error, Exception) else error[2], result["error"])
                for evidence in (f"PR #{PR}", old[:12], new[:12], "(1, 10)->(2, 20)"):
                    self.assertIn(evidence, result["error"])
                    self.assertIn(evidence, self.brain())
                ledger = (self.state / "runs" / "run.log").read_text(encoding="utf-8")
                self.assertIn("harvest-collapse-guard: " + result["error"], ledger)
                mark_done.assert_called_once()
                self.assertEqual(mark_done.call_args.args[0], {"_done": [ISSUE]})

    def test_rest_pr_metadata_allows_a_normal_fix(self):
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new)
        self.assert_allowed(result, gh_calls, sh_calls)
        self.assertIn(["gh", "api", f"repos/{ad.REPO}/pulls/{PR}"], sh_calls)
        self.assertFalse(any(args[:2] == ["pr", "view"] for args in gh_calls))

    def test_unreadable_or_unparsable_pr_metadata_is_refused(self):
        errors = ((1, "", "API unavailable"), (0, "not json", ""))
        for error in errors:
            with self.subTest(error=error):
                self.build_repo()
                old, new = self.scenario_grown()
                result, gh_calls, sh_calls = self.harvest(old, new, pr_error=error)
                self.assert_refused(result, gh_calls, sh_calls, heads=False)
                self.assertIn("cannot be", result["error"])

    def test_missing_or_invalid_pr_sha_is_refused(self):
        for payload in ({}, None, [], {"head": None, "base": {}},
                        {"head": {"sha": ""}, "base": {"sha": "a" * 40}},
                        {"head": {"sha": "a" * 40}, "base": {"sha": 42}}):
            with self.subTest(payload=payload):
                self.build_repo()
                old, new = self.scenario_grown()
                result, gh_calls, sh_calls = self.harvest(
                    old, new, pr_error=(0, json.dumps(payload), ""))
                self.assert_refused(result, gh_calls, sh_calls, heads=False)
                self.assertIn("metadata cannot be parsed", result["error"])

    def test_unreadable_card_body_is_refused(self):
        # a card whose body cannot be read is not an empty card: without the target PR the
        # AD cannot tell which head the PR had, so the harvest is refused
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new, body_error=(1, "", "body unavailable"))
        self.assert_refused(result, gh_calls, sh_calls, heads=False)

    def test_unparsable_card_body_is_refused(self):
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new, body_error=(0, "not json", ""))
        self.assert_refused(result, gh_calls, sh_calls, heads=False)

    def test_missing_harvest_ref_is_refused(self):
        # the harvest branch does not exist: the guard may not read that as "nothing to check"
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new, harvest_ref="refs/heads/missing-harvest",
                                                 lane_ref=False)
        self.assert_refused(result, gh_calls, sh_calls, heads=False)

    def test_real_filter_failures_use_the_rejection_flow(self):
        for failure in ("missing-ref", "missing-base", "git-exit", "timeout", "os-error"):
            with self.subTest(failure=failure):
                self.build_repo()
                old, new = self.scenario_grown()
                if failure == "missing-base":
                    self.base = "0" * 40
                real_run = subprocess.run

                def filter_git(cmd, *args, **kwargs):
                    if "merge-base" in cmd and "refs/fleet/lane" in cmd:
                        if failure == "git-exit":
                            return subprocess.CompletedProcess(cmd, 128, b"", b"filter git failed")
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(cmd, 120)
                        if failure == "os-error":
                            raise OSError("filter git unavailable")
                    return real_run(cmd, *args, **kwargs)

                with mock.patch.object(ad.subprocess, "run", side_effect=filter_git), \
                     mock.patch.object(ad, "mark_done", wraps=ad.mark_done) as mark_done:
                    result, gh_calls, sh_calls = self.harvest(
                        old, new, real_filter=True, lane_ref=failure != "missing-ref")
                self.assert_refused(result, gh_calls, sh_calls, heads=False)
                self.assertTrue(result["done"], "the refused harvest still occupies the lane")
                self.assertEqual(result["status"], "filter-failed")
                self.assertTrue(result["error"])
                ledger = (self.state / "runs" / "run.log").read_text(encoding="utf-8")
                for evidence in ("harvest filter failed", result["error"]):
                    self.assertIn(evidence, self.brain())
                    self.assertIn(evidence, ledger)
                mark_done.assert_called_once()
                self.assertEqual(mark_done.call_args.args[0], {"_done": [ISSUE]})

    def test_unreadable_card_with_a_missing_harvest_branch_is_refused(self):
        # the card of the target PR cannot be read and the harvest branch does not exist
        # either: neither of the two may let the push pass (the review finding of #1833)
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new, body_error=(1, "", "body unavailable"),
                                                 harvest_ref="refs/heads/missing-harvest", lane_ref=False)
        self.assert_refused(result, gh_calls, sh_calls, heads=False)

    def test_card_that_mentions_an_unrelated_pr_is_not_guarded(self):
        # an ordinary card may quote another PR: it is no fix/rebase card, so there is no
        # head of the AD to compare with and the harvest goes through
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new, card=PLAIN_CARD_WITH_PR)
        self.assert_allowed(result, gh_calls, sh_calls)

    def test_card_without_a_target_pr_is_allowed(self):
        # an ordinary implementation card names no PR: there is no head to compare with
        self.build_repo()
        old, new = self.scenario_grown()
        result, gh_calls, sh_calls = self.harvest(old, new, card=PLAIN_CARD)
        self.assert_allowed(result, gh_calls, sh_calls)


class HarvestCollapseReplayTest(HarvestFixture, unittest.TestCase):
    """Replay of the six collapsed PRs of 2026-11-03 (Refs #1833)."""

    def build_sizes(self, files_before, lines_before, files_after, lines_after):
        """A history whose head before the round and harvest head have the recorded sizes."""
        self.build_repo()
        for index in range(files_before):
            self.write(f"f{index}", self.lines(self.share(lines_before, files_before, index), "old"))
        old = self.commit("old")
        for index in range(files_before):
            (self.repo / f"f{index}").unlink()
        for index in range(files_after):
            self.write(f"g{index}", self.lines(self.share(lines_after, files_after, index), "new"))
        new = self.commit("new")
        return old, new

    @staticmethod
    def share(lines, files, index):
        """The lines of file `index` when `lines` lines are spread over `files` files."""
        base = lines // files
        return base + (1 if index < lines % files else 0)

    def test_every_recorded_incident_is_refused(self):
        """Every one of the six recorded collapses is refused by the guard."""
        for incident in RECORDED_INCIDENTS:
            with self.subTest(pr=incident["pr"]):
                old, new = self.build_sizes(incident["files_before"], incident["lines_before"],
                                            incident["files_after"], incident["lines_after"])
                result, gh_calls, sh_calls = self.harvest(old, new)
                self.assert_refused(result, gh_calls, sh_calls, heads=False)
                # the size rule itself must have fired, not just any refusal
                self.assertIn("collapsed", self.brain(), self.brain())

    def test_manifest_replay_uses_recorded_base_and_reports_all_six(self):
        # Exercise the replay driver with locally available objects. These fixtures
        # verify its wiring; they are not evidence for the six real incident SHAs.
        self.build_repo()
        old, new = self.scenario_lines_collapse()
        entries = [dict(pr=str(item["pr"]), base=self.base, before=old, after=new)
                   for item in RECORDED_INCIDENTS]
        with mock.patch(__name__ + ".manifest_paths", return_value=["fixture"]), \
             mock.patch(__name__ + ".manifest_entries", return_value=entries), \
             mock.patch(__name__ + ".REAL_REPOSITORIES", (str(self.repo),)), \
             mock.patch("builtins.print") as output:
            self.test_replay_of_the_restore_manifest_refuses_every_incident()
        reports = [call.args[0] for call in output.call_args_list
                   if call.args and str(call.args[0]).startswith("PR #")]
        self.assertEqual(len(reports), 6)
        for entry, report in zip(entries, reports):
            self.assertIn(f"PR #{entry['pr']} base={entry['base']}", report)
            self.assertIn("sizes=(1, 100)->(1, 4) REJECT:", report)

    def test_manifest_replay_requires_every_incident(self):
        entries = [dict(pr=str(item["pr"]), base="a" * 40,
                        before="b" * 40, after="c" * 40)
                   for item in RECORDED_INCIDENTS[:-1]]
        with mock.patch(__name__ + ".manifest_paths", return_value=["fixture"]), \
             mock.patch(__name__ + ".manifest_entries", return_value=entries):
            with self.assertRaisesRegex(AssertionError, "all six"):
                self.test_replay_of_the_restore_manifest_refuses_every_incident()

    def test_replay_of_the_restore_manifest_refuses_every_incident(self):
        """The real before/after heads of the six collapsed PRs (the box keeps the manifest)."""
        paths = manifest_paths()
        if not paths:
            self.skipTest(f"the restore manifest with the real before/after heads is not reachable "
                          f"from this workspace (looked for {MANIFEST_NAME}); run it on the box with "
                          "AD_HARVEST_COLLAPSE_MANIFEST=<path> to replay the six real SHA pairs")
        entries = manifest_entries(paths[0])
        self.assertTrue(entries, f"the manifest {paths[0]} carries no before/after heads")
        required = {str(incident["pr"]) for incident in RECORDED_INCIDENTS}
        selected = [entry for entry in entries if entry["pr"] in required]
        self.assertEqual({entry["pr"] for entry in selected}, required,
                         "the manifest must include all six incident PRs, including #1824")
        self.assertEqual(len(selected), len(required), "duplicate incident PRs in manifest")
        for entry in selected:
            with self.subTest(pr=entry["pr"]):
                for key in ("base", "before", "after"):
                    self.assertRegex(entry[key] or "", r"^[0-9a-fA-F]{40}$",
                                     f"PR #{entry['pr']} requires its real {key} SHA")
                self.build_repo()
                self.assertTrue(self.import_real_objects(entry),
                                f"PR #{entry['pr']}: base/before/after objects are not reachable")
                # Both dispatch/filter base and PR metadata must use the recorded base.
                self.base = entry["base"]
                card = f"重基PR：#{entry['pr']} {BRANCH}"
                result, gh_calls, sh_calls = self.harvest(entry["before"], entry["after"],
                                                         card=card)
                self.assert_refused(result, gh_calls, sh_calls, heads=False)
                detail = result.get("error") or ""
                self.assertTrue(any(word in detail for word in ("does not contain", "collapsed")),
                                detail)
                self.assertIn(f"PR #{entry['pr']}", detail)
                with mock.patch.object(ad, "LOCAL", str(self.repo)):
                    before_size = ad.diff_size(entry["base"], entry["before"])
                    after_size = ad.diff_size(entry["base"], entry["after"])
                self.assertIn(f"{before_size}->{after_size}", detail)
                print(f"PR #{entry['pr']} base={entry['base']} before={entry['before']} "
                      f"after={entry['after']} sizes={before_size}->{after_size} REJECT: {detail}")


if __name__ == "__main__":
    unittest.main()
