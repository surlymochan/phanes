#!/usr/bin/env python3
"""Machine self-check of the harvested diff (Refs #1729).

Covers the three gates of `harvest_self_check.inspect` (binary / build artefact
rejection, `no-tests`, `missing-failure-tests`), the allowlist for legitimate binary
resources and the harvest integration in `autodispatch.py` (result published to the
harvest log, the issue and the applicable PR, labels created and verified, and the
fail-open path when the self-check itself breaks).
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harvest_self_check

SPEC = importlib.util.spec_from_file_location("autodispatch", HERE / "autodispatch.py")
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)

PR_URL = "https://github.com/surlymochan/phanes/pull/7"


class SampleRepo:
    """A throwaway git repo the self-check can be run against."""

    def __init__(self, path):
        self.path = path
        self.git("init", "-q", ".")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.path, *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode()

    def write(self, name, content, binary=False):
        path = os.path.join(self.path, name)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb" if binary else "w") as stream:
            stream.write(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message, "--allow-empty")
        return self.git("rev-parse", "HEAD").strip()


class SelfCheckGateTest(unittest.TestCase):
    """The gates of `harvest_self_check.inspect` on real diffs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = SampleRepo(temporary.name)
        self.base = self.repo.commit("base")

    def inspect(self):
        return harvest_self_check.inspect(self.repo.path, self.base, "HEAD")

    def test_binary_file_is_rejected_with_its_path(self):
        self.repo.write("assets/logo.png", b"\x89PNG\r\n\x1a\n\x00", binary=True)
        self.repo.commit("add a binary")

        result = self.inspect()

        self.assertIn("assets/logo.png", result["rejected"])
        self.assertEqual(result["rejected_reasons"]["assets/logo.png"], "binary")

    def test_renamed_binary_is_rejected_under_its_new_path(self):
        self.repo.write("assets/old.bin", b"\x00abc", binary=True)
        self.base = self.repo.commit("base with a binary")
        os.rename(os.path.join(self.repo.path, "assets/old.bin"), os.path.join(self.repo.path, "assets/new.bin"))
        self.repo.commit("rename the binary")

        result = self.inspect()

        self.assertIn("assets/new.bin", result["rejected"])
        self.assertNotIn("assets/old.bin", result["rejected"])

    def test_dependency_directory_and_archive_are_rejected(self):
        self.repo.write("node_modules/pkg/index.js", "module.exports = 1;\n")
        self.repo.write("release.tar.gz", b"\x1f\x8b\x08", binary=True)
        self.repo.commit("add build artefacts")

        result = self.inspect()

        self.assertIn("node_modules/pkg/index.js", result["rejected"])
        self.assertIn("release.tar.gz", result["rejected"])

    def test_allowlisted_fixture_image_is_not_rejected(self):
        self.repo.write(".harvest-self-check-allow", "# fixture images are legitimate\ntests/fixtures/*.png\n")
        self.repo.write("tests/fixtures/pixel.png", b"\x89PNG\r\n\x1a\n\x00", binary=True)
        self.repo.write("assets/logo.png", b"\x89PNG\r\n\x1a\n\x00", binary=True)
        self.repo.commit("add images")

        result = self.inspect()

        self.assertNotIn("tests/fixtures/pixel.png", result["rejected"])
        self.assertIn("assets/logo.png", result["rejected"])

    def test_code_only_change_is_flagged_no_tests(self):
        self.repo.write("openswarm/lib/thing.py", "VALUE = 1\n")
        self.repo.commit("change code without a test")

        result = self.inspect()

        self.assertTrue(result["no_tests"])
        self.assertFalse(result["missing_failure_tests"])

    def test_documentation_change_is_not_flagged_no_tests(self):
        self.repo.write("docs/thing.md", "some text\n")
        self.repo.commit("change documentation")

        result = self.inspect()

        self.assertFalse(result["no_tests"])

    def test_control_plane_change_without_failure_test_is_flagged(self):
        self.repo.write("openswarm/lib/dispatch/engine.py", "VALUE = 1\n")
        self.repo.commit("change the control plane")

        result = self.inspect()

        self.assertTrue(result["missing_failure_tests"])
        self.assertTrue(result["control_plane"])

    def test_failure_path_added_in_a_normal_test_file_clears_the_flag(self):
        self.repo.write("openswarm/lib/dispatch/engine.py", "VALUE = 1\n")
        self.repo.write("openswarm/tests/test_engine.py",
                        "import pytest\n\n\ndef test_boom():\n    with pytest.raises(ValueError):\n        engine()\n")
        self.repo.commit("control plane change with a failure-path test")

        result = self.inspect()

        self.assertTrue(result["failure_tests"])
        self.assertFalse(result["missing_failure_tests"])
        self.assertFalse(result["no_tests"])

    def test_untouched_failure_test_does_not_count_as_coverage(self):
        self.repo.write("openswarm/tests/test_engine.py",
                        "import pytest\n\n\ndef test_boom():\n    with pytest.raises(ValueError):\n        engine()\n")
        self.base = self.repo.commit("base with a failure-path test")
        self.repo.write("openswarm/tests/test_engine.py",
                        "import pytest\n\n\ndef test_boom():\n    with pytest.raises(ValueError):\n"
                        "        engine()\n\n\ndef test_plain():\n    assert True\n")
        self.repo.write("openswarm/lib/dispatch/engine.py", "VALUE = 1\n")
        self.repo.commit("only an ordinary assertion changed")

        result = self.inspect()

        self.assertFalse(result["failure_tests"])
        self.assertTrue(result["missing_failure_tests"])

    def test_a_diff_that_cannot_be_read_raises_instead_of_guessing(self):
        """A repository that cannot be read must raise (the caller then fails open), never guess."""
        with self.assertRaises(subprocess.CalledProcessError):
            harvest_self_check.inspect(os.path.join(self.repo.path, "missing-repo"), self.base, "HEAD")

    def test_a_huge_test_diff_is_not_judged_but_reported(self):
        """A diff too large to judge must not be judged (no false flag) and must be reported."""
        self.repo.write("openswarm/lib/dispatch/engine.py", "VALUE = 1\n")
        self.repo.write("openswarm/tests/test_engine.py",
                        "import pytest\n\n\ndef test_boom():\n    with pytest.raises(ValueError):\n        engine()\n")
        self.repo.commit("control plane change with a failure-path test")
        limit = harvest_self_check.MAX_DIFF_BYTES
        harvest_self_check.MAX_DIFF_BYTES = 1
        self.addCleanup(setattr, harvest_self_check, "MAX_DIFF_BYTES", limit)

        result = self.inspect()

        self.assertFalse(result["missing_failure_tests"])
        self.assertTrue(any("too large" in warning for warning in result["warnings"]))

    def test_deleted_binary_is_not_rejected(self):
        self.repo.write("assets/legacy.bin", b"\x00abc", binary=True)
        self.base = self.repo.commit("base with a binary")
        os.remove(os.path.join(self.repo.path, "assets/legacy.bin"))
        self.repo.commit("drop the binary")

        result = self.inspect()

        self.assertEqual(result["rejected"], [])

    def test_pure_deletion_does_not_need_a_test(self):
        self.repo.write("openswarm/lib/thing.py", "VALUE = 1\n")
        self.base = self.repo.commit("base with code")
        os.remove(os.path.join(self.repo.path, "openswarm/lib/thing.py"))
        self.repo.commit("drop the code")

        result = self.inspect()

        self.assertFalse(result["no_tests"])

    def test_unknown_base_commit_falls_back_to_the_local_branch(self):
        """The dispatch-time base commit may be missing in the mirror — the gate must still judge."""
        self.base = self.repo.commit("base")
        self.repo.git("checkout", "-q", "-b", "lane")
        self.repo.write("assets/logo.png", b"\x89PNG\r\n\x1a\n\x00", binary=True)
        self.repo.commit("lane adds a binary")
        self.repo.git("checkout", "-q", "main")
        self.repo.write("docs/other.md", "unrelated\n")
        self.repo.commit("main moved on")

        result = harvest_self_check.inspect(self.repo.path, "0" * 40, "lane")

        self.assertIn("assets/logo.png", result["rejected"])


