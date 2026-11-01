import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), HERE]
from fakes import issue  # noqa: E402
from external_lane import contract as C  # noqa: E402
from external_lane import notify  # noqa: E402

NOW = 1_800_000_000


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def card(n=7, labels=("lane-ready", "backend:dot", "p1"), body="do it"):
    i = issue(n, list(labels), body)
    i["html_url"] = f"https://github.com/o/r/issues/{n}"
    return i


class DecideTest(unittest.TestCase):
    def test_target_rules(self):
        self.assertEqual(C.notify_target({"lane-ready", "backend:dot"}), "dot")
        self.assertIsNone(C.notify_target({"lane-ready", "backend:codex"}))            # internal
        self.assertIsNone(C.notify_target({"lane-ready", "backend:external"}))         # pool: no single target
        self.assertIsNone(C.notify_target({"lane-ready", "backend:dot", "backend:x"}))  # ambiguous
        self.assertIsNone(C.notify_target({"lane-claimed", "backend:dot"}))
        self.assertIsNone(C.notify_target({"backend:dot"}))

    def test_comment_with_issue_link_and_marker(self):
        action, body = notify.decide(card(), {"number": 99}, [], NOW)
        self.assertEqual(action, "comment")
        self.assertIn("#7", body)
        self.assertIn("https://github.com/o/r/issues/7", body)
        self.assertIn(C.notify_marker(7), body)
        _, rework = notify.decide(card(body="重基PR：#55 fleet/dot-issue7-1"), {"number": 99}, [], NOW)
        self.assertIn("rework", rework)

    def test_not_allowlisted_without_inbox(self):
        self.assertEqual(notify.decide(card(), None, [], NOW)[0], "skip")

    def test_dedup_24h(self):
        seen = [{"body": "x " + C.notify_marker(7), "created_at": iso(NOW - 3600)}]
        self.assertEqual(notify.decide(card(), {"number": 99}, seen, NOW)[0], "skip")
        old = [{"body": C.notify_marker(7), "created_at": iso(NOW - 25 * 3600)}]
        self.assertEqual(notify.decide(card(), {"number": 99}, old, NOW)[0], "comment")
        other = [{"body": C.notify_marker(8), "created_at": iso(NOW - 60)}]
        self.assertEqual(notify.decide(card(), {"number": 99}, other, NOW)[0], "comment")


class FakeGH:
    repo = "o/r"

    def __init__(self, inbox=True, comments=()):
        self.inbox, self.existing, self.posted, self.gets = inbox, list(comments), [], []

    def r(self, suffix):
        return f"/repos/o/r{suffix}"

    def request(self, method, path, payload=None):
        self.gets.append(path)
        if "/pulls?" in path:
            assert "head=o:external-inbox/dot" in path, path
            return [{"number": 99}] if self.inbox else []
        if "/comments?since=" in path:
            return self.existing + [{"body": b, "created_at": iso(NOW)} for _, b in self.posted]
        raise AssertionError(path)

    def comment(self, n, body):
        self.posted.append((n, body))


class RunTest(unittest.TestCase):
    def test_event_posts_once(self):
        gh = FakeGH()
        ev = {"action": "labeled", "issue": card()}
        self.assertEqual(notify.run(gh, ev, NOW, log=lambda m: None)[0], "comment")
        self.assertEqual(notify.run(gh, ev, NOW, log=lambda m: None)[0], "skip")  # labeled again: deduped
        self.assertEqual([n for n, _ in gh.posted], [99])

    def test_ignored_events_make_no_calls(self):
        gh = FakeGH()
        for ev in ({"issue": card(labels=("lane-ready", "backend:pi"))}, {"issue": card(labels=("p1",))}, {}):
            self.assertEqual(notify.run(gh, ev, NOW, log=lambda m: None)[0], "skip")
        self.assertEqual(gh.gets, [])
        self.assertEqual(notify.run(FakeGH(inbox=False), {"issue": card()}, NOW, log=lambda m: None)[0], "skip")


if __name__ == "__main__":
    unittest.main()
