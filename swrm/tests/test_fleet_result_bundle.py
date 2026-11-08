"""Regression coverage for structured fleet result evidence (issue #1984)."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "bin" / "fleet-result-bundle"
MAX_LOG_BYTES = 64 * 1024


@pytest.fixture
def completed_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "result.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "result.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "swrm/run-1984"], check=True)
    (repo / "result.txt").write_text("result\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "result"], check=True)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def invoke(raw_result=None):
        payload = {
            "id": "run-1984",
            "result": {
                "branch": "swrm/run-1984",
                "commit": commit,
                "workspace": {"repo": str(repo), "base": base},
                "result": raw_result or {},
            },
        }
        process = subprocess.run(
            [sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True,
            capture_output=True, check=True,
        )
        return json.loads(process.stdout)

    return invoke


def test_complete_result_carries_all_four_raw_evidence_classes(completed_run):
    result = completed_run({
        "ci": {"conclusion": "success", "url": "https://ci.example.invalid/42"},
        "tests": {"command": "pytest -q", "rc": 0, "log": "2 passed\n"},
        "diff": {"files_changed": 2, "insertions": 14, "deletions": 3},
        "acceptance": [{"item": "bundle is readable", "result": "pass", "detail": "git bundle verify: ok"}],
        "opinion": "APPROVE: looks good",
    })

    evidence = result["evidence"]
    assert evidence["version"] == 1
    assert evidence["ci"] == {"status": "available", "conclusion": "success", "url": "https://ci.example.invalid/42"}
    assert evidence["tests"] == {"status": "available", "command": "pytest -q", "rc": 0, "log": "2 passed\n", "truncated": False}
    assert evidence["diff"] == {"status": "available", "files_changed": 2, "insertions": 14, "deletions": 3}
    assert evidence["acceptance"] == {"status": "available", "items": [{"item": "bundle is readable", "result": "pass", "detail": "git bundle verify: ok"}]}
    assert result["reference_opinion"] == "APPROVE: looks good"
    assert "opinion" not in json.dumps(evidence).lower()


def test_missing_evidence_is_explicitly_unavailable(completed_run):
    evidence = completed_run({"ci": {"conclusion": "failure"}})["evidence"]

    assert evidence["ci"]["status"] == "available"
    for field in ("tests", "diff", "acceptance"):
        assert evidence[field] == {"status": "unavailable"}


def test_test_log_is_size_limited_and_marked_truncated(completed_run):
    evidence = completed_run({"tests": {"command": "pytest -q", "rc": 1, "log": "x" * (MAX_LOG_BYTES + 1000)}})["evidence"]

    assert evidence["tests"]["truncated"] is True
    assert len(evidence["tests"]["log"].encode("utf-8")) <= MAX_LOG_BYTES


def test_legacy_result_remains_bundle_compatible(completed_run):
    result = completed_run({"summary": "legacy completed result"})

    assert result["branch"] == "swrm/run-1984"
    assert result["commit"]
    assert result["bundle"]
    assert result["evidence"] == {
        "version": 1,
        "ci": {"status": "unavailable"},
        "tests": {"status": "unavailable"},
        "diff": {"status": "unavailable"},
        "acceptance": {"status": "unavailable"},
    }
    assert "reference_opinion" not in result
