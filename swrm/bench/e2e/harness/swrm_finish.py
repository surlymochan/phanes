#!/usr/bin/env python3
"""Read an swrm run result, record executor token usage, merge the branch.

Usage: swrm_finish.py <stdout-path> <events-dir> <workdir>
Exit status is 0 when the branch was merged or the run already landed in place.
A missing branch after a failed run exits 1. This script does not call a model.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.metrics import iter_json_events, looks_like_usage, normalize_usage  # noqa: E402


def session_from_events(path: str | None) -> str | None:
    if not path:
        return None
    source = Path(path)
    if not source.is_file():
        return None
    for event in iter_json_events(source.read_text(encoding="utf-8", errors="replace")):
        if event.get("session_id"):
            return event["session_id"]
    return None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=bench-e2e@local",
            "-c",
            "user.name=bench-e2e",
            *args,
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def extract_result_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    found = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if _is_result_receipt(obj):
            found = obj
    return found


def _is_result_receipt(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("id"), str) or not obj["id"].strip():
        return False
    terminal_statuses = ("done", "partial", "failed", "stopped", "budget_exceeded")
    if "status" in obj and obj["status"] not in terminal_statuses:
        return False
    run_dir = obj.get("run_dir")
    workspace = obj.get("workspace")
    has_run_context = (
        (isinstance(run_dir, str) and bool(run_dir.strip()))
        or (isinstance(workspace, dict) and workspace.get("mode") in ("worktree", "in-place", "github-issue"))
    )
    if obj.get("status") in terminal_statuses and has_run_context:
        return True
    # A statusless result can still be recognized by its run context. Without
    # that context, require the top-level usage summary as well as attempts;
    # telemetry events may carry attempt metadata but are not run receipts.
    if "status" not in obj and has_run_context:
        return True
    attempts = obj.get("attempts")
    has_executor_attempts = isinstance(attempts, list) and bool(attempts) and all(
        isinstance(attempt, dict)
        and isinstance(attempt.get("backend"), str)
        and bool(attempt["backend"].strip())
        and "model" in attempt
        and isinstance(attempt.get("meta"), dict)
        for attempt in attempts
    )
    if not has_executor_attempts:
        return False
    return obj.get("status") in terminal_statuses or looks_like_usage(obj.get("usage"))


def executor_calls(result: dict) -> list[dict]:
    attempt_calls = []
    for attempt in result.get("attempts") or []:
        meta = attempt.get("meta") or {}
        usage = meta.get("usage")
        if not looks_like_usage(usage):
            continue
        attempt_calls.append(
            {
                "source": "swrm-result:attempts.meta.usage",
                "session_id": meta.get("session_id") or meta.get("thread_id") or session_from_events(attempt.get("events")),
                "model": attempt.get("model"),
                "usage": normalize_usage(usage),
                "missing_usage": False,
                "saw_result": True,
            }
        )
    if attempt_calls:
        return attempt_calls
    usage = result.get("usage")
    if looks_like_usage(usage):
        return [
            {
                "source": "swrm-result:usage",
                "session_id": result.get("session_id"),
                "model": result.get("model"),
                "usage": normalize_usage(usage),
                "missing_usage": False,
                "saw_result": True,
            }
        ]
    return []


def copy_raw_events(result: dict, events: Path) -> None:
    for index, attempt in enumerate(result.get("attempts") or [], start=1):
        path = attempt.get("events")
        if not path:
            continue
        source = Path(path)
        if source.is_file():
            shutil.copyfile(source, events / f"raw-executor-{index}.jsonl")


def merge_branch(repo: Path, result: dict) -> tuple[bool, str]:
    run_id = result.get("id")
    if not run_id:
        return False, "result has no id"
    branch = f"swrm/{run_id}"
    mode = ((result.get("workspace") or {}).get("mode")) or ""
    exists = _git(repo, "rev-parse", "--verify", "--quiet", branch).returncode == 0
    if mode == "in-place":
        return True, "executor ran in place; left the worktree as-is"
    if not exists:
        return False, f"branch {branch} does not exist"
    _git(repo, "reset", "--hard", "HEAD")
    _git(repo, "clean", "-fd")
    merged = _git(repo, "merge", "--no-ff", "-m", f"merge {branch}", branch)
    detail = (merged.stdout or "") + (merged.stderr or "")
    return merged.returncode == 0, detail.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: swrm_finish.py <stdout> <events-dir> <workdir>", file=sys.stderr)
        return 2
    stdout_path, events, workdir = Path(argv[1]), Path(argv[2]), Path(argv[3])
    text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    result = extract_result_object(text)
    if result is None:
        run_dir = None
    else:
        run_dir = result.get("run_dir")
    if run_dir:
        result_path = Path(run_dir) / "result.json"
        if result_path.is_file():
            shutil.copyfile(result_path, events / "swrm-result.json")
            try:
                loaded = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                result = loaded
    calls = executor_calls(result or {})
    (events / "executor-usage.json").write_text(
        json.dumps({"calls": calls}, indent=2) + "\n",
        encoding="utf-8",
    )
    if result:
        copy_raw_events(result, events)
        ok, detail = merge_branch(workdir, result)
        (events / "merge.txt").write_text(detail + "\n", encoding="utf-8")
        return 0 if ok else 1
    (events / "merge.txt").write_text("no swrm result object on stdout\n", encoding="utf-8")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
