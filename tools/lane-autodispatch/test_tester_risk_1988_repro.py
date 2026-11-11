#!/usr/bin/env python3
"""Reproduction of #1988 "Classify tester evidence review risk" (L2 batch BUNDLE-AUTODISPATCH-1-CLEAN).

Written on the batch base `762785945` **without** the implementation: the card asks for a
pure classifier that reads the diff statistics of the target PR, the historical
`needs-human` marks and a configurable sampling hash, and that answers `full_read` or
`sampled`; the decision and its reason belong to the card state.

On that base the reproduction fails, because

  * `tools/lane-autodispatch/tester_risk.py` (the classifier) does not exist at all, and
  * the dispatcher (`tools/lane-autodispatch/autodispatch.py`) neither classifies a verify
    card nor keeps a decision with the card - `tools/cards/verify-intent-template.md:9-15`
    lists the risk level as optional text only and the verify validation reads neither the
    risk nor the diff.

The file name is deliberately *not* `test_tester_risk.py`: the open bundle PR #2125 already
carries that canonical path (the acceptance command of the card), so a second file with the
same name would collide with it. Both files can live side by side.

Run it with the pytest command of the card or with the unittest discovery the Swrm gate
runs in this directory (`python -m unittest discover -s . -p 'test_*.py'`); no pytest-only
feature is used here. Everything runs offline: no network, no `gh`, no control plane.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AD_DIR = Path(__file__).resolve().parent
AD_SCRIPT = AD_DIR / "autodispatch.py"
CLASSIFIER = AD_DIR / "tester_risk.py"
SCRATCH = Path(__file__).resolve().parents[2] / ".swrm-tmp"  # gitignored scratch root

VERIFY_BODY = "## Verify Intent\n目标 PR: #1988\n证据要求:\n- [ ] 测试日志"
ROUTINE_DIFF = {"files_changed": 2, "additions": 12, "deletions": 3}


def load_classifier():
    """The pure classifier of #1988 (`tools/lane-autodispatch/tester_risk.py`)."""
    if not CLASSIFIER.exists():
        raise AssertionError(
            f"{CLASSIFIER} does not exist: the dispatcher has no reviewer risk classifier, "
            "so a verify card with a huge diff and a verify card with a one line diff are read "
            "the same way (#1988)")
    spec = importlib.util.spec_from_file_location("tester_risk_repro", CLASSIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "classify", None)):
        raise AssertionError(
            f"{CLASSIFIER} exposes no pure classify() function: the review depth of a verify "
            "card cannot be decided (#1988)")
    return module


