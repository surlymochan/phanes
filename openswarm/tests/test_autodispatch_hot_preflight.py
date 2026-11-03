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

ENGINE_SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_hot_preflight_ad", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(ENGINE_SPEC)
ENGINE_SPEC.loader.exec_module(E)

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

    def sleep(self, _seconds):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = pathlib.Path(self.tmp.name)
        self.calls = []
        self.created = []
        self.repo_cards = {}
        self.repo_backlog = ""
        self.orig = {name: getattr(autodispatch, name)
                     for name in ("D", "gh", "sh", "log", "replenish", "time")}
        autodispatch.D = str(self.state_dir)
        autodispatch.log = lambda msg, brain=True: None
        autodispatch.gh = self.fake_gh
        autodispatch.sh = self.fake_sh
        self.addCleanup(self.restore)

    def restore(self):
        for name, value in self.orig.items():
            setattr(autodispatch, name, value)

    def fake_gh(self, args, timeout=60):
        self.calls.append(" ".join(str(a) for a in args))
        if list(args[:2]) == ["issue", "create"]:
            self.created.append(list(args))
            return 0, "https://github.com/surlymochan/phanes/issues/42\n", ""
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
        self.assertEqual(len(ledger), 1)
        self.assertTrue(ledger[0]["minted"])
        self.assertEqual(ledger[0]["issue"], "42")
        self.assertFalse(ledger[0]["preflight"]["blocked"])

    def test_the_refused_card_is_filed_once_the_hot_file_is_free(self):
        self.backlog("card.md", "src/shared.py")
        self.assertEqual(autodispatch.replenish(5, self.busy_state("src/shared.py")), [])
        self.assertEqual(autodispatch.replenish(5, {}), ["#42"])   # the lane finished
        self.assertTrue((self.state_dir / "backlog" / "filed" / "card.md").exists())

    def test_a_batch_files_one_card_per_hot_file(self):
        """Two cards of one batch on the same hot file: the second one would be blocked."""
        self.backlog("a.md", "src/shared.py")
        self.backlog("b.md", "src/shared.py")
        self.assertEqual(autodispatch.replenish(5, {}), ["#42"])    # only the first card is filed
        self.assertEqual(len(self.created), 1)
        self.assertFalse((self.state_dir / "backlog" / "a.md").exists())
        self.assertTrue((self.state_dir / "backlog" / "b.md").exists())   # the second one waits
        ledger = self.ledger()
        self.assertEqual([record["minted"] for record in ledger], [True, False])
        self.assertEqual(ledger[1]["reason"], "hot_file_conflict")

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


class SharedPolicy(Base):
    """The AD and the dispatch engine read one hot-file list, so they agree."""

    def setUp(self):
        super().setUp()
        self.cfg_path = self.state_dir / "cfg.json"
        with open(self.cfg_path, "w", encoding="utf-8") as stream:
            json.dump({"repo": "acme/project", "local_repo": str(self.tmp.name),
                       "state_dir": str(self.state_dir / "engine"),
                       "ssh": {"control_dir": str(self.state_dir / "cm")}}, stream)
        self.engine = E.Dispatcher(E.load_config(str(self.cfg_path)),
                                   out=open(os.devnull, "w"))
        self.addCleanup(self.engine.out.close)

    def test_both_sides_read_the_same_caps(self):
        self.assertEqual(autodispatch.HOT["lock"].cap_for("openswarm/bin/openswarm"), 1)
        self.assertEqual(self.engine.hot_lock.cap_for("openswarm/bin/openswarm"), 1)

    def test_both_sides_block_the_same_card(self):
        body = CARD.format(hot="openswarm/bin/openswarm")
        counts = {"openswarm/bin/openswarm": 1}
        ad = autodispatch.hot_verdict(body, counts)
        mint = self.engine.preflight_card(body, counts)
        self.assertTrue(ad["blocked"])
        self.assertTrue(mint["blocked"])
        self.assertEqual([c["file"] for c in ad["conflicts"]],
                         [c["file"] for c in mint["conflicts"]])

    def test_both_sides_allow_the_same_free_card(self):
        body = CARD.format(hot="openswarm/bin/openswarm")
        counts = {}
        self.assertFalse(autodispatch.hot_verdict(body, counts)["blocked"])
        self.assertFalse(self.engine.preflight_card(body, counts)["blocked"])

    def test_the_ad_verdict_is_the_verdict_of_the_shared_lock(self):
        """One decision code: the AD asks the shared lock, it does not judge on its own."""
        body = CARD.format(hot="openswarm/bin/openswarm")
        counts = {"openswarm/bin/openswarm": 1}
        shared = autodispatch.HOT["lock"].preflight(["openswarm/bin/openswarm"], counts)
        self.assertEqual(autodispatch.hot_verdict(body, counts), shared)


