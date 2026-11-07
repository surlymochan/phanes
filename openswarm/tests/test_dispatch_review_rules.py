"""The review rules version hash of a review event (Refs #1852).

Every review event carries the hash of the rules the reviewer worked by: the review prompt.
The hash only *names* the rules - the prompt is never changed by it - so that a change of the
rules shows up as a break in the metrics instead of a silent drift of the review outcome.
It is the first 12 hex digits of the sha256 of the prompt text and of nothing else, so that
anyone who reads the event can recompute it; the tests below compute the expected value
themselves (hashlib), they do not ask the dispatcher for it.

The hash of a review is the one of its dispatch: a review that ran under other rules keeps the
hash of those rules, not the ones of the moment it is harvested.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dispatch_engine_rules_test",
                                               os.path.join(HERE, "..", "lib", "dispatch", "engine.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

SHA = "a" * 40
PROMPT = "Check the diff.\nVERDICT: APPROVE (or VERDICT: CHANGES)\n"
# The hash of the default review prompt, pinned: the first 12 hex digits of the sha256 of that
# prompt.  A change of the default prompt has to change this value on purpose - the hash exists
# to make a change of the rules visible (Refs #1852).
DEFAULT_PROMPT_HASH = "7e1e63682f69"


def expected_hash(text):
    """The expected version hash of a rules text, computed here and not by the dispatcher."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dispatch-rules-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        raw = {"repo": "acme/project", "local_repo": self.tmp, "state_dir": os.path.join(self.tmp, "state"),
               "ssh": {"control_dir": os.path.join(self.tmp, "cm")}, "local_hosts": ["box"]}
        path = os.path.join(self.tmp, "cfg.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        self.cfg = E.load_config(path)

    def make(self, rules=None, dry_run=False):
        self.calls = []

        def runner(cmd, **kwargs):
            text = " ".join(str(c) for c in cmd)
            self.calls.append(text)
            for needle, result in rules or []:
                if needle in text:
                    return result(text) if callable(result) else result
            return 0, "", ""

        d = E.Dispatcher(self.cfg, runner=runner, dry_run=dry_run, out=open(os.devnull, "w"))
        self.addCleanup(d.out.close)
        return d

    def called(self, needle):
        return [c for c in self.calls if needle in c]

    def review_dispatcher(self, verdict="VERDICT: APPROVE\nSUMMARY: ok\n", pr=20):
        """A dispatcher whose lane branch holds a verdict for the PR."""
        answer = json.dumps({"headRefOid": SHA, "state": "OPEN",
                             "headRefName": "fleet/M5-issue200-r", "body": "Closes #200"})
        return self.make([("git -C", (0, verdict, "")),
                          ("pr view %d" % pr, (0, answer, ""))])


class RulesHashTests(unittest.TestCase):
    """The hash of a rules text: stable for the same text, another one for a changed text."""

    def test_the_same_prompt_hashes_to_the_same_value(self):
        self.assertEqual(E.rules_hash(PROMPT), E.rules_hash(PROMPT))
        self.assertRegex(E.rules_hash(PROMPT), r"^[0-9a-f]{12}$")

    def test_one_changed_character_changes_the_hash(self):
        self.assertNotEqual(E.rules_hash(PROMPT), E.rules_hash(PROMPT + "!"))
        self.assertNotEqual(E.rules_hash(PROMPT), E.rules_hash("Check the diff.\nVERDICT: APPROVE"
                                                                " (or VERDICT: CHANGES)\n "))

    def test_an_empty_prompt_still_has_a_hash(self):
        self.assertRegex(E.rules_hash(""), r"^[0-9a-f]{12}$")

    def test_the_hash_is_the_sha256_of_the_text(self):
        """The hash is the first 12 hex digits of the sha256 - computed here, not by the code."""
        self.assertEqual(E.rules_hash(PROMPT), expected_hash(PROMPT))

    def test_the_hash_of_the_default_prompt_is_the_pinned_one(self):
        """The default review prompt hashes to the pinned value (Refs #1852).

        The pin is the visible sign of a changed prompt: a change of the rules has to update
        this value deliberately, it must not slip through unnoticed.
        """
        default_prompt = E.DEFAULTS["templates"]["review_task"]
        self.assertEqual(expected_hash(default_prompt), DEFAULT_PROMPT_HASH)
        self.assertEqual(E.rules_hash(default_prompt), DEFAULT_PROMPT_HASH)


class ReviewRulesHashTests(Base):
    """The hash of the rules of a dispatcher: the hash of its review prompt and nothing else."""

    def test_the_hash_is_the_hash_of_the_review_prompt(self):
        d = self.make()
        prompt = d.cfg["templates"]["review_task"]
        self.assertEqual(d.review_rules_hash(), expected_hash(prompt))

    def test_the_hash_covers_the_prompt_and_nothing_else(self):
        """The prompt text alone is hashed: no other text is mixed into the hash."""
        d = self.make()
        prompt = d.cfg["templates"]["review_task"]
        self.assertNotEqual(d.review_rules_hash(), expected_hash(prompt + "\n"))
        d.cfg["templates"]["rules"] = d.cfg["templates"]["rules"] + "\nOne more rule."
        self.assertEqual(d.review_rules_hash(), expected_hash(prompt))


class ReviewEventTests(Base):
    """The review event (the review record and the published comment) names its rules."""

    def test_the_review_record_carries_the_rules_hash(self):
        d = self.review_dispatcher()
        st = {"issue": "PR20", "run": "r1", "review_pr": 20, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {}))
        record = d.load_reviews()["20"]
        self.assertEqual(record["rules_hash"], expected_hash(d.cfg["templates"]["review_task"]))
        self.assertIn("rules_hash=%s -->" % record["rules_hash"], self.called("pr comment 20")[0])

    def test_a_failed_review_event_carries_the_rules_hash(self):
        d = self.make()
        st = {"issue": "PR21", "run": "r2", "review_pr": 21, "review_sha": SHA}
        self.assertTrue(d.finish_review("M5", {"backend": "pi"}, st,
                                        {"status": "failed", "error_class": "exec_error"}, {}))
        self.assertEqual(d.load_reviews()["21"]["rules_hash"], expected_hash(d.cfg["templates"]["review_task"]))

    def test_the_event_keeps_the_rules_hash_of_its_dispatch(self):
        """A review that ran under other rules keeps the hash of those rules."""
        d = self.review_dispatcher()
        st = {"issue": "PR20", "run": "r1", "review_pr": 20, "review_sha": SHA, "rules_hash": "0123456789ab"}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {}))
        self.assertEqual(d.load_reviews()["20"]["rules_hash"], "0123456789ab")
        self.assertIn("rules_hash=0123456789ab -->", self.called("pr comment 20")[0])

    def test_a_changed_prompt_changes_the_hash_of_the_next_event(self):
        d = self.review_dispatcher()
        before = d.review_rules_hash()
        d.cfg["templates"]["review_task"] = d.cfg["templates"]["review_task"] + "\nOne more rule."
        self.assertNotEqual(d.review_rules_hash(), before)
        st = {"issue": "PR20", "run": "r1", "review_pr": 20, "review_sha": SHA}
        info = {"branch": "lane/x", "commit": "c", "backend": "pi", "model": "m"}
        self.assertTrue(d.finish_review("M5", {"host": "box", "backend": "pi"}, st, info, {}))
        self.assertEqual(d.load_reviews()["20"]["rules_hash"], expected_hash(d.cfg["templates"]["review_task"]))


