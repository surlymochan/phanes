import sys
from pathlib import Path as _FixturePath
sys.path.insert(0, str(_FixturePath(__file__).resolve().parents[1] / "lane-autodispatch"))
from pr_guard_fixtures import no_open_prs
"""The hot-file preflight on the real AD backlog import path (Refs #1721).

The AD (``tools/lane-autodispatch/autodispatch.py``) is where the planner's cards become
issues: ``replenish()`` reads the backlog (the local ``backlog/*.md`` and the repo's
``docs/backlog/next/*.md``) and calls ``file_issue()``.  These tests drive that path
with the real code: a card whose hot file is at its cap is not filed, it keeps its place
so the next tick retries it, and the refusal is written to the mint ledger.  A card
whose hot file is free is filed as before.

The dispatch side of the same tick is covered too: a card whose hot file is in flight is
not handed to a lane, it is counted as blocked (not as ready supply) and the lanes status
carries the reason, the files and the waiting time.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = pathlib.Path(__file__).parents[2]
AUTODISPATCH_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

SPEC = importlib.util.spec_from_file_location("autodispatch_hot_preflight", AUTODISPATCH_PATH)
autodispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(autodispatch)

CARD = "# 改共享文件\nlabels: p1,backend:codex\n\n热文件：{hot}\n\n验收：`true`\n"
BODY = "热文件：{hot}\n\n验收：`true`\n"
SHA = "a" * 40


class FakeClock:
    """A stand-in for the time module: the tick reads the clock through it."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def localtime(self, stamp=None):
        if isinstance(stamp, time.struct_time): return stamp
        return time.localtime(self.now if stamp is None else stamp)

    def strftime(self, fmt, stamp=None):
        return time.strftime(fmt, self.localtime(stamp))

    def strptime(self, text, fmt):
        return time.strptime(text, fmt)

    def mktime(self, stamp):
        return time.mktime(stamp)

    def sleep(self, _seconds):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = pathlib.Path(self.tmp.name)
        self.calls = []
        self.created = []
        self.repo_cards = {}
        self.repo_backlog = ""
        self.orig = {name: getattr(autodispatch, name)
                     for name in ("D", "gh", "sh", "log", "replenish", "time", "preflight_card")}
        autodispatch.D = str(self.state_dir)
        autodispatch.log = lambda msg, brain=True: None
        autodispatch.gh = self.fake_gh
        autodispatch.preflight_card = lambda text: None  # these tests judge the hot files, not the card lint
        autodispatch.sh = self.fake_sh
        self.addCleanup(self.restore)

    def restore(self):
        for name, value in self.orig.items():
            setattr(autodispatch, name, value)

    @no_open_prs

    def fake_gh(self, args, timeout=60):
        self.calls.append(" ".join(str(a) for a in args))
        if list(args[:2]) == ["issue", "create"]:
            self.created.append(list(args))
            return 0, "https://example.invalid/issues/42\n", ""
        if "planner" in args:            # a planner issue is open: no planner fallback
            return 0, '[{"number": 1}]', ""
        return 0, "[]", ""

    def fake_sh(self, cmd, timeout=120, inp=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        if "ls-tree" in text:
            return 0, self.repo_backlog, ""
        if "ls-remote" in text:
            return 0, SHA + "\trefs/heads/main", ""
        for path, body in self.repo_cards.items():
            if f"origin/main:{path}" in text:
                return 0, body, ""
        return 0, "", ""

    def backlog(self, name, hot):
        """Write a card into the local backlog directory (the AD's first import source)."""
        directory = self.state_dir / "backlog"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(CARD.format(hot=hot), encoding="utf-8")
        return path

    def ledger(self):
        path = self.state_dir / "mint-log.json"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)

    def busy_state(self, hot):
        """The AD state of a lane that works on a card with the given hot file."""
        return {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA,
                       "at": "2026-11-02 21:00:00", "hot": [hot]}}


