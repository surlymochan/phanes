#!/usr/bin/env python3
"""Offline contract for counting only fresh, dispatchable lane-ready issues (Refs #950).

`gh issue list --label lane-ready` answers from the search index, so a row can still
look `lane-ready` while the issue itself carries `lane-failed`/`lane-pr`/`in-progress`.
The `_ready` count in `lanes-status.json` must follow the authoritative labels: stale
rows are skipped (and logged), never counted. A failed authoritative read is NOT stale:
the row is kept and logged as a read error, so a transient `gh` failure can never drive
`_ready` to a false 0.

The fixture is entirely local — fake search rows plus fake authoritative issue labels.
The check never calls `gh`, never writes a real issue, and never touches the live
autodispatch state directory.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "lane-autodispatch" / "autodispatch.py"


def load_autodispatch():
    """Import the real autodispatcher so the contract is checked against shipped code."""
    spec = importlib.util.spec_from_file_location("autodispatch", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def search_row(number: int) -> dict:
    """One search-index row: it only carries what the (lagging) search index showed."""
    return {"n": number, "title": f"#{number}", "body": "", "prio": 3, "backend": None,
            "strict": False, "deps_ok": True}


def fake_gh(search=None, authoritative=None, calls=None):
    """Stub for `gh`: `issue view` answers from the fake authoritative labels, never the network.

    An issue that is missing from `authoritative` answers like a failed read (`rc != 0`)."""
    def gh(args, timeout=60):
        if calls is not None:
            calls.append(list(args))
        if args[:3] == ["issue", "list", "--state"]:
            return 0, json.dumps(search or []), ""
        if args[:2] == ["issue", "view"]:
            current = (authoritative or {}).get(int(args[2]))
            if current is None:
                return 1, "", "gh: could not read the issue"
            labels = [{"name": name} for name in current["labels"]]
            return 0, json.dumps({"state": current["state"], "labels": labels}), ""
        if args[:2] == ["issue", "edit"]:
            return 0, "", ""
        raise AssertionError(f"unexpected gh call: {args}")
    return gh


def fake_sh(args, timeout=120, inp=None):
    """Stub for the shell: only the base lookup answers, every other command is a no-op."""
    if isinstance(args, list) and args[:2] == ["git", "ls-remote"]:
        return 0, "0" * 40 + "\trefs/heads/main\n", ""
    return 0, "", ""


def check_fresh_ready_bool() -> None:
    """`fresh_ready` keeps its bool contract (Refs #985): stale residual False, clean ready True.

    The tri-state lives in `label_state`; `fresh_ready` must stay a plain bool, otherwise
    main's `tools/check-ad-stale-label-skip.py` contract breaks."""
    module = load_autodispatch()
    authoritative = {
        985: {"state": "OPEN", "labels": ["lane-ready", "lane-failed"]},
        986: {"state": "OPEN", "labels": ["lane-ready"]},
    }
    module.gh = fake_gh(authoritative=authoritative)
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        assert module.fresh_ready(985, {}) is False, "lane-failed residual must be skipped"
        assert module.fresh_ready(986, {}) is True, "clean lane-ready must stay dispatchable"


def check_residual_semantics() -> None:
    """The #985 residual semantics survive on the freshen_queue path (Refs #985).

    A stale row whose authoritative labels still carry `lane-failed` + `lane-ready` counts
    as a lane-failed residual and tells the operator to drop the leftover `lane-ready`."""
    module = load_autodispatch()
    authoritative = {201: {"state": "OPEN", "labels": ["lane-ready", "lane-failed"]}}
    module.gh = fake_gh(authoritative=authoritative)
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        before = module.STALE_STRIPPED_N
        residual_before = module.STALE_LANE_FAILED_N

        fresh = module.freshen_queue([search_row(201)], {})
        assert fresh == [], fresh
        assert module.STALE_STRIPPED_N == before + 1, module.STALE_STRIPPED_N
        assert module.STALE_LANE_FAILED_N == residual_before + 1, module.STALE_LANE_FAILED_N

        log = (Path(raw) / "autodispatch.log").read_text()
        assert "stale search hit lane-failed + lane-ready" in log, log
        assert "remove lane-ready label" in log, log


def check_stale_rows() -> None:
    """Shipped code contract: freshen_queue() drops stale rows and logs the skip."""
    module = load_autodispatch()
    authoritative = {
        201: {"state": "OPEN", "labels": ["lane-ready", "lane-failed"]},
        202: {"state": "OPEN", "labels": ["lane-ready", "in-progress"]},
        203: {"state": "OPEN", "labels": ["lane-ready"]},
    }
    module.gh = fake_gh(authoritative=authoritative)
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        before = module.STALE_STRIPPED_N

        fresh = module.freshen_queue([search_row(201), search_row(202), search_row(203)], {})
        assert [item["n"] for item in fresh] == [203], fresh
        blocked = sum(not item.get("deps_ok", True) for item in fresh)
        assert len(fresh) - blocked == 1  # the `_ready` count of lanes-status.json

        log = (Path(raw) / "autodispatch.log").read_text()
        assert "skip #201" in log and "stale search" in log, log
        assert module.STALE_STRIPPED_N == before + 2, module.STALE_STRIPPED_N


def check_read_error() -> None:
    """A failed authoritative read is not stale: the row is kept and no stale skip is counted."""
    module = load_autodispatch()
    module.gh = fake_gh(authoritative={})  # every `gh issue view` fails
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        before = module.STALE_STRIPPED_N

        fresh = module.freshen_queue([search_row(201)], {})
        assert [item["n"] for item in fresh] == [201], fresh
        assert module.STALE_STRIPPED_N == before, module.STALE_STRIPPED_N

        log = (Path(raw) / "autodispatch.log").read_text()
        assert "label read failed" in log, log
        assert "stale search" not in log, log


def check_one_read_per_tick() -> None:
    """Every issue is read authoritatively at most once per tick (the per-tick cache)."""
    module = load_autodispatch()
    numbers = (201, 202, 203)
    authoritative = {n: {"state": "OPEN", "labels": ["lane-ready"]} for n in numbers}
    calls: list = []
    module.gh = fake_gh(authoritative=authoritative, calls=calls)
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        rows = [search_row(n) for n in numbers]
        module.freshen_queue(rows, {})
        module.freshen_queue(rows, {})  # same tick: the cached answers are reused
        views = [c for c in calls if c[:2] == ["issue", "view"]]
        assert len(views) == len(numbers), views


def check_read_budget() -> None:
    """The per-tick read budget bounds the authoritative reads; unverified rows are kept."""
    module = load_autodispatch()
    module.FRESH_READ_BUDGET = 2
    numbers = (201, 202, 203)
    authoritative = {n: {"state": "OPEN", "labels": ["lane-ready"]} for n in numbers}
    calls: list = []
    module.gh = fake_gh(authoritative=authoritative, calls=calls)
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        before = module.STALE_STRIPPED_N

        fresh = module.freshen_queue([search_row(n) for n in numbers], {})
        views = [c for c in calls if c[:2] == ["issue", "view"]]
        assert len(views) == 2, views
        assert len(fresh) == len(numbers), fresh  # the unread row is kept, never stripped
        assert module.STALE_STRIPPED_N == before, module.STALE_STRIPPED_N


def check_tick() -> None:
    """Tick contract: a stale-only search queue yields `_ready` 0 in lanes-status.json."""
    module = load_autodispatch()
    # The search index still shows #201 as `lane-ready`; the issue itself carries lane-failed.
    search = [{"number": 201, "title": "#201 stale", "labels": [{"name": "lane-ready"}], "body": ""}]
    authoritative = {201: {"state": "OPEN", "labels": ["lane-ready", "lane-failed"]}}
    module.gh = fake_gh(search=search, authoritative=authoritative)
    module.sh = fake_sh
    module.replenish = lambda k, state: []
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        module.tick({}, {}, 0)
        status = json.loads((Path(raw) / "lanes-status.json").read_text())
        assert status["_ready"] == 0, status
        assert status["_blocked"] == 0, status
        log = (Path(raw) / "autodispatch.log").read_text()
        assert "skip #201" in log and "stale search" in log, log


def check_tick_mixed() -> None:
    """Tick contract: `_ready` counts the fresh rows only (the stale row is skipped)."""
    module = load_autodispatch()
    search = [{"number": n, "title": f"#{n}", "labels": [{"name": "lane-ready"}], "body": ""}
              for n in (201, 202)]
    authoritative = {
        201: {"state": "OPEN", "labels": ["lane-ready", "lane-failed"]},
        202: {"state": "OPEN", "labels": ["lane-ready"]},
    }
    module.gh = fake_gh(search=search, authoritative=authoritative)
    module.sh = fake_sh
    module.replenish = lambda k, state: []
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        module.tick({}, {}, 0)
        status = json.loads((Path(raw) / "lanes-status.json").read_text())
        assert status["_ready"] == 1, status
        assert status["_stale_stripped_n"] == 1, status
        log = (Path(raw) / "autodispatch.log").read_text()
        assert "skip #201" in log and "stale search" in log, log


def check_tick_read_error() -> None:
    """Tick contract: a failed read keeps the issue in `_ready` (no false 0, no stale skip, no dispatch)."""
    module = load_autodispatch()
    search = [{"number": 201, "title": "#201 unreadable", "labels": [{"name": "lane-ready"}], "body": ""}]
    calls: list = []
    module.gh = fake_gh(search=search, authoritative={}, calls=calls)  # `gh issue view` fails
    module.sh = fake_sh
    module.remote = lambda L, cfg, run="": {"lane": "idle"}
    module.replenish = lambda k, state: []
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        module.tick({"CX1": {"host": "fake", "backend": "pi"}}, {}, 0)
        status = json.loads((Path(raw) / "lanes-status.json").read_text())
        assert status["_ready"] == 1, status
        assert status["_stale_stripped_n"] == 0, status
        labelled = [c for c in calls if c[:2] == ["issue", "edit"] and "in-progress" in c]
        assert not labelled, labelled  # never dispatch on unverified labels
        log = (Path(raw) / "autodispatch.log").read_text()
        assert "label read failed" in log, log
        assert "stale search" not in log, log


def check_tick_one_read_per_issue() -> None:
    """Tick contract: the dispatch candidates and the count share one read per issue."""
    module = load_autodispatch()
    numbers = (201, 202, 203)
    search = [{"number": n, "title": f"#{n}", "labels": [{"name": "lane-ready"}], "body": ""}
              for n in numbers]
    authoritative = {n: {"state": "OPEN", "labels": ["lane-ready"]} for n in numbers}
    calls: list = []
    module.gh = fake_gh(search=search, authoritative=authoritative, calls=calls)
    module.sh = fake_sh
    module.remote = lambda L, cfg, run="": {"lane": "idle"}
    module.replenish = lambda k, state: []
    with tempfile.TemporaryDirectory() as raw:
        module.D = raw
        module.BRAIN_LOG = f"{raw}/brain.md"
        module.tick({"CX1": {"host": "fake", "backend": "pi"}}, {}, 0)
        views = [c for c in calls if c[:2] == ["issue", "view"]]
        assert len(views) == len(numbers), views
        status = json.loads((Path(raw) / "lanes-status.json").read_text())
        assert status["_ready"] == len(numbers), status


def main() -> None:
    check_fresh_ready_bool()
    check_residual_semantics()
    check_stale_rows()
    check_read_error()
    check_one_read_per_tick()
    check_read_budget()
    check_tick()
    check_tick_mixed()
    check_tick_read_error()
    check_tick_one_read_per_issue()
    print("autodispatch fresh ready count contract: OK (stale search skipped, read errors kept)")


if __name__ == "__main__":
    main()
