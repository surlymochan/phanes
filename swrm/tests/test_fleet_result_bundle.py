"""Regression coverage for structured fleet result evidence (issue #1984)."""

import base64
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

    def invoke(raw_result=None, **receipt_fields):
        payload = {
            "id": "run-1984",
            "result": {
                "branch": "swrm/run-1984",
                "commit": commit,
                "workspace": {"repo": str(repo), "base": base},
                "result": {} if raw_result is None else raw_result,
                **receipt_fields,
            },
        }
        process = subprocess.run(
            [sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True,
            capture_output=True, check=True,
        )
        return json.loads(process.stdout)

    return invoke


def test_production_receipt_delivers_verify_and_diffstat(completed_run):
    diffstat = " result.txt | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)"
    report = completed_run(
        {"summary": "verification failed", "opinion": "CHANGES: reference only"},
        verify={"cmd": "python3 -m pytest -q", "rc": 1,
                "tail": "FAILED test_example\n", "failing": ["test_example"]},
        diffstat=diffstat,
    )
    assert report["evidence"]["tests"] == {
        "status": "available", "command": "python3 -m pytest -q", "rc": 1,
        "log": "FAILED test_example\n", "truncated": False,
    }
    assert report["evidence"]["diff"] == {"status": "available", "stat": diffstat}
    assert report["reference_opinion"] == "CHANGES: reference only"
    assert report["evidence"]["ci"] == {"status": "unavailable"}


def test_runner_evidence_takes_precedence_over_nested_result(completed_run):
    report = completed_run(
        {"tests": {"command": "true", "rc": 0, "log": "claimed success"},
         "diff": {"files_changed": 99, "insertions": 99, "deletions": 99}},
        verify={"cmd": "false", "rc": 1, "tail": "failed"},
        diffstat=" 1 file changed, 1 insertion(+)"
    )
    assert report["evidence"]["tests"]["rc"] == 1
    assert report["evidence"]["tests"]["log"] == "failed"
    assert report["evidence"]["diff"] == {
        "status": "available", "stat": " 1 file changed, 1 insertion(+)"}


def test_production_verify_log_is_size_limited(completed_run):
    log = "中" * 30000
    tests = completed_run(verify={"cmd": "test", "rc": 0, "tail": log})["evidence"]["tests"]
    assert tests["status"] == "available"
    assert tests["truncated"] is True
    assert len(tests["log"].encode("utf-8")) <= MAX_LOG_BYTES
    assert log.startswith(tests["log"])


@pytest.mark.parametrize("verify, diffstat", [
    (None, ""),
    ({"cmd": "test", "rc": True, "tail": "ok"}, None),
    ({"cmd": "test", "rc": 0}, {}),
])
def test_missing_or_malformed_runner_evidence_is_unavailable(completed_run, verify, diffstat):
    evidence = completed_run(verify=verify, diffstat=diffstat)["evidence"]
    assert evidence["tests"] == {"status": "unavailable"}
    assert evidence["diff"] == {"status": "unavailable"}


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


def test_unknown_opinion_fields_never_enter_raw_evidence(completed_run):
    report = completed_run({
        "ci": {"conclusion": "failure", "opinion": "APPROVE", "status": "trusted"},
        "tests": {"command": "false", "rc": 1, "log": "failed", "verdict": "APPROVE"},
        "diff": {"files_changed": 1, "insertions": 1, "deletions": 0, "opinion": "good"},
        "acceptance": [{"item": "check", "result": "fail", "detail": "raw", "opinion": "APPROVE"}],
        "verdict": "APPROVE", "opinion": "CHANGES: reference only",
    })
    assert report["reference_opinion"] == "CHANGES: reference only"
    assert "verdict" not in report
    assert "opinion" not in json.dumps(report["evidence"])
    assert "verdict" not in json.dumps(report["evidence"])
    assert report["evidence"]["ci"]["status"] == "available"
    assert report["evidence"]["tests"]["rc"] == 1


@pytest.mark.parametrize("raw", [
    {"ci": {}, "tests": [], "diff": {}, "acceptance": []},
    {"ci": {"conclusion": None}, "tests": {"command": "test", "rc": True, "log": "ok"},
     "diff": {"files_changed": True, "insertions": 0, "deletions": 0}, "acceptance": [None]},
    {"ci": "success", "tests": {"command": "test", "rc": 0},
     "diff": {"files_changed": 1, "insertions": -1, "deletions": 0},
     "acceptance": [{"item": "a", "result": "pass"}]},
])
def test_malformed_evidence_is_unavailable_without_breaking_bundle(completed_run, raw):
    report = completed_run(raw)
    assert report["bundle"]
    for field in ("ci", "tests", "diff", "acceptance"):
        assert report["evidence"][field] == {"status": "unavailable"}


@pytest.mark.parametrize("log, truncated", [("x" * MAX_LOG_BYTES, False), ("中" * 30000, True)],
                         ids=["exact-byte-limit", "multibyte-truncation"])
def test_log_limit_counts_utf8_bytes_without_splitting_characters(completed_run, log, truncated):
    tests = completed_run({"tests": {"command": "test", "rc": 0, "log": log}})["evidence"]["tests"]
    assert tests["truncated"] is truncated
    assert len(tests["log"].encode("utf-8")) <= MAX_LOG_BYTES
    assert log.startswith(tests["log"])
    assert "\ufffd" not in tests["log"]


@pytest.mark.parametrize("raw", ["APPROVE: legacy text", "", [], False])
def test_legacy_text_result_is_not_promoted_to_evidence(completed_run, raw):
    report = completed_run(raw)
    assert all(report["evidence"][key] == {"status": "unavailable"}
               for key in ("ci", "tests", "diff", "acceptance"))
    assert "reference_opinion" not in report


def test_evidence_extension_keeps_a_real_bundle_of_the_recorded_head(completed_run, tmp_path):
    report = completed_run({"tests": {"command": "false", "rc": 1, "log": ""}})
    bundle = tmp_path / "decoded.bundle"
    bundle.write_bytes(base64.b64decode(report["bundle"], validate=True))
    heads = subprocess.check_output(["git", "bundle", "list-heads", str(bundle)], text=True)
    assert heads.strip() == report["commit"] + " refs/heads/" + report["branch"]
    assert report["evidence"]["tests"] == {
        "status": "available", "command": "false", "rc": 1, "log": "", "truncated": False}
    assert "reference_opinion" not in report