class BacklogImport(Base):
    """The actual mint path: the backlog import of the AD."""

    def test_a_card_at_its_cap_is_not_filed(self):
        self.backlog("card.md", "src/shared.py")
        state = self.busy_state("src/shared.py")
        filed = autodispatch.replenish(5, state)
        self.assertEqual(filed, [])
        self.assertEqual(self.created, [])           # the issue never reached GitHub

    def test_a_refused_card_keeps_its_place_in_the_backlog(self):
        path = self.backlog("card.md", "src/shared.py")
        autodispatch.replenish(5, self.busy_state("src/shared.py"))
        self.assertTrue(path.exists())              # not moved to backlog/filed/
        self.assertFalse((self.state_dir / "backlog" / "filed" / "card.md").exists())

    def test_a_refused_card_is_written_to_the_mint_ledger(self):
        self.backlog("card.md", "src/shared.py")
        autodispatch.replenish(5, self.busy_state("src/shared.py"))
        ledger = self.ledger()
        self.assertEqual(len(ledger), 1)
        self.assertFalse(ledger[0]["minted"])
        self.assertEqual(ledger[0]["reason"], "hot_file_conflict")
        self.assertEqual(ledger[0]["preflight"]["conflicts"],
                         [{"file": "src/shared.py", "inflight": 1, "limit": 1}])
        self.assertEqual(ledger[0]["preflight"]["advice"], "改期或换题")

    def test_a_free_card_is_filed_and_recorded(self):
        path = self.backlog("card.md", "src/other.py")
        filed = autodispatch.replenish(5, self.busy_state("src/shared.py"))
        self.assertEqual(filed, ["#42"])
        self.assertEqual(len(self.created), 1)
        self.assertIn("lane-ready", self.created[0])
        self.assertFalse(path.exists())
        self.assertTrue((self.state_dir / "backlog" / "filed" / "card.md").exists())
        ledger = self.ledger()
        self.assertEqual([r["minted"] for r in ledger], [False, True])
        self.assertEqual(ledger[-1]["issue"], "42")
        self.assertFalse(ledger[-1]["preflight"]["blocked"])

    def test_the_refused_card_is_filed_once_the_hot_file_is_free(self):
        self.backlog("card.md", "src/shared.py")
        self.assertEqual(autodispatch.replenish(5, self.busy_state("src/shared.py")), [])
        self.assertEqual(autodispatch.replenish(5, {}), ["#42"])   # the lane finished
        self.assertTrue((self.state_dir / "backlog" / "filed" / "card.md").exists())

    def test_failed_issue_creation_is_recorded_and_retried(self):
        path = self.backlog("card.md", "src/shared.py")
        autodispatch.gh = lambda *a, **kw: (1, "", "synthetic failure")
        self.assertEqual(autodispatch.replenish(1, {}), [])
        self.assertTrue(path.exists())
        self.assertEqual([r["reason"] for r in self.ledger()], ["mint_pending", "mint_failed"])

    def test_a_batch_files_one_card_per_hot_file(self):
        """Two cards of one batch on the same hot file: the second one would be blocked."""
        self.backlog("a.md", "src/shared.py")
        self.backlog("b.md", "src/shared.py")
        self.assertEqual(autodispatch.replenish(5, {}), ["#42"])    # only the first card is filed
        self.assertEqual(len(self.created), 1)
        self.assertFalse((self.state_dir / "backlog" / "a.md").exists())
        self.assertTrue((self.state_dir / "backlog" / "b.md").exists())   # the second one waits
        ledger = self.ledger()
        self.assertEqual([record["minted"] for record in ledger], [False, True, False])
        self.assertEqual(ledger[-1]["reason"], "hot_file_conflict")

    def test_a_batch_files_cards_of_different_hot_files(self):
        self.backlog("a.md", "src/shared.py")
        self.backlog("b.md", "src/other.py")
        self.assertEqual(autodispatch.replenish(5, {}), ["#42", "#42"])
        self.assertEqual(len(self.created), 2)

    def test_a_malformed_card_is_recorded_in_the_mint_ledger(self):
        directory = self.state_dir / "backlog"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bad.md").write_text("# no labels line\n正文\n", encoding="utf-8")
        self.assertEqual(autodispatch.replenish(5, {}), [])
        ledger = self.ledger()
        self.assertEqual(len(ledger), 1)
        self.assertFalse(ledger[0]["minted"])
        self.assertEqual(ledger[0]["reason"], "bad_format")
        self.assertEqual(self.created, [])

    def test_the_repo_backlog_card_is_not_imported_while_it_conflicts(self):
        path = "docs/backlog/next/20261101-0335-18.md"
        self.repo_backlog = path + "\n"
        self.repo_cards[path] = CARD.format(hot="src/shared.py")
        state = self.busy_state("src/shared.py")
        self.assertEqual(autodispatch.replenish(5, state), [])
        self.assertNotIn(path, state["_imported"])   # not imported: the next tick retries it
        self.assertEqual(self.created, [])

    def test_the_repo_backlog_card_is_imported_once_the_hot_file_is_free(self):
        path = "docs/backlog/next/20261101-0335-18.md"
        self.repo_backlog = path + "\n"
        self.repo_cards[path] = CARD.format(hot="src/shared.py")
        state = {}
        self.assertEqual(autodispatch.replenish(5, state), ["#42"])
        self.assertIn(path, state["_imported"])
        self.assertEqual(len(self.created), 1)



