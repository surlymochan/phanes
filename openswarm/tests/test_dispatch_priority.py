"""Dispatch priority scoring (lib/dispatch/priority.py): big cards, wait lift, backend match, starvation.

Covers the pool order of Refs #1705: a card marked with a 90-minute budget must not
stay at the tail of the queue behind the 30-minute cards, and a small card must not
be starved by the big ones.
"""
import datetime
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCH = os.path.join(os.path.dirname(HERE), "lib", "dispatch")


def _load(name):
    spec = importlib.util.spec_from_file_location("dispatch_%s_under_test" % name,
                                                  os.path.join(DISPATCH, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = _load("priority")
E = _load("engine")

NOW = 1_793_700_000.0
THRESHOLD = 1800.0
# The creation time of the test cards: two minutes before the clock of the dispatcher.
CREATED = datetime.datetime.fromtimestamp(NOW - 120, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def card(n, budget=None, backend=None, prio=1, labels=(), body="", waited_s=None):
    """One card record of the pool."""
    record = {"n": n, "prio": prio, "backend": backend, "labels": list(labels), "body": body}
    if budget is not None:
        record["budget"] = budget
    if waited_s is not None:
        record["waited_s"] = waited_s
    return record


class BudgetMarker(unittest.TestCase):
    """The card's own size: the budget label and the budget body line."""

    def test_label_carries_the_budget(self):
        self.assertEqual(P.budget_minutes(card(1, labels=["budget:90"])), 90)
        self.assertEqual(P.budget_minutes(card(1, labels=["lane-ready", "budget:45"])), 45)

    def test_body_line_carries_the_budget(self):
        self.assertEqual(P.budget_minutes(card(1, body="预算：90\n验收：`pytest`")), 90)
        self.assertEqual(P.budget_minutes(card(1, body="Budget: 60")), 60)

    def test_label_wins_over_the_body_line(self):
        self.assertEqual(P.budget_minutes(card(1, labels=["budget:90"], body="预算：30")), 90)

    def test_a_card_without_a_marker_has_no_size(self):
        self.assertIsNone(P.budget_minutes(card(1)))
        self.assertIsNone(P.budget_minutes(card(1, labels=["budget:many"])))
        self.assertIsNone(P.budget_minutes(card(1, body="预算：很多")))

    def test_created_at_parses_the_github_timestamp(self):
        self.assertEqual(P.created_at("2026-11-02T09:00:00Z"), 1793610000.0)
        self.assertEqual(P.created_at(CREATED), NOW - 120)
        self.assertIsNone(P.created_at("not a timestamp"))
        self.assertIsNone(P.created_at(None))


class Score(unittest.TestCase):
    """The three score components and the starvation lift."""

    def test_big_card_bonus(self):
        big = P.score_card(card(1, budget=90), "pi", NOW)
        small = P.score_card(card(2, budget=30), "pi", NOW)
        self.assertTrue(big["big"])
        self.assertFalse(small["big"])
        self.assertGreater(big["size"], small["size"])
        self.assertEqual(small["size"], 0.0)          # the 30-minute card is the baseline
        self.assertGreater(big["score"], small["score"])
        self.assertEqual(P.score_card(card(1, budget=90), "pi", NOW)["score"],
                         P.score_card(card(2, budget=90), "pi", NOW)["score"])

    def test_wait_lifts_the_score(self):
        waited = P.score_card(card(1, budget=30, waited_s=600), "pi", NOW)
        fresh = P.score_card(card(2, budget=30, waited_s=0), "pi", NOW)
        self.assertEqual(waited["wait"], 10.0)
        self.assertGreater(waited["score"], fresh["score"])
        self.assertLess(P.sort_key(card(1, waited_s=600), "pi", NOW),
                        P.sort_key(card(2, waited_s=0), "pi", NOW))

    def test_backend_match_points(self):
        match = P.score_card(card(1, backend="pi"), "pi", NOW)
        unpinned = P.score_card(card(2, backend=None), "pi", NOW)
        fallback = P.score_card(card(3, backend="codex"), "pi", NOW,
                                fallback={"pi": ["codex", "copilot", "agy"]})
        self.assertEqual(match["backend_match"], "match:pi")
        self.assertEqual(unpinned["backend_match"], "unpinned")
        self.assertTrue(fallback["backend_match"].startswith("fallback:codex"))
        self.assertGreater(match["backend"], unpinned["backend"])
        self.assertGreater(unpinned["backend"], fallback["backend"])

    def test_the_fallback_chain_decides_the_distance(self):
        first = P.score_card(card(1, backend="codex"), "pi", NOW,
                             fallback={"pi": ["codex", "copilot", "agy"]})
        last = P.score_card(card(2, backend="agy"), "pi", NOW,
                            fallback={"pi": ["codex", "copilot", "agy"]})
        self.assertGreater(first["backend"], last["backend"])

    def test_starvation_threshold_lifts_a_small_card(self):
        starved = P.score_card(card(1, budget=30, waited_s=THRESHOLD), "pi", NOW)
        big = P.score_card(card(2, budget=90, waited_s=THRESHOLD - 60), "pi", NOW)
        self.assertTrue(starved["starved"])
        self.assertEqual(starved["starvation"], 60.0)
        self.assertGreater(starved["score"], big["score"])
        self.assertLess(P.sort_key(card(1, budget=30, waited_s=THRESHOLD), "pi", NOW),
                        P.sort_key(card(2, budget=90, waited_s=THRESHOLD - 60), "pi", NOW))

    def test_below_the_threshold_nothing_is_lifted(self):
        below = P.score_card(card(1, budget=30, waited_s=THRESHOLD - 1), "pi", NOW)
        self.assertFalse(below["starved"])
        self.assertEqual(below["starvation"], 0.0)

    def test_a_big_card_is_not_lifted_by_default(self):
        big = P.score_card(card(1, budget=90, waited_s=10 * THRESHOLD), "pi", NOW)
        self.assertFalse(big["starved"])
        lifted = P.score_card(card(1, budget=90, waited_s=10 * THRESHOLD), "pi", NOW,
                              {"starvation_lift_big_cards": True})
        self.assertTrue(lifted["starved"])

    def test_the_weights_are_configurable(self):
        cfg = {"wait_weight": 0.0, "big_bonus": 0.0, "size_weight": 0.0}
        big = P.score_card(card(1, budget=90, waited_s=600), "pi", NOW, cfg)
        small = P.score_card(card(2, budget=30, waited_s=0), "pi", NOW, cfg)
        self.assertEqual(big["score"], small["score"])   # neither the wait nor the size counts


def night_pool(minutes=40):
    """The pool of the night: a big card that waits while the small cards arrive.

    One lane is free per minute.  The big card is pinned to another backend, so the
    old order always put it behind the unpinned small cards; the small cards keep
    arriving, so it never got a lane.
    """
    sequence = [{"at": 0.0, "cards": [card(1, budget=30), card(2, budget=90, backend="codex")]}]
    for minute in range(1, minutes + 1):
        sequence.append({"at": minute * 60.0, "cards": [card(100 + minute, budget=30)]})
    for drain in range(1, minutes + 2):       # the pool drains after the last arrival
        sequence.append({"at": (minutes + drain) * 60.0, "cards": []})
    return sequence


def starved_pool(big_cards=40):
    """A pool of big cards and one small card: the small card must not starve."""
    cards = [card(n, budget=90) for n in range(1, big_cards + 1)]
    cards.append(card(999, budget=30))
    sequence = [{"at": 0.0, "cards": list(cards)}]
    for minute in range(1, 45):
        sequence.append({"at": minute * 60.0, "cards": []})
    return sequence


class Replay(unittest.TestCase):
    """The replay of a recorded pool: the acceptance of Refs #1705."""

    def test_the_big_cards_wait_less_than_with_the_old_order(self):
        old = P.replay_pool(night_pool(), backend="pi", mode="fifo")
        new = P.replay_pool(night_pool(), backend="pi", mode="score")
        self.assertEqual(len(new["big_waits"]), 1)
        self.assertEqual(len(old["big_waits"]), 1)
        self.assertGreater(old["big_wait_median"], 1800.0)     # the big card was stuck
        self.assertLess(new["big_wait_median"], old["big_wait_median"])
        self.assertEqual(new["big_wait_median"], 0.0)

    def test_the_small_cards_stay_under_the_threshold(self):
        new = P.replay_pool(night_pool(), backend="pi", mode="score")
        self.assertTrue(new["small_waits"])
        self.assertLessEqual(new["small_wait_max"], THRESHOLD)

    def test_a_small_card_is_lifted_at_the_threshold(self):
        old = P.replay_pool(starved_pool(), backend="pi", mode="fifo")
        new = P.replay_pool(starved_pool(), backend="pi", mode="score")
        self.assertGreater(old["small_wait_max"], THRESHOLD)   # the old order starved it
        self.assertLessEqual(new["small_wait_max"], THRESHOLD)
        self.assertEqual(new["small_wait_max"], THRESHOLD)     # lifted exactly at the threshold

    def test_the_replay_reports_the_cards_it_could_not_place(self):
        result = P.replay_pool([{"at": 0.0, "cards": [card(1, budget=30)]}], lanes_per_tick=0)
        self.assertEqual(result["left"], [1])
        self.assertEqual(result["dispatched"], [])


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


def issue(n, labels=(), body="", title=None):
    return {"number": n, "title": title or f"card {n}", "labels": [{"name": l} for l in labels],
            "body": body, "createdAt": CREATED}


class EngineOrder(unittest.TestCase):
    """The dispatcher takes the card with the best score and logs why."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-priority-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"]}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as stream:
            json.dump(raw, stream)
        self.cfg = E.load_config(path)

    def make(self, issues, rules=None):
        runner = FakeRunner([("issue list", (0, json.dumps(issues), ""))] + list(rules or []))
        dispatcher = E.Dispatcher(self.cfg, runner=runner, dry_run=False, clock=lambda: NOW,
                                  out=open(os.devnull, "w"))
        self.addCleanup(dispatcher.out.close)
        return dispatcher

    def dispatch_order(self, dispatcher, state=None, lanes=None):
        """Dispatch the queue and return the issue numbers in the order they went out."""
        lanes = lanes or {"P1": {"host": "box", "backend": "pi"}}
        state = state if state is not None else {}
        dispatcher.main_sha = lambda: "a" * 40
        dispatcher.label_state = lambda n: "fresh"
        dispatcher.dispatch = lambda lane, lc, item, base: (f"run-{item['n']}", "verify")
        queue = dispatcher.queue(state)
        dispatcher.assign_issues(lanes, {name: {"lane": "idle"} for name in lanes}, state, queue)
        return [state[name]["issue"] for name in lanes if name in state], state

    def test_a_big_card_goes_before_the_small_card(self):
        dispatcher = self.make([issue(1, ["lane-ready", "budget:30"]),
                                issue(2, ["lane-ready", "budget:90", "backend:codex"])])
        order, _ = self.dispatch_order(dispatcher)
        self.assertEqual(order, [2])       # the big card is not stuck behind the small one

    def test_the_old_order_is_restored_when_the_score_is_off(self):
        dispatcher = self.make([issue(1, ["lane-ready", "budget:30"]),
                                issue(2, ["lane-ready", "budget:90", "backend:codex"])])
        dispatcher.P["enabled"] = False
        order, _ = self.dispatch_order(dispatcher)
        self.assertEqual(order, [1])       # the FIFO order: the unpinned card first

    def test_the_wait_lifts_a_card(self):
        dispatcher = self.make([issue(1, ["lane-ready"]), issue(2, ["lane-ready"])])
        state = {"_pool_seen": {"1": {"at": NOW - 3600}, "2": {"at": NOW}}}
        order, _ = self.dispatch_order(dispatcher, state)
        self.assertEqual(order, [1])       # the card that waited an hour goes first

    def test_the_backend_constraints_still_win(self):
        dispatcher = self.make([issue(1, ["lane-ready", "budget:90", "backend:codex", "strict-backend"])])
        order, _ = self.dispatch_order(dispatcher)
        self.assertEqual(order, [])        # a strict pin is never taken by another backend

    def test_the_dispatch_log_carries_the_score(self):
        dispatcher = self.make([issue(2, ["lane-ready", "budget:90", "backend:codex"])])
        dispatcher.out = open(os.path.join(self.tmp, "log.txt"), "w")
        self.addCleanup(dispatcher.out.close)
        self.dispatch_order(dispatcher)
        dispatcher.out.flush()
        with open(os.path.join(self.tmp, "log.txt")) as stream:
            log = stream.read()
        self.assertIn("score=", log)
        self.assertIn("budget 90m", log)
        self.assertIn("waited 120s", log)

    def test_the_wait_is_measured_from_the_moment_the_card_entered_the_pool(self):
        dispatcher = self.make([issue(1, ["lane-ready"])])
        state = {}
        dispatcher.main_sha = lambda: "a" * 40
        dispatcher.label_state = lambda n: "fresh"
        queue = dispatcher.queue(state)
        self.assertEqual(queue[0]["waited_s"], NOW - P.created_at(CREATED))
        # a card that comes back to the pool starts its wait over
        state["_pool_seen"]["1"]["dispatched"] = True
        queue = dispatcher.queue(state)
        self.assertEqual(queue[0]["waited_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
