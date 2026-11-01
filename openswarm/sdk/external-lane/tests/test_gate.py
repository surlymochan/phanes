import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
from fakes import FakeGitHub, issue  # noqa: E402
from external_lane import cli, gate  # noqa: E402
from external_lane.agent import ExternalLane  # noqa: E402

NOW = 1_800_000_000.0


class DecideTest(unittest.TestCase):
    def test_rc_semantics(self):
        card = [issue(5, ["lane-ready", "backend:dot"])]
        rc, s, st = gate.decide([], card, {}, NOW)
        self.assertEqual((rc, s["reason"], s["next"]), (gate.WAKE, "card", 5))
        self.assertEqual(st["wakes"], [NOW])
        self.assertEqual(gate.decide([], [], {}, NOW)[:2][0], gate.IDLE)
        rc, s, _ = gate.decide([issue(9, ["lane-claimed", "backend:dot"])], card, {}, NOW)
        self.assertEqual((rc, s["reason"]), (gate.IDLE, "holding-claim"))  # finish what you hold first

    def test_hourly_wake_cap(self):
        card, st = [issue(5, ["lane-ready", "backend:dot"])], {}
        rcs = []
        for k in range(6):
            rc, s, st = gate.decide([], card, st, NOW + k * 60)
            rcs.append(rc)
        self.assertEqual(rcs, [0, 0, 0, 0, 1, 1])
        self.assertEqual(s["reason"], "wake-cap")
        self.assertEqual(gate.decide([], card, st, NOW + 3601)[0], gate.WAKE)  # window slides
        self.assertEqual(gate.decide([], card, {}, NOW, max_wakes_per_hour=1)[0], gate.WAKE)

    def test_failure_cooldown(self):
        cards = [issue(5, ["lane-ready", "backend:dot"]), issue(6, ["lane-ready", "backend:dot"])]
        st = {"failed": {"5": NOW - 600}}
        rc, s, _ = gate.decide([], cards, st, NOW)
        self.assertEqual((rc, s["next"], s["cooling"]), (gate.WAKE, 6, [5]))
        rc, s, _ = gate.decide([], cards[:1], st, NOW)
        self.assertEqual((rc, s["reason"]), (gate.IDLE, "no-card"))
        self.assertEqual(gate.decide([], cards[:1], st, NOW + 2 * 3600)[0], gate.WAKE)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.state = os.path.join(tempfile.mkdtemp(), "dot.json")

    def lane(self, issues):
        return ExternalLane("o/r", "dot", gh=FakeGitHub(issues), token="", log=lambda m: None)

    def test_check_is_read_only_on_github(self):
        lane = self.lane([issue(5, ["lane-ready", "backend:dot"])])
        rc, s = gate.check(lane, self.state, now=NOW)
        self.assertEqual((rc, s["rc"], s["agent"]), (0, 0, "dot"))
        self.assertEqual(lane.gh.calls, [])  # FakeGitHub records writes only

    def test_pool_card_counts_and_errors_are_rc2(self):
        self.assertEqual(gate.check(self.lane([issue(8, ["lane-ready", "backend:external"])]), self.state, now=NOW)[0], 0)
        broken = self.lane([])
        broken.gh.list_issues = mock.Mock(side_effect=RuntimeError("401 Bad credentials"))
        rc, s = gate.check(broken, self.state, now=NOW)
        self.assertEqual((rc, s["reason"]), (2, "error"))

    def test_fail_starts_cooldown(self):
        lane = self.lane([issue(5, ["lane-claimed", "backend:dot"])])
        lane.state_file = self.state
        lane.fail(5, "acceptance fails")
        lane.gh.set_labels(5, {"lane-ready", "backend:dot"})  # a human re-queues it
        rc, s = gate.check(lane, self.state, now=None)
        self.assertEqual((rc, s["cooling"]), (1, [5]))

    def test_cli_prints_one_json_line_and_returns_rc(self):
        fake = FakeGitHub([])
        out = io.StringIO()
        with mock.patch.object(cli, "ExternalLane", lambda *a, **k: ExternalLane("o/r", "dot", gh=fake, token="", log=lambda m: None)), \
                redirect_stdout(out):
            rc = cli.main(["--repo", "o/r", "--agent", "dot", "check", "--state-file", self.state])
        self.assertEqual(rc, 1)
        line = out.getvalue().strip()
        self.assertEqual(len(line.splitlines()), 1)
        self.assertEqual(json.loads(line)["reason"], "no-card")
        with redirect_stdout(io.StringIO()) as bad:
            self.assertEqual(cli.main(["--repo", "o/r", "--agent", "codex", "check"]), 2)  # reserved name
        self.assertIn("reserved", bad.getvalue())


class TickTest(unittest.TestCase):
    def test_tick_claims_at_most_one_and_resumes(self):
        fake = FakeGitHub([issue(5, ["lane-ready", "backend:dot"], body="x\n验收：`pytest -q`"),
                           issue(6, ["lane-ready", "backend:external"])])
        lane = ExternalLane("o/r", "dot", gh=fake, token="", log=lambda m: None)
        args = type("A", (), {"work_cmd": None})()
        for expect_resumed in (False, True):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.tick(lane, args), 0)
            brief = json.loads(out.getvalue())
            self.assertEqual((brief["issue"], brief["resumed"]), (5, expect_resumed))
            self.assertRegex(brief["branch"], r"^fleet/dot-issue5-")
        self.assertIn("lane-ready", fake.labels_of(6))  # second tick did not take another card

    def test_pool_claim_repins_card(self):
        fake = FakeGitHub([issue(6, ["lane-ready", "backend:external", "p1"])])
        ExternalLane("o/r", "newbot", gh=fake, token="", log=lambda m: None).claim_next()
        self.assertEqual(fake.labels_of(6), {"lane-claimed", "backend:newbot", "p1"})


if __name__ == "__main__":
    unittest.main()
