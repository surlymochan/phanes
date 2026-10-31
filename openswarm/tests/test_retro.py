"""Weekly retro aggregation and orchestration use fixture ledgers only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

import _load

xs = _load.load()


class RetroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-retro-")
        self.root = Path(self.tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()
        self.policy = {"knowledge": {"repo": str(self.knowledge)}, "timezone": "UTC"}
        self.constants = {key: getattr(xs, key) for key in ("DATA", "LEDGER", "HOME", "DEFAULT_KNOWLEDGE_REPO")}
        xs.DATA = str(self.root / "data")
        xs.LEDGER = str(self.root / "data" / "ledger.jsonl")
        xs.HOME = str(self.root / "home")
        xs.DEFAULT_KNOWLEDGE_REPO = str(self.knowledge)
        self.now = 1_800_000_000
        self.write_ledger(
            {"id": "pass-1", "ts": self.now - 100, "status": "done", "backend": "codex",
             "model": "gpt-test", "card_type": "fix", "exec_pass": True, "accepted": True,
             "peer_review": {"by": "pi:test", "verdict": "approve", "p0": 0, "p1": 0, "p2": 0},
             "routing_advice": {"backend": "codex", "model": "gpt-test"}},
            {"id": "reject-2", "ts": self.now - 200, "status": "failed", "backend": "codex",
             "model": "gpt-test", "card_type": "fix", "exec_pass": False, "accepted": False,
             "peer_review": {"by": "pi:test", "verdict": "changes", "p0": 0, "p1": 1, "p2": 0},
             "accepted_reason": "tests failed", "error_class": "exec_error",
             "routing_advice": {"backend": "pi", "model": "pi-test"}},
            {"id": "run-3", "ts": self.now - 300, "status": "done", "backend": "pi",
             "model": "pi-test", "card_type": "docs"},
        )

    def tearDown(self):
        self.tmp.cleanup()
        for key, value in self.constants.items():
            setattr(xs, key, value)

    def write_ledger(self, *entries):
        path = Path(xs.fleet_ledger_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        return path

    def run_retro(self, *args, policy=None, step=None):
        original_argv = sys.argv
        output, error = io.StringIO(), io.StringIO()
        calls = []

        def fake_step(name, step_args, policy_arg):
            calls.append((name, step_args))
            if step:
                result = step(name, step_args, policy_arg)
                if result is not None:
                    return result
            if name == "lessons escalate":
                return False
            return True

        try:
            sys.argv = ["openswarm", "retro", *args]
            with mock.patch.object(xs, "load_policy", return_value=policy or self.policy), \
                    mock.patch.object(xs, "NOW", return_value=self.now), \
                    mock.patch.object(xs, "retro_step", side_effect=fake_step), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                try:
                    xs.main()
                except SystemExit as exc:
                    return exc.code, output.getvalue(), error.getvalue(), calls
            return 0, output.getvalue(), error.getvalue(), calls
        finally:
            sys.argv = original_argv

    def test_writes_markdown_sections_and_fixture_counts_with_no_pull(self):
        code, output, error, calls = self.run_retro("--days", "7", "--repos", "octo/one", "octo/two", "--no-pull")

        self.assertEqual((code, error), (0, ""))
        summary = json.loads(output)
        self.assertEqual(summary["counts"], {"runs": 3, "groups": 2, "failures": 1, "rejected": 1,
                                             "exec_pass_null": 1,
                                             "lesson_proposals": 0, "guard_proposals": 0,
                                             "routing_comparisons": 2})
        self.assertFalse(any(name == "fleet ledger pull" for name, _ in calls))
        ingest = next(args for name, args in calls if name == "ledger ingest")
        self.assertEqual(ingest.repo, ["octo/one", "octo/two"])
        self.assertEqual(ingest.since, 7)

        report = Path(summary["paths"]["report"]).read_text(encoding="utf-8")
        for heading in ("Runs by backend/model/card_type", "Top failure error_classes", "Rejected runs",
                        "New lesson and guard proposals", "Routing advice vs actual", "Proposed changes"):
            self.assertIn(heading, report)
        self.assertIn("1/2 (50%)", report)
        self.assertIn("null=1", report)
        self.assertIn("peer review P1+", report)
        self.assertIn("1/2 (50%)", report)
        self.assertIn("exec_error", report)
        self.assertIn("tests failed", report)
        self.assertIn("Skipped fleet ledger pull (--no-pull).", report)
        self.assertIn("Skipped lessons escalate", report)

    def test_runs_pull_when_enabled(self):
        code, output, error, calls = self.run_retro()

        self.assertEqual((code, error), (0, ""))
        self.assertIn("fleet ledger pull", [name for name, _ in calls])
        self.assertEqual(json.loads(output)["counts"]["runs"], 3)

    def test_empty_ledger_writes_markdown_sections_with_zero_counts(self):
        self.write_ledger()
        code, output, error, calls = self.run_retro("--days", "7", "--no-pull")

        self.assertEqual((code, error), (0, ""))
        summary = json.loads(output)
        self.assertEqual(summary["counts"], {"runs": 0, "groups": 0, "failures": 0, "rejected": 0,
                                             "exec_pass_null": 0,
                                             "lesson_proposals": 0, "guard_proposals": 0,
                                             "routing_comparisons": 0})
        self.assertFalse(any(name == "fleet ledger pull" for name, _ in calls))

        report = Path(summary["paths"]["report"]).read_text(encoding="utf-8")
        for heading in ("Runs by backend/model/card_type", "Top failure error_classes", "Rejected runs",
                        "New lesson and guard proposals", "Routing advice vs actual", "Proposed changes"):
            self.assertIn(heading, report)
        self.assertIn("0", report)

    def test_missing_knowledge_repo_fails_before_running_steps(self):
        missing = {"knowledge": {"repo": str(self.root / "missing")}}
        code, output, error, calls = self.run_retro(policy=missing)

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("knowledge repo not found", error)
        self.assertEqual(calls, [])

    def test_rate_limit_is_noted_and_later_steps_continue(self):
        def rate_limited(name, _args, _policy):
            if name == "ledger ingest":
                raise xs.BusError("HTTP 403: API rate limit exceeded")
            return None

        code, output, error, calls = self.run_retro("--no-pull", step=rate_limited)

        self.assertEqual((code, error), (0, ""))
        names = [name for name, _ in calls]
        self.assertIn("ledger backfill-pass", names)
        self.assertIn("routing weights", names)
        self.assertIn("lessons propose", names)
        self.assertIn("routing compare", names)
        self.assertIn("rate limit", Path(json.loads(output)["paths"]["report"]).read_text(encoding="utf-8"))
        self.assertTrue(json.loads(output)["steps"]["ledger ingest"]["rate_limited"])

    def test_escalate_skips_without_command_and_invokes_cli_when_available(self):
        args = type("Args", (), {})()
        with mock.patch.object(xs, "cmd_lessons_escalate", None, create=True):
            self.assertFalse(xs.retro_step("lessons escalate", args, self.policy))

        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(xs, "cmd_lessons_escalate", lambda *_: None, create=True), \
                mock.patch.object(xs.subprocess, "run", return_value=result) as run:
            self.assertTrue(xs.retro_step("lessons escalate", args, self.policy))
        self.assertEqual(run.call_args.args[0][-2:], ["lessons", "escalate"])

    def test_is_failure_covers_exec_pass_false_and_failed_or_rejected_status(self):
        self.assertTrue(xs.retro_is_failure({"exec_pass": False, "status": "done"}))
        self.assertTrue(xs.retro_is_failure({"exec_pass": True, "status": "failed"}))
        self.assertTrue(xs.retro_is_failure({"exec_pass": True, "status": "rejected"}))
        self.assertFalse(xs.retro_is_failure({"exec_pass": True, "status": "done"}))
        self.assertFalse(xs.retro_is_failure({"exec_pass": None, "status": "done"}))
        self.assertFalse(xs.retro_is_failure({}))

    def test_rejected_covers_accepted_false_or_rejected_status(self):
        entries = [
            {"id": "not-accepted", "status": "done", "accepted": False},
            {"id": "status-rejected", "status": "rejected", "accepted": True},
            {"id": "no-accepted-field", "status": "done"},
            {"id": "accepted", "status": "done", "accepted": True},
        ]
        self.assertEqual([entry["id"] for entry in xs.retro_rejected(entries)],
                         ["not-accepted", "status-rejected"])

    def test_failure_counts_fall_back_to_last_attempt_error_class(self):
        entries = [
            {"id": "direct", "status": "failed", "error_class": "exec_error"},
            {"id": "attempts", "status": "failed",
             "attempts": [{"error_class": "timeout"}, {"error_class": "exec_error"}]},
            {"id": "blank-last-attempt", "status": "failed",
             "attempts": [{"error_class": "timeout"}, {"error_class": ""}]},
            {"id": "no-class", "status": "failed"},
            {"id": "no-class-attempts", "status": "failed", "attempts": [{"note": "no class"}]},
            {"id": "bad-attempts", "status": "failed", "attempts": "exec_error"},
            {"id": "passed", "status": "done", "error_class": "exec_error"},
        ]
        self.assertEqual(xs.retro_failure_counts(entries), [("exec_error", 2), ("timeout", 1)])

    def test_failure_counts_sorted_by_count_then_name(self):
        entries = [
            {"id": "1", "status": "failed", "error_class": "zeta"},
            {"id": "2", "status": "failed", "error_class": "alpha"},
            {"id": "3", "status": "failed", "error_class": "zeta"},
            {"id": "4", "status": "failed", "error_class": "beta"},
            {"id": "5", "status": "failed", "error_class": "alpha"},
        ]
        self.assertEqual(xs.retro_failure_counts(entries), [("alpha", 2), ("zeta", 2), ("beta", 1)])

    def test_ledger_fixture_classifies_failures_and_rejections(self):
        entries = list(xs.ledger_entries_from(xs.fleet_ledger_path()))
        self.assertEqual([entry["id"] for entry in entries], ["pass-1", "reject-2", "run-3"])
        self.assertEqual([entry["id"] for entry in entries if xs.retro_is_failure(entry)], ["reject-2"])
        self.assertEqual([entry["id"] for entry in xs.retro_rejected(entries)], ["reject-2"])
        self.assertEqual(xs.retro_failure_counts(entries), [("exec_error", 1)])

    def test_retro_ratio_empty_and_non_boolean_items_and_formatting(self):
        empty_prs = []
        self.assertEqual(xs.retro_ratio(empty_prs, "accepted"), "— (0/0)")
        non_booleans = [
            {"accepted": 1},
            {"accepted": 0},
            {"accepted": "true"},
            {"accepted": "false"},
            {"accepted": None},
            {"accepted": [True]},
            {},
        ]
        self.assertEqual(xs.retro_ratio(non_booleans, "accepted"), "— (0/0)")
        entries = [
            {"accepted": True},
            {"accepted": False},
            {"accepted": True},
            {"accepted": 1},
            {"accepted": "true"},
            {"accepted": None},
            {},
        ]
        self.assertEqual(xs.retro_ratio(entries, "accepted"), "2/3 (67%)")
        self.assertEqual(xs.retro_ratio([{"accepted": True}, {"accepted": True}], "accepted"), "2/2 (100%)")
        self.assertEqual(xs.retro_ratio([{"accepted": False}, {"accepted": False}], "accepted"), "0/2 (0%)")

    def test_retro_peer_review_p1_ratio_filters_corrupted_entries_and_counts_priority(self):
        empty_prs = []
        self.assertEqual(xs.retro_peer_review_p1_ratio(empty_prs), "— (0/0)")
        corrupted = [
            {},
            {"peer_review": None},
            {"peer_review": "approve"},
            {"peer_review": 123},
            {"peer_review": [1, 2, 3]},
            {"peer_review": {"p0": 0, "p1": 0}},
            {"peer_review": {"p1": 0, "p2": 0}},
            {"peer_review": {"p0": 0, "p2": 0}},
            {"peer_review": {"p0": "0", "p1": 0, "p2": 0}},
            {"peer_review": {"p0": True, "p1": 0, "p2": 0}},
            {"peer_review": {"p0": 0, "p1": False, "p2": 0}},
            {"peer_review": {"p0": -1, "p1": 0, "p2": 0}},
            {"peer_review": {"p0": 0, "p1": -2, "p2": 0}},
            {"peer_review": {"p0": 0, "p1": 0, "p2": -1}},
        ]
        self.assertEqual(xs.retro_peer_review_p1_ratio(corrupted), "— (0/0)")

        p2_only = [{"peer_review": {"p0": 0, "p1": 0, "p2": 5}}]
        self.assertEqual(xs.retro_peer_review_p1_ratio(p2_only), "0/1 (0%)")

        entries = [
            {"peer_review": {"p0": 1, "p1": 0, "p2": 0}},
            {"peer_review": {"p0": 0, "p1": 2, "p2": 0}},
            {"peer_review": {"p0": 1, "p1": 1, "p2": 0}},
            {"peer_review": {"p0": 0, "p1": 0, "p2": 3}},
            {"peer_review": {"p0": 0, "p1": 0, "p2": 0}},
            {"peer_review": "corrupted"},
            {"peer_review": {"p0": -1, "p1": 0, "p2": 0}},
            {},
        ]
        self.assertEqual(xs.retro_peer_review_p1_ratio(entries), "3/5 (60%)")

    def test_retro_exec_pass_ratio_counts_boolean_ratio_and_nulls(self):
        empty_prs = []
        self.assertEqual(xs.retro_exec_pass_ratio(empty_prs), "0/0; null=0")
        all_null = [
            {"exec_pass": None},
            {"exec_pass": "pass"},
            {"exec_pass": 1},
            {},
        ]
        self.assertEqual(xs.retro_exec_pass_ratio(all_null), "0/0; null=4")
        mixed = [
            {"exec_pass": True},
            {"exec_pass": False},
            {"exec_pass": True},
            {"exec_pass": None},
            {"exec_pass": "ignored"},
            {},
        ]
        self.assertEqual(xs.retro_exec_pass_ratio(mixed), "2/3 (67%); null=3")
        self.assertEqual(xs.retro_exec_pass_ratio([{"exec_pass": True}, {"exec_pass": True}]), "2/2 (100%); null=0")
        self.assertEqual(xs.retro_exec_pass_ratio([{"exec_pass": False}]), "0/1 (0%); null=0")

    def test_retro_safe_escapes_markdown_and_truncates_length(self):
        raw = "tag <script>alert(1)</script> | `code` [label](link) \\ path\r\nline2"
        expected = "tag &lt;script&gt;alert(1)&lt;/script&gt; \\| \\`code\\` \\[label\\](link) \\\\ path  line2"
        self.assertEqual(xs.retro_safe(raw), expected)

        self.assertEqual(xs.retro_safe(""), "-")
        self.assertEqual(xs.retro_safe(None), "-")

        long_str = "a" * 300
        self.assertEqual(len(xs.retro_safe(long_str)), 240)
        self.assertEqual(xs.retro_safe(long_str), "a" * 240)
        self.assertEqual(xs.retro_safe("a" * 240), "a" * 240)
        self.assertEqual(xs.retro_safe("a" * 239), "a" * 239)

        self.assertEqual(xs.retro_safe("abcdef", limit=3), "abc")
        self.assertEqual(xs.retro_safe("abcdef", limit=6), "abcdef")
        self.assertEqual(xs.retro_safe("abcdef", limit=10), "abcdef")
        self.assertEqual(xs.retro_safe("<>", limit=4), "&lt;")


if __name__ == "__main__":
    unittest.main()
