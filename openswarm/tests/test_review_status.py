"""`openswarm/lib/review/status.py`: the commit status of a lane review (Refs #1447).

The module is loaded by path: `openswarm/lib/review/` is a namespace package without an
`__init__.py`, so it must not be imported as a package.
"""
import importlib.util
import pathlib
import subprocess
import unittest

HERE = pathlib.Path(__file__).resolve().parent
STATUS_MODULE = HERE.parent / "lib" / "review" / "status.py"

spec = importlib.util.spec_from_file_location("review_status", STATUS_MODULE)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class FakeRunner:
    """Stand in for `subprocess.run` and record every call."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(list(command), self.returncode, self.stdout, self.stderr)

    @property
    def commands(self):
        return [call[0] for call in self.calls]


class ParseVerdictTests(unittest.TestCase):
    """A review conclusion is read as `VERDICT:` and `SUMMARY:` lines."""

    def test_a_normal_conclusion_yields_the_verdict_and_the_summary(self):
        text = "# Review\n\nSUMMARY: looks good, one nit\nVERDICT: APPROVE\n"

        verdict, summary = status.parse_verdict(text)

        self.assertEqual(verdict, "APPROVE")
        self.assertEqual(summary, "looks good, one nit")

    def test_the_first_verdict_line_wins(self):
        text = "VERDICT: CHANGES\n\nVERDICT: APPROVE\n"

        verdict, summary = status.parse_verdict(text)

        self.assertEqual(verdict, "CHANGES")
        self.assertIsNone(summary)

    def test_a_conclusion_without_a_verdict_has_none(self):
        verdict, summary = status.parse_verdict("# Review\n\nSUMMARY: no verdict here\n")

        self.assertIsNone(verdict)
        self.assertEqual(summary, "no verdict here")

    def test_the_verdict_marker_is_case_sensitive(self):
        self.assertIsNone(status.parse_verdict("verdict: APPROVE\n")[0])
        self.assertIsNone(status.parse_verdict("Verdict: CHANGES\n")[0])
        # the value is case sensitive as well
        self.assertIsNone(status.parse_verdict("VERDICT: approve\n")[0])
        self.assertIsNone(status.parse_verdict("VERDICT: Changes\n")[0])
        # a longer word is not a verdict either
        self.assertIsNone(status.parse_verdict("VERDICT: APPROVED\n")[0])

    def test_the_verdict_marker_has_to_start_the_line(self):
        self.assertIsNone(status.parse_verdict("the reviewer wrote VERDICT: APPROVE\n")[0])
        self.assertEqual(status.parse_verdict("VERDICT: APPROVE\n")[0], "APPROVE")

    def test_the_first_summary_line_wins(self):
        text = "SUMMARY: first\nSUMMARY: second\nVERDICT: CHANGES\n"

        verdict, summary = status.parse_verdict(text)

        self.assertEqual(verdict, "CHANGES")
        self.assertEqual(summary, "first")

    def test_an_empty_conclusion_has_neither_verdict_nor_summary(self):
        self.assertEqual(status.parse_verdict(""), (None, None))
        self.assertEqual(status.parse_verdict(None), (None, None))


class BuildStatusTests(unittest.TestCase):
    """The verdict maps onto the commit status, and the description names the review."""

    def payload(self, verdict, **kwargs):
        arguments = {"reviewer": "codex-reviewer", "backend": "codex", "model": "gpt-5",
                     "run_id": "run-1"}
        arguments.update(kwargs)
        return status.build_status(verdict, SHA, **arguments)

    def test_approve_is_a_success(self):
        self.assertEqual(self.payload("APPROVE")["state"], "success")

    def test_changes_is_a_failure(self):
        self.assertEqual(self.payload("CHANGES")["state"], "failure")

    def test_a_missing_verdict_is_pending(self):
        self.assertEqual(self.payload(None)["state"], "pending")

    def test_the_payload_carries_the_sha_the_context_and_the_description(self):
        payload = self.payload("APPROVE")

        self.assertEqual(payload["sha"], SHA)
        self.assertEqual(payload["context"], "lane-review")
        self.assertEqual(payload["description"],
                         "APPROVE by codex-reviewer (codex/gpt-5) run run-1")

    def test_the_description_of_a_missing_verdict_reads_pending(self):
        self.assertEqual(self.payload(None)["description"],
                         "PENDING by codex-reviewer (codex/gpt-5) run run-1")

    def test_the_context_can_be_chosen(self):
        payload = self.payload("APPROVE", context="custom-context")

        self.assertEqual(payload["context"], "custom-context")

    def test_a_long_description_is_cut_at_140_characters(self):
        payload = self.payload("APPROVE", reviewer="r" * 200, run_id="run-" + "9" * 200)

        description = payload["description"]
        self.assertEqual(len(description), 140)
        self.assertTrue(description.endswith("…"))

    def test_a_description_of_exactly_140_characters_is_kept(self):
        reviewer = "r" * (140 - len("APPROVE by  (codex/gpt-5) run run-1"))

        payload = self.payload("APPROVE", reviewer=reviewer)

        self.assertEqual(len(payload["description"]), 140)
        self.assertFalse(payload["description"].endswith("…"))

    def test_a_sha_that_is_no_commit_is_refused(self):
        for bad in ("abc", "z" * 40, SHA[:-1], SHA + "0", ""):
            with self.assertRaises(ValueError):
                status.build_status("APPROVE", bad, "codex-reviewer", "codex", "gpt-5", "run-1")

    def test_an_unknown_verdict_is_refused(self):
        with self.assertRaises(ValueError):
            status.build_status("MAYBE", SHA, "codex-reviewer", "codex", "gpt-5", "run-1")


class PostStatusTests(unittest.TestCase):
    """The status is sent with `gh api`, and a missing setup never raises."""

    def payload(self, **kwargs):
        body = {"sha": SHA, "state": "success", "context": "lane-review",
                "description": "APPROVE by codex-reviewer (codex/gpt-5) run run-1"}
        body.update(kwargs)
        return body

    def test_the_status_is_sent_with_gh_api(self):
        runner = FakeRunner()

        result = status.post_status("acme/project", self.payload(), runner=runner,
                                    env={"GH_TOKEN": "secret"})

        self.assertTrue(result["posted"])
        self.assertEqual(result["sha"], SHA)
        self.assertEqual(result["state"], "success")
        self.assertEqual(len(runner.calls), 1)
        command = runner.commands[0]
        self.assertEqual(command[0], "gh")
        self.assertEqual(command[1], "api")
        self.assertEqual(command[2], "-X")
        self.assertEqual(command[3], "POST")
        self.assertEqual(command[4], "repos/acme/project/statuses/%s" % SHA)
        self.assertEqual(command[5], "-f")
        self.assertEqual(command[6], "state=success")
        self.assertEqual(command[7], "-f")
        self.assertEqual(command[8], "context=lane-review")
        self.assertEqual(command[9], "-f")
        self.assertEqual(command[10],
                         "description=APPROVE by codex-reviewer (codex/gpt-5) run run-1")

    def test_the_environment_is_handed_to_the_call(self):
        runner = FakeRunner()

        status.post_status("acme/project", self.payload(), runner=runner,
                           env={"GH_TOKEN": "secret"})

        self.assertEqual(runner.calls[0][1]["env"], {"GH_TOKEN": "secret"})

    def test_a_missing_repository_skips_the_status(self):
        runner = FakeRunner()

        result = status.post_status("", self.payload(), runner=runner,
                                    env={"GH_TOKEN": "secret"})

        self.assertIn("skipped", result)
        self.assertEqual(runner.calls, [])

    def test_without_a_token_and_without_a_gh_login_the_status_is_skipped(self):
        runner = FakeRunner(returncode=1)

        result = status.post_status("acme/project", self.payload(), runner=runner, env={})

        self.assertIn("skipped", result)
        # only the login probe ran, the status was never sent
        self.assertEqual(runner.commands, [["gh", "auth", "status"]])

    def test_a_github_token_is_enough_and_no_login_probe_runs(self):
        runner = FakeRunner()

        result = status.post_status("acme/project", self.payload(), runner=runner,
                                    env={"GITHUB_TOKEN": "secret"})

        self.assertTrue(result["posted"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.commands[0][1], "api")

    def test_a_gh_login_without_a_token_is_enough(self):
        runner = FakeRunner()

        result = status.post_status("acme/project", self.payload(), runner=runner, env={})

        self.assertTrue(result["posted"])
        self.assertEqual(runner.commands[0], ["gh", "auth", "status"])
        self.assertEqual(runner.commands[1][1], "api")

    def test_a_rejected_call_is_reported_and_does_not_raise(self):
        runner = FakeRunner(returncode=1, stderr="HTTP 404")

        result = status.post_status("acme/project", self.payload(), runner=runner,
                                    env={"GH_TOKEN": "secret"})

        self.assertIn("error", result)
        self.assertNotIn("posted", result)

    def test_a_payload_without_a_commit_is_skipped(self):
        runner = FakeRunner()

        result = status.post_status("acme/project", {"state": "success"}, runner=runner,
                                    env={"GH_TOKEN": "secret"})

        self.assertIn("skipped", result)
        self.assertEqual(runner.calls, [])


class HeadChangedTests(unittest.TestCase):
    """A status belongs to the commit that was reviewed."""

    def test_the_same_head_is_not_changed(self):
        self.assertFalse(status.head_changed(SHA, SHA))

    def test_a_rewritten_head_is_changed(self):
        self.assertTrue(status.head_changed(SHA, OTHER_SHA))

    def test_a_missing_sha_counts_as_changed(self):
        self.assertTrue(status.head_changed(None, SHA))
        self.assertTrue(status.head_changed(SHA, None))
        self.assertTrue(status.head_changed("", ""))

    def test_the_comparison_ignores_case_and_spaces(self):
        self.assertFalse(status.head_changed(SHA.upper(), SHA))


if __name__ == "__main__":
    unittest.main()
