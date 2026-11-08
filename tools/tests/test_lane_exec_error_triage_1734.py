"""Reproduction tests for the #1734 lane ``exec_error`` triage (Refs #1734).

The card asks for a tool that reads the artefacts a burst of
``failed/exec_error`` runs left behind and reports the *evidence*.  The review
of the PR that implements it added three blockers that must hold:

* **B1** -- a lane comparison must never conclude a root cause from the coarse
  signatures: equal signatures do not prove a common cause and differing
  signatures do not rule one out.  A lane without ``error_class`` has an
  *unknown* remote execution state, so the tool header must not claim that
  such a lane never reached the executor classification.
* **B2** -- the GH2 trial run must succeed *and* must not be an
  ``exec_error`` before ``PAUSE-GH2`` is lifted.
* **B3** -- the text report must survive a missing comparison lane and say
  that the evidence is insufficient instead of crashing.

The tests below describe that behaviour, so they fail on the baseline (no
triage tool, no diagnosis document) and pass once the tool and the document
are fixed.
"""

import ast
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "lane-exec-error-triage.py"

# 2026-11-03 00:08:40-00:25:12 (UTC+8): the GH2 burst was nine runs, not four
# (the card comment lists #1700, #1685, #1694, #1702, #1713, #1714, #1716,
# #1717, #1718).
BURST_FIRST = datetime(2026, 11, 2, 16, 8, 40, tzinfo=timezone.utc)
BURST_LAST = datetime(2026, 11, 2, 16, 25, 12, tzinfo=timezone.utc)
BURST_RUNS = (
    ("20261103-000840-aaaa", 118),
    ("20261103-001100-bbbb", 121),
    ("20261103-001700-cccc", 115),
    ("20261103-002100-dddd", 120),
    ("20261103-002300-eeee", 117),
    ("20261103-002500-ffff", 120),
    ("20261103-002100-gggg", 119),
    ("20261103-002400-hhhh", 118),
    ("20261103-002512-iiii", 120),
)

BROKER_TAIL = ("pi isolated launcher\n"
               "socat: connect(/tmp/openswarm-pi-abc123/broker.sock): "
               "No such file or directory\n")

# Words that mark a statement as "not determinable from this evidence".
UNKNOWN_MARKERS = (
    "unknown", "unknowable", "undetermined", "indeterminate", "insufficient",
    "inconclusive", "not enough", "not sufficient", "not conclusive",
    "cannot", "can not", "does not", "do not", "not proven", "not prove",
    "not rule", "not determine", "no conclusion", "may ", "might ", "could ",
    "未知", "无法", "不能", "不足以", "证据不足", "不确定", "未必", "不一定",
)

_TRIAGE = None


