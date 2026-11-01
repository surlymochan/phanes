"""Rerun a failed CI run once, and only once (Refs #1445, part of #1432).

A pull request whose CI failed is often worth a second chance: a flaky job, a
timeout, a runner that never started.  But rerunning a failing job forever turns
a red PR into an endless loop, so every failed run is rerun *once* and never
again.  The memory of that decision lives in a small JSON ledger, the
:class:`RerunLedger`::

    {"runs": {"1234567": {"pr": 1445, "at": 1730000000.0}}}

The ledger is written atomically (temporary file + ``os.replace``), so a crashed
writer never leaves a half written file behind.  A missing or damaged ledger is
not fatal: it then reads as empty, which means "nothing was rerun yet", so the
module keeps working instead of blocking a merge.

The module knows nothing about GitHub beyond the shape of the ``gh`` output it
consumes, so it can be unit tested offline:

``select_reruns(runs, ledger)``
    A pure selection: which failed runs may be rerun once.

``rerun_once(pr, runs, ledger, ...)``
    The action: run ``gh run rerun <id> --failed`` for the selected runs and
    remember the ones that really went through.

The wiring into the merge loop stays with the parent card (#1432).

``openswarm/lib/merge/`` is a namespace package without an ``__init__.py``, so
this module is loaded by path (see ``openswarm/tests/test_merge_rerun.py``).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

# The conclusions of `gh run list` that mean "the CI of this PR failed".
FAILURE_CONCLUSIONS = ("failure", "timed_out", "startup_failure")

# The default ignore pattern: a failing macOS job is a known local problem (no
# macOS runner in the fleet), so it must not be rerun.
DEFAULT_IGNORE_RE = r"(?i)macos"


def _key(run_id: Any) -> str:
    """The text form of a run id; the ledger is keyed by text."""
    if isinstance(run_id, bool) or not isinstance(run_id, (int, str)):
        return ""
    return str(run_id).strip()


def _number(value: Any, name: str) -> float:
    """A finite float; the clock of the caller is always passed in."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{} must be a number, got {!r}".format(name, value))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("{} must be finite, got {!r}".format(name, value))
    return number


def _at_of(record: Any) -> float:
    """The timestamp of a ledger record; a damaged one counts as ancient."""
    if not isinstance(record, dict):
        return float("-inf")
    at = record.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return float("-inf")
    return float(at)


class RerunLedger:
    """The runs that were already rerun once, in one JSON file.

    The file holds one record per run::

        {"runs": {"1234567": {"pr": 1445, "at": 1730000000.0}}}

    A missing or damaged file is not an error: the ledger then reads as empty,
    so every failed run looks like it was never rerun.  The file is written
    atomically (temporary file + ``os.replace``).
    """

    def __init__(self, path: Any) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ read

    def load(self) -> Dict[str, Dict[str, Any]]:
        """Every record of the ledger, keyed by run id (empty when damaged)."""
        try:
            with open(self.path, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        runs = payload.get("runs")
        if not isinstance(runs, dict):
            return {}
        return {str(key): dict(row) for key, row in runs.items() if isinstance(row, dict)}

    def seen(self, run_id: Any) -> bool:
        """Whether the run was already rerun once."""
        key = _key(run_id)
        if not key:
            return False
        return key in self.load()

    # ----------------------------------------------------------------- write

    def record(self, run_id: Any, pr: Any, now: Any) -> Dict[str, Any]:
        """Remember that the run of `pr` was rerun at `now`."""
        key = _key(run_id)
        if not key:
            raise ValueError("run_id must not be empty")
        records = self.load()
        record = {"pr": pr, "at": _number(now, "now")}
        records[key] = record
        self._write(records)
        return dict(record)

    def prune(self, older_than_s: Any, now: Any) -> List[str]:
        """Drop the records older than `older_than_s` seconds before `now`.

        The removed run ids are returned.  The file is only rewritten when a
        record was really dropped, so a prune that finds nothing leaves the
        file (and its timestamp) alone.
        """
        window = _number(older_than_s, "older_than_s")
        cutoff = _number(now, "now") - window
        records = self.load()
        stale = [key for key, row in records.items() if _at_of(row) < cutoff]
        if not stale:
            return []
        for key in stale:
            records.pop(key)
        self._write(records)
        return stale

    def _write(self, records: Dict[str, Dict[str, Any]]) -> None:
        """Write the ledger atomically (temporary file + ``os.replace``)."""
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"runs": records}
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(directory),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        temporary = handle.name
        try:
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _matcher(ignore_re: Any):
    """The compiled ignore pattern; None disables the filter.

    A broken pattern is a user error: it is reported as a ``ValueError`` (with
    the reason) instead of letting a raw ``re.error`` escape to the caller.
    """
    if ignore_re is None:
        return None
    if hasattr(ignore_re, "search"):
        return ignore_re
    try:
        return re.compile(ignore_re)
    except re.error as error:
        raise ValueError("invalid ignore pattern {!r}: {}".format(ignore_re, error)) from error