def load_dispatcher():
    """The dispatcher that must classify a verify card and keep the decision (#1988)."""
    spec = importlib.util.spec_from_file_location("autodispatch_risk_repro", AD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify(module, diff_stats, history=(), *, pr_number=1988, sample_rate=0.2, seed="risk-v1"):
    """Call the pure classifier with the inputs the card names.

    The card names the inputs, not the parameter names, so the call is tried in the
    documented shapes; a classifier that takes none of them is a failure, not a skip.
    """
    configs = (dict(pr_number=pr_number, sample_rate=sample_rate, seed=seed),
               dict(pr=pr_number, sample_rate=sample_rate, seed=seed))
    attempts = []
    for config in configs:
        attempts.append(lambda config=config: module.classify(
            diff_stats=diff_stats, needs_human_history=history, **config))
        attempts.append(lambda config=config: module.classify(diff_stats, history, **config))
        attempts.append(lambda config=config: module.classify(diff_stats, **config))
    error = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as failure:
            error = failure
    raise AssertionError(f"the classifier takes none of the inputs of #1988: {error}")


def decision_of(dispatcher, body):
    """The dispatcher's own decision for a verify card body."""
    for name in ("tester_risk_decision", "tester_risk_for_card", "classify_tester_risk"):
        helper = getattr(dispatcher, name, None)
        if callable(helper):
            return helper(body)
    raise AssertionError(
        "the dispatcher has no tester risk classification: a verify card is read the same way "
        "whatever its diff and its needs-human history (#1988)")


def record_with_card(dispatcher, state, issue, decision):
    """Write the decision into the card state."""
    for name in ("record_tester_risk", "keep_tester_risk", "save_tester_risk"):
        helper = getattr(dispatcher, name, None)
        if callable(helper):
            helper(state, issue, decision)
            return
    raise AssertionError(
        "the dispatcher keeps no decision with the card: the review depth and its reason are "
        "lost as soon as the lane slot is released (#1988)")


def find_in_state(state, decision):
    """The key under which the state keeps the decision (None when it keeps none)."""
    if isinstance(state, dict):
        for key, value in state.items():
            if value == decision:
                return key
            found = find_in_state(value, decision)
            if found is not None:
                return found
    elif isinstance(state, list):
        for item in state:
            found = find_in_state(item, decision)
            if found is not None:
                return found
    return None


def fake_reader(pull_request, events=("[]",)):
    """A `gh api` reader that answers the pull request and its label history."""
    def reader(cmd, **kwargs):
        if list(cmd[:2]) == ["gh", "api"]:
            if "/events" in cmd[2]:
                return 0, "\n".join(events), ""
            return 0, json.dumps(pull_request), ""
        raise AssertionError(f"unexpected command: {cmd}")
    return reader


class TesterRiskClassifierTests(unittest.TestCase):
    """The pure classifier the card asks for (Refs #1988)."""

    def setUp(self):
        self.module = load_classifier()

    def test_a_large_diff_always_needs_a_full_read(self):
        result = classify(self.module, {"files_changed": 120, "additions": 6000, "deletions": 4000})

        self.assertEqual(result["decision"], "full_read")
        self.assertTrue(result["reason"], "the decision must carry its reason")
        self.assertIn("diff", result["reason"].lower())

    def test_a_needs_human_history_always_needs_a_full_read(self):
        result = classify(self.module, ROUTINE_DIFF, history=[False, True, False])

        self.assertEqual(result["decision"], "full_read")
        self.assertTrue(result["reason"], "the decision must carry its reason")
        self.assertRegex(result["reason"].lower(), r"human|history")

    def test_a_routine_card_is_sampled_stably_for_one_pr_and_seed(self):
        first = classify(self.module, ROUTINE_DIFF, pr_number=1988, seed="risk-v1")
        second = classify(self.module, ROUTINE_DIFF, pr_number=1988, seed="risk-v1")

        self.assertEqual(first, second, "the same PR and seed must decide the same way")
        self.assertIn(first["decision"], {"full_read", "sampled"})
        self.assertTrue(first["reason"], "the decision must carry its reason")

    def test_the_sample_rate_decides_how_many_routine_cards_are_read_in_full(self):
        # The bundle documents the knob as AD_TESTER_RISK_SAMPLE_RATE: 0 = every routine card
        # is sampled, 1 = every routine card is read in full.
        for rate, expected in ((0, "sampled"), (1, "full_read")):
            for pr_number in (7, 1988, 4242):
                with self.subTest(rate=rate, pr_number=pr_number):
                    result = classify(self.module, {"files_changed": 1, "additions": 3, "deletions": 2},
                                      pr_number=pr_number, sample_rate=rate, seed="risk-v1")
                    self.assertEqual(result["decision"], expected)

    def test_missing_or_malformed_data_fails_closed_to_a_full_read(self):
        cases = (
            (None, ()),
            ("many", ()),
            ({"files_changed": "many", "additions": 1, "deletions": 1}, ()),
            ({"files_changed": True, "additions": 1, "deletions": 1}, ()),
            ({"files_changed": 1, "additions": 1}, ()),
            ({"files_changed": 1, "additions": 1, "deletions": 1}, None),
            ({"files_changed": 1, "additions": 1, "deletions": 1}, "unknown"),
        )
        for diff_stats, history in cases:
            with self.subTest(diff_stats=diff_stats, history=history):
                result = classify(self.module, diff_stats, history)
                self.assertEqual(result["decision"], "full_read")
                self.assertTrue(result["reason"], "the decision must carry its reason")

    def test_the_classifier_is_pure(self):
        """A pure function: no command, no network, no state that changes the answer."""
        with mock.patch("subprocess.run", side_effect=AssertionError("the classifier runs a command")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("the classifier runs a command")), \
                mock.patch("subprocess.check_output", side_effect=AssertionError("the classifier runs a command")):
            first = classify(self.module, ROUTINE_DIFF, pr_number=1988)
            second = classify(self.module, ROUTINE_DIFF, pr_number=1988)
        self.assertEqual(first, second)


class TesterRiskDispatchTests(unittest.TestCase):
    """The dispatcher must classify a verify card and keep the decision (#1988)."""

    def setUp(self):
        self.dispatcher = load_dispatcher()
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)

    def decide(self, pull_request, *, rate="0", events=("[]",)):
        """The decision the dispatcher makes for a verify card with that target PR."""
        env = {"AD_TESTER_RISK_SAMPLE_RATE": rate, "AD_TESTER_RISK_SEED": "risk-v1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.dispatcher, "sh", fake_reader(pull_request, events)):
            return decision_of(self.dispatcher, VERIFY_BODY)

    def test_the_dispatcher_classifies_the_target_pr_of_a_verify_card(self):
        routine = {"changed_files": 1, "additions": 3, "deletions": 2, "labels": []}
        decision = self.decide(routine)

        self.assertIn(decision["decision"], {"full_read", "sampled"})
        self.assertTrue(decision["reason"], "the decision must carry its reason")

    def test_the_dispatcher_reads_the_diff_of_the_target_pr(self):
        large = {"changed_files": 120, "additions": 6000, "deletions": 4000, "labels": []}

        decision = self.decide(large)

        self.assertEqual(decision["decision"], "full_read")
        self.assertTrue(decision["reason"], "the decision must carry its reason")

    def test_the_dispatcher_reads_the_needs_human_history_of_the_target_pr(self):
        marked = {"changed_files": 1, "additions": 3, "deletions": 2,
                  "labels": [{"name": "needs-human"}]}

        decision = self.decide(marked)

        self.assertEqual(decision["decision"], "full_read")
        self.assertTrue(decision["reason"], "the decision must carry its reason")

    def test_the_decision_stays_with_the_card(self):
        decision = {"decision": "full_read", "reason": "large diff: 120 files"}
        state = {}

        record_with_card(self.dispatcher, state, 1988, decision)

        key = find_in_state(state, decision)
        self.assertIsNotNone(key, f"the card state keeps no decision: {state}")
        self.assertIn("1988", str(key), "the decision must stay with its own card")

    def test_the_review_lane_receives_the_decision_and_its_reason(self):
        decision = {"decision": "full_read", "reason": "large diff: 120 files"}
        item = {"n": 1988, "title": "验证证据审查风险", "body": VERIFY_BODY,
                "labels": ["lane-ready", "verify-intent"], "verify": True,
                "tester_risk": decision}
        cfg = {"host": "box", "fleet": "fleet", "repo": "repo"}
        with mock.patch.object(self.dispatcher, "D", self.tmp.name), \
                mock.patch.object(self.dispatcher, "sh", lambda *args, **kwargs: (0, '{"id":"run-1"}', "")), \
                mock.patch.object(self.dispatcher, "log", lambda *args, **kwargs: None):
            self.dispatcher.dispatch("CX2", cfg, item, "0" * 40)
        task = (Path(self.tmp.name) / "tasks" / "CX2-1988.md").read_text(encoding="utf-8")

        self.assertIn(decision["decision"], task,
                      "the review lane does not see how deep the evidence must be read")
        self.assertIn(decision["reason"], task,
                      "the review lane does not see why the evidence must be read that deep")


if __name__ == "__main__":
    unittest.main()
