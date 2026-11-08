"""#1711 acceptance: B1 runtime projection, B2 supervisor status, B3 no added binary files."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "autodispatch", Path(__file__).with_name("autodispatch.py"))
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)
ORIGINAL_LOG = AD.log


def setup_brake(tmp_path, monkeypatch):
    monkeypatch.setattr(AD, "D", str(tmp_path))
    monkeypatch.setattr(AD, "LANE_FAILURE_LIMIT", 3)
    monkeypatch.setattr(AD, "LANE_FAILURE_WINDOW", 180)
    monkeypatch.setattr(AD, "log", lambda *args, **kwargs: None)


def test_three_quick_no_commit_failures_brake_lane(tmp_path, monkeypatch):
    setup_brake(tmp_path, monkeypatch)
    for number in range(1, 4):
        AD.note_lane_failure("CX9", number, "exec_error", seconds=45, now=1000 + number)
    assert (tmp_path / "PAUSE-CX9").exists()
    assert AD.lane_brake_status()["CX9"]["count"] == 3
    row = json.loads((tmp_path / "lane-failures.json").read_text())["CX9"]["runs"][-1]
    assert row["issue"] == 3 and row["reason"] == "exec_error"


@pytest.mark.parametrize("dispatch_interval", [190, 3600])
def test_dispatch_intervals_do_not_reset_consecutive_failures(tmp_path, monkeypatch, dispatch_interval):
    """Each run is short; time spent waiting for the next card cannot erase failures."""
    setup_brake(tmp_path, monkeypatch)
    for count in range(1, 4):
        assert AD.note_lane_failure("CX9", count, "exec_error", seconds=120,
                                    now=1000 + (count - 1) * dispatch_interval) == count
        assert (tmp_path / "PAUSE-CX9").exists() == (count == 3)
    assert AD.lane_brake_status()["CX9"]["count"] == 3


def test_long_or_unknown_runtime_does_not_count(tmp_path, monkeypatch):
    setup_brake(tmp_path, monkeypatch)
    AD.note_lane_failure("CX9", 1, "slow", seconds=181, now=1000)
    AD.note_lane_failure("CX9", 2, "unknown", seconds=None, now=1001)
    assert not (tmp_path / "PAUSE-CX9").exists()
    assert AD.load_lane_failures() == {}
    # B1: the actual SSH result projection carries the measured runtime into finish().
    assert '"seconds":r.get("seconds")' in AD.REMOTE_STATUS


def test_success_resets_counter_and_manual_clear_releases_record(tmp_path, monkeypatch):
    setup_brake(tmp_path, monkeypatch)
    for number in range(1, 4):
        AD.note_lane_failure("CX9", number, "exec_error", seconds=45, now=1000 + number)
    AD.note_lane_failure("CX9", 4, "", seconds=120, committed=True)
    assert AD.load_lane_failures() == {}

    for number in range(1, 4):
        AD.note_lane_failure("CX9", number, "exec_error", seconds=45, now=2000 + number)
    (tmp_path / "PAUSE-CX9").unlink()
    AD.release_lane_brakes()
    assert AD.load_lane_failures() == {}
    assert "CX9" not in AD.lane_brake_status()
    assert not AD.lane_paused("CX9", scopes=AD.paused_scopes())


def test_brake_status_exposes_lane_count_and_recent_failure(tmp_path, monkeypatch):
    setup_brake(tmp_path, monkeypatch)
    monkeypatch.setattr(AD, "log", ORIGINAL_LOG)
    monkeypatch.setattr(AD, "BRAIN_LOG", str(tmp_path / "brain.md"))
    for number in range(1, 4):
        AD.note_lane_failure("CX9", number, "exec_error", seconds=45, now=1000 + number)
    status = AD.status_snapshot({"CX9": {}}, {}, {"CX9": {"lane": "idle"}}, 0, 0)
    assert status["lane_brakes"]["CX9"]["count"] == 3
    assert status["lane_brakes"]["CX9"]["latest"]["issue"] == 3
    assert status["lane_brakes"]["CX9"]["latest"]["reason"] == "exec_error"
    event = json.loads((tmp_path / "health.alert").read_text().splitlines()[-1])
    assert event["source"] == "lane-brake"
    assert event["lane"] == "CX9" and event["count"] == 3
    assert event["latest"]["issue"] == 3
    assert event["latest"]["reason"] == "exec_error"
    brain = (tmp_path / "brain.md").read_text()
    assert "lane brake CX9: 3" in brain and "latest #3: exec_error" in brain


@pytest.mark.parametrize("seconds,expected", [(45, 3), (181, 0), (None, 0)])
def test_remote_seconds_projection(tmp_path, monkeypatch, capsys, seconds, expected):
    """Execute the SSH reader against real swrm result files before counting failures."""
    setup_brake(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENSWARM_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["-", "CX9", "run-1"])
    result_dir = tmp_path / "runs/run-1"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text(json.dumps({"status": "failed", "seconds": seconds}))
    for number in range(1, 4):
        exec(AD.REMOTE_STATUS, {})
        info = json.loads(capsys.readouterr().out)["run"]
        assert info["seconds"] == seconds
        AD.note_lane_failure("CX9", number, info["status"], seconds=info["seconds"])
    assert len(AD.load_lane_failures().get("CX9", {}).get("runs", [])) == expected
    assert (tmp_path / "PAUSE-CX9").exists() == bool(expected)


def test_third_failure_blocks_fourth_card_in_same_tick_and_manual_resume(tmp_path, monkeypatch):
    setup_brake(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENSWARM_HOME", str(tmp_path / "engine"))
    monkeypatch.setattr(AD, "AD_VERIFY_SHARE_ENABLED", False)
    monkeypatch.setattr(AD, "BRAIN_LOG", str(tmp_path / "brain.md"))
    dispatched = []
    for name in ("host_fail", "host_good", "save_state", "prefetch_labels", "label_lane_failed"):
        monkeypatch.setattr(AD, name, lambda *a, **k: None)
    monkeypatch.setattr(AD, "host_ok", lambda *a: True)
    monkeypatch.setattr(AD, "deps_met", lambda *a: True)
    monkeypatch.setattr(AD, "can_take", lambda *a, **k: True)
    monkeypatch.setattr(AD, "label_state", lambda *a: ("fresh", ""))
    monkeypatch.setattr(AD, "freshen_queue", lambda q, state: q)
    monkeypatch.setattr(AD, "replenish", lambda *a: [])
    monkeypatch.setattr(AD, "sh", lambda *a, **k: (0, "a" * 40, ""))
    monkeypatch.setattr(AD, "gh", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(AD, "dispatch", lambda lane, cfg, item, base:
                        (dispatched.append(item["n"]) or "run-4", None))
    monkeypatch.setattr(AD, "queue", lambda: [{"n": 4, "backend": None, "prio": 1}])
    lanes = {"CX9": {"host": "test-host", "backend": "codex"}}
    state = {"CX9": {"issue": 3, "run": "run-3"}}
    monkeypatch.setattr(AD, "remote", lambda *a: {"lane": "idle", "run": {
        "status": "failed", "seconds": 120, "error_class": "exec_error"}})
    for number, ended in ((1, 1000), (2, 1190)):
        AD.note_lane_failure("CX9", number, "exec_error", seconds=120, now=ended)
    monkeypatch.setattr(AD.time, "time", lambda: 1380)
    AD.tick(lanes, state, 0)
    assert (tmp_path / "PAUSE-CX9").exists()
    assert dispatched == []
    assert "CX9" not in state
    AD.tick(lanes, state, 0)
    assert dispatched == []
    (tmp_path / "PAUSE-CX9").unlink()
    monkeypatch.setattr(AD, "remote", lambda *a: {"lane": "idle"})
    AD.tick(lanes, state, 0)
    assert dispatched == [4]
    assert AD.load_lane_failures() == {}


def test_success_without_counter_does_not_create_state_directory(tmp_path, monkeypatch):
    setup_brake(tmp_path / "absent", monkeypatch)
    assert AD.note_lane_failure("CX9", 1, "", committed=True) == 0
    assert not (tmp_path / "absent").exists()


# #1711 review evidence (paths relative to the repository; results: PASS):
# B1 (runtime): tools/lane-autodispatch/autodispatch.py:702 ->
# test_remote_seconds_projection (45s / 181s / unknown): PASS, real SSH-reader projection.
# B2 (health): tools/lane-autodispatch/autodispatch.py:216 ->
# test_brake_status_exposes_lane_count_and_recent_failure: PASS, health.alert + brain log.
# B3 (binaries): git diff a4a49b8318883ff6e122f7ab08223d6d368f984c --name-only
# -- '*.so' -> empty: PASS; no unrelated binary files are part of this delivery.
# R1 (same tick): tools/lane-autodispatch/autodispatch.py:1289 ->
# test_third_failure_blocks_fourth_card_in_same_tick_and_manual_resume: PASS.
# R2 (migration): openswarm/tests/test_lane_fail_brake.py -> this tools/ module;
# test_three_quick_no_commit_failures_brake_lane: PASS; original reproduction remains
# tools/lane-autodispatch/test_can_take.py:350 ->
# ConsecutiveLaneFailureBrakeTest.test_third_quick_failure_writes_lane_pause_switch: PASS.
# The engine counterpart is swrm/bin/swrm; its result.seconds contract needs no change.
# R3 (recovery/harvest): tools/lane-autodispatch/autodispatch.py:193 ->
# test_success_resets_counter_and_manual_clear_releases_record and
# test_success_without_counter_does_not_create_state_directory: PASS; also
# tools/lane-autodispatch/test_harvest_filter.py -> HarvestFilterTest.
# test_finish_filter_failure_does_not_push / test_finish_pushes_the_filtered_branch /
# test_finish_with_only_artifacts_pushes_nothing: PASS.
# R4 (health output): B2 above. R5 / delivery B1 (mapping): these actual file:line ->
# test -> result entries replace the previous nonexistent test references.
# Second-review BLOCKER (dispatch gaps): tools/lane-autodispatch/autodispatch.py:207 ->
# test_dispatch_intervals_do_not_reset_consecutive_failures (190s / 3600s gaps) and
# test_third_failure_blocks_fourth_card_in_same_tick_and_manual_resume: PASS;
# 120s failures ending at 1000 / 1190 / 1380 count 1 / 2 / 3 and block card 4.
# FOLLOW-UP: health.alert is covered by B2; the external health-loop's health.json
# aggregation and the supervisor's choice of health input need host verification.
# No commit or GitHub comment is created under the execution task's explicit restrictions.