def load_tool():
    """Load the triage tool; the baseline does not have it yet."""
    global _TRIAGE
    if _TRIAGE is None:
        if not SCRIPT.exists():
            raise AssertionError(
                "missing %s: #1734 needs the lane exec_error triage tool"
                % SCRIPT.relative_to(ROOT))
        spec = importlib.util.spec_from_file_location("lane_exec_error_triage", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TRIAGE = module
    return _TRIAGE


def module_docstring():
    """The header description of the triage tool."""
    if not SCRIPT.exists():
        raise AssertionError(
            "missing %s: #1734 needs the lane exec_error triage tool"
            % SCRIPT.relative_to(ROOT))
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    return ast.get_docstring(tree) or ""


def sentences(text):
    """Split a text into sentences.

    The header is prose that may wrap over several lines, so only the
    sentence punctuation splits it -- a wrapped sentence stays one piece.
    """
    parts = re.split(r"(?<=[.。;；!?])\s+", text)
    return [part.strip() for part in parts if part and part.strip()]


def assert_unknown(case, value, message=""):
    """The comparison result must be "unknown", never a yes/no answer."""
    case.assertNotIsInstance(value, bool,
                             "%s the comparison must not answer with a boolean" % message)
    case.assertTrue(value is None or (isinstance(value, str)
                                      and value.strip().lower() in
                                      ("unknown", "undetermined", "inconclusive",
                                       "insufficient", "insufficient evidence")),
                    "%s expected an unknown root cause, got %r" % (message, value))


def write_run(root, run_id, lane, error_class, rc, seconds, backend="pi", with_result=True,
              tail="", started=None, finished=None):
    """Write a run directory that looks like a real openswarm run."""
    run_dir = Path(root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    result = {"id": run_id, "status": "failed", "error_class": error_class, "backend": backend,
              "model": "MiniMax-M3.1-Flash", "lane": lane, "seconds": seconds, "commit": None,
              "attempts": [{"backend": backend, "rc": rc, "seconds": seconds,
                            "error_class": error_class, "error_tail": tail}]}
    if with_result:
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if started is None:
        started = BURST_FIRST.timestamp()
    if finished is None:
        finished = started + seconds
    (run_dir / "status.json").write_text(json.dumps(
        {"lane": lane, "backend": backend, "started": started, "finished": finished,
         "state": "finished", "status": "failed"}), encoding="utf-8")
    return run_dir


def write_gh2_burst(root, lane="GH2", runs=BURST_RUNS):
    """The GH2 burst: nine runs, each failed/exec_error within ~2 minutes."""
    span = (BURST_LAST - BURST_FIRST).total_seconds()
    last = max(len(runs) - 1, 1)
    for index, (run_id, seconds) in enumerate(runs):
        finished = BURST_FIRST.timestamp() + span * index / last
        run_dir = write_run(root, run_id, lane, "exec_error", 1, seconds,
                            tail="socat: connect(/tmp/openswarm-pi-abc123/broker.sock): "
                                 "No such file or directory",
                            started=finished - seconds, finished=finished)
        (run_dir / "events.1.pi.jsonl.err").write_text(BROKER_TAIL, encoding="utf-8")
    return root


class LaneExecErrorTriageTests(unittest.TestCase):
    """The triage tool itself (B1 and B3)."""

    def test_the_nine_run_burst_is_reported_with_its_evidence(self):
        triage = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            write_gh2_burst(tmp)
            report = triage.build_report([tmp])
        lane = report["lanes"][0]
        self.assertEqual(lane["lane"], "GH2")
        self.assertEqual(lane["failures"], 9)
        self.assertEqual(lane["dominant_signature"], "exec_error:broker_unreachable")
        self.assertEqual(lane["causes"], {"broker_unreachable": 9})
        self.assertEqual(lane["first_failure"], BURST_FIRST)
        self.assertEqual(lane["last_failure"], BURST_LAST)
        evidence = lane["evidence"][0]
        self.assertEqual(evidence["file"], "events.1.pi.jsonl.err")
        self.assertIn("broker.sock", evidence["text"])

    def test_a_lane_without_an_error_class_keeps_its_remote_state_unknown(self):
        triage = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            # The harvest could not fetch the remote result bundle: the run
            # carries no error_class at all.
            write_run(tmp, "20261102-231000-1111", "CX9", None, None, 95, backend="codex")
            report = triage.build_report([tmp])
        lane = report["lanes"][0]
        self.assertEqual(lane["lane"], "CX9")
        self.assertEqual(lane["dominant_signature"], "none:missing_error_class")
        self.assertEqual(lane["causes"], {"missing_error_class": 1})

    def test_a_run_without_a_result_is_reported_as_an_unknown_execution_state(self):
        triage = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            write_run(tmp, "20261102-235000-3333", "CX9", None, None, 40, backend="codex",
                      with_result=False)
            report = triage.build_report([tmp])
        lane = report["lanes"][0]
        self.assertEqual(lane["dominant_signature"], "none:no_result")
        self.assertEqual(lane["causes"], {"no_result": 1})

    def test_equal_signatures_do_not_prove_a_common_root_cause(self):
        triage = load_tool()
        lane = {"lane": "GH2", "dominant_signature": "exec_error:broker_unreachable"}
        compare = triage.compare_lanes(lane, lane)
        assert_unknown(self, compare.get("same_root_cause"), "equal signatures:")
        reason = str(compare.get("reason") or "").lower()
        self.assertTrue(any(marker in reason for marker in UNKNOWN_MARKERS),
                        "the reason must say that the evidence is insufficient, got %r"
                        % compare.get("reason"))

    def test_different_signatures_do_not_rule_a_common_root_cause_out(self):
        triage = load_tool()
        gh2 = {"lane": "GH2", "dominant_signature": "exec_error:broker_unreachable"}
        cx9 = {"lane": "CX9", "dominant_signature": "none:missing_error_class"}
        compare = triage.compare_lanes(gh2, cx9)
        assert_unknown(self, compare.get("same_root_cause"), "different signatures:")

    def test_two_lanes_on_the_same_host_are_not_joined_by_the_tool(self):
        # M19 died the same way as GH2 on the same host (see the card comment);
        # the tool reports both lanes, the common cause stays a conclusion the
        # operator has to draw from the field evidence.
        triage = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            write_gh2_burst(Path(tmp) / "gh2", lane="GH2")
            write_gh2_burst(Path(tmp) / "m19", lane="M19", runs=BURST_RUNS[:6])
            report = triage.build_report([tmp], compare_lane="M19")
        lanes = {item["lane"] for item in report["lanes"]}
        self.assertEqual(lanes, {"GH2", "M19"})
        assert_unknown(self, (report.get("compare") or {}).get("same_root_cause"),
                       "same host lanes:")

    def test_a_missing_comparison_lane_is_reported_as_insufficient_evidence(self):
        triage = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            write_gh2_burst(tmp)
            report = triage.build_report([tmp], compare_lane="CX9")
        compare = report.get("compare") or {}
        assert_unknown(self, compare.get("same_root_cause"), "missing comparison lane:")
        text = triage.render_text(report)
        self.assertTrue(any(marker in text.lower() for marker in UNKNOWN_MARKERS),
                        "the text report must say that the evidence is insufficient, got:\n%s"
                        % text)

    def test_the_harvest_failures_carry_no_error_class(self):
        triage = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            harvest = Path(tmp) / "harvest.json"
            harvest.write_text(json.dumps(
                {"ok": False, "status": "failed", "completed": [], "fetch_fails": 2,
                 "failed": [{"host": "gores2", "lane": "CX9", "run_id": "r1",
                             "status": "failed", "exit_code": 255,
                             "error": "remote run info failed (rc=255)",
                             "fetch_fails": 2}]}), encoding="utf-8")
            rows = triage.harvest_failures(harvest)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["error_class"])

    def test_the_documented_command_runs_and_reports_the_missing_lane(self):
        # The command the diagnosis document tells the operator to run.
        load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            write_gh2_burst(tmp)
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--runs", tmp, "--compare-lane", "CX9"],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(any(marker in completed.stdout.lower() for marker in UNKNOWN_MARKERS),
                        "the command must report that the evidence is insufficient, got:\n%s"
                        % completed.stdout)


