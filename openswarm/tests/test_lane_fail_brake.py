"""#1711 acceptance: B1 runtime projection, B2 supervisor status, B3 no added binary files."""
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "autodispatch", Path(__file__).resolve().parents[2] / "tools/lane-autodispatch/autodispatch.py")
AD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AD)


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
    assert AD.lane_brake_status()["CX9"]["paused"] is False


def test_brake_status_exposes_lane_count_and_recent_failure(tmp_path, monkeypatch):
    setup_brake(tmp_path, monkeypatch)
    for number in range(1, 4):
        AD.note_lane_failure("CX9", number, "exec_error", seconds=45, now=1000 + number)
    status = AD.status_snapshot({"CX9": {}}, {}, {"CX9": {"lane": "idle"}}, 0, 0)
    assert status["lane_brakes"]["CX9"]["count"] == 3
    assert status["lane_brakes"]["CX9"]["latest"]["issue"] == 3
    assert status["lane_brakes"]["CX9"]["latest"]["reason"] == "exec_error"
