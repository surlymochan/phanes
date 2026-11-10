"""CLI and result receipt edge cases for swrm_finish."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FINISH = ROOT / "harness" / "swrm_finish.py"
sys.path.insert(0, str(ROOT))

from harness.swrm_finish import extract_result_object  # noqa: E402

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Bench E2E",
    "GIT_AUTHOR_EMAIL": "bench-e2e@example.invalid",
    "GIT_COMMITTER_NAME": "Bench E2E",
    "GIT_COMMITTER_EMAIL": "bench-e2e@example.invalid",
}


def test_receipt_ignores_unrelated_trailing_json_object():
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
        "run_dir": "/tmp/swrm/runs/run-123",
    }
    telemetry = {"id": "metrics-456", "event": "usage", "count": 7}
    stdout = "starting run\n" + json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt


def test_receipt_ignores_trailing_telemetry_with_attempt_metadata():
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
        "run_dir": "/tmp/swrm/runs/run-123",
    }
    telemetry = {
        "id": "metrics-456",
        "event": "usage",
        "attempts": [
            {
                "backend": "cursor",
                "model": "grok",
                "meta": {"usage": {"inputTokens": 8}},
            }
        ],
    }
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize(
    "run_context",
    [
        {"workspace": {"mode": "worktree"}},
        {"run_dir": "/tmp/swrm/runs/metrics-456"},
    ],
)
def test_receipt_ignores_trailing_statusless_telemetry_with_run_context(run_context):
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
    }
    telemetry = {"id": "metrics-456", "event": "usage", "count": 7, **run_context}
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


def test_receipt_keeps_support_for_multiple_stdout_objects():
    receipt = {
        "id": "run-789",
        "status": "failed",
        "run_dir": "/tmp/swrm/runs/run-789",
        "attempts": [],
    }
    stdout = "noise\n" + json.dumps({"ok": True}) + "\n" + json.dumps(receipt)

    assert extract_result_object(stdout) == receipt


def test_receipt_requires_run_result_shape():
    telemetry = {"id": "metrics-456", "status": "usage", "event": "usage"}

    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize("result", ["ok", {"tokens": 7}, None])
def test_receipt_ignores_trailing_log_with_result_field(result):
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
    }
    telemetry = {"id": "metrics-456", "event": "usage", "result": result}
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"attempts": []},
        {"attempts": [], "status": "done"},
        {"attempts": [{"retry": 1}]},
        {"attempts": [None]},
        {"workspace": {}},
        {"run_dir": ""},
        {"run_dir": "   "},
        {"status": "usage", "run_dir": "/tmp/metrics"},
    ],
)
def test_receipt_ignores_trailing_retry_metrics(metadata):
    receipt = {
        "id": "run-123",
        "status": "done",
        "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
    }
    telemetry = {"id": "metrics-456", "event": "retry_metrics", **metadata}
    stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

    assert extract_result_object(stdout) == receipt
    assert extract_result_object(json.dumps(telemetry)) is None


@pytest.mark.parametrize("status", ["done", "partial", "failed", "stopped", "budget_exceeded"])
def test_receipt_returns_last_run_result(status):
    receipt = {"id": "run-123", "status": status, "run_dir": "/tmp/swrm/runs/run-123"}
    stdout = json.dumps({**receipt, "id": "previous-run"}) + "\n" + json.dumps(receipt)

    assert extract_result_object(stdout) == receipt


def test_unroutable_receipt_without_run_context_wins_over_previous_receipt():
    previous = {"id": "previous", "status": "done", "run_dir": "runs/previous"}
    receipt = {"id": "current", "status": "unroutable", "scope": "task",
               "decision": {"ok": False, "reasons": [], "skipped": {},
                            "planner": {}, "profile": {}}}
    telemetry = {"id": "metrics", "event": "usage", "run_dir": "runs/current"}
    assert extract_result_object(json.dumps(receipt)) == receipt
    assert extract_result_object("\n".join(map(json.dumps, [previous, receipt, telemetry]))) == receipt
    assert extract_result_object(json.dumps({"id": "metrics", "status": "unroutable"})) is None


class SwrmFinishReceiptTests(unittest.TestCase):
    """#2108: a telemetry object must never replace a real run receipt."""

    def git(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            text=True,
            capture_output=True,
            env={**os.environ, **GIT_IDENTITY},
        )

    def test_telemetry_with_terminal_status_does_not_replace_the_receipt(self):
        receipt = {
            "id": "run-123",
            "status": "done",
            "workspace": {"mode": "worktree", "path": "/tmp/run-123"},
            "run_dir": "/tmp/swrm/runs/run-123",
        }
        telemetry = {
            "id": "metrics-456",
            "event": "usage",
            "status": "done",
            "run_dir": "/tmp/swrm/runs/run-123",
        }
        stdout = json.dumps(receipt) + "\n" + json.dumps(telemetry)

        self.assertEqual(extract_result_object(stdout), receipt)
        self.assertIsNone(extract_result_object(json.dumps(telemetry)))

    def test_latest_valid_receipt_wins_over_older_richer_receipt(self):
        old = {"id": "old", "status": "done", "run_dir": "runs/old",
               "workspace": {"mode": "worktree"},
               "attempts": [{"backend": "cursor", "model": "model", "meta": {}}]}
        new = {"id": "new", "status": "done", "run_dir": "runs/new"}

        self.assertEqual(extract_result_object(json.dumps(old) + "\n" + json.dumps(new)), new)

    def test_statusless_null_attempt_is_not_a_receipt(self):
        self.assertIsNone(extract_result_object(json.dumps({"id": "metrics", "attempts": [None]})))

    def test_finish_merges_the_receipt_branch_not_the_telemetry_branch(self):
        with tempfile.TemporaryDirectory() as temp:
            tempdir = Path(temp)
            repo = tempdir / "repo"
            repo.mkdir()
            self.git(repo, "init", "-q")
            (repo / "base.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-qm", "base")
            self.git(repo, "checkout", "-qb", "swrm/run-123")
            (repo / "merged.txt").write_text("merged\n", encoding="utf-8")
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-qm", "run-123 work")
            self.git(repo, "checkout", "-q", "-")

            receipt = {
                "id": "run-123",
                "status": "done",
                "workspace": {"mode": "worktree", "path": str(repo)},
                "run_dir": str(tempdir / "runs" / "run-123"),
            }
            telemetry = {
                "id": "metrics-456",
                "event": "usage",
                "status": "done",
                "run_dir": receipt["run_dir"],
            }
            stdout = tempdir / "stdout.txt"
            stdout.write_text(
                json.dumps(receipt) + "\n" + json.dumps(telemetry) + "\n",
                encoding="utf-8",
            )
            events = tempdir / "events"
            events.mkdir()

            proc = subprocess.run(
                [sys.executable, str(FINISH), str(stdout), str(events), str(repo)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, **GIT_IDENTITY},
            )

            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((repo / "merged.txt").is_file())
            subject = self.git(repo, "log", "-1", "--pretty=%s").stdout.strip()
            self.assertEqual(subject, "merge swrm/run-123")
            telemetry_branch = self.git(
                repo, "rev-parse", "--verify", "--quiet", "swrm/metrics-456", check=False
            )
            self.assertNotEqual(telemetry_branch.returncode, 0)


