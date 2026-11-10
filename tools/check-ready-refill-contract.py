#!/usr/bin/env python3
"""Offline contract check for autodispatch low-queue refill messages.

The check uses a temporary autodispatch directory and replaces only the
external GitHub/git calls.  It never reads or writes the live backlog.

Since #1662 it also covers the refill triggers of a pool that runs dry: the
refill tops the pool up to the target, a card that cannot be run is quarantined
in `backlog/rejected/` without overwriting an earlier card of the same name, the
refill stays bounded while the queue cannot be read, and an unreadable planner
query keeps the single-planner gate closed.
"""
import importlib.util
import json
import os
import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"
READY_TARGET_PATH = ROOT / "tools/check-ad-ready-target.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autodispatch_contract", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_ready_target_contract():
    """Import the shared card-shape predicate used by the ready-queue check."""
    spec = importlib.util.spec_from_file_location("ad_ready_target_contract", READY_TARGET_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tick(module, temp_dir, gh, shell, replenish=None):
    module.D = str(temp_dir)
    module.BRAIN_LOG = str(temp_dir / "brain.log")
    module.queue = lambda *args, **kwargs: []
    module.gh = gh
    module.sh = shell
    if replenish is not None:
        module.replenish = replenish
    module.tick({}, {}, 0)
    return (temp_dir / "autodispatch.log").read_text()


def check_rejected_cards(module, temp_dir, thick, replenish):
    """一张不合规的卡不再拖垮整批补卡：它被退回并记录原因，其余卡照常铸出 (Refs #1662)。"""
    root = temp_dir / "rejected"
    root.mkdir(parents=True, exist_ok=True)
    backlog = root / "backlog"
    backlog.mkdir()
    (backlog / "a-thin.md").write_text("# Thin card\nlabels: p1\n")
    (backlog / "b-good.md").write_text(thick)
    filed = []
    module.file_issue = lambda text, extra=(): filed.append(text.splitlines()[0]) or "#501"
    module.D = str(root)
    module.BRAIN_LOG = str(root / "brain.log")
    module.gh = lambda args, timeout=60: (
        (0, json.dumps([{"number": 42}]), "")
        if args[:3] == ["issue", "list", "--state"]
        else (0, "", "")
    )
    module.sh = lambda *args, **kwargs: (0, "", "")
    module.LAST_MINT_LOG = 0.0  # the planner-gate line is throttled: force it for this check
    module.replenish = replenish

    out = module.replenish(5, {})

    assert out == ["#501"], out
    assert filed == ["# Local card"], filed
    assert (backlog / "rejected" / "a-thin.md").is_file(), "the unusable card must be quarantined"
    assert (backlog / "filed" / "b-good.md").is_file(), "the usable card must be minted"
    log = (root / "autodispatch.log").read_text()
    assert "backlog a-thin.md" in log and "thin card" in log, log
    assert "planner issue already open (#42)" in log, log


def check_quarantine_keeps_both_cards(module, temp_dir, replenish):
    """隔离卡不覆盖同名旧卡：两份内容都保留 (Refs #1662)。"""
    root = temp_dir / "quarantine"
    backlog = root / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "a-thin.md").write_text("# Thin card\nlabels: p1\n")
    rejected = backlog / "rejected"
    rejected.mkdir()
    (rejected / "a-thin.md").write_text("earlier card")
    module.file_issue = lambda text, extra=(): "#501"
    module.D = str(root)
    module.BRAIN_LOG = str(root / "brain.log")
    module.gh = lambda args, timeout=60: (0, json.dumps([{"number": 42}]), "")
    module.sh = lambda *args, **kwargs: (0, "", "")
    module.replenish = replenish

    out = module.replenish(5, {})

    assert out == [], out
    assert (rejected / "a-thin.md").read_text() == "earlier card", "the earlier card must survive"
    moved = rejected / "a-thin.1.md"
    assert moved.is_file(), sorted(path.name for path in rejected.iterdir())
    assert "Thin card" in moved.read_text(), moved.read_text()


def check_refill_target(module, temp_dir):
    """池见底时补到 target 而不是只补到 low：否则池子一直贴着阈值 (Refs #1662)。"""
    # AD_LOW can exceed the default AD_TARGET without disabling low-water refill.
    assert module.refill_need(25, low=30, target=20) == 5
    assert module.refill_need(0, low=30, target=20) == 30
    assert module.refill_need(25, low=30, target=20, starved=8) == 8
    assert module.refill_need(30, low=30, target=20) == 0
    temp_dir.mkdir(parents=True, exist_ok=True)
    needs = []
    module.D = str(temp_dir)
    module.BRAIN_LOG = str(temp_dir / "brain.log")
    module.gh = lambda args, timeout=60: (0, "", "")
    module.sh = lambda *args, **kwargs: (0, "", "")

    run_tick(module, temp_dir, module.gh, module.sh, replenish=lambda k, state: needs.append(k) or [])

    assert needs == [module.TARGET], needs
    log = (temp_dir / "autodispatch.log").read_text()
    assert "WARNING queue low" in log, log
    assert f"need {module.TARGET} card(s)" in log, log


