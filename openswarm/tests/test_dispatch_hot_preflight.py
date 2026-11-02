"""Hot-file preflight of the mint path and the blocked details of the AD (Refs #1721).

The mint side (the planner mints a card before it enters the queue) and the dispatch
side (the AD hands the cards to the lanes) read one hot-file policy, so they never
disagree about a cap: the sample replay at the end replays the hot-file cards of the
2026-11-02 night and asserts that both sides block exactly the same cards.
"""
import importlib.util
import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_engine_hot_preflight", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)

HOTFILES_SPEC = importlib.util.spec_from_file_location(
    "dispatch_hotfiles_preflight", os.path.join(os.path.dirname(HERE), "lib", "dispatch", "hotfiles.py"))
hotfiles = importlib.util.module_from_spec(HOTFILES_SPEC)
HOTFILES_SPEC.loader.exec_module(hotfiles)

AUTODISPATCH_SPEC = importlib.util.spec_from_file_location(
    "autodispatch_hot_preflight_sample", os.path.join(REPO, "tools", "lane-autodispatch", "autodispatch.py"))
AUTODISPATCH = importlib.util.module_from_spec(AUTODISPATCH_SPEC)
AUTODISPATCH_SPEC.loader.exec_module(AUTODISPATCH)

SAMPLE = os.path.join(HERE, "fixtures", "hotfiles_ad_sample.json")
SHA = "a" * 40