class SwrmFinishEdgeTests(unittest.TestCase):
    def test_creates_missing_nested_events_directory_before_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            tempdir = Path(temp)
            stdout = tempdir / "stdout.txt"
            stdout.write_text("no result here\n", encoding="utf-8")
            events = tempdir / "nested" / "events"
            workdir = tempdir / "worktree"
            workdir.mkdir()

            proc = subprocess.run(
                [sys.executable, str(FINISH), str(stdout), str(events), str(workdir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertTrue(events.is_dir())
            self.assertEqual(json.loads((events / "executor-usage.json").read_text(encoding="utf-8")), {"calls": []})
            self.assertEqual((events / "merge.txt").read_text(encoding="utf-8"), "no swrm result object on stdout\n")

    def test_creates_no_extra_structure_for_existing_events_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            tempdir = Path(temp)
            stdout = tempdir / "stdout.txt"
            stdout.write_text("no result here\n", encoding="utf-8")
            events = tempdir / "events"
            events.mkdir()
            workdir = tempdir / "worktree"
            workdir.mkdir()

            proc = subprocess.run(
                [sys.executable, str(FINISH), str(stdout), str(events), str(workdir)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertEqual(
                {path.name for path in events.iterdir()},
                {"executor-usage.json", "merge.txt"},
            )


if __name__ == "__main__":
    unittest.main()