class DispatchSide(Base):
    """The dispatch side of the tick: a card at its cap waits instead of being counted ready."""

    def setUp(self):
        super().setUp()
        self.lanes = {"P0": {"host": "box", "backend": "pi"}, "P1": {"host": "box", "backend": "pi"}}
        self.queue = [{"n": 9, "title": "card 9", "body": BODY.format(hot="src/shared.py"),
                       "prio": 1, "backend": "pi", "strict": False}]
        self.orig.update({name: getattr(autodispatch, name)
                          for name in ("remote", "queue", "label_state", "prefetch_labels",
                                       "dispatch", "deps_met", "freshen_queue")})
        autodispatch.remote = lambda *a, **k: {"lane": "idle", "run": None}
        autodispatch.queue = lambda: [dict(item) for item in self.queue]
        autodispatch.label_state = lambda n, cache=None: ("fresh", {})
        autodispatch.prefetch_labels = lambda *a, **k: None
        autodispatch.dispatch = lambda *a, **k: ("run-1", "verify")
        autodispatch.deps_met = lambda item: True
        autodispatch.replenish = lambda *a, **k: []

    def restore(self):
        super().restore()
        for name in ("remote", "queue", "label_state", "prefetch_labels", "dispatch",
                     "deps_met", "freshen_queue"):
            setattr(autodispatch, name, self.orig[name])

    def status(self):
        with open(self.state_dir / "lanes-status.json", encoding="utf-8") as stream:
            return json.load(stream)

    def test_a_card_at_its_cap_is_not_dispatched(self):
        state = self.busy_state("src/shared.py")
        autodispatch.tick(self.lanes, state, 0)
        self.assertEqual([c for c in self.calls if "in-progress" in c], [])

    def test_a_card_at_its_cap_is_blocked_and_not_ready(self):
        state = self.busy_state("src/shared.py")
        autodispatch.tick(self.lanes, state, 0)
        status = self.status()
        self.assertEqual(status["_ready"], 0)       # the card is blocked, not supply
        self.assertEqual(status["_blocked"], 1)
        self.assertEqual(status["_blocked_reasons"], {"deps": 0, "hot_file": 1})

    def test_the_blocked_card_carries_its_files_and_its_waiting_time(self):
        clock = FakeClock(1_000_000.0)
        autodispatch.time = clock
        state = self.busy_state("src/shared.py")
        autodispatch.tick(self.lanes, state, 0)
        details = self.status()["_blocked_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["issue"], 9)
        self.assertEqual(details[0]["reason"], "hot_file")
        self.assertEqual(details[0]["files"], ["src/shared.py"])
        self.assertEqual(details[0]["wait_seconds"], 0)

    def test_the_waiting_time_grows_and_survives_the_ticks(self):
        clock = FakeClock(1_000_000.0)
        autodispatch.time = clock
        state = self.busy_state("src/shared.py")
        autodispatch.tick(self.lanes, state, 0)
        clock.now += 90
        autodispatch.tick(self.lanes, state, 0)
        details = self.status()["_blocked_details"]
        self.assertEqual(details[0]["wait_seconds"], 90)
        self.assertIn("9", state["_blocked_since"])   # the first observation is kept

    def test_a_card_that_is_no_longer_blocked_forgets_its_waiting_time(self):
        clock = FakeClock(1_000_000.0)
        autodispatch.time = clock
        state = self.busy_state("src/shared.py")
        autodispatch.tick(self.lanes, state, 0)
        clock.now += 30
        self.queue = []
        autodispatch.tick(self.lanes, state, 0)
        self.assertEqual(self.status()["_blocked_details"], [])
        self.assertEqual(state["_blocked_since"], {})

    def test_a_free_card_is_dispatched(self):
        state = self.busy_state("src/other.py")
        autodispatch.tick(self.lanes, state, 0)
        self.assertTrue([c for c in self.calls if "in-progress" in c])
        self.assertEqual(self.status()["_ready"], 0)   # the card left the queue
        self.assertEqual(self.status()["_blocked"], 0)


if __name__ == "__main__":
    unittest.main()