class DispatchTick(Base):
    def setUp(self):
        super().setUp()
        self.lanes = {"P0": {"host": "node", "backend": "pi"},
                      "P1": {"host": "node", "backend": "pi"}}
        self.queue = [{"n": 9, "title": "card", "body": BODY.format(hot="src/shared.py"),
                       "prio": 1, "backend": "pi", "strict": False}]
        overrides = {
            "audit_switches": lambda: None, "pause_records": lambda: [],
            "paused_scopes": lambda **kw: set(), "lane_paused": lambda *a: False,
            "host_ok": lambda *a: True, "host_good": lambda *a: None,
            "host_came_back": lambda *a: False, "update_busy_stale": lambda *a: None,
            "remote": lambda lane, *a: {"lane": "busy" if lane == "P0" else "idle"},
            "reconcile": lambda *a: False, "verify_recovered_run": lambda *a: False,
            "queue": lambda *a, **k: [dict(i) for i in self.queue], "deps_met": lambda i: True,
            "prefetch_labels": lambda *a: None, "label_state": lambda *a: ("fresh", {}),
            "freshen_queue": lambda q, *a: q, "memory_preflight": lambda *a: None,
            "dispatch": lambda *a: ("run", "verify"), "replenish": lambda *a: [],
            "refresh_minimax_burn": lambda: None, "observe_verify_verdicts": lambda *a: None,
            "pause_reasons": lambda *a: {}, "minimax_burn_status": lambda: {},
        }
        for name, value in overrides.items():
            if name not in self.orig: self.orig[name] = getattr(autodispatch, name)
            setattr(autodispatch, name, value)

    def status(self):
        return json.loads((self.state_dir / "lanes-status.json").read_text())

    def test_tick_blocks_hot_file_and_measures_wait(self):
        clock = FakeClock(1000000)
        autodispatch.time = clock
        state = self.busy_state("src/shared.py")
        autodispatch.tick(self.lanes, state, 0)
        self.assertEqual(self.status()["_ready"], 0)
        self.assertEqual(self.status()["_blocked"], 1)
        self.assertEqual(self.status()["_blocked_details"][0]["files"], ["src/shared.py"])
        self.assertFalse(any("in-progress" in call for call in self.calls))
        clock.now += 90
        autodispatch.tick(self.lanes, state, 0)
        self.assertEqual(self.status()["_blocked_details"][0]["wait_seconds"], 90)
        saved = json.loads((self.state_dir / "state.json").read_text())
        self.assertIn("9", saved["_blocked_since"])
        self.queue = []
        autodispatch.tick(self.lanes, state, 0)
        self.assertEqual(state["_blocked_since"], {})

    def test_tick_dispatches_a_card_below_its_limit(self):
        state = self.busy_state("src/other.py")
        autodispatch.tick(self.lanes, state, 0)
        self.assertTrue(any("in-progress" in call for call in self.calls))
        self.assertEqual(state["P1"]["hot"], ["src/shared.py"])
        self.assertEqual(self.status()["_blocked"], 0)

    def test_dependency_block_is_in_details_and_not_ready(self):
        autodispatch.deps_met = lambda item: False
        autodispatch.tick(self.lanes, self.busy_state("src/shared.py"), 0)
        detail = self.status()["_blocked_details"][0]
        self.assertEqual(detail["reason"], "deps")
        self.assertEqual(self.status()["_ready"], 0)
        self.assertEqual(self.status()["_blocked"], 1)

    def test_unknown_legacy_inflight_blocks_declared_files(self):
        state = self.busy_state("src/shared.py")
        del state["P0"]["hot"]
        verdict = autodispatch.hot_verdict(BODY.format(hot="src/other.py"), autodispatch.hot_inflight(state))
        self.assertTrue(verdict["blocked"])
        self.assertTrue(verdict["unknown_inflight"])

    def test_synthetic_replay_matches_dispatch_admission(self):
        fixture = json.loads((ROOT / "tools/tests/fixtures/hotfiles_synthetic.json").read_text())
        self.assertIn("合成样本", fixture["source"])
        for case in fixture["cases"]:
            state = {f"P{i}": {"issue": i + 1, "hot": [path]}
                     for i, path in enumerate(case["inflight"])}
            counts = autodispatch.hot_inflight(state)
            verdict = autodispatch.hot_verdict(BODY.format(hot=case["hot"]), counts)
            self.assertEqual(verdict["blocked"], case["blocked"])
            self.assertEqual(verdict["blocked"], autodispatch.HOT_LOCK.blocked([case["hot"]], counts))