class FakeRunner:
    """Answers commands by first matching substring rule; records every call."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.calls = []

    def __call__(self, cmd, timeout=120, inp=None, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        self.calls.append(text)
        for needle, result in self.rules:
            if needle in text:
                return result(text) if callable(result) else result
        return 0, "", ""

    def called(self, needle):
        return [c for c in self.calls if needle in c]


def issue(n, labels=(), body="", title=None):
    return {"number": n, "title": title or f"card {n}", "labels": [{"name": l} for l in labels], "body": body}


def card(n, body="", backend=None, prio=1):
    """One queue row, in the shape ``Dispatcher.queue`` returns."""
    return {"n": n, "title": f"card {n}", "body": body, "prio": prio, "backend": backend, "strict": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-hot-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg_path = os.path.join(self.tmp, "cfg.json")
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"],
               "hot_shared": {"src/shared.py": 1}}
        with open(self.cfg_path, "w") as stream:
            json.dump(raw, stream)
        self.cfg = E.load_config(self.cfg_path)

    def make(self, rules=None, dry_run=False):
        self.runner = FakeRunner(rules)
        d = E.Dispatcher(self.cfg, runner=self.runner, dry_run=dry_run, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        return d


class MintPreflight(Base):
    """The mint path: a card whose hot file is at its cap is not minted."""

    def test_a_card_at_its_cap_is_not_minted(self):
        d = self.make()
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        minted = []
        records = d.mint_cards([{"title": "改共享文件", "body": "热文件：src/shared.py"}],
                               {"P0": {"host": "box", "backend": "pi"}}, state,
                               mint=lambda card: minted.append(card) or "42")
        self.assertEqual(minted, [])                      # the card never reached GitHub
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertFalse(record["minted"])
        self.assertEqual(record["reason"], "hot_file_conflict")
        self.assertTrue(record["preflight"]["blocked"])
        self.assertEqual(record["preflight"]["conflicts"],
                         [{"file": "src/shared.py", "inflight": 1, "limit": 1}])
        self.assertEqual(record["preflight"]["advice"], "改期或换题")

    def test_a_card_below_its_cap_is_minted(self):
        d = self.make()
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/other.py"]}}
        records = d.mint_cards([{"title": "改共享文件", "body": "热文件：src/shared.py"}],
                               {"P0": {"host": "box", "backend": "pi"}}, state,
                               mint=lambda card: "42")
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["minted"])
        self.assertEqual(records[0]["issue"], "42")
        self.assertFalse(records[0]["preflight"]["blocked"])
        self.assertEqual(records[0]["preflight"]["advice"], "可铸卡")

    def test_the_mint_judges_the_in_flight_cards_like_the_ad_does(self):
        """The preflight reads the in-flight cards, so both sides block the same cards."""
        d = self.make()
        records = d.mint_cards([{"title": "a", "body": "热文件：src/shared.py"}],
                               {}, {}, mint=lambda card: "42")
        self.assertTrue(records[0]["minted"])   # nothing is in flight yet: neither side blocks
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        records = d.mint_cards([{"title": "a", "body": "热文件：src/shared.py"}],
                               {"P0": {"host": "box", "backend": "pi"}}, state, mint=lambda card: "42")
        self.assertFalse(records[0]["minted"])

    def test_a_batch_mints_one_card_per_hot_file(self):
        """Two cards of one batch on the same hot file: the second one would be blocked."""
        d = self.make()
        records = d.mint_cards([{"title": "a", "body": "热文件：src/shared.py"},
                                {"title": "b", "body": "热文件：src/shared.py"}],
                               {}, {}, mint=lambda card: "42")
        self.assertTrue(records[0]["minted"])
        self.assertFalse(records[1]["minted"])
        self.assertEqual(records[1]["reason"], "hot_file_conflict")
        self.assertEqual(records[1]["preflight"]["conflicts"],
                         [{"file": "src/shared.py", "inflight": 1, "limit": 1}])

    def test_the_refused_mint_is_kept_in_the_mint_ledger(self):
        d = self.make()
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        d.mint_cards([{"title": "改共享文件", "body": "热文件：src/shared.py"}],
                     {"P0": {"host": "box", "backend": "pi"}}, state, mint=lambda card: "42")
        with open(d.path("mint-log.json"), encoding="utf-8") as stream:
            ledger = json.load(stream)
        self.assertEqual(len(ledger), 1)
        self.assertFalse(ledger[0]["minted"])
        self.assertEqual(ledger[0]["reason"], "hot_file_conflict")
        self.assertTrue(ledger[0]["preflight"]["blocked"])
        self.assertEqual(ledger[0]["preflight"]["conflicts"][0]["file"], "src/shared.py")

    def test_the_mint_and_the_dispatch_side_read_the_same_caps(self):
        """One hot-file list: the policy file, not a second copy in the code."""
        d = self.make()
        self.assertEqual(d.hot_lock.cap_for("openswarm/lib/dispatch/engine.py"), 1)
        self.assertEqual(d.hot_lock.cap_for("src/shared.py"), 1)      # the dispatch config refines it
        self.assertEqual(d.hot_lock.cap_for("src/other.py"), 1)       # the default cap of the policy
        policy = E.dispatch_module("hot_policy").load_hot_policy(self.cfg)
        self.assertIn("openswarm/lib/dispatch/engine.py", policy["caps"][0])


class MintCli(Base):
    """The mint flow of the command line: refuse the conflicting card, mint the other."""

    def setUp(self):
        super().setUp()
        self.card = os.path.join(self.tmp, "card.md")
        with open(self.card, "w", encoding="utf-8") as stream:
            stream.write("# 改共享文件\nlabels: p1,backend:codex\n\n热文件：src/shared.py\n\n验收：`true`\n")
        os.makedirs(self.cfg["state_dir"], exist_ok=True)
        with open(self.cfg["lanes_file"], "w", encoding="utf-8") as stream:
            json.dump({"P0": {"host": "box", "backend": "pi"}}, stream)
        with open(os.path.join(self.cfg["state_dir"], "state.json"), "w", encoding="utf-8") as stream:
            json.dump({"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA,
                              "at": "2026-11-02 21:00:00", "hot": ["src/shared.py"]}}, stream)

    def test_the_cli_refuses_the_card_and_writes_the_ledger(self):
        calls = []

        def fake(cmd, timeout=120, inp=None, env=None):
            calls.append(" ".join(cmd) if isinstance(cmd, list) else cmd)
            return 0, "https://github.com/acme/project/issues/42\n", ""

        original = E.run_cmd
        E.run_cmd = fake
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = E.main(["dispatch", "mint", "--config", self.cfg_path, "--card", self.card])
        finally:
            E.run_cmd = original
        self.assertEqual(rc, 2)
        self.assertEqual([c for c in calls if "issue create" in c], [])
        with open(os.path.join(self.cfg["state_dir"], "mint-log.json"), encoding="utf-8") as stream:
            ledger = json.load(stream)
        self.assertFalse(ledger[0]["minted"])
        self.assertEqual(ledger[0]["preflight"]["conflicts"][0]["file"], "src/shared.py")

    def test_the_cli_mints_a_free_card_with_the_ready_label(self):
        calls = []

        def fake(cmd, timeout=120, inp=None, env=None):
            calls.append(" ".join(cmd) if isinstance(cmd, list) else cmd)
            return 0, "https://github.com/acme/project/issues/42\n", ""

        with open(os.path.join(self.cfg["state_dir"], "state.json"), "w", encoding="utf-8") as stream:
            json.dump({}, stream)
        original = E.run_cmd
        E.run_cmd = fake
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = E.main(["dispatch", "mint", "--config", self.cfg_path, "--card", self.card])
        finally:
            E.run_cmd = original
        self.assertEqual(rc, 0)
        created = [c for c in calls if "issue create" in c]
        self.assertEqual(len(created), 1)
        self.assertIn("lane-ready", created[0])


class BlockedDetails(Base):
    """The AD side: the blocked cards carry the reason, the files and the waiting time."""

    def tick(self, d, lanes, state, queue):
        d.probe = lambda _lanes, _state: {L: {"lane": "idle", "run": None} for L in lanes}
        d.harvest = lambda *_args: None
        d.sweep_leaked_inprogress = lambda *_args: None
        d.queue = lambda: list(queue)
        d.dispatch_reviews = lambda *_args: None
        d.replenish = lambda *_args: None
        d.tick(lanes, state)
        with open(d.path("lanes-status.json"), encoding="utf-8") as stream:
            return json.load(stream)

    def lanes(self):
        return {"P0": {"host": "box", "backend": "pi"}, "P1": {"host": "box", "backend": "pi"}}

    def test_a_hot_blocked_card_is_reported_with_its_files_and_its_waiting_time(self):
        d = self.make()
        d.clock = lambda: 1_000_000.0
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        queue = [card(9, body="热文件：src/shared.py")]
        status = self.tick(d, self.lanes(), state, queue)
        self.assertEqual(status["_ready"], 0)          # the card is blocked, not ready
        self.assertEqual(status["_blocked"], 1)
        self.assertEqual(status["_blocked_reasons"], {"deps": 0, "hot_file": 1})
        details = status["_blocked_details"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["issue"], 9)
        self.assertEqual(details[0]["reason"], "hot_file")
        self.assertEqual(details[0]["files"], ["src/shared.py"])
        self.assertEqual(details[0]["wait_seconds"], 0)

    def test_the_waiting_time_grows_and_survives_the_ticks(self):
        d = self.make()
        clock = {"now": 1_000_000.0}
        d.clock = lambda: clock["now"]
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        queue = [card(9, body="热文件：src/shared.py")]
        self.tick(d, self.lanes(), state, queue)
        clock["now"] += 90
        status = self.tick(d, self.lanes(), state, queue)
        self.assertEqual(status["_blocked_details"][0]["wait_seconds"], 90)
        # the first observation is kept in the state, so a restart does not reset the clock
        with open(d.path("state.json"), encoding="utf-8") as stream:
            saved = json.load(stream)
        self.assertIn("9", saved["_blocked_since"])

    def test_a_card_that_is_no_longer_blocked_forgets_its_waiting_time(self):
        d = self.make()
        clock = {"now": 1_000_000.0}
        d.clock = lambda: clock["now"]
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        queue = [card(9, body="热文件：src/shared.py")]
        self.tick(d, self.lanes(), state, queue)
        clock["now"] += 30
        status = self.tick(d, self.lanes(), state, [])       # the card left the queue
        self.assertEqual(status["_blocked_details"], [])
        self.assertEqual(state["_blocked_since"], {})

    def test_a_card_waiting_for_its_dependency_is_reported_as_well(self):
        d = self.make()
        d.clock = lambda: 1_000_000.0
        d.deps_met = lambda item: False
        status = self.tick(d, self.lanes(), {}, [card(9, body="依赖：#3")])
        self.assertEqual(status["_ready"], 0)
        self.assertEqual(status["_blocked"], 1)
        self.assertEqual(status["_blocked_reasons"], {"deps": 1, "hot_file": 0})
        self.assertEqual(status["_blocked_details"][0]["reason"], "deps")
        self.assertEqual(status["_blocked_details"][0]["files"], [])

    def test_the_replenish_watermark_uses_the_same_count_as_ready(self):
        d = self.make()
        d.clock = lambda: 1_000_000.0
        state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                        "hot": ["src/shared.py"]}}
        queue = [card(9, body="热文件：src/shared.py")]
        needs = []
        d.probe = lambda _lanes, _state: {L: {"lane": "idle", "run": None} for L in self.lanes()}
        d.harvest = lambda *_args: None
        d.sweep_leaked_inprogress = lambda *_args: None
        d.queue = lambda: list(queue)
        d.dispatch_reviews = lambda *_args: None
        d.replenish = lambda need, *_args: needs.append(need)
        d.cfg["replenish"] = {"cmd": ["true"], "low": 1, "target": 5}
        d.tick(self.lanes(), state)
        self.assertEqual(needs, [5])       # the blocked card does not count as supply


class SampleReplay(Base):
    """Replay the hot-file cards of the 2026-11-02 night: both sides block the same cards.

    The sample holds the real cards the AD imports from ``docs/backlog/next/``: the
    replay reads them from the repository, so the sample cannot drift away from the
    cards the AD really sees.
    """

    def setUp(self):
        super().setUp()
        with open(SAMPLE, encoding="utf-8") as stream:
            self.sample = json.load(stream)

    def cases(self):
        """Every sample case with the body of its card, read from the repository."""
        for case in self.sample["cases"]:
            with open(os.path.join(REPO, case["card"]), encoding="utf-8") as stream:
                yield case, stream.read()

    def test_the_sample_is_not_empty(self):
        self.assertTrue(self.sample["cases"])

    def test_the_sample_cards_are_the_cards_of_the_repository(self):
        """The sample is traceable: the card files are the ones the AD imports."""
        for case, text in self.cases():
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            self.assertEqual(digest, case["card_sha256"], case["card"])

    def test_the_sample_cards_declare_the_hot_files_of_the_sample(self):
        for case, text in self.cases():
            declared = sorted(hotfiles.parse_hot_files(text))
            self.assertEqual(declared, sorted(case["hot_files"]), case["card"])

    def test_the_mint_preflight_agrees_with_the_ad_blocking_decision(self):
        d = self.make()
        for case, text in self.cases():
            inflight = case["inflight"]
            ad = d.hot_lock.blocked(d.hot_of(text), inflight)
            mint = d.preflight_card(text, inflight)
            self.assertEqual(mint["blocked"], ad, case["card"])
            self.assertEqual(mint["blocked"], case["blocked"], case["card"])
            self.assertEqual(bool(mint["conflicts"]), ad, case["card"])

    def test_the_ad_of_the_backlog_import_agrees_with_the_mint_side(self):
        """The AD and the dispatch engine read one hot-file list, so they agree."""
        d = self.make()
        for case, text in self.cases():
            inflight = case["inflight"]
            ad = AUTODISPATCH.hot_verdict(text, inflight)
            mint = d.preflight_card(text, inflight)
            self.assertEqual(ad["blocked"], mint["blocked"], case["card"])
            self.assertEqual(ad["blocked"], case["blocked"], case["card"])
            self.assertEqual([c["file"] for c in ad["conflicts"]],
                             [c["file"] for c in mint["conflicts"]], case["card"])

    def test_the_conflict_names_the_file_that_is_at_its_cap(self):
        d = self.make()
        case = [c for c in self.sample["cases"]
                if c["inflight"] == {"openswarm/bin/openswarm": 1}][0]
        _, text = next((c, t) for c, t in self.cases() if c is case)
        verdict = d.preflight_card(text, case["inflight"])
        self.assertTrue(verdict["blocked"])
        self.assertEqual([c["file"] for c in verdict["conflicts"]], ["openswarm/bin/openswarm"])
        self.assertEqual(verdict["advice"], "改期或换题")

    def test_a_documentation_file_never_blocks(self):
        d = self.make()
        for case, text in self.cases():
            if all(path.startswith("docs/") for path in case["hot_files"]):
                self.assertFalse(d.preflight_card(text, case["inflight"])["blocked"], case["card"])

    def test_the_mint_of_the_sample_blocks_exactly_the_cards_the_ad_blocks(self):
        d = self.make()
        for case, text in self.cases():
            state = {"P0": {"issue": 7, "run": "r1", "host": "box", "base": SHA, "at": "2026-11-02 21:00:00",
                            "hot": [path for path, count in case["inflight"].items() for _ in range(count)]}}
            records = d.mint_cards([{"title": case["card"], "body": text}],
                                   {"P0": {"host": "box", "backend": "pi"}}, state, mint=lambda card: "1")
            record = records[0]
            self.assertEqual(record["preflight"]["blocked"], case["blocked"], case["card"])
            self.assertEqual(record["minted"], not case["blocked"], case["card"])


class CardFile(Base):
    """The card files the planner writes and the mint path reads."""

    def test_the_title_the_labels_and_the_body_are_read(self):
        card = E.dispatch_module("card").parse_card(
            "# 改共享文件\nlabels: p1,backend:codex\n\n热文件：src/shared.py\n\n验收：`true`\n")
        self.assertEqual(card["title"], "改共享文件")
        self.assertEqual(card["labels"], ["p1", "backend:codex"])
        self.assertIn("热文件：src/shared.py", card["body"])

    def test_a_card_without_labels_has_none(self):
        card = E.dispatch_module("card").parse_card("# 标题\n\n正文\n")
        self.assertEqual(card["title"], "标题")
        self.assertEqual(card["labels"], [])
        self.assertEqual(card["body"], "正文")


class HotPolicy(Base):
    """One hot-file list: the policy file, read by the mint side and by the AD side."""

    def setUp(self):
        super().setUp()
        self.policy_module = E.dispatch_module("hot_policy")

    def test_the_shipped_policy_carries_the_shared_list(self):
        policy = self.policy_module.load_hot_policy(self.cfg)
        shipped = policy["caps"][0]
        self.assertEqual(shipped.get("openswarm/lib/dispatch/engine.py"), 1)
        self.assertEqual(policy["default_limit"], 1)

    def test_a_local_policy_refines_the_shipped_one(self):
        local = os.path.join(self.tmp, "policy.local.json")
        with open(local, "w", encoding="utf-8") as stream:
            json.dump({"dispatch": {"hot_shared": {"src/shared.py": 3},
                                    "hot_union": ["docs/notes.md"], "hot_default": 2}}, stream)
        cfg = dict(self.cfg)
        cfg["policy_file"] = local
        policy = self.policy_module.load_hot_policy(cfg)
        self.assertEqual(policy["caps"][-1], {"src/shared.py": 3})
        self.assertEqual(policy["union"], ["docs/notes.md"])
        self.assertEqual(policy["default_limit"], 2)

    def test_a_broken_policy_file_is_skipped(self):
        broken = os.path.join(self.tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as stream:
            stream.write("{not json")
        policy = self.policy_module.load_hot_policy(self.cfg, paths=[broken])
        self.assertEqual(policy["caps"], [])
        self.assertIsNone(policy["default_limit"])


if __name__ == "__main__":
    unittest.main()