def check_unreadable_queue_is_bounded(module, temp_dir):
    """队列读不出来时补卡有界：不会每个 tick 重复补到 target (Refs #1662)。"""
    temp_dir.mkdir(parents=True, exist_ok=True)
    needs = []
    module.D = str(temp_dir)
    module.BRAIN_LOG = str(temp_dir / "brain.log")
    module.gh = lambda args, timeout=60: (0, "", "")
    module.sh = lambda *args, **kwargs: (0, "", "")

    def broken_queue(*args, **kwargs):
        raise RuntimeError("gh: API rate limit exceeded")

    module.queue = broken_queue
    module.replenish = lambda k, state: needs.append(k) or ["#501"]
    state = {"_ready": 5}
    for _ in range(module.QUEUE_UNREADABLE_REFILL_MAX + 2):
        module.tick({}, state, 0)

    assert len(needs) == module.QUEUE_UNREADABLE_REFILL_MAX, needs
    assert needs == sorted(needs, reverse=True), needs
    log = (temp_dir / "autodispatch.log").read_text()
    assert "queue unreadable" in log, log
    assert "bounded retries exhausted" in log, log


def check_planner_query_unreadable(module, temp_dir, replenish):
    """planner 查询读不出来时保守跳过建卡，单 planner 门控不被破坏 (Refs #1662)。"""
    temp_dir.mkdir(parents=True, exist_ok=True)
    commands = []

    def fake_gh(args, timeout=60):
        commands.append(args)
        if args[:2] == ["issue", "list"] and "planner" in args:
            return 0, "not-json", ""
        return 0, "", ""

    module.D = str(temp_dir)
    module.BRAIN_LOG = str(temp_dir / "brain.log")
    module.gh = fake_gh
    module.sh = lambda *args, **kwargs: (0, "", "")
    module.LAST_MINT_LOG = 0.0  # the planner-gate line is throttled: force it for this check
    module.replenish = replenish

    assert module.replenish(1, {}) == []
    assert len(commands) == 1, commands
    log = (temp_dir / "autodispatch.log").read_text()
    assert "planner query unreadable" in log, log
    assert "skip planner creation" in log, log


def check_mint_pause(module, temp_dir, replenish):
    """PAUSE-MINT 停铸卡时必须留下日志证据 (Refs #1662)。"""
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / "PAUSE-MINT").write_text("")
    module.D = str(temp_dir)
    module.BRAIN_LOG = str(temp_dir / "brain.log")
    module.replenish = replenish

    assert module.replenish(5, {}) == []
    log = (temp_dir / "autodispatch.log").read_text()
    assert "replenish skipped" in log, log
    assert "PAUSE-MINT" in log, log


def main():
    module = load_module()
    real_replenish = module.replenish
    ready_target = load_ready_target_contract()
    scratch_root = ROOT / ".swrm-tmp"
    scratch_root.mkdir(exist_ok=True)
    old_home = os.environ.get("OPENSWARM_HOME")
    with tempfile.TemporaryDirectory(dir=scratch_root) as raw_dir:
        temp_dir = pathlib.Path(raw_dir)
        # the pause store of the live box must not influence the offline check
        os.environ["OPENSWARM_HOME"] = str(temp_dir / "home")
        backlog = temp_dir / "backlog"
        backlog.mkdir()
        thick = "# Local card\nlabels: p1\nImplement the bounded refill path.\n验收：python3 tools/check-ready-refill-contract.py\n"
        (backlog / "local.md").write_text(thick)
        candidates = temp_dir / "candidates"
        candidates.mkdir()
        thin = "# Thin card\nlabels: p1\n"
        (candidates / "thin.md").write_text(thin)
        (candidates / "thick.md").write_text(thick)
        accepted = ready_target.local_candidates(candidates)
        assert accepted == [thick], accepted
        assert ready_target.is_thin_card(thin)
        assert not ready_target.is_thin_card(thick)

        filed = []

        def file_issue(text, extra=()):
            filed.append(text.splitlines()[0])
            return "#501"

        module.file_issue = file_issue
        module.D = str(temp_dir)
        module.BRAIN_LOG = str(temp_dir / "brain.log")
        module.gh = lambda args, timeout=60: (
            (0, json.dumps([{"number": 42}]), "")
            if args[:3] == ["issue", "list", "--state"]
            else (0, "", "")
        )
        module.sh = lambda *args, **kwargs: (0, "", "")
        local = run_tick(module, temp_dir, module.gh, module.sh)
        assert filed == ["# Local card"], filed
        assert json.loads((temp_dir / "lanes-status.json").read_text())["_ready"] < 10
        assert "filed 1 issue(s) from backlog" in local, local

        empty_dir = temp_dir / "empty"
        (empty_dir / "backlog").mkdir(parents=True)

        def planner_exists(args, timeout=60):
            if args[:3] == ["issue", "list", "--state"]:
                return 0, json.dumps([{"number": 42}]), ""
            return 0, "", ""

        empty = run_tick(
            module,
            empty_dir,
            planner_exists,
            lambda *args, **kwargs: (0, "", ""),
        )
        assert "WARNING queue low" in empty, empty

        check_rejected_cards(module, temp_dir, thick, real_replenish)
        check_quarantine_keeps_both_cards(module, temp_dir, real_replenish)
        check_refill_target(module, temp_dir / "target")
        check_unreadable_queue_is_bounded(module, temp_dir / "unreadable")
        check_planner_query_unreadable(module, temp_dir / "planner", real_replenish)
        check_mint_pause(module, temp_dir / "paused", real_replenish)

    if old_home is None:
        os.environ.pop("OPENSWARM_HOME", None)
    else:
        os.environ["OPENSWARM_HOME"] = old_home

    print("ready refill contract: OK")


if __name__ == "__main__":
    main()
