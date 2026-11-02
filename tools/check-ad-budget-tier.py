#!/usr/bin/env python3
"""Offline contract for the tiered dispatch budget of the lane autodispatcher (Refs #1686).

A large card — the `budget:large` label or the body marker `预算：90` — is dispatched
with `--budget-minutes 90`; every other card keeps the default 45 minutes; a malformed
marker falls back to 45 and is reported in the dispatch log.  The check fakes the shell,
so it never calls gh, git or SSH.
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools/lane-autodispatch/autodispatch.py"


def load_autodispatch():
    spec = importlib.util.spec_from_file_location("ad_budget_tier", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatch_command(module, card, directory):
    """Run dispatch() against a fake shell and return the remote command it built."""
    calls = []

    def fake_sh(cmd, timeout=120, inp=None):
        calls.append(cmd)
        return 0, '{"id":"run-1"}', ""

    module.D = directory
    module.sh = fake_sh
    module.dispatch("C1", {"host": "instabox", "fleet": "fleet", "repo": "repo"}, card, "b" * 40)
    return " ".join(calls[-1])


def main() -> None:
    module = load_autodispatch()
    card = {"n": 1686, "title": "budget card", "labels": ["lane-ready"], "body": ""}

    with tempfile.TemporaryDirectory() as raw:
        large = dict(card, labels=["lane-ready", "budget:large"])
        marked = dict(card, body="实现大卡。\n预算：90\n")
        broken = dict(card, body="实现大卡。\n预算：九十\n")

        command = dispatch_command(module, large, raw)
        assert "--budget-minutes 90" in command, command

        command = dispatch_command(module, marked, raw)
        assert "--budget-minutes 90" in command, command

        command = dispatch_command(module, card, raw)
        assert "--budget-minutes 45" in command, command

        command = dispatch_command(module, broken, raw)
        assert "--budget-minutes 45" in command, command

    decision = module.budget_for(broken)
    assert decision["minutes"] == 45, decision
    assert decision["warn"], decision

    decision = module.budget_for(large)
    assert decision["minutes"] == 90, decision
    assert not decision["warn"], decision
    assert "budget:large" in decision["basis"], decision

    # The dispatch log line answers "which budget did this issue get, and why?".
    line = module.dispatch_log_line("C1", {"host": "instabox"}, large, "run-1", "b" * 40, decision)
    assert "#1686" in line and "budget 90m" in line and "budget:large" in line, line

    # A planner card may carry the marker; a malformed one is reported, never filed as is.
    good = "# Card\nlabels: p1\n实现正文。\n验收：python3 check.py\n"
    large_card = "# Card\nlabels: p1,budget:large\n实现正文。\n验收：python3 check.py\n"
    broken_card = "# Card\nlabels: p1\n实现正文。\n预算：九十\n验收：python3 check.py\n"
    assert module.card_budget(good)["tier"] == "default"
    assert module.card_budget(good)["error"] is None
    assert module.card_budget(large_card)["tier"] == "large"
    assert module.card_budget(broken_card)["error"], module.card_budget(broken_card)

    # An unmarked card that looks large is only hinted, never promoted.
    long_card = "# Card\nlabels: p1\n" + ("实现正文。" * 300) + "\n验收：python3 check.py\n"
    assert module.card_budget(long_card)["tier"] == "default"
    assert module.card_budget_hint(long_card), "a long unmarked card must be hinted"
    assert module.card_budget_hint(good) is None
    assert module.card_budget_hint(large_card) is None

    print("ad budget tier contract: OK")


if __name__ == "__main__":
    main()