class DispatchTests(Base):
    """The dispatch records the rules version of the review it sends out."""

    def dispatch_review(self, d):
        d.idle_lanes = lambda *args: ["M1"]
        d.main_sha = lambda: SHA
        d.dispatch = lambda *args: ("run", "verify")
        d.review_item = lambda item: {"n": item["number"], "title": "review", "body": ""}
        d.review_candidates = lambda *args: [{"number": 11, "headRefOid": SHA, "headRefName": "fleet/M5-issue101-r"}]
        state = {}
        d.dispatch_reviews({"M1": {"host": "box", "backend": "pi"}}, {"M1": {"lane": "idle"}}, state, [])
        return state

    def test_the_lane_state_carries_the_rules_hash(self):
        d = self.make()
        state = self.dispatch_review(d)
        self.assertEqual(state["M1"]["rules_hash"], expected_hash(d.cfg["templates"]["review_task"]))
        self.assertRegex(state["M1"]["rules_hash"], r"^[0-9a-f]{12}$")

    def test_the_dispatch_log_names_the_rules_hash(self):
        d = self.make()
        state = self.dispatch_review(d)
        with open(d.path(d.cfg["log_file"])) as stream:
            log = stream.read()
        self.assertIn("rules %s" % state["M1"]["rules_hash"], log)