class LaneExecErrorTriageHeaderTests(unittest.TestCase):
    """The header of the tool must not contradict the comparison (B1)."""

    FORBIDDEN = (
        r"never reached the executor classification",
        r"not executor failures",
        r"only when their dominant signatures match",
        r"same root cause only when",
        r"not the same failure",
    )

    def test_the_header_does_not_deduce_the_root_cause_from_the_signature(self):
        doc = module_docstring()
        for pattern in self.FORBIDDEN:
            self.assertIsNone(re.search(pattern, doc, re.I),
                              "the tool header still claims %r" % pattern)

    def test_the_header_says_that_the_signature_cannot_decide_the_root_cause(self):
        doc = module_docstring()
        signature_sentences = [part for part in sentences(doc) if "signature" in part.lower()]
        self.assertTrue(signature_sentences,
                        "the tool header must explain what the signature is used for")
        self.assertTrue(
            any(any(marker in part.lower() for marker in UNKNOWN_MARKERS)
                for part in signature_sentences),
            "the tool header must say that the signature cannot decide the root cause:\n%s"
            % doc)

    def test_the_header_says_that_a_missing_error_class_leaves_the_state_unknown(self):
        doc = module_docstring()
        class_sentences = [part for part in sentences(doc) if "error_class" in part]
        self.assertTrue(class_sentences,
                        "the tool header must explain the role of error_class")
        self.assertTrue(
            any(any(marker in part.lower() for marker in UNKNOWN_MARKERS)
                for part in class_sentences),
            "the tool header must say that a lane without error_class has an unknown "
            "remote execution state:\n%s" % doc)


if __name__ == "__main__":
    unittest.main()