class HarvestIntegrationTest(unittest.TestCase):
    """The self-check result must reach the harvest log, the issue and the applicable PR."""

    LANE = "m1"
    CFG = {"host": "box", "backend": "codex", "model": "m"}
    INFO = {"branch": "work", "commit": "abc123", "repo": "repo", "backend": "codex", "model": "m",
            "status": "ok", "verify_rc": 0, "summary": "did the thing"}

    @staticmethod
    def gh_default(args):
        if args[:2] == ["issue", "view"]:
            return 0, '{"labels": []}', ""
        if args[:2] == ["pr", "create"]:
            return 0, PR_URL + "\n", ""
        if args[:2] == ["pr", "list"]:
            return 0, "[]", ""
        if args[:2] == ["pr", "view"]:
            return 0, '{"labels": []}', ""
        return 0, "", ""

    @staticmethod
    def gh_with_pr(args):
        if args[:2] == ["pr", "list"]:
            return 0, f'[{{"number": 7, "url": "{PR_URL}", "body": "Closes #1729"}}]', ""
        return HarvestIntegrationTest.gh_default(args)

    def run_finish(self, module, gh_handler=None):
        """Drive `finish()` with fake git / gh and return the recorded calls."""
        handler = gh_handler or self.gh_default
        gh_calls, sh_calls, logs = [], [], []

        def fake_gh(args, timeout=60):
            gh_calls.append(list(args))
            return handler(args)

        def fake_sh(cmd, timeout=120, inp=None):
            sh_calls.append(cmd)
            return 0, "", ""

        state = {}
        with mock.patch.object(AUTODISPATCH, "gh", fake_gh), \
                mock.patch.object(AUTODISPATCH, "sh", fake_sh), \
                mock.patch.object(AUTODISPATCH, "log", lambda msg, brain=True: logs.append(msg)), \
                mock.patch.object(AUTODISPATCH, "load_self_check", lambda: module):
            AUTODISPATCH.finish(self.LANE, self.CFG, {"issue": 1729, "run": "r1", "base": "main"}, self.INFO, state)
        return gh_calls, sh_calls, logs, state

    def test_self_check_failure_does_not_block_the_harvest(self):
        class Broken:
            @staticmethod
            def inspect(*args, **kwargs):
                raise RuntimeError("boom")

        gh_calls, sh_calls, logs, state = self.run_finish(Broken())

        # the harvest continued: the branch was pushed and the PR opened
        self.assertTrue(any("push" in " ".join(call) for call in sh_calls))
        self.assertTrue(any(call[:2] == ["pr", "create"] for call in gh_calls))
        # the warning is logged and published on the PR
        self.assertTrue(any("failed open" in line for line in logs))
        comments = [call for call in gh_calls if call[:2] == ["pr", "comment"]]
        self.assertTrue(comments)
        self.assertIn("failed open", comments[0][-1])

    def test_missing_self_check_module_fails_open(self):
        gh_calls, sh_calls, logs, state = self.run_finish(None)  # the module is not deployed

        self.assertTrue(any("push" in " ".join(call) for call in sh_calls))
        self.assertTrue(any("unavailable" in line for line in logs))
        comments = [call for call in gh_calls if call[:2] == ["pr", "comment"]]
        self.assertTrue(comments)
        self.assertIn("unavailable", comments[0][-1])

    def test_rejected_harvest_is_refused_before_the_push_and_published(self):
        class Rejecting:
            @staticmethod
            def inspect(*args, **kwargs):
                return {"paths": ["assets/logo.png"], "rejected": ["assets/logo.png"], "no_tests": False,
                        "missing_failure_tests": False, "warnings": []}

        gh_calls, sh_calls, logs, state = self.run_finish(Rejecting(), gh_handler=self.gh_with_pr)

        # nothing was pushed and no PR was opened
        self.assertFalse(any("push" in " ".join(call) for call in sh_calls))
        self.assertFalse(any(call[:2] == ["pr", "create"] for call in gh_calls))
        # the rejected file list is in the harvest log, on the issue and on the applicable PR
        self.assertTrue(any("assets/logo.png" in line for line in logs))
        issue_comments = [call for call in gh_calls if call[:2] == ["issue", "comment"]]
        self.assertTrue(any("assets/logo.png" in call[-1] for call in issue_comments))
        pr_comments = [call for call in gh_calls if call[:2] == ["pr", "comment"]]
        self.assertTrue(any("assets/logo.png" in call[-1] for call in pr_comments))

    def test_pr_carries_the_self_check_labels(self):
        class Clean:
            @staticmethod
            def inspect(*args, **kwargs):
                return {"paths": ["openswarm/lib/dispatch/engine.py"], "rejected": [], "no_tests": True,
                        "missing_failure_tests": True, "warnings": []}

        gh_calls, sh_calls, logs, state = self.run_finish(Clean())

        created = [call[2] for call in gh_calls if call[:2] == ["label", "create"]]
        self.assertEqual(created, ["no-tests", "missing-failure-tests"])
        added = [call for call in gh_calls if call[:2] == ["pr", "edit"] and "--add-label" in call]
        self.assertTrue(added)
        self.assertIn("no-tests", added[0])
        self.assertIn("missing-failure-tests", added[0])

    def test_missing_labels_are_created_added_and_verified(self):
        calls = []

        def handler(args):
            calls.append(list(args))
            if args[:2] == ["pr", "view"]:
                return 0, '{"labels": [{"name": "no-tests"}]}', ""
            return 0, "", ""

        with mock.patch.object(AUTODISPATCH, "gh", lambda args, timeout=60: handler(args)):
            missing = AUTODISPATCH.apply_pr_labels(PR_URL, ["no-tests", "missing-failure-tests"])

        self.assertEqual(missing, ["missing-failure-tests"])  # the PR did not take the second label
        created = [call[2] for call in calls if call[:2] == ["label", "create"]]
        self.assertEqual(created, ["no-tests", "missing-failure-tests"])
        added = [call for call in calls if call[:2] == ["pr", "edit"] and "--add-label" in call]
        self.assertEqual(len(added), 1)
        self.assertIn("no-tests", added[0])
        self.assertIn("missing-failure-tests", added[0])
        self.assertTrue(any(call[:2] == ["pr", "view"] for call in calls))  # the labels were read back

    def test_applied_labels_report_nothing_missing(self):
        def handler(args):
            if args[:2] == ["pr", "view"]:
                return 0, '{"labels": [{"name": "no-tests"}]}', ""
            return 0, "", ""

        with mock.patch.object(AUTODISPATCH, "gh", lambda args, timeout=60: handler(args)):
            missing = AUTODISPATCH.apply_pr_labels(PR_URL, ["no-tests"])

        self.assertEqual(missing, [])

    def test_a_failing_label_operation_is_logged_and_published(self):
        """A label that cannot be created or added must be reported, not silently dropped."""
        class Clean:
            @staticmethod
            def inspect(*args, **kwargs):
                return {"paths": ["openswarm/lib/dispatch/engine.py"], "rejected": [], "no_tests": True,
                        "missing_failure_tests": False, "warnings": []}

        def handler(args):
            if args[:2] == ["label", "create"]:
                return 1, "", "label creation is not allowed"
            if args[:2] == ["pr", "view"]:
                return 0, '{"labels": []}', ""
            return HarvestIntegrationTest.gh_default(args)

        gh_calls, sh_calls, logs, state = self.run_finish(Clean(), gh_handler=handler)

        self.assertTrue(any("label operations failed" in line for line in logs))
        comments = [call for call in gh_calls if call[:2] == ["pr", "comment"]]
        self.assertTrue(comments)
        self.assertIn("Labels not applied: no-tests", comments[0][-1])


if __name__ == "__main__":
    unittest.main()