def _candidates(runs: Any, ledger: Any, ignore_re: Any = DEFAULT_IGNORE_RE) -> List[Dict[str, Any]]:
    """Judge every run of `runs`: may it be rerun once?

    One row per distinct run id, in the order of the first appearance: a run id
    that shows up twice is only judged once.  The rows carry the reason of the
    verdict, so the caller can explain a run it left alone.
    """
    matcher = _matcher(ignore_re)
    decided: List[Dict[str, Any]] = []
    judged = set()
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        run_id = run.get("databaseId")
        key = _key(run_id)
        if not key or key in judged:
            continue
        judged.add(key)
        name = str(run.get("name") or "")
        conclusion = str(run.get("conclusion") or "").strip().lower()
        candidate: Dict[str, Any] = {
            "run_id": run_id,
            "name": name,
            "conclusion": conclusion,
            "head_sha": run.get("headSha"),
            "selected": False,
            "reason": "",
        }
        if conclusion not in FAILURE_CONCLUSIONS:
            candidate["reason"] = "conclusion '{}' is not a failure".format(conclusion or "none")
        elif matcher is not None and matcher.search(name):
            candidate["reason"] = "name '{}' matches the ignore pattern".format(name)
        elif ledger is not None and ledger.seen(key):
            candidate["reason"] = "run {} was already rerun".format(key)
        else:
            candidate["selected"] = True
            candidate["reason"] = "conclusion '{}' is a rerunnable failure".format(conclusion)
        decided.append(candidate)
    return decided


def select_reruns(runs: Any, ledger: Any, ignore_re: Any = DEFAULT_IGNORE_RE) -> List[Dict[str, Any]]:
    """Pick the failed runs of `runs` that were not rerun yet.

    `runs` are the rows of ``gh run list --json databaseId,name,conclusion,headSha``.
    A run is picked when its conclusion is a failure (``failure``, ``timed_out``
    or ``startup_failure``), when its name does not match `ignore_re` and when
    the ledger does not know it yet.  The same run id is picked only once, even
    when the list holds it twice.

    The selection is pure: it reads the ledger but changes nothing.  Every
    picked row has the keys ``run_id``, ``name``, ``conclusion`` and
    ``head_sha``.
    """
    return [
        {
            "run_id": candidate["run_id"],
            "name": candidate["name"],
            "conclusion": candidate["conclusion"],
            "head_sha": candidate["head_sha"],
        }
        for candidate in _candidates(runs, ledger, ignore_re)
        if candidate["selected"]
    ]


def _rerun_command(run_id: Any) -> List[str]:
    """The command that reruns the failed jobs of one run."""
    return ["gh", "run", "rerun", str(run_id), "--failed"]


def _returncode(result: Any) -> int:
    """The exit code of a runner result; a broken result counts as a failure."""
    if isinstance(result, dict):
        code = result.get("returncode", 0)
    else:
        code = getattr(result, "returncode", 0)
    if isinstance(code, bool) or not isinstance(code, int):
        return 1
    return code


def _stderr(result: Any) -> str:
    """The error output of a runner result, as text."""
    if isinstance(result, dict):
        stderr = result.get("stderr", "")
    else:
        stderr = getattr(result, "stderr", "")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return str(stderr or "").strip()


def _execute(command: List[str], runner: Any):
    """Run the command; (True, "") when it went through, else (False, why)."""
    try:
        result = runner(command, check=False, capture_output=True, text=True)
    except Exception as exc:  # the runner itself blew up
        return False, "{}: {}".format(type(exc).__name__, exc)
    code = _returncode(result)
    if code == 0:
        return True, ""
    stderr = _stderr(result)
    return False, "gh run rerun exited with {}{}".format(code, ": " + stderr if stderr else "")


def rerun_once(
    pr: Any,
    runs: Any,
    ledger: Any,
    runner: Any = subprocess.run,
    now: Any = None,
    dry_run: bool = False,
    ignore_re: Any = DEFAULT_IGNORE_RE,
) -> List[Dict[str, Any]]:
    """Rerun every failed run of `pr` that was not rerun yet, once.

    For every selected run ``gh run rerun <id> --failed`` is executed.  The run
    is remembered in the ledger only when the command really went through, so a
    command that failed is tried again by the next round.  `runner` is the
    callable that runs the command (``subprocess.run`` by default; it is called
    as ``runner(command, check=False, capture_output=True, text=True)``) and
    `now` is the timestamp written into the ledger (the wall clock when it is
    None).

    With ``dry_run=True`` nothing is executed and nothing is written: the
    returned rows are the plan.

    `ignore_re` is the pattern of the run names that are never rerun
    (:data:`DEFAULT_IGNORE_RE` by default); ``None`` disables the filter and a
    broken pattern raises a ``ValueError``.

    Every row is ``{"run_id": ..., "action": "rerun"|"skip", "reason": ...}``.
    A run that was not selected is reported as skipped with the reason it was
    left alone, so the caller can tell "not failed" from "already rerun".
    """
    moment = time.time() if now is None else _number(now, "now")
    actions: List[Dict[str, Any]] = []
    for candidate in _candidates(runs, ledger, ignore_re):
        if not candidate["selected"]:
            actions.append({
                "run_id": candidate["run_id"],
                "action": "skip",
                "reason": candidate["reason"],
            })
            continue
        if dry_run:
            actions.append({
                "run_id": candidate["run_id"],
                "action": "rerun",
                "reason": "planned: " + candidate["reason"],
            })
            continue
        ok, detail = _execute(_rerun_command(candidate["run_id"]), runner)
        if ok:
            if ledger is not None:
                ledger.record(candidate["run_id"], pr, moment)
            actions.append({
                "run_id": candidate["run_id"],
                "action": "rerun",
                "reason": candidate["reason"],
            })
        else:
            actions.append({
                "run_id": candidate["run_id"],
                "action": "skip",
                "reason": "rerun failed: " + detail,
            })
    return actions
